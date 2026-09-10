# -*- coding: utf-8 -*-
"""跨平台系统命令与进程封装（Windows / macOS·Linux 双实现，API 一致）。

所有命令通过 subprocess 执行并附加 CREATE_NO_WINDOW（Windows）避免弹出黑窗口；
编码处理：优先 utf-8，失败回退 gbk，确保中文路径与输出不乱码。

- Windows：优先用 ctypes 直接调系统 API（GetExtendedTcpTable / EnumProcesses）
  实现高速端口/进程查询，tasklist/netstat 等仅作回退。
- macOS / Linux：使用系统自带 lsof / ps 做同样的一次性快照 + TTL 缓存，
  对外函数签名与语义保持一致，上层（php/nginx/redis manager）无需区分平台。
"""
import os
import re
import signal
import subprocess
import sys
import threading
import time

from .config import IS_WIN, WNRP_ROOT

# --------------------------------------------------------------------------- #
# 平台探测
# --------------------------------------------------------------------------- #
_iphlpapi = None  # Windows：iphlpapi.dll（GetExtendedTcpTable）
_kernel32 = None  # Windows：kernel32.dll
if IS_WIN:
    import ctypes

    _iphlpapi = ctypes.windll.iphlpapi
    _kernel32 = ctypes.windll.kernel32

# --------------------------------------------------------------------------- #
# 高效端口/进程查询（Windows 用 ctypes 直接调 API；posix 用 lsof / ps 快照）
# --------------------------------------------------------------------------- #
# MIB_TCP_STATE 枚举（仅 LISTENING 需要）
_MIB_TCP_STATE_LISTEN = 2
_TCP_TABLE_OWNER_PID_ALL = 5

# TCP 端口 -> PID 全量快照缓存（一次查询，本地匹配所有端口）
_tcp_snapshot: tuple[float, dict[int, list[int]]] | None = None
_tcp_snapshot_lock = threading.Lock()
_TCP_SNAPSHOT_TTL = 2.0  # 秒

# 全局存活 PID 集合缓存（一次全量查询，本地匹配）
_alive_cache: tuple[float, set[int]] | None = None
_alive_cache_lock = threading.Lock()
_ALIVE_CACHE_TTL = 3.0


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
        kw: dict = {"capture_output": True, "timeout": timeout}
        if IS_WIN:
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        p = subprocess.run(args, **kw)
        return p.returncode, _decode(p.stdout), _decode(p.stderr)
    except subprocess.TimeoutExpired:
        return -1, "", "命令执行超时"
    except OSError as e:
        return -1, "", f"无法执行 {args[0]}：{e}"
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def run_cmd_stdin(args: list[str], stdin_text: str = "", timeout: int = 15) -> tuple[int, str, str]:
    """执行命令并写入 stdin，返回 (returncode, stdout, stderr)。

    适用于需要把任意一行（含引号/空格）原样交给子进程交互程序
    （如 redis-cli 的 stdin 逐行执行模式），避免手工拆分 argv。
    """
    try:
        kw: dict = {
            "input": stdin_text.encode("utf-8", errors="replace"),
            "capture_output": True,
            "timeout": timeout,
        }
        if IS_WIN:
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        p = subprocess.run(args, **kw)
        return p.returncode, _decode(p.stdout), _decode(p.stderr)
    except subprocess.TimeoutExpired:
        return -1, "", "命令执行超时"
    except OSError as e:
        return -1, "", f"无法执行 {args[0]}：{e}"
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


# --------------------------------------------------------------------------- #
# TCP 端口全量快照
# --------------------------------------------------------------------------- #
def _get_tcp_table() -> list[tuple[int, int, int]] | None:
    """Windows：返回 [(local_addr, local_port, pid), ...]，仅 TCP 监听/已建立连接。

    使用 GetExtendedTcpTable（IP Helper API），比 netstat 快几个数量级，
    且不创建任何外部进程。查询失败返回 None，空列表表示查询成功但无监听端口。
    """
    if _iphlpapi is None:
        return None
    import ctypes

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
    #   DWORD dwNumEntries;  MIB_TCPROW_OWNER_PID row[dwNumEntries];
    # MIB_TCPROW_OWNER_PID：DWORD dwState, dwLocalAddr, dwLocalPort, dwRemoteAddr,
    #                       dwRemotePort, dwOwningPid;  端口以网络字节序存储。
    num = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong))[0]
    row_size = 6 * 4
    rows: list[tuple[int, int, int]] = []
    base = ctypes.addressof(buf)
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


def _lsof_listen_snapshot() -> dict[int, list[int]] | None:
    """macOS/Linux：一次 lsof 返回全量 TCP 监听 {port: [pid, ...]}。

    使用 `lsof -nP -iTCP -sTCP:LISTEN -F pn`：
      -F pn 输出中，'p' 开头为 pid，'n' 开头为地址（如 127.0.0.1:9000 / *:9000）。
    查询失败返回 None；空 dict 表示查询成功但当前无监听端口。
    """
    code, out, _ = run_cmd(
        ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pn"], timeout=15
    )
    if code != 0:
        return None
    snapshot: dict[int, list[int]] = {}
    cur_pid: int | None = None
    for raw in out.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line[0] == "p":
            try:
                cur_pid = int(line[1:])
            except ValueError:
                cur_pid = None
        elif line[0] == "n" and cur_pid is not None:
            m = re.search(r":(\d+)\s*$", line[1:])
            if not m:
                m = re.search(r":(\d+)(?:\s|$)", line[1:])
            if m:
                port = int(m.group(1))
                if 0 < port <= 65535:
                    lst = snapshot.setdefault(port, [])
                    if cur_pid not in lst:
                        lst.append(cur_pid)
    return snapshot


def get_tcp_snapshot(force: bool = False) -> dict[int, list[int]] | None:
    """一次底层查询返回全量 {port: [pid, ...]}（仅 TCP 监听）。

    带 TTL 缓存：2s 内的重复调用直接命中缓存，避免反复查询全系统端口表。
    force=True 时跳过缓存强制重建（启停进程后必须强制，否则会命中旧快照）。
    返回 None 表示底层查询失败（调用方应回退逐端口慢速路径）；
    返回空 dict 表示查询成功但当前无任何监听端口。
    """
    global _tcp_snapshot
    now = time.monotonic()
    with _tcp_snapshot_lock:
        cached = _tcp_snapshot
        if not force and cached and now - cached[0] < _TCP_SNAPSHOT_TTL:
            return cached[1]

    if IS_WIN:
        rows = _get_tcp_table()
        if rows is None:
            return None
        snapshot: dict[int, list[int]] = {}
        for addr, port, pid in rows:
            if port > 0 and pid > 0:
                lst = snapshot.setdefault(port, [])
                if pid not in lst:
                    lst.append(pid)
    else:
        snapshot = _lsof_listen_snapshot()
        if snapshot is None:
            return None
    with _tcp_snapshot_lock:
        _tcp_snapshot = (now, snapshot)
    return snapshot


def port_to_pid_fast(port: int, force: bool = False) -> list[int]:
    """端口 -> PID 列表（带 TTL 缓存）。优先用全量快照，失败时回退逐端口查询。"""
    snap = get_tcp_snapshot(force=force)
    if snap is not None:
        return list(snap.get(port, []))
    return port_to_pid(port)


def port_to_pid(port: int) -> list[int]:
    """返回监听指定端口的 PID 列表（仅 TCP，逐端口慢速查询，作为回退路径）。"""
    if not IS_WIN:
        # 注意：macOS 的 lsof 要求 -i 值与参数连写（-iTCP:9001），拆开会被当成文件名
        code, out, _ = run_cmd(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-F", "p"], timeout=10
        )
        pids: list[int] = []
        if code == 0:
            for line in out.splitlines():
                s = line.strip()
                if s.startswith("p"):
                    try:
                        pid = int(s[1:])
                    except ValueError:
                        continue
                    if pid > 0 and pid not in pids:
                        pids.append(pid)
        return pids

    # Windows：netstat -ano 回退
    code, out, _ = run_cmd(["netstat", "-ano"], timeout=10)
    if code != 0:
        return []
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
            addr = parts[1]
            host, _, port_s = addr.rpartition(":")
            if host in ("127.0.0.1", "0.0.0.0", "[::1]", "::") and int(port_s) == port:
                pid = int(parts[4])
                if pid > 0 and pid not in pids:
                    pids.append(pid)
    return pids


# --------------------------------------------------------------------------- #
# 进程命令行全量快照（posix；Windows 走 tasklist 专用路径）
# --------------------------------------------------------------------------- #
_posix_proc_snap: tuple[float, dict[int, str]] | None = None
_posix_proc_lock = threading.Lock()
_POSIX_PROC_TTL = 2.5  # 秒


def get_process_cmd_snapshot(force: bool = False) -> dict[int, str] | None:
    """全进程 {pid: 完整命令行} 快照，带 TTL 缓存。

    posix：一次 `ps -axo pid=,command=` 即得全部进程，各 manager 在本地按
    命令行子串/进程名过滤，避免每服务各跑一次 ps。
    Windows：返回 None（上层继续使用 tasklist 专用路径）。
    """
    if IS_WIN:
        return None
    global _posix_proc_snap
    now = time.monotonic()
    with _posix_proc_lock:
        cached = _posix_proc_snap
        if not force and cached and now - cached[0] < _POSIX_PROC_TTL:
            return cached[1]
    code, out, _ = run_cmd(["ps", "-axo", "pid=,command="], timeout=15)
    if code != 0:
        return None
    snapshot: dict[int, str] = {}
    for raw in out.splitlines():
        sp = raw.find(" ")
        if sp <= 0:
            continue
        try:
            pid = int(raw[:sp].strip())
        except ValueError:
            continue
        cmd = raw[sp:].strip()
        if cmd:
            snapshot[pid] = cmd
    with _posix_proc_lock:
        _posix_proc_snap = (now, snapshot)
    return snapshot


def cmdline_matches_pids(needles, force: bool = False) -> dict[int, str]:
    """返回命令行中含任一 needle（大小写不敏感）的 {pid: 命令行}。

    Windows 返回空 dict（上层沿用 tasklist 等专用路径）。
    """
    snap = get_process_cmd_snapshot(force=force)
    if not snap:
        return {}
    lows = [str(n).lower() for n in needles if str(n).strip()]
    if not lows:
        return {}
    hits: dict[int, str] = {}
    for pid, cmd in snap.items():
        cl = cmd.lower()
        if any(x in cl for x in lows):
            hits[pid] = cmd
    return hits


# --------------------------------------------------------------------------- #
# 进程集合 / 存活判断
# --------------------------------------------------------------------------- #
def _enum_pids() -> set[int]:
    """获取全部 PID。Windows 用 PSAPI EnumProcesses，posix 用一次 ps。"""
    if not IS_WIN:
        code, out, _ = run_cmd(["ps", "-axo", "pid="], timeout=10)
        pids: set[int] = set()
        if code == 0:
            for tok in out.split():
                try:
                    pids.add(int(tok))
                except ValueError:
                    continue
        return pids
    # Windows：PSAPI EnumProcesses（零外部进程，比 tasklist 快得多）
    if _kernel32 is None or not hasattr(__import__("ctypes").windll, "psapi"):
        # 回退：tasklist 全量
        code, out, _ = run_cmd(["tasklist", "/FO", "CSV", "/NH"], timeout=10)
        pids = set()
        if code == 0:
            for line in out.splitlines():
                parts = line.split('","')
                if len(parts) >= 2:
                    try:
                        pids.add(int(parts[1].strip('"')))
                    except ValueError:
                        continue
        return pids
    import ctypes

    psapi = ctypes.windll.psapi
    pid_buf = (ctypes.c_ulong * 4096)()
    cb = ctypes.sizeof(pid_buf)
    needed = ctypes.c_ulong(0)
    if not psapi.EnumProcesses(ctypes.byref(pid_buf), cb, ctypes.byref(needed)):
        return set()
    count = needed.value // ctypes.sizeof(ctypes.c_ulong)
    return {int(pid_buf[i]) for i in range(count) if pid_buf[i]}


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
    """一次全量查询返回当前全部存活 PID 集合（带 TTL 缓存）。

    与 alive_pids() 等价，语义上强调「批量快照」用途：
    一轮刷新中所有版本/服务的进程存活判断只需一次全量查询，本地匹配即可。
    """
    return alive_pids()


def invalidate_process_cache() -> None:
    """清空 TCP / 存活 PID / posix 命令行三层快照缓存。

    启停进程后必须调用（立即反映新进程/已退出进程），否则轮询判定会命中
    操作前的旧快照而误报「未监听/仍在运行」。
    """
    global _tcp_snapshot, _alive_cache, _posix_proc_snap
    with _tcp_snapshot_lock:
        _tcp_snapshot = None
    with _alive_cache_lock:
        _alive_cache = None
    with _posix_proc_lock:
        _posix_proc_snap = None


def is_pid_alive_fast(pid: int) -> bool:
    """判断 PID 是否存活（使用全局存活缓存）。"""
    return pid in alive_pids()


def is_pid_alive(pid: int) -> bool:
    """逐 PID 判断是否存活（不经缓存，用于关键确认场景）。"""
    if IS_WIN:
        code, out, _ = run_cmd(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], timeout=10
        )
        if code != 0:
            return False
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# PID -> 名称 / 路径 / 结束进程
# --------------------------------------------------------------------------- #
def pid_to_name(pid: int) -> str:
    if IS_WIN:
        code, out, _ = run_cmd(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], timeout=10
        )
        if code == 0 and out.strip():
            return out.strip().split(",")[0].strip('"')
        return f"PID {pid}"
    code, out, _ = run_cmd(["ps", "-p", str(pid), "-o", "comm="], timeout=10)
    if code == 0 and out.strip():
        return out.strip().splitlines()[0].strip()
    return f"PID {pid}"


def pid_to_path(pid: int) -> str:
    """获取进程可执行文件/完整命令行（用于校验进程身份，可能为空）。

    Windows 用 PowerShell 拿可执行文件路径；posix 用 `ps -o command=`，
    返回完整命令行（argv[0] 通常为完整路径），供上层按进程名匹配。
    """
    if IS_WIN:
        code, out, _ = run_cmd(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).Path",
            ],
            timeout=10,
        )
    else:
        code, out, _ = run_cmd(["ps", "-p", str(pid), "-o", "command="], timeout=10)
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def kill_pid(pid: int) -> bool:
    """结束进程。Windows 强杀；posix 先 SIGTERM 优雅退出，超时后补 SIGKILL。"""
    if IS_WIN:
        code, _, _ = run_cmd(["taskkill", "/F", "/PID", str(pid)], timeout=10)
        return code == 0
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            break
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def kill_by_port(port: int) -> tuple[bool, list[int]]:
    """按端口结束所有监听进程，返回 (是否全部成功, 杀掉的 PID 列表)。"""
    pids = port_to_pid(port)
    ok = True
    for pid in pids:
        if not kill_pid(pid):
            ok = False
    return ok, pids


RUN_HIDDEN = os.path.join(WNRP_ROOT, "RunHiddenConsole.exe")


def start_hidden(exe: str, args: list[str], workdir: str | None = None) -> tuple[int, str, str]:
    """后台隐藏方式启动进程，返回 (returncode, stdout, stderr)。

    Windows：优先复用 RunHiddenConsole.exe（与各 start_phpXX.bat 行为一致），
    缺失时退化为 CREATE_NO_WINDOW 直接启动。
    macOS/Linux：脱离终端会话（start_new_session），stdin/stdout/stderr 丢弃。
    """
    try:
        if IS_WIN:
            cmd = [exe] + args
            if os.path.exists(RUN_HIDDEN):
                cmd = [RUN_HIDDEN, exe] + args
            subprocess.Popen(
                cmd,
                cwd=workdir,
                creationflags=subprocess.CREATE_NO_WINDOW,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                [exe] + args,
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        return 0, "", ""
    except OSError as e:
        return -1, "", f"启动失败：{e}"
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def open_path(path: str) -> None:
    """用系统默认应用打开文件/文件夹（os.startfile / mac `open`），失败静默。"""
    try:
        if IS_WIN:
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(
                ["open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
    except Exception:  # noqa: BLE001
        pass


def open_terminal(cwd: str = "", path_prepend: str = "",
                  command: str = "php -v") -> bool:
    """新开一个终端窗口；可把目录前置到 PATH、切换工作目录并执行一条命令。

    Windows：cmd /k（先 set PATH，再 cd /d）；macOS：Terminal do script；
    其它 Linux：尝试常见终端模拟器，找不到返回 False。
    """
    cmd_line = command or "php -v"
    try:
        if IS_WIN:
            script = ""
            if path_prepend:
                script += f'set "PATH={path_prepend};%PATH%" && '
            if cwd:
                script += f'cd /d "{cwd}" && '
            script += f"echo phpvm && {cmd_line}"
            subprocess.Popen(
                ["cmd", "/k", script],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            return True
        if sys.platform == "darwin":
            script = ""
            if path_prepend:
                script += f'export PATH="{path_prepend}:$PATH"; '
            if cwd:
                script += f'cd "{cwd}"; '
            script += f"echo phpvm; {cmd_line}"
            subprocess.Popen(
                ["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                 "-e", 'tell application "Terminal" to activate'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        # 其它 Linux：常见终端二选一
        for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
            args = [term]
            if term in ("gnome-terminal", "konsole"):
                args += ["--"] if term == "gnome-terminal" else ["-e"]
            args += ["bash", "-c",
                     (f'export PATH="{path_prepend}:$PATH"; ' if path_prepend else "")
                     + (f'cd "{cwd}"; ' if cwd else "") + "php -v; exec bash"]
            try:
                subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return True
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return False
