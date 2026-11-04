# -*- coding: utf-8 -*-
"""MySQL 实例管理：发现 / 状态 / 启动 / 停止 / 重启 / 日志。

约定：
- 实例 = 含 `bin/mysqld(.exe)`（或根目录直接放 mysqld）的目录，来源按优先级：
  1. 环境根下的 mysql* 目录（如 C:\\wnrp\\mysql，phpvm 自管理）；
  2. `settings.mysql_dirs` 显式配置的目录（用户手工指定，优先级最高）；
  3. Windows 已注册服务反推的安装目录（bin\\mysqld.exe 的上一级）；
  4. PATH 中 mysqld 所在目录的上一级；
  5. 常见安装位置（C/D/E…:\\mysql、Program Files\\MySQL\\*、phpstudy / xampp）；
- 端口与数据目录从实例配置文件（my.ini / my.cnf）解析，默认 3306；
- **Windows 服务优先**：若该实例已注册为 Windows 服务（默认名 MySQL），
  控制走 `net start/stop <服务名>`，绝不再拉起第二个 mysqld（避免抢 datadir）；
  未注册服务时退回进程模式（隐藏启动 mysqld，关闭用 mysqladmin / 按 PID 终止）；
- 启停 Windows 服务需要管理员权限：非管理员时返回明确提示，不静默失败。
"""
import ctypes
import glob
import os
import re
import threading
import time
from dataclasses import dataclass, field

from . import process_utils as pu
from .config import IS_WIN, WNRP_ROOT
from .i18n import t

SERVER_EXE = "mysqld.exe" if IS_WIN else "mysqld"
CLI_EXE = "mysql.exe" if IS_WIN else "mysql"
ADMIN_EXE = "mysqladmin.exe" if IS_WIN else "mysqladmin"
CONF_NAMES = ("my.ini", "my.cnf", "my-default.ini")
DEFAULT_PORT = 3306
# 常见 Windows 服务名（按顺序探测，按二进制路径归属到实例）
_SERVICE_CANDIDATES = ("MySQL", "MySQL80", "MySQL57", "MySQL56", "mysql")
# config.json settings 中用户显式指定的实例目录（列表，支持环境变量）
MYSQL_DIRS_SETTING = "mysql_dirs"

_RE_PORT = re.compile(r"^\s*port\s*=\s*(\d+)", re.M | re.I)
_RE_DATADIR = re.compile(r"^\s*datadir\s*=\s*[\"']?([^\"'\r\n]+)[\"']?", re.M | re.I)
_RE_VERSION = re.compile(r"Ver\s+([\d.]+)")


def is_admin() -> bool:
    """当前进程是否具备管理员/root 权限（服务启停需要）。"""
    if IS_WIN:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:  # noqa: BLE001
            return False
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


@dataclass
class MysqlInstance:
    """单个 MySQL 实例（安装目录）。"""

    name: str          # 目录名：mysql
    dir: str
    server: str        # mysqld 可执行文件路径
    cli: str           # mysql 客户端（可能为空）
    admin: str         # mysqladmin（可能为空）
    conf: str          # 配置文件路径
    port: int = DEFAULT_PORT
    datadir: str = ""
    version: str = ""
    running: bool = False
    pids: list = field(default_factory=list)
    service: str = ""        # Windows 服务名；空 = 未注册服务
    service_state: str = ""  # RUNNING / STOPPED / ...
    start_type: str = ""     # AUTO_START / DEMAND_START / ...

    @property
    def mode(self) -> str:
        return "service" if self.service else "process"

    def __post_init__(self):
        self.pids = list(self.pids or [])


class MysqlManager:
    """MySQL 实例发现与启停控制。"""

    def __init__(self, root: str = WNRP_ROOT, discover: bool = True):
        self.root = root
        # discover=False 只扫环境根（测试 / 纯自管理场景）
        self.discover = discover
        self.instances: list[MysqlInstance] = []
        self.refresh_instances()

    # ------------------------------------------------------------------ #
    # 发现
    # ------------------------------------------------------------------ #
    def _instance_dirs(self) -> list[str]:
        """候选实例目录：环境根 mysql* 优先，再并入自动发现的外部安装。"""
        cands = sorted(glob.glob(os.path.join(self.root, "mysql*")))
        if self.discover:
            cands.extend(_discovered_dirs())
        return list(_iter_instance_dirs(cands))

    def refresh_instances(self) -> list[MysqlInstance]:
        found: list[MysqlInstance] = []
        used: set[str] = set()
        for d in self._instance_dirs():
            server = _first_existing(d, SERVER_EXE)
            if not server:
                continue
            conf = _first_existing(d, *CONF_NAMES) or ""
            name = _unique_name(os.path.basename(d.rstrip("\\/")), d, used)
            used.add(name)
            inst = MysqlInstance(
                name=name,
                dir=d,
                server=server,
                cli=_first_existing(d, CLI_EXE) or "",
                admin=_first_existing(d, ADMIN_EXE) or "",
                conf=conf,
            )
            if conf:
                try:
                    with open(conf, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    text = ""
                m = _RE_PORT.search(text)
                if m:
                    inst.port = int(m.group(1))
                m = _RE_DATADIR.search(text)
                if m:
                    inst.datadir = m.group(1).strip().rstrip("\\/")
            found.append(inst)
        self.instances = found
        for inst in self.instances:
            self._attach_service(inst)
            self.get_status(inst)
        return self.instances

    # Windows 服务归属：按服务二进制路径 / defaults-file 是否指向该实例
    def _attach_service(self, inst: MysqlInstance) -> None:
        if not IS_WIN:
            return
        for name, cfg in _mysql_service_configs():
            blob = (cfg.get("bin") or "").lower()
            marker = inst.dir.lower()
            # 用完整配置文件路径比较（只比文件名会把不同安装的 my.ini 混为一谈）
            conf_marker = inst.conf.lower() if inst.conf else ""
            if marker in blob or (conf_marker and conf_marker in blob):
                inst.service = name
                inst.start_type = cfg.get("start_type") or ""
                st = _svc_query(name)
                inst.service_state = (st or {}).get("state", "")
                return

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def get_status(self, inst: MysqlInstance) -> tuple[bool, list[int]]:
        """返回 (运行中, PID 列表)。

        以端口监听 ∩ mysqld 进程为准；服务模式下同时刷新服务状态。
        """
        pids: list[int] = []
        # port_to_pid 返回 PID 列表（同端口可能有多个监听者）
        port_pids = pu.port_to_pid_fast(inst.port) or pu.port_to_pid(inst.port) or []
        # 端口被非 MySQL 程序占用时也列出（便于排查冲突）；
        # 此处不查进程名：每次刷新都查会让非系统进程多付一次子进程开销
        for pid in port_pids:
            if pid not in pids:
                pids.append(pid)
        if not pids:
            # 进程模式兜底：按进程名查找（可能未监听默认端口）
            try:
                for pid, pname in _mysqld_pids():
                    if pid not in pids:
                        pids.append(pid)
            except Exception:  # noqa: BLE001
                pass
        inst.pids = pids
        inst.running = bool(pids)
        if inst.service:
            st = _svc_query(inst.service)
            inst.service_state = (st or {}).get("state", "")
            if inst.service_state.upper().startswith("RUNNING"):
                inst.running = True
        if not inst.version:
            inst.version = self.detect_version(inst)
        return inst.running, pids

    def detect_version(self, inst: MysqlInstance) -> str:
        """运行 `mysqld --version`（按二进制 mtime 缓存，避免每次刷新都起进程）。"""
        key = os.path.normcase(os.path.abspath(inst.server))
        try:
            mtime = os.path.getmtime(inst.server)
        except OSError:
            mtime = 0.0
        with _version_lock:
            cached = _version_cache.get(key)
            if cached and cached[0] == mtime:
                return cached[1]
        code, out, err = pu.run_cmd([inst.server, "--version"], timeout=10)
        m = _RE_VERSION.search(out or err or "")
        ver = m.group(1) if m else ""
        with _version_lock:
            _version_cache[key] = (mtime, ver)
        return ver

    # ------------------------------------------------------------------ #
    # 启停
    # ------------------------------------------------------------------ #
    def start(self, inst: MysqlInstance) -> str:
        running, _ = self.get_status(inst)
        if running:
            return t("MySQL 已在运行（端口 {port}）", port=inst.port)
        if inst.mode == "service":
            if not is_admin():
                return t("启停 Windows 服务需要管理员权限。\n"
                         "请以「管理员身份运行」phpvm 后再启动 {svc} 服务，"
                         "或在系统服务中手动启动。", svc=inst.service)
            code, out, err = pu.run_cmd(["net", "start", inst.service], timeout=60)
            text = (out or err or "").strip()
            _svc_cache_clear()  # 服务状态已变，丢弃缓存立即复查
            self.get_status(inst)
            if code == 0 or inst.running:
                return t("已启动服务 {svc}：{msg}", svc=inst.service, msg=text or t("完成"))
            return t("启动服务 {svc} 失败：{msg}", svc=inst.service, msg=text)
        # 进程模式
        args = ["--defaults-file=" + inst.conf] if inst.conf else []
        try:
            pu.start_hidden(inst.server, args, workdir=inst.dir)
        except Exception as e:  # noqa: BLE001
            return t("启动 mysqld 失败：{err}", err=e)
        for _ in range(20):  # 最多等约 8 秒
            time.sleep(0.4)
            try:
                pu.invalidate_process_cache()
            except Exception:  # noqa: BLE001
                pass
            running, _ = self.get_status(inst)
            if running:
                return t("MySQL 已启动（端口 {port}）", port=inst.port)
        return t("MySQL 启动超时，请查看错误日志：{path}", path=self.error_log_path(inst) or "—")

    def stop(self, inst: MysqlInstance) -> str:
        running, pids = self.get_status(inst)
        if not running:
            return t("MySQL 未在运行（端口 {port}）", port=inst.port)
        if inst.mode == "service":
            if not is_admin():
                return t("启停 Windows 服务需要管理员权限。\n"
                         "请以「管理员身份运行」phpvm 后再停止 {svc} 服务。", svc=inst.service)
            code, out, err = pu.run_cmd(["net", "stop", inst.service], timeout=90)
            text = (out or err or "").strip()
            _svc_cache_clear()  # 服务状态已变，丢弃缓存立即复查
            for _ in range(15):  # 等待端口释放
                time.sleep(0.4)
                running, _ = self.get_status(inst)
                if not running:
                    break
            if not running:
                return t("已停止服务 {svc}", svc=inst.service)
            return t("停止服务 {svc} 未见效：{msg}", svc=inst.service, msg=text)
        # 进程模式：先优雅关闭（mysqladmin），失败再按 PID 终止
        if inst.admin:
            code, out, err = pu.run_cmd(
                [inst.admin, "-P", str(inst.port), "shutdown"], timeout=20)
            for _ in range(15):
                time.sleep(0.4)
                running, _ = self.get_status(inst)
                if not running:
                    return t("MySQL 已正常关闭")
        for pid in pids:
            pu.kill_pid(pid)
        time.sleep(0.6)
        running, _ = self.get_status(inst)
        return t("MySQL 已停止") if not running else t("MySQL 仍未停止，请手动结束进程")

    def restart(self, inst: MysqlInstance) -> str:
        msg = self.stop(inst)
        if t("MySQL 未在运行") in msg or t("MySQL 已停止") in msg or t("已停止服务") in msg \
                or t("MySQL 已正常关闭") in msg:
            return msg + "\n" + self.start(inst)
        return t("重启中止（停止未成功）：{msg}", msg=msg)

    # ------------------------------------------------------------------ #
    # 日志 / 路径
    # ------------------------------------------------------------------ #
    def error_log_path(self, inst: MysqlInstance) -> str:
        """错误日志（.err）路径：数据目录下最近修改的一个。"""
        base = inst.datadir or os.path.join(inst.dir, "data")
        if not os.path.isdir(base):
            return ""
        try:
            errs = [os.path.join(base, f) for f in os.listdir(base) if f.endswith(".err")]
        except OSError:
            return ""
        if not errs:
            return ""
        return max(errs, key=os.path.getmtime)

    def tail_error_log(self, inst: MysqlInstance, max_lines: int = 200) -> str:
        path = self.error_log_path(inst)
        if not path:
            return t("未找到错误日志（数据目录：{dir}）", dir=inst.datadir or "—")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            return t("读取错误日志失败：{err}", err=e)
        tail = lines[-max_lines:] if len(lines) > max_lines else lines
        return "".join(tail).rstrip() or t("（日志为空）")

    def default_instance(self) -> MysqlInstance | None:
        return self.instances[0] if self.instances else None


# --------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------- #
def _first_existing(d: str, *names: str) -> str:
    """在目录根与 bin/ 子目录中查找首个存在的可执行文件/配置。"""
    for name in names:
        for sub in ("", "bin", "sbin"):
            p = os.path.join(d, sub, name) if sub else os.path.join(d, name)
            if os.path.exists(p):
                return p
    return ""


# --------------------------------------------------------------------- #
# 环境根之外的实例发现（第三方安装位置）
# --------------------------------------------------------------------- #
_discover_cache: tuple[float, list[str]] | None = None
_discover_lock = threading.Lock()
_DISCOVER_TTL = 15.0  # 秒；用户新装 MySQL 后最多这么久被自动发现

# mysqld --version 缓存：{可执行文件小写路径: (mtime, 版本)}，mtime 未变即复用
_version_cache: dict[str, tuple[float, str]] = {}
_version_lock = threading.Lock()

_service_cache: tuple[float, list[tuple[str, dict]]] | None = None
_service_lock = threading.Lock()
_SERVICE_TTL = 30.0


def _discovered_dirs() -> list[str]:
    """环境根之外的候选目录（显式配置 / 服务 / PATH / 常见位置），带缓存。

    枚举 Windows 服务约 100ms、扫盘符有磁盘开销，而 GUI 面板与 CLI 会反复
    构造 MysqlManager（每次全量探测太贵），故缓存结果。
    """
    global _discover_cache
    now = time.monotonic()
    with _discover_lock:
        cached = _discover_cache
        if cached and now - cached[0] < _DISCOVER_TTL:
            return list(cached[1])
    dirs = (_configured_dirs() + _service_install_dirs()
            + _path_mysqld_dirs() + _common_install_dirs())
    with _discover_lock:
        _discover_cache = (now, dirs)
    return list(dirs)


def _configured_dirs() -> list[str]:
    """`settings.mysql_dirs`：用户显式指定的实例目录（列表或单个字符串）。"""
    try:
        from .config import Config

        raw = Config().settings.get(MYSQL_DIRS_SETTING) or []
    except Exception:  # noqa: BLE001 - 配置不可读不应阻断发现
        return []
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        p = os.path.expandvars(os.path.expanduser(item.strip().strip('"').strip("'")))
        if os.path.isdir(p):
            out.append(p)
    return out


def _service_install_dirs() -> list[str]:
    """从已注册的 MySQL 服务反推安装目录（...\\bin\\mysqld.exe → ...）。"""
    out: list[str] = []
    for _name, cfg in _mysql_service_configs():
        exe = _bin_exe_path(cfg.get("bin") or "")
        if not exe:
            continue
        bin_dir = os.path.dirname(exe)
        parent = os.path.dirname(bin_dir)
        if os.path.basename(bin_dir).lower() in ("bin", "sbin") and os.path.isdir(parent):
            out.append(parent)
        elif os.path.isdir(bin_dir):
            out.append(bin_dir)
    return out


def _bin_exe_path(raw: str) -> str:
    """从 `sc qc` 的 BINARY_PATH_NAME 取可执行文件路径（去掉引号与启动参数）。"""
    m = re.search(r'"([^"]+?\.exe)"', raw, re.I) or \
        re.search(r"([A-Za-z]:\\[^\s]+?\.exe)", raw, re.I)
    return m.group(1) if m else ""


def _path_mysqld_dirs() -> list[str]:
    """PATH 中 mysqld 所在目录的上一级（用户把 ...\\mysql\\bin 加进了 PATH）。"""
    out: list[str] = []
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isfile(os.path.join(entry, SERVER_EXE)):
            continue
        base = entry.rstrip("\\/")
        parent = os.path.dirname(base)
        out.append(parent if os.path.basename(base).lower() in ("bin", "sbin") else base)
    return out


def _windows_fixed_drives() -> list[str]:
    """本机固定盘符（跳过光驱 / 断连的网络盘，避免 isdir 长时间阻塞）。"""
    if not IS_WIN:
        return []
    fallback = [f"{c}:\\" for c in "CDEF"]
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
    except Exception:  # noqa: BLE001
        return fallback
    drives: list[str] = []
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        try:
            if k32.GetDriveTypeW(root) != 3:  # DRIVE_FIXED
                continue
        except Exception:  # noqa: BLE001
            pass
        drives.append(root)
    return drives or fallback


def _common_install_dirs() -> list[str]:
    """常见安装位置（只在目录存在时返回，不做全盘搜索）。"""
    cands: list[str] = []
    if IS_WIN:
        for root in _windows_fixed_drives():
            cands.append(os.path.join(root, "mysql"))
            cands.append(os.path.join(root, "phpstudy_pro", "Extensions"))
            cands.append(os.path.join(root, "xampp", "mysql"))
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base:
                cands.append(os.path.join(base, "MySQL"))
    else:
        cands += ["/usr/local/mysql", "/opt/homebrew/opt/mysql",
                  "/usr/local/opt/mysql", "/usr/mysql"]
    return [c for c in cands if os.path.isdir(c)]


def _mysql_service_names() -> list[str]:
    """服务名含 mysql / mariadb 的 Windows 服务（固定候选优先，再全量枚举）。"""
    if not IS_WIN:
        return []
    code, out, _ = pu.run_cmd(
        ["sc", "query", "type=", "service", "state=", "all"], timeout=10)
    present = set(re.findall(r"^SERVICE_NAME:\s*(\S+)\s*$", out or "", re.M))
    if not present:
        return list(_SERVICE_CANDIDATES)  # 枚举失败：退回固定候选逐个试
    names = [n for n in _SERVICE_CANDIDATES if n in present]
    for n in present:
        low = n.lower()
        if ("mysql" in low or "mariadb" in low) and n not in names:
            names.append(n)
    return names


def _mysql_service_configs() -> list[tuple[str, dict]]:
    """[(服务名, `sc qc` 结果)]，带 TTL 缓存（服务 bin 路径基本不变）。"""
    global _service_cache
    now = time.monotonic()
    with _service_lock:
        cached = _service_cache
        if cached and now - cached[0] < _SERVICE_TTL:
            return list(cached[1])
    items: list[tuple[str, dict]] = []
    for name in _mysql_service_names():
        cfg = _svc_config(name)
        if cfg:
            items.append((name, cfg))
    with _service_lock:
        _service_cache = (now, items)
    return list(items)


def _normalize_instance_dir(p: str) -> str:
    """bin / sbin 目录上溯到安装根（用户配置或 PATH 常直接指向 ...\\mysql\\bin）。"""
    p = p.rstrip("\\/") or p
    if os.path.basename(p).lower() in ("bin", "sbin"):
        parent = os.path.dirname(p)
        if parent and _first_existing(parent, SERVER_EXE):
            return parent
    return p


def _iter_instance_dirs(paths) -> list[str]:
    """把候选路径展开为「真正含 mysqld」的目录并去重。

    每个候选先看自身（实例根），自身不是实例时才展开一层子目录
    （兼容 C:\\Program Files\\MySQL\\MySQL Server 8.0 这类容器目录）。
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(p: str) -> bool:
        p = _normalize_instance_dir(p)
        key = os.path.normcase(os.path.abspath(p))
        if key in seen or not _first_existing(p, SERVER_EXE):
            return False
        seen.add(key)
        out.append(p)
        return True

    for p in paths:
        if not p or not os.path.isdir(p):
            continue
        if add(p):
            continue  # 自身就是实例，不必再展开
        try:
            subs = sorted(glob.glob(os.path.join(p, "*")))
        except OSError:
            subs = []
        for sub in subs:
            if os.path.isdir(sub):
                add(sub)
    return out


def _unique_name(base: str, d: str, used: set[str]) -> str:
    """同名目录（如 C:\\wnrp\\mysql 与 D:\\mysql）用盘符后缀区分，保证下拉可选。"""
    name = base or "mysql"
    if name not in used:
        return name
    drive = os.path.splitdrive(os.path.abspath(d))[0].rstrip(":").upper()
    cand = f"{name}@{drive}" if drive else f"{name}@2"
    i = 2
    while cand in used:
        cand = f"{name}@{drive}-{i}" if drive else f"{name}@{i}"
        i += 1
    return cand


def _mysqld_pids() -> list[tuple[int, str]]:
    """返回 [(pid, 进程名), ...]：进程名含 mysqld 的进程。

    走进程镜像名快照（Windows ctypes 一次枚举，零子进程）；快照不可用时
    才退回逐 PID tasklist 的旧路径 —— 避免一次状态刷新派生上百个子进程。
    """
    try:
        return pu.find_pids_by_image_name("mysqld")
    except Exception:  # noqa: BLE001
        return []


_svc_cache: dict[str, tuple[float, dict | None]] = {}
_svc_cache_lock = threading.Lock()
_SVC_CACHE_TTL = 3.0  # 秒


def _svc_cache_clear() -> None:
    """清空服务状态缓存（启停服务后调用，立即读到新状态）。"""
    global _service_cache, _discover_cache
    with _svc_cache_lock:
        _svc_cache.clear()
    with _service_lock:
        _service_cache = None
    with _discover_lock:
        _discover_cache = None


def _svc_query(name: str, force: bool = False) -> dict | None:
    """`sc query <name>`：返回 {state}；服务不存在返回 None。

    带 3 秒 TTL 缓存：面板轮询与 start/stop 内的多次复查不会反复派生 `sc`；
    启停服务后由调用方 `_svc_cache_clear()` 强制失效。
    """
    now = time.monotonic()
    with _svc_cache_lock:
        cached = _svc_cache.get(name)
        if not force and cached and now - cached[0] < _SVC_CACHE_TTL:
            return cached[1]
    code, out, err = pu.run_cmd(["sc", "query", name], timeout=10)
    text = f"{out}\n{err}"
    if code != 0 or "1060" in text:
        result = None
    else:
        m = re.search(r"STATE\s*:\s*\d+\s+(\w+)", text)
        result = {"state": m.group(1) if m else "", "raw": text}
    with _svc_cache_lock:
        _svc_cache[name] = (now, result)
    return result


def _svc_config(name: str) -> dict | None:
    """`sc qc <name>`：返回 {bin, start_type}；服务不存在返回 None。"""
    code, out, err = pu.run_cmd(["sc", "qc", name], timeout=10)
    text = f"{out}\n{err}"
    if code != 0 or "1060" in text:
        return None
    m_bin = re.search(r"BINARY_PATH_NAME\s*:\s*(.+)", text)
    m_start = re.search(r"START_TYPE\s*:\s*\d+\s+(\w+)", text)
    return {
        "bin": m_bin.group(1).strip() if m_bin else "",
        "start_type": m_start.group(1) if m_start else "",
        "raw": text,
    }
