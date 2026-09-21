# -*- coding: utf-8 -*-
"""整套服务编排：一键启动 / 停止（各 PHP + Redis + [MySQL] + Nginx）。

顺序：
- 启动：PHP → Redis → MySQL → Nginx（先备好后端，最后开入口）；
- 停止：Nginx → PHP → Redis → MySQL（先关入口，再停后端）。

每项独立 try/except，单项失败不影响后续，最终汇总为多行文本返回。

每项的成败都会写入全局运行日志（core.run_log），供「运行日志」页签查看。
"""
from . import run_log
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
            if self.redis_mgr is not None:
                self.redis_mgr.get_status_all()
        except Exception:  # noqa: BLE001
            pass
        if self.mysql_mgr is not None:
            for inst in self.mysql_mgr.instances:
                try:
                    self.mysql_mgr.get_status(inst)
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _verb(action: str, ok_: bool) -> str:
        """日志措辞：启动/停止 + 成功/失败（action 为空时退化为操作成功/失败）。"""
        table = {
            ("start", True): t("启动成功"), ("start", False): t("启动失败"),
            ("stop", True): t("停止成功"), ("stop", False): t("停止失败"),
        }
        return table.get((action, ok_), t("操作成功") if ok_ else t("操作失败"))

    def _call(self, tag: str, fn, *args, action: str = "") -> str:
        """调用单项服务操作：异常不外抛先转文本，结果写入全局运行日志。

        「自动启动 / 一键启停」时逐项记录 PHP / Redis / MySQL / Nginx
        各自成功还是失败，便于事后在「运行日志」页签定位是哪一项没起来。
        """
        try:
            msg = fn(*args)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}：{e}"
            run_log.error("services", f"{tag} {self._verb(action, False)}：{msg}")
        else:
            run_log.ok("services", f"{tag} {self._verb(action, True)}：{msg}")
        return f"{tag}：{msg}"

    # ------------------------------------------------------------------ #
    def start_all(self) -> str:
        self._refresh()
        lines: list[str] = []

        for v in self.php_mgr.versions or []:
            if getattr(v, "running", False):
                continue
            lines.append(self._call(f"PHP {v.name}", self.php_mgr.start, v, action="start"))
        for inst in (self.redis_mgr.instances if self.redis_mgr else []):
            if getattr(inst, "running", False):
                continue
            lines.append(self._call(f"Redis {inst.name}", self.redis_mgr.start, inst,
                                    action="start"))
        if self.mysql_mgr is not None:
            for m in self.mysql_mgr.instances or []:
                if getattr(m, "running", False):
                    continue
                lines.append(self._call(f"MySQL {m.name}", self.mysql_mgr.start, m,
                                        action="start"))
        try:
            running, _ = self.nginx_mgr.get_status()
        except Exception:  # noqa: BLE001
            running = False
        if not running:
            lines.append(self._call("Nginx", self.nginx_mgr.start, action="start"))

        summary = "\n".join(lines) or t("所有服务均已在运行，无需启动")
        if not lines:
            run_log.info("services", summary)
        return summary

    def stop_all(self) -> str:
        self._refresh()
        lines: list[str] = []

        try:
            running, _ = self.nginx_mgr.get_status()
        except Exception:  # noqa: BLE001
            running = False
        if running:
            lines.append(self._call("Nginx", self.nginx_mgr.stop, action="stop"))

        for v in self.php_mgr.versions or []:
            if not getattr(v, "running", False):
                continue
            lines.append(self._call(f"PHP {v.name}", self.php_mgr.stop, v, action="stop"))
        for inst in (self.redis_mgr.instances if self.redis_mgr else []):
            if not getattr(inst, "running", False):
                continue
            lines.append(self._call(f"Redis {inst.name}", self.redis_mgr.stop, inst,
                                    action="stop"))
        if self.mysql_mgr is not None:
            for m in self.mysql_mgr.instances or []:
                if not getattr(m, "running", False):
                    continue
                lines.append(self._call(f"MySQL {m.name}", self.mysql_mgr.stop, m,
                                        action="stop"))

        summary = "\n".join(lines) or t("没有正在运行的服务")
        if not lines:
            run_log.info("services", summary)
        return summary
