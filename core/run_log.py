# -*- coding: utf-8 -*-
"""全局运行日志：应用启动/退出、服务启停结果、异常与告警的统一记录。

为什么需要：
- 状态栏只显示「最后一条」消息，服务没起来、自动启动失败等线索转瞬即逝；
- 排障时关心的是「什么时候、哪个组件、成功还是失败」。

设计要点：
- 内存环形缓冲（最近 MAX_ENTRIES 条）→ 「运行日志」页签实时展示；
- 追加落盘 ``<数据目录>/run_log.log``，超过 MAX_BYTES 轮转为 ``run_log.log.1``；
- 线程安全：worker 线程可直接写入；订阅者回调的异常一律吞掉 ——
  日志系统自身绝不抛错、绝不影响业务；
- 不依赖 tkinter，GUI（main.py / ui/*）与 CLI（cli.py）共用同一份记录与文件；
- 环境变量 ``PHPVM_RUN_LOG=0`` 可整体关闭（测试或极简场景）。

约定（调用方）：
- scope 用短横线小写模块名：``app`` / ``env`` / ``services`` / ``php`` /
  ``nginx`` / ``crash`` / ``recover`` / ``update`` / ``ui``；
- message 由调用方 ``t()`` 翻译后再传入，本模块不做翻译。
"""
import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from . import app_paths

LEVELS = ("info", "ok", "warn", "error")
MAX_ENTRIES = 2000          # 内存保留条数（面板展示上限）
MAX_BYTES = 1024 * 1024     # 落盘单文件上限，超过则轮转
LOG_NAME = "run_log.log"

_lock = threading.RLock()
_entries: deque = deque(maxlen=MAX_ENTRIES)
_seq = 0
_sinks: list = []
_enabled = (os.environ.get("PHPVM_RUN_LOG", "1") or "1").lower() not in (
    "0", "false", "off", "no")


@dataclass(frozen=True)
class Entry:
    """一条运行日志。seq 单调递增，便于增量拉取。"""

    seq: int
    ts: float
    level: str
    scope: str
    message: str

    @property
    def time_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts))

    @property
    def line(self) -> str:
        """落盘 / 导出用的单行文本（message 自带换行时原样保留）。"""
        return f"{self.time_text} [{self.level.upper()}] [{self.scope}] {self.message}"


# --------------------------------------------------------------------------- #
# 开关与路径
# --------------------------------------------------------------------------- #
def enabled() -> bool:
    return _enabled


def set_enabled(value: bool) -> None:
    """整体开关（默认开；关闭后既不记内存也不落盘）。"""
    global _enabled
    _enabled = bool(value)


def log_path() -> str:
    """落盘日志文件路径（数据目录下，安装态为 ~/.phpvm）。"""
    return app_paths.data_file(LOG_NAME)


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def _rotate(path: str) -> None:
    """超过上限把当前文件改名为 .1（保留一份历史，旧的覆盖）。"""
    try:
        if os.path.getsize(path) < MAX_BYTES:
            return
    except OSError:
        return
    try:
        backup = path + ".1"
        if os.path.exists(backup):
            os.remove(backup)
        os.replace(path, backup)
    except OSError:
        pass


def _write_file(entry: Entry) -> None:
    path = log_path()
    _rotate(path)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry.line + "\n")
    except OSError:
        pass


def log(level: str, scope: str, message) -> Entry | None:
    """写入一条日志并返回条目（关闭状态或空消息返回 None）。"""
    if not _enabled:
        return None
    text = str(message).strip()
    if not text:
        return None
    global _seq
    with _lock:
        _seq += 1
        entry = Entry(
            seq=_seq,
            ts=time.time(),
            level=level if level in LEVELS else "info",
            scope=str(scope),
            message=text,
        )
        _entries.append(entry)
        _write_file(entry)
        sinks = list(_sinks)
    # 回调在锁外执行：订阅者（UI 面板）只做入队，不阻塞写入方
    for cb in sinks:
        try:
            cb(entry)
        except Exception:  # noqa: BLE001
            pass
    return entry


def info(scope: str, message) -> Entry | None:
    return log("info", scope, message)


def ok(scope: str, message) -> Entry | None:
    return log("ok", scope, message)


def warn(scope: str, message) -> Entry | None:
    return log("warn", scope, message)


def error(scope: str, message) -> Entry | None:
    return log("error", scope, message)


# --------------------------------------------------------------------------- #
# 读取 / 订阅
# --------------------------------------------------------------------------- #
def snapshot(limit: int | None = None) -> list[Entry]:
    """内存中的日志副本（时间正序）；limit 给定时只取最后 N 条。"""
    with _lock:
        items = list(_entries)
    return items[-limit:] if limit else items


def entries_since(seq: int) -> list[Entry]:
    """seq 之后的新条目（面板增量拉取）。"""
    with _lock:
        return [e for e in _entries if e.seq > seq]


def last_seq() -> int:
    with _lock:
        return _seq


def clear() -> None:
    """清空内存缓冲（落盘文件保留，供事后排查）。"""
    with _lock:
        _entries.clear()


def add_sink(cb) -> None:
    """注册订阅回调（在写入线程内调用，务必只做入队等轻量操作）。"""
    with _lock:
        if cb not in _sinks:
            _sinks.append(cb)


def remove_sink(cb) -> None:
    with _lock:
        if cb in _sinks:
            _sinks.remove(cb)


def as_text(limit: int | None = None) -> str:
    """内存日志拼成文本（面板导出 / 复制用）。"""
    return "\n".join(e.line for e in snapshot(limit))


def tail_file(limit: int = 500) -> list[str]:
    """读取落盘日志尾部若干行（文件被外部清空/不存在时返回空表）。"""
    try:
        with open(log_path(), "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    return lines[-limit:]
