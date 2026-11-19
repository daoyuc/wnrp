# -*- coding: utf-8 -*-
"""端口占用巡检核心：列出本机所有 TCP 监听端口与占用进程（只读）。

口径与边界：

- **只读**：仅执行 ``netstat -ano -p TCP``（Windows）或
  ``lsof -nP -iTCP -sTCP:LISTEN -F pcn``（macOS / Linux），Windows 另加一次
  ``tasklist`` 取进程名；不杀进程、不改配置、不写文件、不起后台进程；
- 「归属」只按**可执行文件名**推断（nginx / php-cgi / redis-server / mysqld），
  不读进程路径 —— 所以它表示"看起来是谁"，不是"phpvm 确认管辖"；
- 同端口被多个 PID 监听（IPv4 / IPv6 各一条、或 SO_REUSEADDR）时按
  ``(port, pid)`` 合并监听地址，汇总里给出 ``conflicts``；
- 命令缺失 / 超时统一归一为错误码（``no_tool`` / ``timeout`` / ``failed``），
  绝不向上抛：面板与 CLI 共用同一份数据。
"""
import csv
import io
import os
import shutil
import sys

from ... import process_utils as pu

DEFAULT_TIMEOUT = 8.0

#: 错误码 → i18n 源文案（界面与 CLI 统一 ``t(ERROR_MESSAGES[code])`` 展示）
ERROR_MESSAGES = {
    "no_tool": "找不到端口查询工具（netstat / lsof）",
    "timeout": "端口查询超时",
    "failed": "端口查询失败",
}
ERROR_CODES = tuple(ERROR_MESSAGES)

#: 归属 → i18n 源文案（除 other 外都是固定品牌名，翻译时通常原样保留）
KIND_MESSAGES = {
    "nginx": "Nginx",
    "php": "PHP",
    "redis": "Redis",
    "mysql": "MySQL",
    "other": "其他程序",
}
KINDS = tuple(KIND_MESSAGES)

_PHP_STEMS = ("php", "php-cgi", "php-cgi7", "php-fpm")
_REDIS_STEMS = ("redis", "redis-server")


# --------------------------------------------------------------------------- #
# 纯解析函数（测试用固定文本喂入，不依赖本机进程）
# --------------------------------------------------------------------------- #
def split_addr(text: str) -> tuple[str, int | None]:
    """把 ``0.0.0.0:80`` / ``[::1]:9081`` / ``*:6379`` 拆成 (地址, 端口)。"""
    raw = str(text or "").strip()
    if not raw:
        return "", None
    host, sep, port_s = raw.rpartition(":")
    if not sep:
        return raw, None
    try:
        port = int(port_s)
    except ValueError:
        return raw, None
    if not 0 < port <= 65535:
        return raw, None
    return host, port


def process_kind(name: str) -> str:
    """按可执行文件名推断归属（``phpstorm64`` 不算 PHP）。"""
    base = os.path.basename(str(name or "")).strip().lower()
    if not base:
        return "other"
    stem = base[:-4] if base.endswith(".exe") else base
    if stem == "nginx":
        return "nginx"
    if stem in _PHP_STEMS or stem.startswith("php-cgi-") or \
            stem.startswith("php-fpm-"):
        return "php"
    if stem in _REDIS_STEMS or stem.startswith("redis-server-"):
        return "redis"
    if stem in ("mysqld", "mariadbd"):
        return "mysql"
    return "other"


def parse_netstat(text: str) -> list[dict]:
    """解析 Windows ``netstat -ano -p TCP`` 文本，只保留 LISTENING 行。"""
    rows: list[dict] = []
    for line in str(text or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() != "TCP":
            continue
        if not parts[3].upper().startswith("LISTEN"):
            continue
        addr, port = split_addr(parts[1])
        if port is None:
            continue
        pid = 0
        for token in parts[4:]:
            if token.isdigit():
                pid = int(token)
                break
        rows.append({"port": port, "addr": addr, "pid": pid,
                     "process": ""})
    return rows


def parse_lsof(text: str) -> list[dict]:
    """解析 ``lsof -F pcn`` 输出（p=PID / c=命令 / n=地址，n 可多条）。"""
    rows: list[dict] = []
    pid = 0
    name = ""
    for line in str(text or "").splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:].strip()
        if tag == "p":
            pid = int(value) if value.isdigit() else 0
            name = ""
        elif tag == "c":
            name = value
        elif tag == "n":
            addr, port = split_addr(value)
            if port is None:
                continue
            rows.append({"port": port, "addr": addr, "pid": pid,
                         "process": name})
    return rows


def parse_tasklist(text: str) -> dict[int, str]:
    """解析 ``tasklist /FO CSV /NH`` 为 ``{pid: 进程名}``（失败返回空）。"""
    names: dict[int, str] = {}
    for row in csv.reader(io.StringIO(str(text or ""))):
        if len(row) < 2:
            continue
        try:
            pid = int(row[1].strip())
        except ValueError:
            continue
        if pid > 0:
            names[pid] = row[0].strip()
    return names


def merge_rows(rows, names: dict[int, str] | None = None) -> list[dict]:
    """按 ``(port, pid)`` 去重并合并监听地址，补进程名与归属。"""
    names = names or {}
    merged: dict[tuple[int, int], dict] = {}
    for row in rows or []:
        try:
            port = int(row.get("port") or 0)
            pid = int(row.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if port <= 0:
            continue
        key = (port, pid)
        item = merged.get(key)
        if item is None:
            proc = str(row.get("process") or names.get(pid) or "")
            item = {"port": port, "pid": pid, "process": proc,
                    "kind": process_kind(proc), "addrs": []}
            merged[key] = item
        elif not item["process"]:
            proc = str(row.get("process") or names.get(pid) or "")
            if proc:
                item["process"] = proc
                item["kind"] = process_kind(proc)
        addr = str(row.get("addr") or "")
        if addr and addr not in item["addrs"]:
            item["addrs"].append(addr)

    out: list[dict] = []
    for item in merged.values():
        item["addrs"].sort()
        item["addr"] = ", ".join(item["addrs"])
        item.pop("addrs", None)
        out.append(item)
    out.sort(key=lambda it: (it["port"], it["pid"]))
    return out


# --------------------------------------------------------------------------- #
# 本机采集
# --------------------------------------------------------------------------- #
def _windows_names(timeout: int) -> dict[int, str]:
    if not shutil.which("tasklist"):
        return {}
    code, out, _ = pu.run_cmd(["tasklist", "/FO", "CSV", "/NH"],
                              timeout=timeout)
    if code != 0 or not out.strip():
        return {}
    return parse_tasklist(out)


def listening_ports(timeout: float = DEFAULT_TIMEOUT) -> tuple[list[dict], str]:
    """采集本机 TCP 监听端口，返回 ``(rows, error_code)``（正常时 error 为空）。"""
    limit = max(1, int(timeout))
    if sys.platform.startswith("win"):
        if not shutil.which("netstat"):
            return [], "no_tool"
        code, out, _ = pu.run_cmd(["netstat", "-ano", "-p", "TCP"],
                                  timeout=limit)
        if code == -1:
            return [], "timeout"
        if code != 0 and not out.strip():
            return [], "failed"
        rows = parse_netstat(out)
        return merge_rows(rows, _windows_names(limit)), ""

    if not shutil.which("lsof"):
        return [], "no_tool"
    code, out, _ = pu.run_cmd(
        ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pcn"], timeout=limit)
    if code == -1:
        return [], "timeout"
    # lsof 没有任何监听时返回 1 且输出为空，这不是错误
    return merge_rows(parse_lsof(out)), ""


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def report(rows=None, only: str = "", kind: str = "", limit: int = 0,
           timeout: float = DEFAULT_TIMEOUT) -> dict:
    """汇总监听端口；``rows`` 可注入（测试用，免跑真实命令）。

    ``only`` 匹配端口号 / 进程名 / 地址子串；``kind`` 按归属过滤；
    ``limit`` 限制行数（按端口升序取前 N 条）。
    """
    error = ""
    if rows is None:
        rows, error = listening_ports(timeout)
    items = list(rows or [])

    needle = (only or "").strip().lower()
    if needle:
        items = [it for it in items
                 if needle in str(it.get("port") or "")
                 or needle in str(it.get("process") or "").lower()
                 or needle in str(it.get("addr") or "").lower()]
    wanted = (kind or "").strip().lower()
    if wanted:
        items = [it for it in items if it.get("kind") == wanted]
    if limit and limit > 0:
        items = items[:limit]

    counts = {k: 0 for k in KINDS}
    # 冲突按「同端口出现不同进程名」判定：nginx 多 worker（SO_REUSEPORT）同名
    # 共监听一个端口是正常形态，不能报成冲突。
    by_port: dict[int, set[str]] = {}
    for it in items:
        key = str(it.get("kind") or "other")
        counts[key] = counts.get(key, 0) + 1
        name = str(it.get("process") or "").strip().lower()
        if not name:
            name = f"pid:{it.get('pid') or 0}"
        by_port.setdefault(int(it["port"]), set()).add(name)
    conflicts = sorted(p for p, names in by_port.items() if len(names) > 1)

    services = sum(counts[k] for k in ("nginx", "php", "redis", "mysql"))
    return {
        "ok": not error,
        "error": error,
        "total": len(items),
        "ports": len(by_port),
        "services": services,
        "other": counts.get("other", 0),
        "conflicts": conflicts,
        "counts": counts,
        "items": items,
    }
