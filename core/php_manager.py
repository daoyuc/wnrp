# -*- coding: utf-8 -*-
"""PHP 版本管理：自动发现、版本解析、按端口精确启停/重启、状态判定、ini 关键配置提取。

核心设计：**按端口精确启停**，绝不使用 `taskkill /IM php-cgi.exe` 一刀切，
从而解决现有 start_phpXX.bat 相互误杀的问题。
状态判定三重校验：端口监听存在 + PID 存活 + 进程路径包含 php-cgi。
"""
import glob
import os
import re
import time
from dataclasses import dataclass

from . import process_utils as pu
from .config import WNRP_ROOT, Config

CGI_NAME = "php-cgi.exe"
CLI_NAME = "php.exe"
INI_NAME = "php.ini"
WEB_INI_NAME = "php-web.ini"  # php82/php85 的 Web/FastCGI 配置

# 需要跳过的目录
SKIP_DIRS = {"phpvm", "phpcbf"}

# 关键配置项（按展示顺序）
KEY_INI_ITEMS = [
    "memory_limit",
    "post_max_size",
    "upload_max_filesize",
    "max_file_uploads",
    "max_execution_time",
    "max_input_time",
    "extension_dir",
    "date.timezone",
    "display_errors",
    "error_reporting",
    "default_charset",
    "opcache.enable",
]


class PortConflictError(RuntimeError):
    """端口被其它进程占用。"""


@dataclass
class PhpVersion:
    name: str            # 目录名：php / php56 / php74 / php82 ...
    display: str         # PHP 版本号，如 8.2.4
    dir: str             # 绝对目录路径
    cgi: str             # php-cgi.exe 完整路径
    ini: str             # FastCGI 配置文件路径（php82 为 php-web.ini）
    port: int            # 当前配置端口
    running: bool = False
    pid: int | None = None


class PhpManager:
    def __init__(self, config: Config):
        self.config = config
        self.versions: list[PhpVersion] = []

    # ------------------------------------------------------------------ #
    # 扫描与解析
    # ------------------------------------------------------------------ #
    def scan_versions(self) -> list[PhpVersion]:
        """扫描 C:\\wnrp\\php* 目录，自动发现版本。"""
        self.versions = []
        for d in sorted(glob.glob(os.path.join(WNRP_ROOT, "php*"))):
            base = os.path.basename(d)
            if base in SKIP_DIRS or not base.startswith("php"):
                continue
            cgi = os.path.join(d, CGI_NAME)
            if not os.path.exists(cgi):
                continue
            # php82/php85 使用 php-web.ini，其余使用 php.ini；缺失时回退
            ini = os.path.join(d, WEB_INI_NAME) if base in ("php82", "php85") else os.path.join(d, INI_NAME)
            if not os.path.exists(ini):
                ini = os.path.join(d, INI_NAME)
            self.versions.append(
                PhpVersion(
                    name=base,
                    display="",
                    dir=d,
                    cgi=cgi,
                    ini=ini,
                    port=self.config.get_port(base),
                )
            )
        # php(5.x) 保持最前，其余按名称排序
        self.versions.sort(key=lambda v: (v.name != "php", v.name))
        return self.versions

    def resolve(self, refresh_status: bool = True, fast: bool = True) -> list[PhpVersion]:
        """解析各版本号并（可选）刷新运行状态。耗时操作，建议后台线程调用。

        fast=True 时状态判定仅用「端口监听 + PID 存活」（适合定时刷新）；
        完整操作后校验用 fast=False（额外校验进程路径含 php-cgi）。
        """
        for v in self.versions:
            v.display = self.parse_version(v)
            if refresh_status:
                v.running, v.pid = self.get_status(v, fast=fast)
        return self.versions

    def parse_version(self, v: PhpVersion) -> str:
        """从 php -v 首行解析版本号，如 8.2.4。"""
        exe = os.path.join(v.dir, CLI_NAME)
        if not os.path.exists(exe):
            exe = v.cgi
        code, out, err = pu.run_cmd([exe, "-v"], timeout=10)
        text = out or err
        m = re.search(r"PHP\s+([0-9]+\.[0-9]+\.[0-9]+)", text)
        return m.group(1) if m else "未知"

    # ------------------------------------------------------------------ #
    # 状态判定（三重校验）
    # ------------------------------------------------------------------ #
    def get_status(self, v: PhpVersion, fast: bool = False) -> tuple[bool, int | None]:
        """(是否运行, PID)。端口监听 + PID 存活 + 路径含 php-cgi（fast 时跳过路径校验）。"""
        pids = pu.port_to_pid_fast(v.port)
        if not pids:
            return False, None
        for pid in pids:
            if not pu.is_pid_alive_fast(pid):
                continue
            if fast:
                return True, pid
            path = pu.pid_to_path(pid) or ""
            if "php-cgi" in path.lower():
                return True, pid
            if not path:
                return True, pid
        return False, None

    # ------------------------------------------------------------------ #
    # 启停 / 重启
    # ------------------------------------------------------------------ #
    def start(self, v: PhpVersion) -> str:
        running, pid = self.get_status(v)
        if running:
            return f"[{v.name}] 已在运行（PID {pid}，端口 {v.port}）"

        # 端口被其它进程占用则提示，不强行杀
        pids = pu.port_to_pid(v.port)
        if pids:
            names = ", ".join(f"{pu.pid_to_name(p)}({p})" for p in pids[:3])
            raise PortConflictError(
                f"[{v.name}] 端口 {v.port} 已被占用：{names}\n"
                f"请先停止占用进程，或在界面中修改 {v.name} 的端口，"
                f"并同步修改 nginx vhost 的 fastcgi_pass。"
            )

        pu.start_hidden(v.cgi, ["-b", f"127.0.0.1:{v.port}", "-c", v.ini], workdir=v.dir)
        time.sleep(0.8)
        running, pid = self.get_status(v)
        if running:
            return f"[{v.name}] 启动成功（PID {pid}，端口 {v.port}）"
        raise RuntimeError(
            f"[{v.name}] 启动失败：端口 {v.port} 未能监听，请查看 php.ini 配置或端口是否被占用。"
        )

    def stop(self, v: PhpVersion) -> str:
        pids = pu.port_to_pid(v.port)
        if not pids:
            return f"[{v.name}] 未在运行（端口 {v.port} 无监听）"
        killed = []
        for pid in pids:
            if pu.kill_pid(pid):
                killed.append(pid)
        time.sleep(0.3)
        running, _ = self.get_status(v)
        if not running:
            return f"[{v.name}] 已停止（结束 PID {', '.join(map(str, killed))}）"
        raise RuntimeError(f"[{v.name}] 停止失败，请手动结束相关进程")

    def restart(self, v: PhpVersion) -> str:
        self.stop(v)
        return self.start(v)

    # ------------------------------------------------------------------ #
    # ini 配置读取
    # ------------------------------------------------------------------ #
    def read_ini(self, v: PhpVersion) -> str:
        """返回 ini 完整内容。"""
        try:
            with open(v.ini, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError as e:
            return f"读取失败：{e}"

    def read_key_ini(self, v: PhpVersion) -> dict:
        """提取关键配置项 + 已启用扩展列表。"""
        result: dict = {}
        enabled_ext: list[str] = []
        try:
            with open(v.ini, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            return {"__error__": str(e)}

        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(";"):
                continue
            m = re.match(r"^extension\s*=\s*(\S+)", line, re.IGNORECASE)
            if m:
                enabled_ext.append(m.group(1))
                continue
            for key in KEY_INI_ITEMS:
                if line.lower().startswith(key.lower() + "=") or line.lower().startswith(key.lower() + " ="):
                    result[key] = line.split("=", 1)[1].strip()
                    break

        result["__extensions__"] = enabled_ext
        return result
