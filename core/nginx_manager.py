# -*- coding: utf-8 -*-
"""Nginx 管理：启动 / 停止 / 重载 / 配置检查 / 状态与版本获取。

与现有脚本约定一致：
  nginx.exe -p C:/wnrp/nginx（设置前缀）
  -s reload 平滑重载；-s quit 优雅停止；-t 配置检查（输出走 stderr）
状态判定：tasklist 查找 nginx.exe 进程。
"""
import os
import re
import time

from . import process_utils as pu
from .config import WNRP_ROOT

NGINX_EXE = os.path.join(WNRP_ROOT, "nginx", "nginx.exe")
NGINX_PREFIX = os.path.join(WNRP_ROOT, "nginx")


class NginxManager:
    def __init__(self):
        self.exe = NGINX_EXE
        self.prefix = NGINX_PREFIX

    # ------------------------------------------------------------------ #
    def get_status(self) -> tuple[bool, list[int]]:
        """(是否运行, PID 列表)。worker 进程与 master 均计入。"""
        code, out, _ = pu.run_cmd(
            ["tasklist", "/FI", "IMAGENAME eq nginx.exe", "/FO", "CSV", "/NH"], timeout=10
        )
        if code != 0:
            return False, []
        pids = [int(m) for m in re.findall(r'"nginx\.exe","(\d+)"', out)]
        return len(pids) > 0, pids

    def get_version(self) -> str:
        code, out, err = pu.run_cmd([self.exe, "-p", self.prefix, "-V"], timeout=10)
        text = err or out
        m = re.search(r"nginx/(\d+\.\d+\.\d+)", text)
        return m.group(1) if m else "未知"

    def _ensure_exe(self) -> str | None:
        if not os.path.exists(self.exe):
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
        code, out, err_text = pu.run_cmd([self.exe, "-p", self.prefix], timeout=10)
        time.sleep(0.8)
        running, pids = self.get_status()
        if running:
            return f"Nginx 启动成功（PID {', '.join(map(str, pids))}）"
        return f"Nginx 启动失败：{err_text.strip() or out.strip() or '未知错误'}"

    def stop(self) -> str:
        running, pids = self.get_status()
        if not running:
            return "Nginx 未在运行"
        code, _, err_text = pu.run_cmd([self.exe, "-p", self.prefix, "-s", "quit"], timeout=10)
        time.sleep(0.8)
        running, _ = self.get_status()
        if not running:
            return "Nginx 已停止"
        # 优雅退出未生效时兜底强制结束
        for pid in pids:
            pu.kill_pid(pid)
        time.sleep(0.3)
        running, _ = self.get_status()
        if not running:
            return "Nginx 已停止（强制结束）"
        return f"Nginx 停止失败：{err_text.strip()}"

    def reload(self) -> str:
        running, _ = self.get_status()
        if not running:
            return "Nginx 未在运行，无法重载"
        code, out, err_text = pu.run_cmd([self.exe, "-p", self.prefix, "-s", "reload"], timeout=10)
        text = (out or err_text).strip()
        if code == 0 and not text:
            return "Nginx 已平滑重载"
        return f"Nginx 重载完成：{text}" if "error" not in text.lower() else f"Nginx 重载失败：{text}"

    def test_config(self) -> str:
        code, out, err_text = pu.run_cmd([self.exe, "-p", self.prefix, "-t"], timeout=10)
        text = (out or err_text).strip()
        return text or ("配置检查通过" if code == 0 else "配置检查失败")
