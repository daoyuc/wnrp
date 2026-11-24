# -*- coding: utf-8 -*-
"""总览仪表盘页。

顶栏主操作 + 健康横幅；下方服务 metric 卡网格、告警 / 崩溃 / 近日志卡片。
数据异步聚合（core.overview），渲染在主线程；点击服务卡可跳转对应导航页。
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core import i18n, overview as ovmod
from core import run_log
from core.resource_monitor import format_rss as _format_rss

from . import theme
from . import widgets as w

t = i18n.t


class OverviewPanel(ttk.Frame):
    def __init__(self, master, notify, php_mgr, nginx_mgr, redis_mgr,
                 mysql_mgr, vhost_mgr, config, services, on_navigate=None):
        super().__init__(master, padding=theme.PAD_MD)
        self.notify = notify
        self.php_mgr = php_mgr
        self.nginx_mgr = nginx_mgr
        self.redis_mgr = redis_mgr
        self.mysql_mgr = mysql_mgr
        self.vhost_mgr = vhost_mgr
        self.config = config
        self.services = services
        self.on_navigate = on_navigate  # callable(key: str) -> None
        self._busy = False
        self._action_busy = False
        self._queue: queue.Queue = queue.Queue()
        self._draining = False
        # 按 (kind, rname) 复用 MetricCard，避免 8s 刷新 destroy/重建闪动
        self._svc_cards: dict[tuple[str, str], w.MetricCard] = {}
        self._svc_foot_var = tk.StringVar(value="")
        self._svc_foot: ttk.Label | None = None
        self._list_cache: dict[int, tuple] = {}
        self._build()
        self.auto_refresh()
        self._start_drain()

    def _build(self) -> None:
        self._header = w.PageHeader(
            self, title=t("总览"),
            subtitle=t("一眼查看服务健康、站点告警与最近运行情况"),
        )
        self._toolbar = w.Toolbar(
            self,
            primary=[
                {"text": t("全部启动"), "kind": "accent",
                 "command": self._start_all},
                {"text": t("全部停止"), "kind": "danger",
                 "command": self._stop_all},
            ],
            secondary=[
                {"text": t("体检全部"), "command": self._diagnose_all},
                {"text": t("刷新"), "command": self.auto_refresh},
            ],
        )
        self._action_btns = list(self._toolbar.buttons)
        self._banner = w.HealthBanner(self)
        self._banner.set(w.PILL_OFF, t("加载中…"), t("…"))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, theme.PAD_SM))
        right = ttk.Frame(body)
        right.pack(side="right", fill="both", expand=True, padx=(theme.PAD_SM, 0))

        svc_box = ttk.LabelFrame(left, text=t("服务状态"), padding=theme.PAD_SM)
        svc_box.pack(fill="both", expand=True, pady=(0, theme.PAD_SM))
        self._svc_host = ttk.Frame(svc_box)
        self._svc_host.pack(fill="both", expand=True)
        for c in range(3):
            self._svc_host.columnconfigure(c, weight=1)
        self._svc_foot = ttk.Label(self._svc_host, textvariable=self._svc_foot_var,
                                   style="PageSub.TLabel")
        self._svc_empty = w.EmptyState(
            svc_box, title=t("暂无服务数据"),
            hint=t("刷新或检查环境根目录后重试"),
        )

        alert_box = ttk.LabelFrame(left, text=t("告警"), padding=theme.PAD_SM)
        alert_box.pack(fill="both", expand=True)
        self._alert_list = self._make_list(alert_box)
        self._alert_empty = w.EmptyState(
            alert_box, title=t("无告警"), hint=t("站点与 hosts 当前看起来正常"),
        )

        crash_box = ttk.LabelFrame(right, text=t("最近崩溃"), padding=theme.PAD_SM)
        crash_box.pack(fill="both", expand=True, pady=(0, theme.PAD_SM))
        self._crash_list = self._make_list(crash_box)
        self._crash_empty = w.EmptyState(
            crash_box, title=t("无崩溃记录"), hint=t("近期没有 php-cgi 崩溃事件"),
        )

        log_box = ttk.LabelFrame(right, text=t("最近运行日志"), padding=theme.PAD_SM)
        log_box.pack(fill="both", expand=True)
        self._log_list = self._make_list(log_box)
        self._log_list.bind("<Double-1>", self._goto_run_log)
        self._log_empty = w.EmptyState(
            log_box, title=t("暂无日志"), hint=t("操作服务后会在此显示最近记录"),
        )

    def _make_list(self, parent) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=("text",), show="tree",
                            height=6, selectmode="browse")
        tree.column("#0", width=0, stretch=False)
        tree.column("text", stretch=True)
        tree.pack(fill="both", expand=True)
        return tree

    def _goto(self, key: str) -> None:
        if callable(self.on_navigate):
            try:
                self.on_navigate(key)
            except Exception:  # noqa: BLE001
                pass

    def _goto_run_log(self, _event=None) -> None:
        self._goto("run_log")

    # ------------------------------------------------------------------ #
    def auto_refresh(self) -> None:
        if self._busy:
            return
        self._busy = True

        def worker():
            ov = None
            metrics = None
            err = None
            try:
                # 状态探测只跑一轮（原先 build_overview + collect_metrics 各扫一遍）
                ov, metrics = ovmod.build_dashboard(
                    self.config, self.php_mgr, self.nginx_mgr,
                    self.redis_mgr, self.mysql_mgr, self.vhost_mgr)
            except Exception as e:  # noqa: BLE001
                err = e
            self._queue.put(("data", (ov, metrics, err)))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    def _start_drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        self.after(120, self._drain)

    def _drain(self) -> None:
        try:
            if not self.winfo_exists():
                self._draining = False
                return
            while True:
                try:
                    kind, payload = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._dispatch_one(kind, payload)
                except Exception as e:  # noqa: BLE001
                    run_log.error("ui", t("内部错误：{err}", err=e))
            self.after(120 if self.winfo_ismapped() else 400, self._drain)
        except tk.TclError:
            self._draining = False

    def _dispatch_one(self, kind: str, payload) -> None:
        if kind == "data":
            ov, metrics, err = payload
            if err is not None:
                self._busy = False
                self.notify(t("总览聚合失败：{err}", err=err))
            else:
                self._render(ov, metrics)
        elif kind == "diag_error":
            self._set_action_busy(False)
            self.notify(payload)
        else:  # diag_result
            self._set_action_busy(False)
            err, warn, total = payload
            messagebox.showinfo(
                t("体检结果"),
                t("共 {n} 个站点，错误 {e} 项，警告 {w} 项",
                  n=total, e=err, w=warn), parent=self)

    def refresh_theme(self) -> None:
        """主题热切换：metric 卡样式由 ttk 重建；列表无需自绘。"""
        return

    def _render(self, ov, metrics=None) -> None:
        self._busy = False
        if ov is None:
            return
        if ov.health == "err":
            self._banner.set(
                w.PILL_ERR, t("关键服务未运行，请检查 Nginx / PHP"), t("异常"))
        elif ov.health == "warn":
            self._banner.set(
                w.PILL_WARN,
                t("存在 {n} 项告警", n=len(ov.alerts)),
                t("告警"))
        else:
            self._banner.set(
                w.PILL_OK,
                t("环境正常 · 全部在线 · 共 {n} 个站点", n=ov.site_count),
                t("健康"))

        self._fill_services(ov, metrics)
        self._fill_list(self._alert_list, self._alert_empty,
                        ov.alerts, empty_ok=True)
        self._fill_list(self._crash_list, self._crash_empty,
                        ov.crashes, empty_ok=True)
        self._fill_list(self._log_list, self._log_empty,
                        ov.recent_logs, empty_ok=True)

    def _fill_services(self, ov, metrics=None) -> None:
        res_map: dict = {}
        if metrics:
            for s in metrics.get("services", []):
                res_map[(s["kind"], s["name"])] = s

        def res_hint(kind, name, running: bool) -> str:
            if not running:
                return t("已停止")
            s = res_map.get((kind, name))
            if not s or not s["running"]:
                return ""
            mem = _format_rss(s["rss_bytes"])
            cpu = (f"{s['cpu_percent']:.0f}%" if s["cpu_percent"] >= 0
                   else t("未知"))
            return t("内存 {mem} · CPU {cpu}", mem=mem, cpu=cpu)

        rows = []
        rows.append(("Nginx", ov.nginx.running, ov.nginx.detail or "",
                     "nginx", "Nginx", "nginx"))
        if ov.php:
            for s in ov.php:
                rows.append((f"PHP {s.name}", s.running, s.detail,
                             "php", s.name, "php"))
        else:
            rows.append((t("PHP"), False, t("未检测到版本"), "php", "", "php"))
        for s in ov.redis:
            rows.append((t("Redis") + " " + s.name, s.running, s.detail,
                         "redis", s.name, "redis"))
        for s in ov.mysql:
            rows.append((t("MySQL") + " " + s.name, s.running, s.detail,
                         "mysql", s.name, "mysql"))

        if not rows:
            if self._svc_host.winfo_ismapped():
                self._svc_host.pack_forget()
            self._svc_empty.show()
            return
        self._svc_empty.hide()
        if not self._svc_host.winfo_ismapped():
            self._svc_host.pack(fill="both", expand=True)

        cols = 3
        wanted: set[tuple[str, str]] = set()
        for i, (label, running, detail, kind, rname, nav_key) in enumerate(rows):
            key = (kind, rname)
            wanted.add(key)
            value = t("运行中") if running else t("已停止")
            hint_parts = [p for p in (detail, res_hint(kind, rname, running)) if p]
            hint = " · ".join(hint_parts)
            card = self._svc_cards.get(key)
            if card is None:
                card = w.MetricCard(
                    self._svc_host, label=label, value=value, hint=hint,
                    on_click=(lambda k=nav_key: self._goto(k)),
                )
                self._svc_cards[key] = card
            else:
                card.label_var.set(label)
                card.set(value, hint, animate=True)
            r, c = divmod(i, cols)
            card.grid(row=r, column=c, sticky="nsew",
                      padx=theme.PAD_XS, pady=theme.PAD_XS)

        for key in list(self._svc_cards):
            if key not in wanted:
                self._svc_cards.pop(key).destroy()

        summary = ""
        if metrics:
            tot = metrics.get("totals") or {}
            if tot:
                mem = _format_rss(tot.get("rss_bytes", 0))
                cpu = (f"{tot['cpu_percent']:.0f}%"
                       if tot.get("cpu_percent", -1) >= 0 else t("未知"))
                summary = t("合计 · {n} 个服务运行中 · 内存 {mem} · CPU {cpu}",
                            n=tot.get("service_count", 0), mem=mem, cpu=cpu)
        self._svc_foot_var.set(summary)
        if self._svc_foot is not None:
            if summary:
                foot_row = (len(rows) + cols - 1) // cols
                self._svc_foot.grid(row=foot_row, column=0, columnspan=cols,
                                   sticky="w", pady=(theme.PAD_SM, 0))
            else:
                self._svc_foot.grid_forget()

    def _fill_list(self, tree: ttk.Treeview, empty: w.EmptyState,
                   items: list, *, empty_ok: bool) -> None:
        cache_key = id(tree)
        snapshot = tuple(items or ())
        if self._list_cache.get(cache_key) == snapshot:
            return
        self._list_cache[cache_key] = snapshot

        was_empty = not tree.get_children()
        now_empty = not snapshot

        if now_empty:
            if tree.get_children():
                tree.delete(*tree.get_children())
            if tree.winfo_ismapped():
                tree.pack_forget()
            empty.show()
            return

        if was_empty or not tree.winfo_ismapped():
            empty.hide()
            if not tree.winfo_ismapped():
                tree.pack(fill="both", expand=True)

        tree.delete(*tree.get_children())
        for line in snapshot:
            tree.insert("", "end", values=(line,))

    def _set_action_busy(self, busy: bool) -> None:
        self._action_busy = busy
        w.set_busy(self._action_btns, busy)
        if busy:
            self.notify(t("进行中…"))

    def _start_all(self) -> None:
        if self._action_busy:
            return
        self._set_action_busy(True)
        try:
            msg = self.services.start_all()
        except Exception as e:  # noqa: BLE001
            self._set_action_busy(False)
            self.notify(t("全部启动失败：{err}", err=e))
            return
        self._set_action_busy(False)
        self.notify(msg)
        self.auto_refresh()

    def _stop_all(self) -> None:
        if self._action_busy:
            return
        from .confirm import confirm_stop_all
        if not confirm_stop_all(self.winfo_toplevel()):
            return
        self._set_action_busy(True)
        try:
            msg = self.services.stop_all()
        except Exception as e:  # noqa: BLE001
            self._set_action_busy(False)
            self.notify(t("全部停止失败：{err}", err=e))
            return
        self._set_action_busy(False)
        self.notify(msg)
        self.auto_refresh()

    def _diagnose_all(self) -> None:
        if self._action_busy:
            return
        self._set_action_busy(True)

        def worker():
            try:
                err, warn, total = ovmod.diagnose_all(
                    self.config, self.vhost_mgr, self.php_mgr, self.nginx_mgr)
            except Exception as e:  # noqa: BLE001
                self._queue.put(("diag_error", t("体检失败：{err}", err=e)))
                return
            self._queue.put(("diag_result", (err, warn, total)))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()
