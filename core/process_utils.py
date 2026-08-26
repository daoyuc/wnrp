# -*- coding: utf-8 -*-
"""Windows 系统命令封装：netstat / tasklist / taskkill / 隐藏启动。

所有命令通过 subprocess 执行并附加 CREATE_NO_WINDOW，避免弹出黑窗口。
编码处理：优先 utf-8，失败回退 gbk，确保中文路径与输出不乱码。
"""
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes

from .config import WNRP_ROOT

# --------------------------------------------------------------------------- #
# 高效端口/进程查询（ctypes 直接调 Windows API，避免反复拉起 netstat/tasklist）
# --------------------------------------------------------------------------- #
_iphlpapi = ctypes.windll.iphlpapi if sys.platform.startswith("win") else None
_kernel32 = ctypes.windll.kernel32 if sys.platform.startswith("win") else None

# MIB_TCP_STATE 枚举（仅 LISTENING 需要）
_MIB_TCP_STATE_LISTEN = 2

_TCP_TABLE_OWNER_PID_ALL = 5

# TCP 端口 -> PID 全量快照缓存（一次 GetExtendedTcpTable 查询，本地匹配所有端口）
_tcp_snapshot: tuple[float, dict[int, list[int]]] | None = None
_tcp_snapshot_lock = threading.Lock()
_TCP_SNAPSHOT_TTL = 2.0  # 秒


def _get_tcp_table() -> list[tuple[int, int, int]] | None:
    """返回 [(local_addr, local_port, pid), ...]，仅 TCP 监听/已建立连接。

    使用 GetExtendedTcpTable（IP Helper API），比 netstat 快几个数量级，
    且不创建任何外部进程。查询失败（API 不可用/调用出错）返回 None，
    空列表表示查询成功但当前无任何监听端口。
    """
    if _iphlpapi is None:
        return None
    buf_size = ctypes.c_ulong(0)
    # 第一次调用拿所需缓冲区大小
    _iphlpapi.GetExtendedTcpTable(
        None, ctypes.byref(buf_size), False, 2, _TCP_TABLE_OWNER_PID_ALL, 0
    )
    buf = ctypes.create_string_buffer(buf_size.value)
    ret = _iphlpapi.GetExtendedTcpTable(
        buf, ctypes.byref(buf_size), False, 2, _TCP_TABLE_OWNER_PID_ALL, 0
    )
    if ret != 0:
        return None

    # MIB_TCPTABLE_OWNER_PID 布局：
    #   DWORD dwNumEntries;
    #   MIB_TCPROW_OWNER_PID row[dwNumEntries];
    # MIB_TCPROW_OWNER_PID：DWORD dwState, dwLocalAddr, dwLocalPort, dwRemoteAddr,
    #                       dwRemotePort, dwOwningPid;
    # 注意端口以网络字节序存储，需要 ntohs。
    num = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong))[0]
    row_size = 6 * 4
    rows: list[tuple[int, int, int]] = []
    base = ctypes.addressof(buf)
    # 跳过 dwNumEntries（4 字节）
    for i in range(num):
        off = 4 + i * row_size
        state = ctypes.c_ulong.from_address(base + off).value
        local_addr = ctypes.c_ulong.from_address(base + off + 4).value
        local_port_raw = ctypes.c_ulong.from_address(base + off + 8).value
        pid = ctypes.c_ulong.from_address(base + off + 20).value
        # dwLocalPort 以网络字节序（big-endian）存储，需 ntohs
        local_port = ((local_port_raw >> 8) & 0xFF) | ((local_port_raw & 0xFF) << 8)
        rows.append((local_addr, local_port, pid))
    return rows


def get_tcp_snapshot() -> dict[int, list[int]] | None:
    """一次 GetExtendedTcpTable 返回全量 {port: [pid, ...]}（仅 TCP 监听）。

    带 TTL 缓存：2s 内的重复调用直接命中缓存，避免反复查询全系统端口表。
    返回 None 表示底层 API 不可用/查询失败（调用方应回退 netstat 等慢速路径）；
    返回空 dict 表示查询成功但当前无任何监听端口。
    """
    global _tcp_snapshot
    now = time.monotonic()
    with _tcp_snapshot_lock:
        cached = _tcp_snapshot
        if cached and now - cached[0] < _TCP_SNAPSHOT_TTL:
            return cached[1]

    rows = _get_tcp_table()
    if rows is None:
        return None

    snapshot: dict[int, list[int]] = {}
    for addr, port, pid in rows:
        if port > 0 and pid > 0:
            lst = snapshot.setdefault(port, [])
            if pid not in lst:
                lst.append(pid)
    with _tcp_snapshot_lock:
        _tcp_snapshot = (now, snapshot)
    return snapshot


def port_to_pid_fast(port: int) -> list[int]:
    """端口 -> PID 列表（带 TTL 缓存）。优先用 ctypes 全量快照，失败时回退 netstat。"""
    snap = get_tcp_snapshot()
    if snap is not None:
        return list(snap.get(port, []))
    # 回退：旧 netstat 实现（查询失败时按逐端口回退，不污染全量快照）
    return port_to_pid(port)


# 全局存活 PID 集合缓存（一次全量查询，本地匹配）
_alive_cache: tuple[float, set[int]] | None = None
_alive_cache_lock = threading.Lock()
_ALIVE_CACHE_TTL = 3.0


def _enum_pids() -> set[int]:
    """用 PSAPI EnumProcesses 获取全部 PID（零外部进程，比 tasklist 快得多）。"""
    if _kernel32 is None or not hasattr(ctypes.windll, "psapi"):
        # 回退：tasklist 全量
        code, out, _ = run_cmd(["tasklist", "/FO", "CSV", "/NH"], timeout=10)
        pids: set[int] = set()
        if code == 0:
            for line in out.splitlines():
                parts = line.split('","')
                if len(parts) >= 2:
                    try:
                        pids.add(int(parts[1].strip('"')))
                    except ValueError:
                        continue
        return pids
    psapi = ctypes.windll.psapi
    pids = (ctypes.c_ulong * 4096)()
    cb = ctypes.sizeof(pids)
    needed = ctypes.c_ulong(0)
    if not psapi.EnumProcesses(ctypes.byref(pids), cb, ctypes.byref(needed)):
        return set()
    count = needed.value // ctypes.sizeof(ctypes.c_ulong)
    return {int(pids[i]) for i in range(count) if pids[i]}


def alive_pids() -> set[int]:
    """返回当前存活 PID 集合（带 TTL 缓存，一次全量查询）。"""
    global _alive_cache
    now = time.monotonic()
    with _alive_cache_lock:
        cached = _alive_cache
        if cached and now - cached[0] < _ALIVE_CACHE_TTL:
            return cached[1]
    pids = _enum_pids()
    with _alive_cache_lock:
        _alive_cache = (now, pids)
    return pids


def get_process_snapshot() -> set[int]:
    """一次 EnumProcesses 返回当前全部存活 PID 集合（带 TTL 缓存）。

    与 alive_pids() 等价，语义上强调「批量快照」用途：
    一轮刷新中所有版本/服务的进程存活判断只需一次全量查询，本地匹配即可。
    """
    return alive_pids()


def is_pid_alive_fast(pid: int) -> bool:
    """判断 PID 是否存活（使用全局存活缓存）。"""
    return pid in alive_pids()

RUN_HIDDEN = os.path.join(WNRP_ROOT, "RunHiddenConsole.exe")

# netstat -ano 行：协议  本地地址    外部地址  状态      PID
#   TCP    127.0.0.1:9000         0.0.0.0:0              LISTENING       1234
_NETSTAT_RE = re.compile(r"^\s*(\S+)\s+([0-9.]+):(\d+)\s+(\S+)\s+(\S+)\s+(\d+)\s*$")


def _decode(data: bytes) -> str:
    if not data:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def run_cmd(args: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """执行命令，返回 (returncode, stdout, stderr)，均按文本解码。"""
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return p.returncode, _decode(p.stdout), _decode(p.stderr)
    except subprocess.TimeoutExpired:
        return -1, "", "命令执行超时"
    except OSError as e:
        return -1, "", f"无法执行 {args[0]}：{e}"
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def port_to_pid(port: int) -> list[int]:
    """返回监听指定端口的 PID 列表（仅 TCP）。"""
    code, out, _ = run_cmd(["netstat", "-ano"], timeout=10)
    if code != 0:
        return []
    pids: list[int] = []
    for line in out.splitlines():
        m = _NETSTAT_RE.match(line)
        if not m:
            continue
        if m.group(2) == "127.0.0.1" or m.group(2) == "0.0.0.0":
            if int(m.group(3)) == port:
                pid = int(m.group(6))
                if pid > 0 and pid not in pids:
                    pids.append(pid)
    return pids


def is_pid_alive(pid: int) -> bool:
    code, out, _ = run_cmd(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], timeout=10)
    if code != 0:
        return False
    return f'"{pid}"' in out


def pid_to_name(pid: int) -> str:
    code, out, _ = run_cmd(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], timeout=10)
    if code == 0 and out.strip():
        name = out.strip().split(",")[0].strip('"')
        return name
    return f"PID {pid}"


def pid_to_path(pid: int) -> str:
    """通过 PowerShell 获取进程可执行文件路径（可能为空）。"""
    code, out, _ = run_cmd(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-Command",
            f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).Path",
        ],
        timeout=10,
    )
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def kill_pid(pid: int) -> bool:
    code, _, _ = run_cmd(["taskkill", "/F", "/PID", str(pid)], timeout=10)
    return code == 0


def kill_by_port(port: int) -> tuple[bool, list[int]]:
    """按端口结束所有监听进程，返回 (是否全部成功, 杀掉的 PID 列表)。"""
    pids = port_to_pid(port)
    ok = True
    for pid in pids:
        if not kill_pid(pid):
            ok = False
    return ok, pids


def start_hidden(exe: str, args: list[str], workdir: str | None = None) -> tuple[int, str, str]:
    """隐藏方式启动进程，返回 (returncode, stdout, stderr)。

    优先复用现有 C:\\wnrp\\RunHiddenConsole.exe（与各 start_phpXX.bat 行为一致），
    缺失时退化为 CREATE_NO_WINDOW 直接启动。
    """
    try:
        if os.path.exists(RUN_HIDDEN):
            cmd = [RUN_HIDDEN, exe] + args
            subprocess.Popen(
                cmd,
                cwd=workdir,
                creationflags=subprocess.CREATE_NO_WINDOW,
                close_fds=True,
            )
        else:
            cmd = [exe] + args
            subprocess.Popen(
                cmd,
                cwd=workdir,
                creationflags=subprocess.CREATE_NO_WINDOW,
                close_fds=True,
            )
        return 0, "", ""
    except OSError as e:
        return -1, "", f"启动失败：{e}"
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)
