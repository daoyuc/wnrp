# -*- coding: utf-8 -*-
"""MySQL 实例管理：发现 / 状态 / 启动 / 停止 / 重启 / 日志。

约定：
- 实例 = 环境根下含 `bin/mysqld(.exe)` 的 mysql* 目录（如 C:\\wnrp\\mysql）；
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

    def __init__(self, root: str = WNRP_ROOT):
        self.root = root
        self.instances: list[MysqlInstance] = []
        self.refresh_instances()

    # ------------------------------------------------------------------ #
    # 发现
    # ------------------------------------------------------------------ #
    def refresh_instances(self) -> list[MysqlInstance]:
        found: list[MysqlInstance] = []
        for d in sorted(glob.glob(os.path.join(self.root, "mysql*"))):
            if not os.path.isdir(d):
                continue
            server = _first_existing(d, SERVER_EXE)
            if not server:
                continue
            conf = _first_existing(d, *CONF_NAMES) or ""
            inst = MysqlInstance(
                name=os.path.basename(d.rstrip("\\/")),
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
        for name in _SERVICE_CANDIDATES:
            cfg = _svc_config(name)
            if not cfg:
                continue
            blob = (cfg.get("bin") or "").lower()
            marker = inst.dir.lower()
            conf_marker = os.path.basename(inst.conf).lower() if inst.conf else ""
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
        for pid in port_pids:
            name = (pu.pid_to_name(pid) or "").lower()
            # 端口被非 MySQL 程序占用时也展示出来，便于排查冲突
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
        code, out, err = pu.run_cmd([inst.server, "--version"], timeout=10)
        m = _RE_VERSION.search(out or err or "")
        return m.group(1) if m else ""

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


def _mysqld_pids() -> list[tuple[int, str]]:
    """返回 [(pid, 进程名), ...]：进程名含 mysqld 的进程。"""
    out: list[tuple[int, str]] = []
    try:
        alive = pu.get_process_snapshot()
    except Exception:  # noqa: BLE001
        return out
    for pid in alive:
        name = (pu.pid_to_name(pid) or "").lower()
        if "mysqld" in name:
            out.append((pid, name))
    return out


def _svc_query(name: str) -> dict | None:
    """`sc query <name>`：返回 {state}；服务不存在返回 None。"""
    code, out, err = pu.run_cmd(["sc", "query", name], timeout=10)
    text = f"{out}\n{err}"
    if code != 0 or "1060" in text:
        return None
    m = re.search(r"STATE\s*:\s*\d+\s+(\w+)", text)
    return {"state": m.group(1) if m else "", "raw": text}


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
