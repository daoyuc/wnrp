# -*- coding: utf-8 -*-
"""nginx access.log 的只读解析与聚合（纯标准库，无 UI、无后台进程）。

设计边界（见 ``docs/DECISIONS.md`` ADR 17）：

- **只读**：只打开 nginx 自身日志目录（``NginxManager().logs_dir``）里的 access
  日志；调用方只能按**文件名**选择，不能传任意路径（``realpath`` 前缀校验，
  符号链接逃逸也会被拒绝）；
- 逐行解析 combined 格式（兼容末位 ``$request_time`` / ``$upstream_response_time``），
  无法解析的行计入 ``skipped``，绝不抛异常打断统计；
- 不落库、不上传、不执行任何命令，也没有后台进程。

统计口径：Top 路径按**去掉 query 的 route** 聚合（``/api/list?id=1`` 与
``?id=2`` 视为同一路径），错误明细保留原始 path 便于复现。
"""
import os
import re
from collections import Counter

from core.nginx_manager import NginxManager

#: nginx 日志里的月份缩写（不能用 strptime("%b")，它依赖进程 locale）
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

#: combined 格式一行：
#: ``ip - user [time] "request" status bytes "referer" "ua" [request_time]``
_LINE_RE = re.compile(
    r'^(?P<ip>\S+) \S+ (?P<user>\S+) \[(?P<time>[^\]]+)\] '
    r'"(?P<request>[^"]*)" (?P<status>\d{3}) (?P<bytes>\d+|-)'
    r'(?: "(?P<referer>[^"]*)" "(?P<ua>[^"]*)")?'
    r'(?: (?P<seconds>\d+(?:\.\d+)?|[-]))?[ \t]*$'
)

_TIME_RE = re.compile(
    r"^(\d{2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})(?: ([+-]\d{4}))?$"
)

_ACCESS_NAME_RE = re.compile(r"access.*\.log(?:\.\d+)?$", re.I)

#: 只统计标准 HTTP 方法；扫描器塞进来的畸形方法统一归为 OTHER，
#: 避免日志内容污染界面（真机日志里就有 ``\X111A0\X99\X1AN`` 这类噪声）
_METHODS = frozenset({
    "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS",
    "TRACE", "CONNECT",
})

#: 单次最多读多少字节（尾部），避免几百 MB 的日志把内存吃满
MAX_BYTES = 8 * 1024 * 1024
#: 单次最多统计多少行
MAX_LINES = 200000
#: 展示用字段的截断长度（防止畸形日志把界面撑坏）
MAX_FIELD = 300


# --------------------------------------------------------------------------- #
# 日志定位（只认 nginx 日志目录）
# --------------------------------------------------------------------------- #
def logs_dir() -> str:
    """当前生效的 nginx 日志目录；取不到时返回空串。"""
    try:
        return NginxManager().logs_dir
    except Exception:  # noqa: BLE001 - 取不到就当作没有日志
        return ""


def _inside(path: str, base: str) -> bool:
    """``path`` 是否真的位于 ``base`` 之内（解析符号链接后再比前缀）。"""
    try:
        real = os.path.realpath(path)
        root = os.path.realpath(base)
    except OSError:
        return False
    return real == root or real.startswith(root + os.sep)


def access_logs(dir_path: str = "") -> list[dict]:
    """列出可分析的 access 日志（``[{"name", "path", "size"}]``）。

    只接受 nginx 日志目录内的 ``*access*.log``（含轮转的 ``.log.1``）。
    """
    base = dir_path or logs_dir()
    if not base or not os.path.isdir(base):
        return []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out = []
    for name in names:
        if not _ACCESS_NAME_RE.search(name):
            continue
        path = os.path.join(base, name)
        if not os.path.isfile(path) or not _inside(path, base):
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        out.append({"name": name, "path": path, "size": size})
    # 主文件排最前，轮转文件按名字倒序（越新越靠前）
    out.sort(key=lambda it: (it["name"] != "access.log", it["name"]))
    return out


def read_lines(path: str, limit: int = 20000,
               max_bytes: int = MAX_BYTES) -> list[str]:
    """读文件尾部至多 ``limit`` 行（最多回溯 ``max_bytes``）。

    读失败返回空列表；截断处可能落在半行，首行会被丢弃。
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, max(1, max_bytes))
            f.seek(size - chunk)
            data = f.read()
    except OSError:
        return []
    rows = data.decode("utf-8", "replace").splitlines()
    if chunk < size and rows:
        rows = rows[1:]  # 丢弃被截断的首行
    if limit > 0:
        rows = rows[-int(limit):]
    return rows


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def _parse_time(text: str):
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    day, mon, year, hh, mm, ss = (
        int(m.group(1)), _MONTHS.get(m.group(2), 0), int(m.group(3)),
        int(m.group(4)), int(m.group(5)), int(m.group(6)))
    if not mon:
        return None
    try:
        return (year, mon, day, hh, mm, ss)
    except ValueError:  # pragma: no cover - 手工构造，正常不会走到
        return None


def parse_line(line: str) -> dict | None:
    """解析一行 combined 日志，失败返回 None（由调用方计入 skipped）。"""
    m = _LINE_RE.match(line)
    if not m:
        return None
    ts = _parse_time(m.group("time"))
    if ts is None:  # 时间都解析不出来，说明不是标准行，计入 skipped
        return None
    request = m.group("request") or ""
    parts = request.split()
    method = parts[0] if parts else ""
    path = parts[1] if len(parts) > 1 else ""
    if method == "-":
        method, path = "", ""
    method = method.upper()[:16]
    if method not in _METHODS:
        method = "OTHER"
    path = path[:MAX_FIELD]
    route = path.split("?", 1)[0].split("#", 1)[0][:MAX_FIELD]
    ua = (m.group("ua") or "").strip()
    referer = (m.group("referer") or "").strip()
    if ua == "-":
        ua = ""
    if referer == "-":
        referer = ""
    raw_seconds = m.group("seconds") or ""
    try:
        seconds = float(raw_seconds) if raw_seconds not in ("", "-") else None
    except ValueError:
        seconds = None
    status = int(m.group("status"))
    raw_bytes = m.group("bytes")
    return {
        "ip": (m.group("ip") or "")[:64],
        "time": ts,
        "method": method,
        "path": path,
        "route": route or "/",
        "status": status,
        "bytes": 0 if raw_bytes == "-" else int(raw_bytes),
        "referer": referer[:MAX_FIELD],
        "ua": ua[:MAX_FIELD],
        "seconds": seconds,
    }


def _ts_text(ts) -> str:
    if not ts:
        return ""
    return "%04d-%02d-%02d %02d:%02d:%02d" % ts


def _pct(count: int, total: int) -> float:
    return round(count * 100.0 / total, 1) if total else 0.0


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
def aggregate(rows: list[dict], top: int = 10, error_limit: int = 20) -> dict:
    """把解析后的行聚合成一份可直接 JSON 序列化的报告。"""
    total = len(rows)
    top = max(1, int(top))
    codes = Counter(r["status"] for r in rows)
    classes = Counter("%dxx" % (r["status"] // 100) for r in rows)
    ips = Counter(r["ip"] for r in rows if r["ip"])
    routes = Counter(r["route"] for r in rows)
    methods = Counter(r["method"] for r in rows if r["method"])
    uas = Counter(r["ua"] for r in rows if r["ua"])
    referers = Counter(r["referer"] for r in rows if r["referer"])

    hourly = [0] * 24
    for r in rows:
        ts = r.get("time")
        if ts:
            hourly[ts[3] % 24] += 1

    errors = [r for r in rows if r["status"] >= 400]
    slow = [r for r in rows if r.get("seconds") is not None]
    slow.sort(key=lambda r: r["seconds"], reverse=True)
    timed = [r for r in rows if r.get("seconds") is not None]

    status_classes = {k: classes.get(k, 0) for k in ("2xx", "3xx", "4xx", "5xx")}
    if classes.get("1xx"):
        status_classes["1xx"] = classes["1xx"]

    return {
        "requests": total,
        "unique_ips": len(ips),
        "bytes": sum(r["bytes"] for r in rows),
        "status_classes": status_classes,
        "status_codes": [
            {"code": code, "count": n, "pct": _pct(n, total)}
            for code, n in codes.most_common(top)
        ],
        "methods": [
            {"method": m, "count": n, "pct": _pct(n, total)}
            for m, n in methods.most_common(top)
        ],
        "top_paths": [
            {"path": p, "count": n, "pct": _pct(n, total)}
            for p, n in routes.most_common(top)
        ],
        "top_ips": [
            {"ip": ip, "count": n, "pct": _pct(n, total)}
            for ip, n in ips.most_common(top)
        ],
        "top_user_agents": [
            {"ua": ua, "count": n}
            for ua, n in uas.most_common(top)
        ],
        "top_referers": [
            {"referer": ref, "count": n}
            for ref, n in referers.most_common(top)
        ],
        "hourly": [{"hour": h, "count": n} for h, n in enumerate(hourly)],
        "error_rate": _pct(status_classes.get("5xx", 0), total),
        "errors": [
            {
                "time": _ts_text(r["time"]), "ip": r["ip"],
                "status": r["status"], "method": r["method"],
                "path": r["path"],
            }
            for r in errors[-max(1, int(error_limit)):][::-1]
        ],
        "slowest": [
            {
                "time": _ts_text(r["time"]), "path": r["path"],
                "seconds": round(r["seconds"], 3),
            }
            for r in slow[:top]
        ],
        "avg_seconds": (
            round(sum(r["seconds"] for r in timed) / len(timed), 3)
            if timed else None),
        "first_at": _ts_text(rows[0]["time"]) if rows else "",
        "last_at": _ts_text(rows[-1]["time"]) if rows else "",
    }


# --------------------------------------------------------------------------- #
# 报告（GUI / CLI 共用入口）
# --------------------------------------------------------------------------- #
def report(file_name: str = "", lines: int = 20000, top: int = 10,
           dir_path: str = "") -> dict:
    """读一个 access 日志并返回完整报告。

    失败时返回 ``{"ok": False, "error": <code>, ...}``，不抛异常：

    - ``no_access_log``：日志目录不存在或没有 access 日志；
    - ``not_found``：``file_name`` 不在候选列表里（附 ``available``）。
    """
    base = dir_path or logs_dir()
    logs = access_logs(base)
    if not logs:
        return {"ok": False, "error": "no_access_log", "dir": base,
                "available": [], "logs": []}
    if file_name:
        picked = next((it for it in logs if it["name"] == file_name), None)
        if picked is None:
            return {"ok": False, "error": "not_found", "dir": base,
                    "available": [it["name"] for it in logs], "logs": logs}
    else:
        picked = logs[0]
    limit = max(1, min(int(lines or 20000), MAX_LINES))
    raw = read_lines(picked["path"], limit)
    parsed = [r for r in (parse_line(line) for line in raw) if r is not None]
    data = aggregate(parsed, top=top)
    data.update({
        "ok": True,
        "file": picked["name"],
        "path": picked["path"],
        "dir": base,
        "lines": limit,
        "read": len(raw),
        "parsed": len(parsed),
        "skipped": len(raw) - len(parsed),
        "available": [it["name"] for it in logs],
        "logs": logs,
    })
    return data
