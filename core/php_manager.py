# -*- coding: utf-8 -*-
"""PHP 版本管理：自动发现、版本解析、按端口精确启停/重启、状态判定、ini 关键配置提取。

核心设计：**按端口精确启停**，绝不使用 taskkill/`pkill php-cgi` 一刀切，
从而解决现有 start_phpXX.bat 相互误杀的问题。
状态判定：端口监听存在 + PID 存活 + 进程命令行包含本版本 cgi 可执行名。

跨平台版本目录发现：
- Windows：C:\\wnrp（WNRP_ROOT）下 php* 目录，cgi 位于目录根（php-cgi.exe）；
- macOS / Linux：WNRP_ROOT 下 php* 目录优先；其次自动发现 Homebrew keg
  （/opt/homebrew/opt/php@X.Y …，二进制在 dir/bin 或 dir/sbin）。
  brew 版本默认不带独立 ini 时以 PHP 编译默认配置运行（对应 brew 的
  /opt/homebrew/etc/php/<ver>/php.ini）。
"""
import glob
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from . import process_utils as pu
from .config import Config, IS_WIN, WNRP_ROOT, brew_prefixes

CGI_NAME = "php-cgi.exe" if IS_WIN else "php-cgi"
CLI_NAME = "php.exe" if IS_WIN else "php"
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
    name: str            # 目录名/短名：php / php56 / php74 / php82 ...
    display: str         # PHP 版本号，如 8.2.4
    dir: str             # 版本根目录（绝对路径）
    cgi: str             # php-cgi 可执行文件完整路径
    ini: str             # FastCGI 配置文件路径（"" 表示使用 PHP 编译默认配置）
    port: int            # 当前配置端口
    running: bool = False
    pid: int | None = None


# --------------------------------------------------------------------------- #
# 目录发现辅助
# --------------------------------------------------------------------------- #
def _locate_exe(d: str, name: str) -> str:
    """在版本目录内定位可执行文件：目录根 → bin/ → sbin/。找不到返回 ""。"""
    for rel in ("", "bin", "sbin"):
        p = os.path.join(d, rel, name)
        if os.path.exists(p):
            return p
    return ""


def _brew_name(base: str) -> str:
    """Homebrew 公式名 → 短版本名：php@7.4 → php74、php@8.1 → php81、php → php。"""
    m = re.fullmatch(r"php@(\d+)(?:\.(\d+))?", base)
    if m:
        major, minor = int(m.group(1)), int(m.group(2) or 0)
        return f"php{major}{minor}"
    return base


def _resolve_ini(d: str, name: str, is_brew: bool, brew_conf: str = "") -> str:
    """解析版本实际使用的 FastCGI 配置；无则返回 ""（走 PHP 编译默认配置）。

    Windows 版目录习惯：php82/php85 用 php-web.ini，其余用 php.ini（保持原语义）。
    """
    prefer_web = name in ("php82", "php85")
    cands = [WEB_INI_NAME, INI_NAME] if prefer_web else [INI_NAME]
    for fname in cands:
        p = os.path.join(d, fname)
        if os.path.exists(p):
            return p
    # 任意 *.conf 不适用 PHP；容忍目录中散落的 *.ini（取首个）
    for f in sorted(glob.glob(os.path.join(d, "*.ini"))):
        return f
    if brew_conf and os.path.exists(brew_conf):
        return brew_conf
    return "" if not IS_WIN else os.path.join(d, INI_NAME)


def _brew_etc_ini(d: str) -> str:
    """Homebrew keg 对应的 etc 配置：/opt/homebrew/etc/php/7.4/php.ini。"""
    m = re.fullmatch(r"php@(\d+(?:\.\d+)?)", os.path.basename(d))
    if not m:
        return ""
    for prefix in brew_prefixes():
        if os.path.dirname(d) == os.path.join(prefix, "opt"):
            p = os.path.join(prefix, "etc", "php", m.group(1), INI_NAME)
            if os.path.exists(p):
                return p
    return ""


class PhpManager:
    def __init__(self, config: Config):
        self.config = config
        self.versions: list[PhpVersion] = []

    # ------------------------------------------------------------------ #
    # 扫描与解析
    # ------------------------------------------------------------------ #
    def _candidate_dirs(self) -> list[tuple[str, bool]]:
        """返回 [(版本目录, 是否 brew), ...]，去重（按目录 basename，root 优先）。"""
        found: dict[str, tuple[str, bool]] = {}
        # 1) WNRP_ROOT 下的 php* 目录（Windows 唯一来源；root 布局优先）
        for d in sorted(glob.glob(os.path.join(WNRP_ROOT, "php*"))):
            base = os.path.basename(d)
            if base in SKIP_DIRS or not base.startswith("php") or not os.path.isdir(d):
                continue
            if not _locate_exe(d, CGI_NAME):
                continue
            found.setdefault(base, (d, False))
        # 2) Homebrew keg（仅 posix）
        if not IS_WIN:
            for prefix in brew_prefixes():
                for d in sorted(glob.glob(os.path.join(prefix, "opt", "php*"))):
                    base = os.path.basename(d)
                    if base in SKIP_DIRS or not base.startswith("php"):
                        continue
                    if not os.path.isdir(d) or not _locate_exe(d, CGI_NAME):
                        continue
                    name = _brew_name(base)
                    found.setdefault(name, (d, True))
        return list(found.values())

    def scan_versions(self) -> list[PhpVersion]:
        """扫描全部候选目录，自动发现版本。"""
        self.versions = []
        for d, is_brew in self._candidate_dirs():
            base = os.path.basename(d)
            name = base if not is_brew else _brew_name(base)
            cgi = _locate_exe(d, CGI_NAME)
            if not cgi:
                continue
            ini = _resolve_ini(d, name, is_brew, _brew_etc_ini(d) if is_brew else "")
            self.versions.append(
                PhpVersion(
                    name=name,
                    display="",
                    dir=d,
                    cgi=cgi,
                    ini=ini,
                    port=self.config.get_port(name),
                )
            )
        # 默认版本（php）保持最前，其余按名称排序
        self.versions.sort(key=lambda v: (v.name != "php", v.name))
        self._dedupe_same_binary()
        return self.versions

    def _dedupe_same_binary(self) -> None:
        """同一物理可执行文件只保留一条（brew 的 php@8.2/php@8.3 别名可能
        与主 php keg 指向同一二进制，避免界面出现重复版本行）。"""
        seen: set[str] = set()
        kept: list[PhpVersion] = []
        for v in self.versions:
            try:
                r = os.path.realpath(v.cgi)
            except OSError:
                r = os.path.abspath(v.cgi)
            if r in seen:
                continue
            seen.add(r)
            kept.append(v)
        self.versions = kept

    def resolve(self, refresh_status: bool = True, fast: bool = True) -> list[PhpVersion]:
        """解析各版本号并（可选）刷新运行状态。耗时操作，建议后台线程调用。

        fast=True 时状态判定仅用「端口监听 + PID 存活」（适合定时刷新）；
        完整操作后校验用 fast=False（额外校验进程命令行含本版本 php-cgi）。
        版本号解析带 mtime 缓存 + 并发执行。
        """
        with ThreadPoolExecutor(max_workers=min(6, len(self.versions) or 1)) as ex:
            for v, disp in zip(self.versions, ex.map(self.parse_version, self.versions)):
                v.display = disp
        if refresh_status:
            if fast:
                self.refresh_all_status(self.versions, fast=True)
            else:
                for v in self.versions:
                    v.running, v.pid = self.get_status(v, fast=fast)
        return self.versions

    # 版本号缓存：{可执行文件绝对路径: (版本号, mtime)}，mtime 未变即复用
    _VERSION_CACHE: dict[str, tuple[str, float]] = {}
    _VERSION_CACHE_LOCK: threading.Lock = threading.Lock()

    def parse_version(self, v: PhpVersion) -> str:
        """从 php -v 首行解析版本号，如 8.2.4。带 mtime 缓存。"""
        exe = os.path.join(os.path.dirname(v.cgi), CLI_NAME)
        if not os.path.exists(exe):
            exe = v.cgi
        try:
            mtime = os.path.getmtime(exe)
        except OSError:
            mtime = 0.0
        with self._VERSION_CACHE_LOCK:
            cached = self._VERSION_CACHE.get(exe)
            if cached is not None and cached[1] == mtime:
                return cached[0]
        _, out, err = pu.run_cmd([exe, "-v"], timeout=10)
        text = out or err
        m = re.search(r"PHP\s+([0-9]+\.[0-9]+\.[0-9]+)", text)
        version = m.group(1) if m else "未知"
        with self._VERSION_CACHE_LOCK:
            self._VERSION_CACHE[exe] = (version, mtime)
        return version

    # ------------------------------------------------------------------ #
    # 状态判定
    # ------------------------------------------------------------------ #
    def _cmd_matches(self, v: PhpVersion, cmd: str) -> bool:
        """该进程命令行是否属于本版本：含 cgi 可执行名（目录布局差异导致的
        大小写/相对路径差异均允许）。cmd 为空时不强判（保持旧行为）。"""
        if not cmd:
            return True
        return os.path.basename(v.cgi).lower() in cmd.lower()

    def get_status(self, v: PhpVersion, fast: bool = False) -> tuple[bool, int | None]:
        """(是否运行, PID)。端口监听 + PID 存活 + 命令行含 php-cgi（fast 跳过）。

        顺带回写 v.running / v.pid，保证 stop/start 后版本状态立即一致。
        """
        pids = pu.port_to_pid_fast(v.port)
        if not pids:
            v.running, v.pid = False, None
            return v.running, v.pid
        for pid in pids:
            if not pu.is_pid_alive_fast(pid):
                continue
            if fast:
                v.running, v.pid = True, pid
                return v.running, v.pid
            if self._cmd_matches(v, pu.pid_to_path(pid) or ""):
                v.running, v.pid = True, pid
                return v.running, v.pid
        v.running, v.pid = False, None
        return v.running, v.pid

    def refresh_all_status(self, versions: list[PhpVersion] | None = None, fast: bool = True) -> None:
        """一次进程/端口快照内批量刷新全部版本状态。

        仅做 2 次系统调用：一次 TCP 全量快照 + 一次存活 PID 集合，所有版本的
        「端口监听 + PID 存活」判定均在本地完成。仅当底层快照不可用时回退逐版本。
        """
        versions = versions if versions is not None else self.versions
        if not versions:
            return
        snap = pu.get_tcp_snapshot()
        if snap is None:
            for v in versions:
                v.running, v.pid = self.get_status(v, fast=fast)
            return
        alive = pu.get_process_snapshot()
        for v in versions:
            pids = snap.get(v.port) or []
            pid = next((p for p in pids if p in alive), None)
            v.running, v.pid = (True, pid) if pid else (False, None)

    # ------------------------------------------------------------------ #
    # 启停 / 重启
    # ------------------------------------------------------------------ #
    def start(self, v: PhpVersion) -> str:
        running, pid = self.get_status(v)
        if running:
            return f"[{v.name}] 已在运行（PID {pid}，端口 {v.port}）"

        pids = pu.port_to_pid(v.port)
        if pids:
            names = ", ".join(f"{pu.pid_to_name(p)}({p})" for p in pids[:3])
            raise PortConflictError(
                f"[{v.name}] 端口 {v.port} 已被占用：{names}\n"
                f"请先停止占用进程，或在界面中修改 {v.name} 的端口，"
                f"并同步修改 nginx vhost 的 fastcgi_pass。"
            )

        args = ["-b", f"127.0.0.1:{v.port}"]
        if v.ini:
            args += ["-c", v.ini]
        pu.start_hidden(v.cgi, args, workdir=v.dir)
        # php-cgi 冷启动（加载扩展/ini）可能超过 1s；轮询等待最多约 5s
        running, pid = False, None
        for _ in range(15):
            time.sleep(0.3)
            pu.invalidate_process_cache()
            running, pid = self.get_status(v)
            if running:
                break
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
        pu.invalidate_process_cache()
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
        if not v.ini:
            return "（未使用独立配置文件：将读取 PHP 编译默认配置，如需独立配置请在版本目录放置 php.ini）"
        try:
            with open(v.ini, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError as e:
            return f"读取失败：{e}"

    def read_key_ini(self, v: PhpVersion) -> dict:
        """提取关键配置项 + 已启用扩展列表。"""
        result: dict = {}
        enabled_ext: list[str] = []
        if not v.ini:
            return {"__error__": "未使用独立配置文件（将读取 PHP 编译默认配置）"}
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
