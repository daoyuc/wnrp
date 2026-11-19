# -*- coding: utf-8 -*-
"""PHP 错误日志聚合核心：找日志 → 读尾部 → 归一化 → 按级别与消息聚合。

日志来源（只读，按「静态可枚举」原则）：

1. 各 PHP 版本 ``php.ini`` 的 ``error_log``（相对路径按版本目录解析；
   ``syslog`` / 空值跳过）；
2. 站点 root 下的 ``storage/logs``（Laravel）、``runtime/logs``（ThinkPHP）、
   ``logs`` 下的 ``*.log``。

边界：

- 每个文件最多读尾部 :data:`MAX_FILE_BYTES`，全局最多 :data:`MAX_FILES` 个
  文件（按 mtime 倒序），不递归进子目录、不跟随符号链接；
- 单行解析失败即跳过（计入 ``skipped``），不因为一条怪日志放弃整份文件；
- 聚合键做了归一化（数字 → ``N``、绝对路径 → ``<path>``），否则每条带行号的
  报错都是独立条目，Top N 永远是一堆只出现一次的噪音；
- 不写文件、不落库、不起后台进程。
"""
import os
import re
import time

DEFAULT_TOP = 10
DEFAULT_LIMIT = 200
MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 40

#: 级别 → i18n 源文案（界面与 CLI 统一 ``t(LEVEL_MESSAGES[level])``）
LEVEL_MESSAGES = {
    "fatal": "致命错误",
    "error": "错误",
    "warning": "警告",
    "notice": "提示",
    "deprecated": "弃用",
    "other": "其他",
}
LEVELS = tuple(LEVEL_MESSAGES)

ERROR_MESSAGES = {
    "no_source": "没有找到 PHP 错误日志（php.ini 的 error_log 与站点日志目录都为空）",
    "no_match": "日志里没有匹配的错误行",
}

#: Laravel：[2026-11-13 10:00:00] production.ERROR: message
_RE_LARAVEL = re.compile(r"^\[([^\]]{8,48})\]\s+\S+?\.([A-Za-z]+):\s*(.*)$")
#: PHP 原生：[13-Nov-2026 10:00:00 UTC] PHP Warning:  message in ...
_RE_PHP = re.compile(r"^\[([^\]]{8,48})\]\s+PHP\s+([A-Za-z][A-Za-z ]*?):\s*(.*)$")
_RE_NUM = re.compile(r"\d+")
_RE_PATH = re.compile(r"(?:[A-Za-z]:)?[\\/][^\s:'\"]+(?:[\\/][^\s:'\"]+)+")

_SITE_DIRS = (os.path.join("storage", "logs"),
              os.path.join("runtime", "logs"),
              "logs")


# --------------------------------------------------------------------------- #
# 解析（纯函数）
# --------------------------------------------------------------------------- #
def level_of(word: str) -> str:
    """把日志里的级别词归一为 6 个固定级别。"""
    text = str(word or "").strip().lower()
    if text.startswith("fatal"):
        return "fatal"
    if text.startswith("error") or text in ("critical", "alert", "emergency"):
        return "error"
    if text.startswith("warning"):
        return "warning"
    if text.startswith(("notice", "info")):
        return "notice"
    if text.startswith("deprecated"):
        return "deprecated"
    return "other"


def normalize(message: str) -> str:
    """聚合键：去掉行号 / 数字 / 绝对路径，避免同一条错误聚不起来。"""
    text = str(message or "").split("\n")[0].strip()
    text = _RE_PATH.sub("<path>", text)
    text = _RE_NUM.sub("N", text)
    return text[:180]


def parse_line(line: str) -> dict | None:
    """解析一行日志，返回 ``{time, level, message}``；不是错误行返回 None。"""
    text = str(line or "").rstrip()
    if not text or not text.startswith("["):
        return None
    match = _RE_LARAVEL.match(text) or _RE_PHP.match(text)
    if not match:
        return None
    message = match.group(3).strip()
    if not message:
        return None
    return {"time": match.group(1).strip(),
            "level": level_of(match.group(2)),
            "message": message[:200]}


def read_tail(path: str, max_bytes: int = MAX_FILE_BYTES) -> str:
    """读取文件尾部文本；跨平台按二进制读，避免大文件一次读进内存。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    if start:  # 掐掉被截断的首行，避免解析出半条记录
        cut = text.find("\n")
        if cut >= 0:
            text = text[cut + 1:]
    return text


# --------------------------------------------------------------------------- #
# 来源发现
# --------------------------------------------------------------------------- #
def _abs_ini_path(raw: str, base: str) -> str:
    path = os.path.expanduser(str(raw or "").strip().strip('"').strip("'"))
    if not path:
        return ""
    return path if os.path.isabs(path) else os.path.join(base, path)


def ini_sources(config=None) -> list[dict]:
    """各 PHP 版本 ``error_log`` 指向的文件（存在且非空才算）。"""
    out: list[dict] = []
    try:
        from ...php_manager import PhpManager

        mgr = PhpManager(config)
        versions = list(mgr.scan_versions())
    except Exception:  # noqa: BLE001 - 没有 PHP 目录时正常退化为空
        return out
    for version in versions:
        name = str(getattr(version, "name", "") or "")
        base = str(getattr(version, "path", "") or "")
        try:
            keys = mgr.read_key_ini(version)
        except Exception:  # noqa: BLE001
            continue
        raw = str(keys.get("error_log") or "").strip()
        if not raw or raw.lower() == "syslog":
            continue
        path = _abs_ini_path(raw, base)
        if path and os.path.isfile(path):
            out.append({"path": path, "kind": "php", "label": name,
                        "size": os.path.getsize(path)})
    return out


def _roots(entries) -> list[str]:
    roots: list[str] = []
    for entry in entries or []:
        root = str(getattr(entry, "root", "") or "")
        if root and root not in roots:
            roots.append(root)
    return roots


def site_sources(entries) -> list[dict]:
    """站点日志目录里的 ``*.log``（不递归、不跟随符号链接）。"""
    found: list[tuple[float, str, str]] = []
    for root in _roots(entries):
        label = os.path.basename(root.rstrip("\\/")) or root
        for sub in _SITE_DIRS:
            folder = os.path.join(root, sub)
            if not os.path.isdir(folder) or os.path.islink(folder):
                continue
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                if not name.lower().endswith(".log"):
                    continue
                path = os.path.join(folder, name)
                if os.path.islink(path) or not os.path.isfile(path):
                    continue
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if stat.st_size > 0:
                    found.append((stat.st_mtime, path, label))
    found.sort(key=lambda it: it[0], reverse=True)
    return [{"path": path, "kind": "site", "label": label,
             "size": os.path.getsize(path)} for _mtime, path, label in found]


def sources(config=None, entries=None, max_files: int = MAX_FILES) -> list[dict]:
    """全部可读的日志来源（PHP ini 优先，其后站点日志，各自去重）。"""
    if entries is None:
        try:
            from ...vhost_manager import VhostManager

            entries = VhostManager(config).scan()
        except Exception:  # noqa: BLE001
            entries = []
    items = ini_sources(config) + site_sources(entries)
    seen: set[str] = set()
    out: list[dict] = []
    for item in items:
        path = os.path.realpath(item["path"])
        if path in seen:
            continue
        seen.add(path)
        out.append(item)
        if max_files and len(out) >= max_files:
            break
    return out


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def report(config=None, entries=None, only: str = "", level: str = "",
           top: int = DEFAULT_TOP, limit: int = DEFAULT_LIMIT,
           max_files: int = MAX_FILES, log_sources=None) -> dict:
    """扫描日志并聚合；``log_sources`` 可注入以便测试（免读真实配置）。"""
    items = (list(log_sources) if log_sources is not None
             else sources(config, entries=entries, max_files=max_files))
    needle = (only or "").strip().lower()
    if needle:
        items = [it for it in items if needle in it["path"].lower()]

    counts = {k: 0 for k in LEVELS}
    groups: dict[str, dict] = {}
    recent: list[dict] = []
    lines = 0
    skipped = 0
    matched = 0

    for item in items:
        text = read_tail(item["path"])
        for raw in text.splitlines():
            lines += 1
            parsed = parse_line(raw)
            if parsed is None:
                skipped += 1
                continue
            matched += 1
            counts[parsed["level"]] = counts.get(parsed["level"], 0) + 1
            key = f'{parsed["level"]}|{normalize(parsed["message"])}'
            group = groups.get(key)
            if group is None:
                groups[key] = {"message": parsed["message"],
                               "level": parsed["level"], "count": 1,
                               "last": parsed["time"],
                               "source": item["label"],
                               "path": item["path"]}
            else:
                group["count"] += 1
                if parsed["time"] >= group["last"]:
                    group["last"] = parsed["time"]
                    group["path"] = item["path"]
                    group["source"] = item["label"]
            recent.append({"time": parsed["time"], "level": parsed["level"],
                           "message": parsed["message"],
                           "source": item["label"], "file": item["path"]})

    wanted = (level or "").strip().lower()
    top_rows = sorted(groups.values(),
                      key=lambda g: (-g["count"], g["level"]))
    if wanted:
        top_rows = [g for g in top_rows if g["level"] == wanted]
    recent_rows = [r for r in recent if not wanted or r["level"] == wanted]
    recent_rows.sort(key=lambda r: r["time"], reverse=True)

    error = ""
    if not items:
        error = "no_source"
    elif not matched:
        error = "no_match"
    return {
        "ok": not error,
        "error": error,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": len(items),
        "lines": lines,
        "skipped": skipped,
        "entries": matched,
        "counts": counts,
        "top": top_rows[:max(1, int(top or 1))],
        "recent": recent_rows[:max(1, int(limit or 1))],
        "sources": [{"path": it["path"], "kind": it["kind"],
                     "label": it["label"], "size": it["size"]}
                    for it in items],
    }
