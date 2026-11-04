# -*- coding: utf-8 -*-
"""整机物理内存占用快照（零第三方依赖，跨平台）。

与 ``core/resource_monitor.py`` 的区别：后者只统计 phpvm **管理的进程** 的
RSS / CPU，本模块统计的是**整机**物理内存，供主窗口状态栏槽位显示。

平台实现：

- Windows：``GlobalMemoryStatusEx``（ctypes，零子进程，直接给出可用物理内存）；
- Linux：读 ``/proc/meminfo`` 的 ``MemTotal`` / ``MemAvailable``；
- macOS：``sysctl`` 取总量 + ``vm_stat`` 解析空闲 / 非活跃页（两次轻量子进程）；
- 其它平台或取数失败：返回 ``ok=False``，界面显示「内存 —」而不是报错。

采集是**纯读**操作：不写文件、不缓存进程状态、不启动常驻线程，由调用方决定刷新频率。
"""
import ctypes
import os
import re
import subprocess
import sys
from dataclasses import dataclass

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

_GIB = 1024 ** 3


@dataclass(frozen=True)
class MemorySnapshot:
    """整机物理内存快照（字节）。``ok`` 为 False 时表示当前平台取不到可靠数据。"""

    total_bytes: int = 0
    available_bytes: int = 0
    used_bytes: int = 0
    percent: float = -1.0
    source: str = "unavailable"

    @property
    def ok(self) -> bool:
        return self.total_bytes > 0 and 0.0 <= self.percent <= 100.0


def _make(total: int, available: int, source: str) -> MemorySnapshot:
    """从总量 / 可用量推导占用量，并统一钳制边界（纯函数，便于单测）。"""
    total = int(total or 0)
    if total <= 0:
        return MemorySnapshot(source=source)
    available = max(0, min(int(available or 0), total))
    used = total - available
    return MemorySnapshot(total, available, used,
                          round(100.0 * used / total, 1), source)


def from_win_status(total: int, available: int) -> MemorySnapshot:
    """Windows ``GlobalMemoryStatusEx`` 的 ``ullTotalPhys`` / ``ullAvailPhys``。"""
    return _make(total, available, "win32")


def from_meminfo(text: str) -> MemorySnapshot:
    """解析 ``/proc/meminfo``；缺 ``MemAvailable`` 时用 MemFree+Buffers+Cached 近似。"""
    fields: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            fields[key.strip()] = int(parts[0]) * 1024  # /proc/meminfo 单位是 kB
        except ValueError:
            continue
    total = fields.get("MemTotal", 0)
    if not total:
        return MemorySnapshot(source="proc_meminfo")
    available = fields.get("MemAvailable")
    if available is None:
        available = sum(fields.get(k, 0)
                        for k in ("MemFree", "Buffers", "Cached"))
    return _make(total, available, "proc_meminfo")


def from_vm_stat(text: str, total_bytes: int, page_size: int) -> MemorySnapshot:
    """解析 macOS ``vm_stat``：free + inactive + speculative 视为可立即回收的页。"""
    fields: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        m = re.search(r"(\d+)", rest.replace(",", ""))
        if m:
            fields[key.strip()] = int(m.group(1))
    if total_bytes <= 0 or page_size <= 0:
        return MemorySnapshot(source="vm_stat")
    free_pages = sum(fields.get(k, 0) for k in
                     ("Pages free", "Pages inactive", "Pages speculative"))
    return _make(total_bytes, free_pages * page_size, "vm_stat")


def format_gb(n: int) -> str:
    """字节数转 GB 字符串（状态栏短文案用；取不到时显示破折号）。"""
    return f"{n / _GIB:.1f} GB" if n and n > 0 else "—"


# --------------------------------------------------------------------------- #
# 平台取数
# --------------------------------------------------------------------------- #
def _win_snapshot() -> MemorySnapshot:
    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
    dll.GlobalMemoryStatusEx.restype = ctypes.c_bool
    stat = _MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(stat)
    if not dll.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return MemorySnapshot(source="win32")
    return from_win_status(int(stat.ullTotalPhys), int(stat.ullAvailPhys))


def _run(args: list[str]) -> str:
    out = subprocess.run(args, capture_output=True, text=True, timeout=2,
                         check=False)
    return out.stdout if out.returncode == 0 else ""


def _mac_total_bytes() -> int:
    try:
        return int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):
        pass
    try:
        return int(_run(["sysctl", "-n", "hw.memsize"]).strip())
    except (ValueError, OSError, subprocess.SubprocessError):
        return 0


def _mac_snapshot() -> MemorySnapshot:
    total = _mac_total_bytes()
    try:
        page = int(os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):
        page = 4096
    if not total:
        return MemorySnapshot(source="vm_stat")
    try:
        text = _run(["vm_stat"])
    except (OSError, subprocess.SubprocessError):
        return MemorySnapshot(source="vm_stat")
    return from_vm_stat(text, total, page) if text else MemorySnapshot(source="vm_stat")


def snapshot() -> MemorySnapshot:
    """取一次整机物理内存快照；任何异常都降级为 ``ok=False``，绝不抛给调用方。"""
    if IS_WIN:
        try:
            return _win_snapshot()
        except Exception:  # noqa: BLE001 - 探测失败不影响主界面
            return MemorySnapshot(source="win32")
    if os.path.exists("/proc/meminfo"):
        try:
            with open("/proc/meminfo", "r", encoding="utf-8",
                      errors="replace") as f:
                return from_meminfo(f.read())
        except OSError:
            pass
    if IS_MAC:
        return _mac_snapshot()
    return MemorySnapshot()
