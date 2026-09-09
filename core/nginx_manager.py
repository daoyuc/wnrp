# -*- coding: utf-8 -*-
"""Nginx 管理：启动 / 停止 / 重载 / 配置检查 / 状态与版本获取。

跨平台：
- Windows：C:\\wnrp\\nginx\\nginx.exe，命令带 `-p C:/wnrp/nginx`（与现有脚本一致）；
- macOS / Linux：优先 WNRP_ROOT/nginx/nginx（自定义 relocatable 版，同样带 -p）；
  其次自动发现 Homebrew nginx（前缀无 -p，使用编译期默认配置
  /opt/homebrew/etc/nginx/nginx.conf）。
状态判定：Windows 用 tasklist；posix 用一次 ps 全量快照按命令行匹配，
只统计本安装（路径/前缀命中）的 nginx 进程。
"""
import os
import re
import time

from . import process_utils as pu
from .config import IS_WIN, WNRP_ROOT, brew_prefixes

# Windows / 自定义目录布局
ROOT_NGINX = os.path.join(WNRP_ROOT, "nginx")


def _locate_nginx() -> tuple[str, str, str]:
    """返回 (exe, 配置目录/前缀, mode)。mode: 'root'（WNRP 布局，用 -p）| 'brew'。"""
    if IS_WIN:
        return os.path.join(ROOT_NGINX, "nginx.exe"), ROOT_NGINX, "root"
    exe = os.path.join(ROOT_NGINX, "nginx")
    if os.path.exists(exe):
        return exe, ROOT_NGINX, "root"
    for prefix in brew_prefixes():
        for cand in (os.path.join(prefix, "bin", "nginx"),
                     os.path.join(prefix, "opt", "nginx", "bin", "nginx")):
            if os.path.exists(cand):
                return os.path.realpath(cand), os.path.join(prefix, "etc", "nginx"), "brew"
    # 均未找到：返回自定义布局路径，启动时报「未找到」并给出提示
    return exe, ROOT_NGINX, "root"


class NginxManager:
    def __init__(self):
        self.exe, self.prefix, self.mode = _locate_nginx()

    @property
    def conf_dir(self) -> str:
        """配置文件所在目录（UI 展示 / 打开用）。"""
        return self.prefix

    # ------------------------------------------------------------------ #
    # 站点配置目录推导（唯一事实来源，供 vhost 写入 / include / 扫描共用）
    # ------------------------------------------------------------------ #
    @property
    def site_base(self) -> str:
        """phpvm 站点管理基准目录 = 实际生效主配置所在目录：
        - root/Windows 布局（nginx 带 `-p <prefix>`）：主配置默认读
          <prefix>/conf/nginx.conf → 基准 = <prefix>/conf；
        - brew 布局（无 -p）：主配置默认读 <prefix>/nginx.conf → 基准 = <prefix>。
        """
        if IS_WIN or self.mode == "root":
            return os.path.join(self.prefix, "conf")
        return self.prefix

    @property
    def main_conf(self) -> str:
        """当前实际生效的 nginx.conf（phpvm 视角，也用于 include 注入/检测）。"""
        return os.path.join(self.site_base, "nginx.conf")

    @property
    def vhost_dir(self) -> str:
        """phpvm 生成站点配置的目录（写入后需被主配置 include）。"""
        return os.path.join(self.site_base, "vhost")

    # ------------------------------------------------------------------ #
    def _cmd(self, extra: list[str]) -> list[str]:
        """拼完整命令。brew 版不传 -p（使用编译期默认配置）。"""
        if IS_WIN or self.mode == "root":
            return [self.exe, "-p", self.prefix] + extra
        return [self.exe] + extra

    def get_status(self) -> tuple[bool, list[int]]:
        """(是否运行, PID 列表)。

        posix 只统计「本安装」的 nginx：命令行命中本安装锚点
        （brew：Cellar/opt 路径、默认配置目录；自定义布局：exe/前缀），
        避免把用户其它用途的 nginx（argv 仅为裸名 nginx 等）纳入管理范围。
        """
        if IS_WIN:
            code, out, _ = pu.run_cmd(
                ["tasklist", "/FI", "IMAGENAME eq nginx.exe", "/FO", "CSV", "/NH"],
                timeout=10,
            )
            if code != 0:
                return False, []
            pids = [int(m) for m in re.findall(r'"nginx\.exe","(\d+)"', out)]
            return len(pids) > 0, pids
        hits = pu.cmdline_matches_pids(("nginx",))
        alive = pu.get_process_snapshot()
        pids = sorted(p for p, cmd in hits.items() if p in alive and self._cmd_is_mine(cmd))
        return len(pids) > 0, pids

    def _cmd_is_mine(self, cmd: str) -> bool:
        """进程命令行是否属于本 nginx 安装。"""
        anchors = [self.exe, self.prefix]
        if self.mode == "brew":
            root = self.prefix[: -len("/etc/nginx")] if self.prefix.endswith("/etc/nginx") else ""
            if root:
                anchors += [os.path.join(root, "opt", "nginx"),
                            os.path.join(root, "bin", "nginx")]
        return any(a and a in cmd for a in anchors)

    def get_version(self) -> str:
        code, out, err = pu.run_cmd(self._cmd(["-V"]), timeout=10)
        text = err or out
        m = re.search(r"nginx/(\d+\.\d+\.\d+)", text)
        return m.group(1) if m else "未知"

    def _ensure_exe(self) -> str | None:
        if not os.path.exists(self.exe):
            if self.mode == "brew":
                return "未找到 Homebrew nginx，请先安装：brew install nginx"
            return f"未找到 {self.exe}"
        return None

    # ------------------------------------------------------------------ #
    def start(self) -> str:
        err = self._ensure_exe()
        if err:
            return err
        running, pids = self.get_status()
        if running:
            return f"Nginx 已在运行（PID {', '.join(map(str, pids))}）"
        code, out, err_text = pu.run_cmd(self._cmd([]), timeout=10)
        time.sleep(0.8)
        pu.invalidate_process_cache()
        running, pids = self.get_status()
        if running:
            return f"Nginx 启动成功（PID {', '.join(map(str, pids))}）"
        return f"Nginx 启动失败：{err_text.strip() or out.strip() or '未知错误'}"

    def stop(self) -> str:
        import signal as _signal

        running, pids = self.get_status()
        if not running:
            return "Nginx 未在运行"
        if IS_WIN:
            pu.run_cmd(self._cmd(["-s", "quit"]), timeout=10)
        else:
            # posix：pid 文件可能缺失/为空，直接向 master PID 发 SIGQUIT（优雅退出）
            for pid in pids:
                try:
                    os.kill(pid, _signal.SIGQUIT)
                except (ProcessLookupError, PermissionError):
                    pass
        time.sleep(0.8)
        pu.invalidate_process_cache()
        running, _ = self.get_status()
        if not running:
            return "Nginx 已停止"
        # 优雅退出未生效时兜底强制结束
        for pid in pids:
            pu.kill_pid(pid)
        time.sleep(0.3)
        pu.invalidate_process_cache()
        running, _ = self.get_status()
        if not running:
            return "Nginx 已停止（强制结束）"
        return "Nginx 停止失败，请手动检查进程"

    def reload(self) -> str:
        running, pids = self.get_status()
        if not running:
            return "Nginx 未在运行，无法重载"
        # 统一执行 `nginx -s reload`（root 模式带 -p，brew 不带）。
        # 注意：nginx 的 SIGUSR1 是「重开日志文件」，不是重载配置，故不能发给 master 代替。
        code, out, err_text = pu.run_cmd(self._cmd(["-s", "reload"]), timeout=10)
        text = (out or err_text).strip()
        if code == 0:
            return "Nginx 已平滑重载" if not text else f"Nginx 已平滑重载：{text}"
        if not IS_WIN:
            # 命令通道失败时兜底向 master 发 SIGHUP（nginx 重载配置信号）
            import signal as _signal
            for pid in pids:
                try:
                    os.kill(pid, _signal.SIGHUP)
                except (ProcessLookupError, PermissionError):
                    pass
            return f"Nginx 已平滑重载（SIGHUP 兜底）：{text}"
        return f"Nginx 重载失败：{text or '未知错误'}"

    def test_config(self) -> str:
        code, out, err_text = pu.run_cmd(self._cmd(["-t"]), timeout=10)
        text = (out or err_text).strip()
        return text or ("配置检查通过" if code == 0 else "配置检查失败")
