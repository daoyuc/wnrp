# -*- coding: utf-8 -*-
"""整套服务编排：一键启动 / 停止（各 PHP + Redis + [MySQL] + Nginx）。

顺序：
- 启动：PHP → Redis → MySQL → Nginx（先备好后端，最后开入口）；
- 停止：Nginx → PHP → Redis → MySQL（先关入口，再停后端）。

每项独立 try/except，单项失败不影响后续，最终汇总为多行文本返回。
"""
from .i18n import t


class ServiceGroup:
    """把各 manager 组合成「一键启停」。"""

    def __init__(self, php_mgr, nginx_mgr, redis_mgr, mysql_mgr=None):
        self.php_mgr = php_mgr
        self.nginx_mgr = nginx_mgr
        self.redis_mgr = redis_mgr
        self.mysql_mgr = mysql_mgr

    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        """刷新各服务状态（避免重复启停已运行的服务）。"""
        try:
            if not self.php_mgr.versions:
                # 首次调用时尚未扫描（如托盘还没打开过 PHP 页）
                self.php_mgr.versions = self.php_mgr.scan_versions()
            self.php_mgr.refresh_all_status()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.redis_mgr.get_status_all()
        except Exception:  # noqa: BLE001
            pass
        if self.mysql_mgr is not None:
            for inst in self.mysql_mgr.instances:
                try:
                    self.mysql_mgr.get_status(inst)
                except Exception:  # noqa: BLE001
                    pass

    def _call(self, tag: str, fn, *args) -> str:
        try:
            msg = fn(*args)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}：{e}"
        return f"{tag}：{msg}"

    # ------------------------------------------------------------------ #
    def start_all(self) -> str:
        self._refresh()
        lines: list[str] = []

        for v in self.php_mgr.versions or []:
            if getattr(v, "running", False):
                continue
            lines.append(self._call(f"PHP {v.name}", self.php_mgr.start, v))
        for inst in self.redis_mgr.instances or []:
            if getattr(inst, "running", False):
                continue
            lines.append(self._call(f"Redis {inst.name}", self.redis_mgr.start, inst))
        if self.mysql_mgr is not None:
            for m in self.mysql_mgr.instances or []:
                if getattr(m, "running", False):
                    continue
                lines.append(self._call(f"MySQL {m.name}", self.mysql_mgr.start, m))
        try:
            running, _ = self.nginx_mgr.get_status()
        except Exception:  # noqa: BLE001
            running = False
        if not running:
            lines.append(self._call("Nginx", self.nginx_mgr.start))

        return "\n".join(lines) or t("所有服务均已在运行，无需启动")

    def stop_all(self) -> str:
        self._refresh()
        lines: list[str] = []

        try:
            running, _ = self.nginx_mgr.get_status()
        except Exception:  # noqa: BLE001
            running = False
        if running:
            lines.append(self._call("Nginx", self.nginx_mgr.stop))

        for v in self.php_mgr.versions or []:
            if not getattr(v, "running", False):
                continue
            lines.append(self._call(f"PHP {v.name}", self.php_mgr.stop, v))
        for inst in self.redis_mgr.instances or []:
            if not getattr(inst, "running", False):
                continue
            lines.append(self._call(f"Redis {inst.name}", self.redis_mgr.stop, inst))
        if self.mysql_mgr is not None:
            for m in self.mysql_mgr.instances or []:
                if not getattr(m, "running", False):
                    continue
                lines.append(self._call(f"MySQL {m.name}", self.mysql_mgr.stop, m))

        return "\n".join(lines) or t("没有正在运行的服务")
