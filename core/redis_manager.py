# -*- coding: utf-8 -*-
"""Redis 进程管理：多实例发现 / 状态 / 启动 / 停止 / 重启。

约定：
- 实例 = 含 redis-server（*.exe）的 Redis* 目录（Windows：C:\\wnrp\\Redis*，
  二进制在目录根；mac/Linux：WNRP_ROOT 下 Redis* 目录 + 自动发现 Homebrew
  redis keg，二进制在 dir/bin）；
- 端口从实例配置文件（redis.conf / redis.windows.conf）解析，默认 6379；
  目录内无配置时回退使用 brew 的 /opt/homebrew/etc/redis.conf；
- 状态判定：进程名（redis-server）与 TCP 监听快照求交集，同端口多实例
  再按进程命令行路径精确归属；
- 启动：Windows 复用 RunHiddenConsole.exe；posix 脱离终端后台运行。
"""
import glob
import os
import re
import time
from dataclasses import dataclass

from . import process_utils as pu
from .config import IS_WIN, WNRP_ROOT, brew_prefixes
from .i18n import t

if IS_WIN:
    SERVER_NAME = "redis-server.exe"
    CLI_NAME = "redis-cli.exe"
else:
    SERVER_NAME = "redis-server"
    CLI_NAME = "redis-cli"


@dataclass
class RedisInstance:
    """单个 Redis 实例（目录）。"""
    name: str        # 目录名：Redis / Redis-8.4.4 / redis@6.2
    dir: str         # 实例根目录
    server: str      # redis-server 可执行文件路径
    cli: str         # redis-cli 路径（可能为空）
    conf: str        # 配置文件路径
    port: int        # 解析出的监听端口
    version: str = ""   # 版本号（首次探测后缓存）
    running: bool = False
    pids: list = None   # 运行中的 PID 列表

    def __post_init__(self):
        self.pids = [] if self.pids is None else self.pids


# 配置文件优先级：8.x 标准 redis.conf > 老版 redis.windows.conf > 任意 *.conf
_CONF_PREFERENCE = ("redis.conf", "redis.windows.conf")


def _locate_exe(d: str, name: str) -> str:
    """目录根 → bin/ 查找可执行文件。"""
    for rel in ("", "bin"):
        p = os.path.join(d, rel, name)
        if os.path.exists(p):
            return p
    return ""


class RedisManager:
    def __init__(self, root: str = WNRP_ROOT):
        self.root = root
        self.instances: list[RedisInstance] = []
        self.refresh_instances()

    # ------------------------------------------------------------------ #
    # 实例发现
    # ------------------------------------------------------------------ #
    def _candidate_dirs(self) -> list[str]:
        """发现目录：WNRP_ROOT 下 Redis*（优先）+ Homebrew redis keg（仅 posix）。"""
        dirs: list[str] = []
        for d in sorted(glob.glob(os.path.join(self.root, "Redis*"))):
            if os.path.isdir(d) and _locate_exe(d, SERVER_NAME):
                dirs.append(d)
        if not IS_WIN:
            for prefix in brew_prefixes():
                for d in sorted(glob.glob(os.path.join(prefix, "opt", "redis*"))):
                    if os.path.isdir(d) and _locate_exe(d, SERVER_NAME) and d not in dirs:
                        dirs.append(d)
        return dirs

    def refresh_instances(self) -> list[RedisInstance]:
        self.instances = []
        for d in self._candidate_dirs():
            server = _locate_exe(d, SERVER_NAME)
            if not server:
                continue
            self.instances.append(self._build_instance(d, server))
        return self.instances

    def _build_instance(self, d: str, server: str) -> RedisInstance:
        cli = _locate_exe(d, CLI_NAME)
        conf = self._find_conf(d)
        return RedisInstance(
            name=os.path.basename(d),
            dir=d,
            server=server,
            cli=cli,
            conf=conf,
            port=self._read_port(conf),
        )

    @staticmethod
    def _find_conf(d: str) -> str:
        """目录内配置优先；无则回退该 brew 前缀的 /etc/redis.conf。"""
        for name in _CONF_PREFERENCE:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
        for f in sorted(glob.glob(os.path.join(d, "*.conf"))):
            return f
        if not IS_WIN:
            for prefix in brew_prefixes():
                p = os.path.join(prefix, "etc", "redis.conf")
                if os.path.exists(p):
                    return p
        return ""

    @staticmethod
    def _read_port(conf: str) -> int:
        try:
            with open(conf, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    m = re.match(r"port\s+(\d+)", s, re.IGNORECASE)
                    if m:
                        return int(m.group(1))
        except OSError:
            pass
        return 6379

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def _redis_server_pids(self) -> set[int]:
        """全部 redis-server 进程 PID。win：tasklist 一次；posix：ps 快照一次。"""
        if IS_WIN:
            code, out, _ = pu.run_cmd(
                ["tasklist", "/FI", "IMAGENAME eq redis-server.exe", "/FO", "CSV", "/NH"],
                timeout=10,
            )
            if code != 0:
                return set()
            return {int(m) for m in re.findall(r'"redis-server\.exe","(\d+)"', out)}
        return set(pu.cmdline_matches_pids((SERVER_NAME,)))

    def _pid_belongs(self, inst: RedisInstance, pid: int) -> bool:
        """进程是否属于本实例（多实例同端口时精确归属）。"""
        path = pu.pid_to_path(pid).lower()
        if not path:
            return True
        if IS_WIN:
            return path == inst.server.lower()
        # posix：命令行包含实例服务器路径（目录/bin/redis-server）即归属
        return inst.server.lower() in path

    def get_status(self, inst: RedisInstance) -> tuple[bool, list[int]]:
        """(是否运行, PID 列表)：redis-server 进程 ∩ 实例端口监听。"""
        all_redis = self._redis_server_pids()
        if not all_redis:
            inst.running, inst.pids = False, []
            return False, []
        listening = set(pu.port_to_pid_fast(inst.port))
        candidates = sorted(all_redis & listening)
        if not candidates:
            inst.running, inst.pids = False, []
            return False, []
        # 端口唯一 → 直接归属；多实例同端口 → 按进程路径精确匹配
        same_port = [i for i in self.instances if i is not inst and i.port == inst.port]
        if same_port:
            candidates = [p for p in candidates if self._pid_belongs(inst, p)]
        inst.running = bool(candidates)
        inst.pids = candidates
        return inst.running, candidates

    def get_status_all(self) -> list[RedisInstance]:
        for inst in self.instances:
            self.get_status(inst)
        return self.instances

    def get_version(self, inst: RedisInstance) -> str:
        if inst.version:
            return inst.version
        code, out, err = pu.run_cmd([inst.server, "--version"], timeout=10)
        text = out or err
        m = re.search(r"v\s*=\s*([\d.]+)", text) or re.search(r"version\s+([\d.]+)", text)
        inst.version = m.group(1) if m else t("未知")
        return inst.version

    # ------------------------------------------------------------------ #
    # 启停
    # ------------------------------------------------------------------ #
    def start(self, inst: RedisInstance) -> str:
        if not os.path.exists(inst.server):
            return t("[{name}] 未找到 {path}", name=inst.name, path=inst.server)
        if not inst.conf:
            return (t("[{name}] 未找到配置文件，无法启动。\n请在 {dir} 放置 redis.conf，"
                      "或安装 Homebrew redis 并配置对应 /etc/redis.conf。",
                      name=inst.name, dir=inst.dir))
        running, pids = self.get_status(inst)
        if running:
            return t("[{name}] 已在运行（PID {pids}，端口 {port}）",
                     name=inst.name, pids=", ".join(map(str, pids)), port=inst.port)
        others = [p for p in pu.port_to_pid_fast(inst.port) if p not in self._redis_server_pids()]
        if others:
            names = ", ".join(f"{pu.pid_to_name(p)}({p})" for p in others[:3])
            return (t("[{name}] 端口 {port} 已被占用：{names}\n请先停止占用进程，"
                      "或修改 {conf} 中的 port 配置。",
                      name=inst.name, port=inst.port, names=names, conf=inst.conf))
        # 配置与工作目录同目录时传相对文件名（兼容 msys2 移植版不认反斜杠路径）；
        # 其它情况（如 brew 的 /etc/redis.conf）传绝对路径。
        conf_arg = inst.conf
        if os.path.dirname(os.path.abspath(inst.conf)) == os.path.abspath(inst.dir):
            conf_arg = os.path.basename(inst.conf)
        pu.start_hidden(inst.server, [conf_arg], workdir=inst.dir)
        time.sleep(0.8)
        pu.invalidate_process_cache()
        running, pids = self.get_status(inst)
        if running:
            return t("[{name}] 启动成功（PID {pids}，端口 {port}）",
                     name=inst.name, pids=", ".join(map(str, pids)), port=inst.port)
        return (t("[{name}] 启动失败：端口 {port} 未能监听。\n请检查 {conf} 配置与目录权限。",
                  name=inst.name, port=inst.port, conf=inst.conf))

    def stop(self, inst: RedisInstance) -> str:
        running, pids = self.get_status(inst)
        if not running:
            return t("[{name}] 未在运行", name=inst.name)
        ok = True
        for pid in pids:
            if not pu.kill_pid(pid):
                ok = False
        time.sleep(0.3)
        pu.invalidate_process_cache()
        running, _ = self.get_status(inst)
        if not running:
            return (t("[{name}] 已停止", name=inst.name) if ok
                    else t("[{name}] 已停止（部分进程强制结束）", name=inst.name))
        return t("[{name}] 停止失败，请手动检查进程", name=inst.name)

    def restart(self, inst: RedisInstance) -> str:
        parts = [self.stop(inst)]
        if t("未在运行") not in parts[0]:
            time.sleep(0.4)
        parts.append(self.start(inst))
        return "\n".join(parts)

    def ping(self, inst: RedisInstance) -> str:
        """用 redis-cli ping 验证实例连通性（无 cli 时返回空）。"""
        if not inst.cli:
            return ""
        code, out, err = pu.run_cmd([inst.cli, "-p", str(inst.port), "ping"], timeout=10)
        text = (out or err).strip()
        return "PONG" if code == 0 and "PONG" in text else text or t("无响应")

    # ------------------------------------------------------------------ #
    # 命令执行 / 键空间统计（Redis 管理页「Redis 命令」「DB 键空间」用）
    # ------------------------------------------------------------------ #
    def run_command(self, inst: RedisInstance, db: int = 0, command: str = "") -> str:
        """对指定逻辑库执行一条 Redis 命令，返回文本输出。"""
        if not inst.cli:
            return t("未找到 redis-cli，无法执行命令")
        command = command.strip()
        if not command:
            return ""
        code, out, err = pu.run_cmd_stdin(
            [inst.cli, "-p", str(inst.port), "-n", str(db)],
            command + "\n",
            timeout=15,
        )
        text = (out or "").rstrip("\n")
        if err:
            text = f"{text}\n{err}".strip()
        if not text:
            text = t("命令执行失败（退出码 {code}）", code=code) if code != 0 else t("(空输出)")
        return text

    def keyspace_stats(self, inst: RedisInstance) -> list[tuple[int, int]] | None:
        """返回各逻辑库 key 数 [(db, keys), ...]（仅非空库，按 db 升序）。"""
        if not inst.cli:
            return None
        code, out, err = pu.run_cmd(
            [inst.cli, "-p", str(inst.port), "info", "keyspace"], timeout=10
        )
        if code != 0:
            return None
        text = out or err
        if ("could not connect" in text.lower()
                or "connection refused" in text.lower()
                or "NOAUTH" in text):
            return None
        stats = [(int(m.group(1)), int(m.group(2)))
                 for m in re.finditer(r"db(\d+):keys=(\d+)", text)]
        return sorted(stats)
