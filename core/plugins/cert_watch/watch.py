# -*- coding: utf-8 -*-
"""证书到期巡检核心：扫站点配置里的 ``ssl_certificate``，读到剩余天数。

口径与边界：

- 来源只有一处：站点 vhost 配置里的 ``ssl_certificate`` 指令（相对路径按
  配置文件所在目录解析），不扫系统证书库、不联网、不做 OCSP；
- 解析只调用系统已有的 ``openssl``（``openssl x509 -noout -enddate
  -subject -issuer``），不内置 OpenSSL、不下载二进制；找不到 openssl 时
  返回 ``no_tool`` 而不是猜日期；
- 时间判断全用 UTC 时间戳与 ``datetime.strptime("%b %d %Y ...")``，不受
  系统 locale 影响；
- 只读：不续期、不改配置、不写文件。
"""
import os
import re
import shutil
import time
from datetime import datetime, timezone

from ... import process_utils as pu

WARN_DAYS = 30
CRITICAL_DAYS = 7
DEFAULT_TIMEOUT = 8

ERROR_MESSAGES = {
    "no_tool": "找不到 openssl（无法读取证书到期时间）",
    "no_cert": "没有找到站点证书（vhost 里没有 ssl_certificate）",
    "timeout": "读取证书超时",
}
ERROR_CODES = tuple(ERROR_MESSAGES)

LEVEL_MESSAGES = {
    "expired": "已过期",
    "critical": "7 天内到期",
    "warn": "30 天内到期",
    "ok": "正常",
}
LEVELS = tuple(LEVEL_MESSAGES)

_CERT_DIRECTIVE = re.compile(r"^\s*ssl_certificate\s+([^;]+);", re.M)
_CONF_LIMIT = 64 * 1024


# --------------------------------------------------------------------------- #
# 纯解析函数
# --------------------------------------------------------------------------- #
def parse_not_after(text: str) -> float | None:
    """把 ``notAfter=Nov 12 23:59:59 2026 GMT`` 转成 UTC 时间戳。"""
    raw = str(text or "").strip()
    if "=" in raw:
        raw = raw.split("=", 1)[1].strip()
    parts = raw.split()
    if len(parts) < 4:
        return None
    month, day, clock, year = parts[0], parts[1], parts[2], parts[3]
    try:
        dt = datetime.strptime(f"{month} {day} {year} {clock}",
                               "%b %d %Y %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).timestamp()


def parse_x509(text: str) -> dict:
    """解析 ``openssl x509 -enddate -subject -issuer`` 的输出。"""
    out: dict = {}
    for line in str(text or "").splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "notafter":
            out["not_after_raw"] = value
        elif key in ("subject", "issuer"):
            out[key] = value
    return out


def level_of_days(days: int) -> str:
    """按剩余天数分级（负数即已过期）。"""
    if days < 0:
        return "expired"
    if days <= CRITICAL_DAYS:
        return "critical"
    if days <= WARN_DAYS:
        return "warn"
    return "ok"


def find_cert_paths(entries) -> list[dict]:
    """从 vhost 配置里找 ``ssl_certificate``，返回 ``{site, file, path}``。"""
    found: list[dict] = []
    seen: set[str] = set()
    for entry in entries or []:
        conf = str(getattr(entry, "file", "") or "")
        names = str(getattr(entry, "server_name", "") or "").split()
        site = names[0] if names else os.path.basename(conf)
        try:
            with open(conf, "r", encoding="utf-8", errors="replace") as f:
                text = f.read(_CONF_LIMIT)
        except OSError:
            continue
        for match in _CERT_DIRECTIVE.finditer(text):
            raw = match.group(1).strip().strip('"').strip("'")
            if not raw or raw.startswith("$"):
                continue
            path = raw if os.path.isabs(raw) else os.path.join(
                os.path.dirname(conf), raw)
            path = os.path.normpath(path)
            if path in seen:
                continue
            seen.add(path)
            found.append({"site": site, "file": conf, "path": path})
    return found


# --------------------------------------------------------------------------- #
# 读取证书
# --------------------------------------------------------------------------- #
def cert_info(path: str, timeout: int = DEFAULT_TIMEOUT) -> dict | None:
    """调用系统 openssl 读单张证书；失败返回 None。"""
    code, out, _ = pu.run_cmd(
        ["openssl", "x509", "-in", path, "-noout", "-enddate", "-subject",
         "-issuer", "-nameopt", "RFC2253"], timeout=timeout)
    if code != 0 or not out.strip():
        return None
    parsed = parse_x509(out)
    stamp = parse_not_after(parsed.get("not_after_raw", ""))
    if stamp is None:
        return None
    parsed["not_after"] = stamp
    return parsed


def report(config=None, entries=None, only: str = "", within: int = 0,
           certs=None, now: float | None = None,
           timeout: int = DEFAULT_TIMEOUT) -> dict:
    """汇总证书到期情况；``certs`` 可注入（``{site,path,not_after,...}``）。

    ``within`` > 0 时只保留剩余天数不超过它的证书（含已过期）。
    """
    error = ""
    if certs is None:
        if not shutil.which("openssl"):
            return {"ok": False, "error": "no_tool", "total": 0, "items": [],
                    "expired": 0, "critical": 0, "warn": 0, "ok": 0,
                    "counts": {k: 0 for k in LEVELS}, "nearest": None}
        if entries is None:
            entries = []
            try:
                from ...vhost_manager import VhostManager

                entries = VhostManager(config).scan()
            except Exception:  # noqa: BLE001
                entries = []
        rows = []
        for item in find_cert_paths(entries):
            info = cert_info(item["path"], timeout)
            if info is None:
                continue
            rows.append({**item, "not_after": info["not_after"],
                         "subject": info.get("subject", ""),
                         "issuer": info.get("issuer", "")})
        certs = rows
        if not certs:
            error = "no_cert"

    stamp = time.time() if now is None else now
    needle = (only or "").strip().lower()
    items = []
    for cert in certs or []:
        site = str(cert.get("site") or "")
        path = str(cert.get("path") or "")
        if needle and needle not in site.lower() and needle not in path.lower():
            continue
        days = int((float(cert["not_after"]) - stamp) // 86400)
        if within and within > 0 and days > within:
            continue
        items.append({
            "site": site, "path": path,
            "file": str(cert.get("file") or ""),
            "subject": str(cert.get("subject") or ""),
            "issuer": str(cert.get("issuer") or ""),
            "not_after": float(cert["not_after"]),
            "not_after_text": time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(float(cert["not_after"]))),
            "days_left": days,
            "level": level_of_days(days),
        })
    items.sort(key=lambda it: it["days_left"])

    counts = {k: 0 for k in LEVELS}
    for item in items:
        counts[item["level"]] += 1
    return {
        "ok": not error,
        "error": error,
        "total": len(items),
        "expired": counts["expired"],
        "critical": counts["critical"],
        "warn": counts["warn"],
        "valid": counts["ok"],
        "counts": counts,
        "nearest": items[0] if items else None,
        "items": items,
    }
