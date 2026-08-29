# -*- coding: utf-8 -*-
"""Redis 进程管理：多实例发现 / 状态 / 启动 / 停止 / 重启。

约定：
- 实例 = C:\\wnrp 下含 redis-server.exe 的 Redis* 目录（Redis、Redis-8.4.4 …）；
- 端口从实例配置文件（redis.conf / redis.windows.conf）解析，默认 6379；
- 状态判定：tasklist 过滤 redis-server.exe 与 TCP 监听快照求交集，
  避免把占用同端口的其它程序误判为 Redis；
- 启动复用 RunHiddenConsole.exe 隐藏后台运行（与各 start_phpXX.bat 一致）。
"""
import glob
import os
import re
import time
from dataclasses import dataclass

from . import process_utils as pu
from .config import WNRP_ROOT


@dataclass
class RedisInstance:
    """单个 Redis 实例（目录）。"""
    name: str        # 目录名：Redis / Redis-8.4.4
    dir: str         # 实例根目录
    server: str      # redis-server.exe 路径
    cli: str         # redis-cli.exe 路径（可能为空）
    conf: str        # 配置文件路径
    port: int        # 解析出的监听端口
    version: str = ""   # 版本号（首次探测后缓存）
    running: bool = False
    pids: list = None   # 运行中的 PID 列表

    def __post_init__(self):
        self.pids = [] if self.pids is None else self.pids


# 配置文件优先级：8.x 标准 redis.conf > 老版 redis.windows.conf > 任意 *.conf
_CONF_PREFERENCE = ("redis.conf", "redis.windows.conf")


class RedisManager:
    def __init__(self, root: str = WNRP_ROOT):
        self.root = root
        self.instances: list[RedisInstance] = []
        self.refresh_instances()

    # ------------------------------------------------------------------ #
    # 实例发现
    # ------------------------------------------------------------------ #
    def refresh_instances(self) -> list[RedisInstance]:
        self.instances = []
        for d in sorted(glob.glob(os.path.join(self.root, "Redis*"))):
            if not os.path.isdir(d):
                continue
            server = os.path.join(d, "redis-server.exe")
            if not os.path.exists(server):
                continue
            self.instances.append(self._build_instance(d, server))
        return self.instances

    def _build_instance(self, d: str, server: str) -> RedisInstance:
        conf = self._find_conf(d)
        return RedisInstance(
            name=os.path.basename(d),
            dir=d,
            server=server,
            cli=os.path.join(d, "redis-cli.exe") if os.path.exists(
                os.path.join(d, "redis-cli.exe")) else "",
            conf=conf,
            port=self._read_port(conf),
        )

    @staticmethod
    def _find_conf(d: str) -> str:
        for name in _CONF_PREFERENCE:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
        for f in sorted(glob.glob(os.path.join(d, "*.conf"))):
            return f
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
        """全部 redis-server.exe 进程 PID（一次 tasklist）。"""
        code, out, _ = pu.run_cmd(
            ["tasklist", "/FI", "IMAGENAME eq redis-server.exe", "/FO", "CSV", "/NH"],
            timeout=10,
        )
        if code != 0:
            return set()
        return {int(m) for m in re.findall(r'"redis-server\.exe","(\d+)"', out)}

    def get_status(self, inst: RedisInstance) -> tuple[bool, list[int]]:
        """(是否运行, PID 列表)：redis-server 进程 ∩ 实例端口监听。

        多个实例配置相同端口时，额外按进程可执行路径精确归属，
        避免把同端口其它实例的进程误判为本实例运行中。
        """
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
        if not same_port:
            pids = candidates
        else:
            target = inst.server.lower()
            pids = []
            for pid in candidates:
                path = pu.pid_to_path(pid).lower()
                if path == target:
                    pids.append(pid)
        inst.running = bool(pids)
        inst.pids = pids
        return inst.running, pids

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
        inst.version = m.group(1) if m else "未知"
        return inst.version

    # ------------------------------------------------------------------ #
    # 启停
    # ------------------------------------------------------------------ #
    def start(self, inst: RedisInstance) -> str:
        if not os.path.exists(inst.server):
            return f"[{inst.name}] 未找到 {inst.server}"
        if not inst.conf:
            return f"[{inst.name}] 未找到配置文件，无法启动"
        running, pids = self.get_status(inst)
        if running:
            return f"[{inst.name}] 已在运行（PID {', '.join(map(str, pids))}，端口 {inst.port}）"
        # 端口被非 Redis 进程占用则提示，不强杀
        others = [p for p in pu.port_to_pid_fast(inst.port) if p not in self._redis_server_pids()]
        if others:
            names = ", ".join(f"{pu.pid_to_name(p)}({p})" for p in others[:3])
            return (f"[{inst.name}] 端口 {inst.port} 已被占用：{names}\n"
                    f"请先停止占用进程，或修改 {inst.conf} 中的 port 配置。")
        pu.start_hidden(inst.server, [inst.conf], workdir=inst.dir)
        time.sleep(0.8)
        running, pids = self.get_status(inst)
        if running:
            return f"[{inst.name}] 启动成功（PID {', '.join(map(str, pids))}，端口 {inst.port}）"
        return (f"[{inst.name}] 启动失败：端口 {inst.port} 未能监听。\n"
                f"请检查 {inst.conf} 配置与目录权限。")

    def stop(self, inst: RedisInstance) -> str:
        running, pids = self.get_status(inst)
        if not running:
            return f"[{inst.name}] 未在运行"
        ok = True
        for pid in pids:
            if not pu.kill_pid(pid):
                ok = False
        time.sleep(0.3)
        running, _ = self.get_status(inst)
        if not running:
            return f"[{inst.name}] 已停止" if ok else f"[{inst.name}] 已停止（部分进程强制结束）"
        return f"[{inst.name}] 停止失败，请手动检查进程"

    def restart(self, inst: RedisInstance) -> str:
        parts = [self.stop(inst)]
        if "未在运行" not in parts[0]:
            time.sleep(0.4)
        parts.append(self.start(inst))
        return "\n".join(parts)

    def ping(self, inst: RedisInstance) -> str:
        """用 redis-cli ping 验证实例连通性（无 cli 时返回空）。"""
        if not inst.cli:
            return ""
        code, out, err = pu.run_cmd([inst.cli, "-p", str(inst.port), "ping"], timeout=10)
        text = (out or err).strip()
        return "PONG" if code == 0 and "PONG" in text else text or "无响应"
