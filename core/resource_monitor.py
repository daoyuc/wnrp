# -*- coding: utf-8 -*-
"""轻量资源监控（F12 · P2）：统计 phpvm 管理的进程（nginx / 各 PHP 版本 /
Redis 实例 / MySQL 实例）的 CPU 与内存占用。

设计取舍（对标 ServBay / FlyEnv，但不引 psutil）：
- 标准库无 psutil，需分平台取数：
  * macOS / Linux：一次 ``ps -axo pid=,rss=,%cpu=,time=,command=`` 全量快照
    （带 TTL 缓存，复用 process_utils 的风格），本地按 PID 过滤；
  * Windows：ctypes 调 GetProcessMemoryInfo（RSS）+ GetProcessTimes（CPU 时间），
    零外部进程；对**打不开句柄**的进程（以服务方式运行的 mysqld 属 SYSTEM，
    非提权进程 OpenProcess 一律 ERROR_ACCESS_DENIED）再退回一次
    NtQuerySystemInformation 全量快照补数 —— 这也是任务管理器在非提权下
    依然能看到服务进程内存的原理（无需句柄、无需提权）。
- CPU% 用「两次采样的累计 CPU 时间差 / 采样间隔」算瞬时占用（更准确），
  首次采样无基线时回退 ps 的 lifetime %cpu（Windows 回退为 -1 表示未知）。
- 聚合视角以「服务」为单位：每个 nginx / PHP 版本 / Redis 实例 / MySQL 实例
  把名下全部 PID 的 RSS 与 CPU% 求和，得到该服务的资源画像。
"""
import threading
import time

from . import process_utils as pu
from .config import IS_WIN

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    class _ProcessMemoryCounters(ctypes.Structure):
        """标准 PROCESS_MEMORY_COUNTERS（10 字段，SIZE_T 宽度随架构）。

        注意：曾额外追加 `*64` 字段并把 `cb` 设为放大后的 sizeof，
        会让 API 校验 cbSize 失败（Windows 上 RSS 恒为 0）。
        """
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    def _load_mem_info_fn():
        """解析可用的 GetProcessMemoryInfo；都取不到返回 None。

        `GetProcessMemoryInfo` 由 **psapi.dll** 导出，新系统在 kernel32 上以
        `K32GetProcessMemoryInfo` 转发；实测 `kernel32.GetProcessMemoryInfo`
        并不存在（直接调用会 AttributeError，被上层吞掉后 RSS 恒 0）。
        """
        for dll, sym in (("psapi", "GetProcessMemoryInfo"),
                         ("kernel32", "K32GetProcessMemoryInfo"),
                         ("kernel32", "GetProcessMemoryInfo")):
            try:
                lib = ctypes.WinDLL(dll)
            except (OSError, AttributeError):
                continue
            fn = getattr(lib, sym, None)
            if fn is None:
                continue
            fn.argtypes = [wintypes.HANDLE,
                           ctypes.POINTER(_ProcessMemoryCounters),
                           wintypes.DWORD]
            fn.restype = wintypes.BOOL
            return fn
        return None

    _mem_info_fn = _load_mem_info_fn()

    def _load_nt_query_fn():
        """解析 NtQuerySystemInformation；取不到返回 None（则不做系统快照回退）。"""
        try:
            ntdll = ctypes.WinDLL("ntdll")
        except (OSError, AttributeError):  # pragma: no cover - 非 Windows
            return None
        fn = getattr(ntdll, "NtQuerySystemInformation", None)
        if fn is None:
            return None
        fn.argtypes = [ctypes.c_long, ctypes.c_void_p, wintypes.ULONG,
                       ctypes.POINTER(wintypes.ULONG)]
        fn.restype = ctypes.c_long
        return fn

    _nt_query_fn = _load_nt_query_fn()
else:
    _mem_info_fn = None
    _nt_query_fn = None


# --------------------------------------------------------------------------- #
# 进程级采样
# --------------------------------------------------------------------------- #
class ProcSample:
    """单进程资源采样。cpu_percent=-1 表示尚不可知（无基线或取数失败）。"""

    __slots__ = ("pid", "name", "rss_bytes", "cpu_percent", "cpu_time_total", "cmd")

    def __init__(self, pid, name="", rss_bytes=0, cpu_percent=-1.0,
                 cpu_time_total=0.0, cmd=""):
        self.pid = pid
        self.name = name
        self.rss_bytes = rss_bytes
        self.cpu_percent = cpu_percent
        self.cpu_time_total = cpu_time_total
        self.cmd = cmd


# posix 全量 {pid: (rss_kb, cpu_lifetime, cpu_time_seconds, cmd)} 快照 + TTL
_posix_rsrc_snap = None
_posix_rsrc_lock = threading.Lock()
_POSIX_RSRC_TTL = 2.0  # 秒

# CPU 基线缓存：pid -> (monotonic_ts, cpu_time_seconds)
_prev_lock = threading.Lock()
_prev: dict[int, tuple[float, float]] = {}

# Windows 句柄权限
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010


def human_bytes(n: int) -> str:
    """把字节数转可读字符串（B / KB / MB / GB）。"""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


def format_rss(n: int) -> str:
    """内存占用的展示值：0/负值意味着「取不到」而非「不占内存」。

    取不到是常态之一 —— 例如 MySQL 以 Windows 服务方式由 SYSTEM 运行、
    非提权进程的 OpenProcess 被拒（此时 rss=0、ctime=-1）。活跃进程的工作集
    必然 > 0，故这里渲染成「—」，避免把「读不到」误读成「0 B」。
    """
    return human_bytes(n) if n and n > 0 else "—"


def _parse_ps_time(s: str) -> float:
    """解析 ps 的累计 CPU 时间字符串为秒。

    兼容 macOS（``0:00.05`` / ``1:23``）与 Linux（``1:02:03`` / ``2-03:04:05``）。
    """
    s = (s or "").strip()
    if not s:
        return 0.0
    days = 0.0
    if "-" in s:
        dpart, _, rest = s.partition("-")
        try:
            days = float(dpart)
        except ValueError:
            days = 0.0
        s = rest
    comps = s.split(":")
    try:
        if len(comps) == 1:
            total = float(comps[0])
        elif len(comps) == 2:
            total = int(comps[0]) * 60 + float(comps[1])
        else:
            # 时:分:秒（取最后三段，忽略更细的层级）
            h, m, sec = comps[-3], comps[-2], comps[-1]
            total = int(h) * 3600 + int(m) * 60 + float(sec)
    except (ValueError, IndexError):
        return 0.0
    return days * 86400 + total


def _posix_resource_snapshot(force: bool = False) -> dict:
    """一次 ps 返回全量 {pid: (rss_kb, cpu_lifetime, cpu_time, cmd)}，带 TTL 缓存。"""
    global _posix_rsrc_snap
    now = time.monotonic()
    with _posix_rsrc_lock:
        cached = _posix_rsrc_snap
        if not force and cached and now - cached[0] < _POSIX_RSRC_TTL:
            return cached[1]
    code, out, _ = pu.run_cmd(
        ["ps", "-axo", "pid=,rss=,%cpu=,time=,command="], timeout=15)
    snap: dict[int, tuple] = {}
    if code == 0:
        for raw in out.splitlines():
            line = raw.strip()
            if not line:
                continue
            sp1 = line.find(" ")
            if sp1 <= 0:
                continue
            try:
                pid = int(line[:sp1])
            except ValueError:
                continue
            rest = line[sp1:].strip()
            # rss %cpu time command 四列；command 可能含空格，最后整体取
            parts = rest.split(None, 3)
            if len(parts) < 4:
                continue
            try:
                rss = int(parts[0])
                cpu = float(parts[1])
            except ValueError:
                continue
            ctime = _parse_ps_time(parts[2])
            cmd = parts[3]
            snap[pid] = (rss, cpu, ctime, cmd)
    with _posix_rsrc_lock:
        _posix_rsrc_snap = (now, snap)
    return snap


def _win_rss_and_time(pid: int) -> tuple[int, float]:
    """Windows：返回 (工作集字节, 累计 CPU 秒)。

    取数失败时该指标用哨兵值表示「未知」：rss=0（活跃进程工作集必 >0）、
    ctime=-1.0。两者可能单独失败（例如服务以 SYSTEM 运行、非提权进程
    打不开句柄），因此不能让失败值参与 CPU 差值计算，否则会算出荒谬的百分比。
    """
    if _mem_info_fn is None:
        return 0, -1.0
    try:
        k32 = ctypes.windll.kernel32
    except AttributeError:  # pragma: no cover - 非 Windows
        return 0, -1.0
    handle = k32.OpenProcess(
        _PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not handle:
        return 0, -1.0
    try:
        rss = 0
        ctime = -1.0
        pmc = _ProcessMemoryCounters()
        pmc.cb = ctypes.sizeof(pmc)
        if _mem_info_fn(handle, ctypes.byref(pmc), ctypes.sizeof(pmc)):
            rss = int(pmc.WorkingSetSize)

        # GetProcessTimes -> kernel + user FILETIME（100ns 为单位）
        kt = wintypes.FILETIME()
        ut = wintypes.FILETIME()
        if k32.GetProcessTimes(handle, ctypes.byref(wintypes.FILETIME()),
                               ctypes.byref(wintypes.FILETIME()),
                               ctypes.byref(kt), ctypes.byref(ut)):
            def _ft2ns(ft):
                return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

            ctime = (_ft2ns(kt) + _ft2ns(ut)) / 1e7
        return rss, ctime
    except Exception:  # noqa: BLE001
        return 0, -1.0
    finally:
        try:
            k32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass


# Windows 全量进程快照 {pid: (rss, cpu 秒, 存活秒)} + TTL（与 posix 快照同节奏）
_win_sysproc_snap = None
_win_sysproc_lock = threading.Lock()
_WIN_SYSPROC_TTL = 2.0  # 秒

# SYSTEM_PROCESS_INFORMATION 字段偏移：**仅 64 位验证过**（本机 x64 实测：
# 工作集 23,756,800 B 与 tasklist 的 23,200 K 完全吻合）。32 位偏移未实测，
# 因此只在 64 位解释器上启用回退，其余情况维持原行为（读不到就显示「—」）。
_OFF_CREATE_TIME = 0x20
_OFF_USER_TIME = 0x28
_OFF_KERNEL_TIME = 0x30
_OFF_PID = 0x50
_OFF_WORKING_SET = 0x90
_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004


def _win_filetime_now() -> int:
    """当前 FILETIME（100ns 为单位）。"""
    ft = wintypes.FILETIME()
    ctypes.windll.kernel32.GetSystemTimeAsFileTime(ctypes.byref(ft))
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _win_query_system_processes(max_bytes: int = 64 << 20) -> bytes:
    """一次 NtQuerySystemInformation 取回全部进程信息；失败返回空字节。"""
    if _nt_query_fn is None:
        return b""
    size = 1 << 20
    while size <= max_bytes:
        buf = ctypes.create_string_buffer(size)
        ret = wintypes.ULONG(0)
        try:
            status = _nt_query_fn(_SYSTEM_PROCESS_INFORMATION, buf, size,
                                  ctypes.byref(ret))
        except Exception:  # noqa: BLE001 - 结构/权限异常一律退回旧行为
            return b""
        if status == 0:
            return buf.raw[: int(ret.value) or size]
        if status & 0xFFFFFFFF != _STATUS_INFO_LENGTH_MISMATCH:
            return b""
        size = max(size * 2, int(ret.value) * 2)
    return b""


def _win_system_process_snapshot(force: bool = False) -> dict[int, tuple]:
    """Windows 全量进程快照：``{pid: (工作集字节, 累计 CPU 秒, 存活秒)}``（TTL 缓存）。

    为什么需要它：以 Windows 服务运行的 mysqld / nginx 属于 SYSTEM（或 Network Service），
    非提权进程对它 ``OpenProcess`` 一律 ``ERROR_ACCESS_DENIED``——实测连
    ``PROCESS_QUERY_LIMITED_INFORMATION`` 也被拒，于是 RSS 恒 0、CPU 恒「未知」。
    系统信息查询**不需要进程句柄、也不需要提权**，正好补上这类进程的指标。
    """
    global _win_sysproc_snap
    if _nt_query_fn is None or ctypes.sizeof(ctypes.c_void_p) != 8:
        return {}
    now = time.monotonic()
    with _win_sysproc_lock:
        cached = _win_sysproc_snap
        if not force and cached and now - cached[0] < _WIN_SYSPROC_TTL:
            return cached[1]
    data = _win_query_system_processes()
    now_ft = _win_filetime_now()
    snap: dict[int, tuple] = {}
    pos = 0
    total = len(data)
    while pos + _OFF_WORKING_SET + 8 <= total:
        next_off = int.from_bytes(data[pos:pos + 4], "little")
        try:
            create = int.from_bytes(
                data[pos + _OFF_CREATE_TIME:pos + _OFF_CREATE_TIME + 8], "little")
            user = int.from_bytes(
                data[pos + _OFF_USER_TIME:pos + _OFF_USER_TIME + 8], "little")
            kernel = int.from_bytes(
                data[pos + _OFF_KERNEL_TIME:pos + _OFF_KERNEL_TIME + 8], "little")
            rss = int.from_bytes(
                data[pos + _OFF_WORKING_SET:pos + _OFF_WORKING_SET + 8], "little")
            pid = int.from_bytes(data[pos + _OFF_PID:pos + _OFF_PID + 8], "little")
        except Exception:  # noqa: BLE001
            break
        if pid > 0 and rss > 0:
            snap[pid] = (rss, (user + kernel) / 1e7,
                         max(0.0, (now_ft - create) / 1e7))
        if next_off <= 0:
            break
        pos += next_off
    with _win_sysproc_lock:
        _win_sysproc_snap = (now, snap)
    return snap


def snapshot_pids(pids: list[int], force: bool = False) -> dict[int, ProcSample]:
    """返回 {pid: ProcSample}（仅含传入且在系统中存在的 PID）。

    posix 走一次 ps 全量快照（TTL 缓存），Windows 走 ctypes 逐 PID 取数，
    打不开句柄的进程再用系统级快照补数。
    CPU% 尽量用两次采样差；首次/失败为 -1 或 ps 的 lifetime 值。
    """
    result: dict[int, ProcSample] = {}
    now = time.monotonic()
    if IS_WIN:
        sysproc: dict | None = None
        for pid in pids:
            if pid <= 0:
                continue
            rss, ctime = _win_rss_and_time(pid)
            lifetime = None
            if rss <= 0 or ctime < 0:
                # 句柄打不开（典型：SYSTEM 运行的服务进程）→ 系统快照补数
                if sysproc is None:
                    sysproc = _win_system_process_snapshot(force=force)
                hit = sysproc.get(pid)
                if hit:
                    if rss <= 0:
                        rss = hit[0]
                    if ctime < 0:
                        ctime = hit[1]
                    lifetime = hit[2]
            with _prev_lock:
                base = _prev.get(pid)
            cpu = -1.0
            # 两次采样都拿到 CPU 时间才做差值；否则保持「未知」——
            # 把取数失败的 -1 当基线会算出荒谬的百分比（如 1000%+）
            if ctime >= 0 and base is not None and base[1] >= 0:
                dt = now - base[0]
                if dt > 0.05 and ctime >= base[1]:
                    cpu = (ctime - base[1]) / dt * 100.0
            elif ctime >= 0 and lifetime:
                # 首次采样无基线：退化为「存活期内的平均 CPU%」，
                # 与 posix 用 ps 的 lifetime %cpu 兜底是同一策略
                # （多线程服务可能 >100%，与差值口径一致，不做截断）
                cpu = ctime / lifetime * 100.0
            with _prev_lock:
                _prev[pid] = (now, ctime)
            result[pid] = ProcSample(pid=pid, rss_bytes=rss,
                                     cpu_percent=cpu,
                                     cpu_time_total=max(0.0, ctime))
        return result

    snap = _posix_resource_snapshot(force=force)
    for pid in pids:
        if pid <= 0:
            continue
        hit = snap.get(pid)
        if not hit:
            continue
        rss_kb, cpu_life, ctime, cmd = hit
        with _prev_lock:
            base = _prev.get(pid)
        if base is None:
            cpu = cpu_life
        else:
            dt = now - base[0]
            if dt > 0.05 and ctime >= base[1]:
                cpu = (ctime - base[1]) / dt * 100.0
                if cpu < 0:
                    cpu = cpu_life
            else:
                cpu = cpu_life
        with _prev_lock:
            _prev[pid] = (now, ctime)
        name = cmd.split()[0] if cmd else ""
        result[pid] = ProcSample(pid=pid, name=name, rss_bytes=rss_kb * 1024,
                                 cpu_percent=cpu, cpu_time_total=ctime, cmd=cmd)
    return result


def reset_cpu_baseline() -> None:
    """清空 CPU 基线缓存（如长时间未采样的进程已退出）。"""
    with _prev_lock:
        _prev.clear()


# --------------------------------------------------------------------------- #
# 服务级聚合
# --------------------------------------------------------------------------- #
def _service_entry(kind, name, running, pids, samples) -> dict:
    rss = 0
    cpu = 0.0
    known = 0
    cpu_unknown = False
    for pid in pids:
        s = samples.get(pid)
        if s is None:
            continue
        rss += s.rss_bytes
        if s.cpu_percent < 0:
            cpu_unknown = True
        else:
            cpu += s.cpu_percent
            known += 1
    return {
        "kind": kind,
        "name": name,
        "running": bool(running),
        "pids": [int(p) for p in pids],
        "rss_bytes": rss,
        "cpu_percent": (round(cpu, 1) if known or not cpu_unknown
                        else -1.0),
    }


def collect_service_metrics(config, php_mgr, nginx_mgr, redis_mgr=None,
                             mysql_mgr=None) -> dict:
    """聚合 phpvm 管理进程的资源画像，返回可 JSON 序列化的字典。

    任一 manager 缺失（模块停用）时跳过对应分组。进程快照全局只取一次。
    """
    services: list[dict] = []
    all_pids: list[int] = []

    def safe(fn, default=None):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return default

    # Nginx
    ng_run, ng_pids = safe(lambda: nginx_mgr.get_status(), (False, [])) or (False, [])
    all_pids.extend(ng_pids)
    services.append(_service_entry("nginx", "Nginx", ng_run, ng_pids, {}))

    # PHP（按版本）
    if php_mgr is not None:
        versions = safe(lambda: php_mgr.resolve(refresh_status=True, fast=True), []) or []
        for v in versions:
            pids = [v.pid] if getattr(v, "running", False) and getattr(v, "pid", None) else []
            all_pids.extend(pids)
            services.append(_service_entry(
                "php", getattr(v, "name", "?"), getattr(v, "running", False), pids, {}))

    # Redis
    if redis_mgr is not None:
        safe(redis_mgr.refresh_instances)
        safe(redis_mgr.get_status_all)
        for inst in getattr(redis_mgr, "instances", []) or []:
            pids = getattr(inst, "pids", []) or []
            all_pids.extend(pids)
            services.append(_service_entry(
                "redis", getattr(inst, "name", "?"),
                getattr(inst, "running", False), pids, {}))

    # MySQL
    if mysql_mgr is not None:
        safe(mysql_mgr.refresh_instances)
        for inst in getattr(mysql_mgr, "instances", []) or []:
            pids = getattr(inst, "pids", []) or []
            all_pids.extend(pids)
            services.append(_service_entry(
                "mysql", getattr(inst, "name", "?"),
                getattr(inst, "running", False), pids, {}))

    # 一次性采样全部 PID，回填到各服务
    samples = snapshot_pids(list(dict.fromkeys(all_pids)))
    # 重新计算带采样数据的服务条目
    out_services: list[dict] = []
    total_rss = 0
    total_cpu = 0.0
    cpu_known = False
    for svc in services:
        entry = _service_entry(svc["kind"], svc["name"], svc["running"],
                               svc["pids"], samples)
        out_services.append(entry)
        if entry["running"]:
            total_rss += entry["rss_bytes"]
            if entry["cpu_percent"] >= 0:
                total_cpu += entry["cpu_percent"]
                cpu_known = True

    return {
        "updated_at": time.time(),
        "services": out_services,
        "totals": {
            "rss_bytes": total_rss,
            "cpu_percent": round(total_cpu, 1) if cpu_known else -1.0,
            "service_count": sum(1 for s in out_services if s["running"]),
        },
    }
