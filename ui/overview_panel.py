# -*- coding: utf-8 -*-
"""总览仪表盘页（F7）。

顶部一行动作：全部启动 / 全部停止 / 体检全部；下方四块：
- 服务状态：Nginx / 各 PHP 版本 / Redis 实例 / MySQL 实例 在线状态
- 告警：未映射 hosts / 端口未映射 / 证书缺失
- 最近崩溃（run_log 中 scope=crash）
- 最近运行日志（run_log 最近 5 条）

数据异步聚合（核心 build_overview 会触发各 manager 状态查询），渲染在主线程。
"""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core import i18n, overview as ovmod
from core import run_log
from core.resource_monitor import format_rss as _format_rss
from . import theme

C_OK = "#2e7d32"
C_WARN = "#ef6c00"
C_ERR = "#c62828"
C_MUTE = "#9aa0a6"


class OverviewPanel(ttk.Frame):
    def __init__(self, master, notify, php_mgr, nginx_mgr, redis_mgr,
                 mysql_mgr, vhost_mgr, config, services):
        super().__init__(master, padding=12)
        self.notify = notify
        self.php_mgr = php_mgr
        self.nginx_mgr = nginx_mgr
        self.redis_mgr = redis_mgr
        self.mysql_mgr = mysql_mgr
        self.vhost_mgr = vhost_mgr
        self.config = config
        self.services = services
        self._busy = False
        self._queue: queue.Queue = queue.Queue()  # 唯一消费者是主线程 drain
        self._draining = False
        self._build()
        self.auto_refresh()
        self._start_drain()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 10))
        ttk.Button(bar, text=i18n.t("全部启动"), command=self._start_all).pack(side="left", padx=4)
        ttk.Button(bar, text=i18n.t("全部停止"), command=self._stop_all).pack(side="left", padx=4)
        ttk.Button(bar, text=i18n.t("体检全部"), command=self._diagnose_all).pack(side="left", padx=4)

        self.banner_var = tk.StringVar(value=i18n.t("加载中…"))
        self.banner = ttk.Label(self, textvariable=self.banner_var, style="Title.TLabel")
        self.banner.pack(anchor="w", pady=(0, 8))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = ttk.Frame(body)
        right.pack(side="right", fill="y", padx=(8, 0))

        self.svc_text = self._section(left, i18n.t("服务状态"), 9)
        self.alert_text = self._section(left, i18n.t("告警"), 8)

        self.crash_text = self._section(right, i18n.t("最近崩溃"), 6, width=42)
        self.log_text = self._section(right, i18n.t("最近运行日志"), 6, width=42)

    def _section(self, parent, title: str, height: int, width: int = 64) -> tk.Text:
        f = ttk.LabelFrame(parent, text=title, padding=8)
        f.pack(fill="both", expand=True, pady=(0, 8))
        txt = tk.Text(f, height=height, width=width, wrap="word",
                      background=theme.LOG_BG, foreground=theme.LOG_FG,
                      font=theme.mono(), state="disabled",
                      relief="flat", padx=8, pady=6)
        txt.pack(fill="both", expand=True)
        txt.tag_configure("ok", foreground=C_OK)
        txt.tag_configure("warn", foreground=C_WARN)
        txt.tag_configure("err", foreground=C_ERR)
        txt.tag_configure("mute", foreground=C_MUTE)
        return txt

    # ------------------------------------------------------------------ #
    def auto_refresh(self) -> None:
        if self._busy:
            return
        self._busy = True

        def worker():
            # 约定：worker 只入队，绝不跨线程碰 Tk —— 连 after() 也不行
            # （启动期主线程还没进 mainloop，after 会抛
            #  "RuntimeError: main thread is not in main loop"，首屏直接丢）
            ov = None
            err = None
            try:
                ov = ovmod.build_overview(
                    self.config, self.php_mgr, self.nginx_mgr,
                    self.redis_mgr, self.mysql_mgr, self.vhost_mgr)
            except Exception as e:  # noqa: BLE001
                err = e
            metrics = None
            try:
                from core import resource_monitor as rm
                metrics = rm.collect_service_metrics(
                    self.config, self.php_mgr, self.nginx_mgr,
                    self.redis_mgr, self.mysql_mgr)
            except Exception:  # noqa: BLE001
                metrics = None
            self._queue.put(("data", (ov, metrics, err)))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    # ------------------------------------------------------------------ #
    # 队列消息分发（唯一消费者：主线程 drain；worker 只 queue.put）
    # ------------------------------------------------------------------ #
    def _start_drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        self.after(120, self._drain)

    def _drain(self) -> None:
        # 整个循环包在 try 里：窗口销毁 / after 失败时必须停摆，不能让异常冒出
        # Tk 回调 —— 否则末尾的重排不执行，drain 链断裂、_draining 永为 True，
        # 队列消息被永久静默丢弃（busy 不复位、批量动作后不再刷新）
        try:
            if not self.winfo_exists():  # 面板已销毁：停止轮询
                self._draining = False
                return
            while True:
                try:
                    kind, payload = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._dispatch_one(kind, payload)
                except Exception as e:  # noqa: BLE001 - 单条消息失败只丢这一条
                    run_log.error("ui", i18n.t("内部错误：{err}", err=e))
            self.after(120 if self.winfo_ismapped() else 400, self._drain)
        except tk.TclError:  # 面板已销毁：排不了定时器，永久停摆
            self._draining = False

    def _dispatch_one(self, kind: str, payload) -> None:
        """处理单条队列消息（异常由调用方兜住，故这里可以放心操作控件）。"""
        if kind == "data":
            ov, metrics, err = payload
            if err is not None:
                self._busy = False
                self.notify(i18n.t("总览聚合失败：{err}", err=err))
            else:
                self._render(ov, metrics)
        elif kind == "diag_error":
            self.notify(payload)
        else:  # diag_result
            err, warn, total = payload
            messagebox.showinfo(
                i18n.t("体检结果"),
                i18n.t("共 {n} 个站点，错误 {e} 项，警告 {w} 项",
                       n=total, e=err, w=warn), parent=self)

    def _render(self, ov, metrics=None) -> None:
        self._busy = False
        if ov is None:
            return
        # 横幅
        if ov.health == "err":
            self.banner_var.set(i18n.t("关键服务未运行，请检查 Nginx / PHP"))
            self.banner.configure(foreground=C_ERR)
        elif ov.health == "warn":
            self.banner_var.set(i18n.t("存在 {n} 项告警", n=len(ov.alerts)))
            self.banner.configure(foreground=C_WARN)
        else:
            self.banner_var.set(i18n.t("环境正常 · 全部在线 · 共 {n} 个站点", n=ov.site_count))
            self.banner.configure(foreground=C_OK)
        self._fill_services(ov, metrics)
        self._fill(self.alert_text, ov.alerts or [i18n.t("无告警")], default_tag="ok")
        self._fill(self.crash_text, ov.crashes or [i18n.t("无")], default_tag="mute")
        self._fill(self.log_text, ov.recent_logs or [i18n.t("无")], default_tag="mute")

    def _fill_services(self, ov, metrics=None) -> None:
        # 资源指标按 (kind, name) 索引，行内附带内存/CPU（F12 轻量资源监控）
        res_map: dict = {}
        if metrics:
            for s in metrics.get("services", []):
                res_map[(s["kind"], s["name"])] = s

        def _res_text(kind, name) -> str:
            s = res_map.get((kind, name))
            if not s or not s["running"]:
                return ""
            mem = _format_rss(s["rss_bytes"])
            cpu = f"{s['cpu_percent']:.0f}%" if s["cpu_percent"] >= 0 else i18n.t("未知")
            return i18n.t("内存 {mem} · CPU {cpu}", mem=mem, cpu=cpu)

        rows = []
        rows.append((i18n.t("Nginx"), ov.nginx.running, ov.nginx.detail, "nginx", "Nginx"))
        if ov.php:
            for s in ov.php:
                rows.append((f"PHP {s.name}", s.running, s.detail, "php", s.name))
        else:
            rows.append((i18n.t("PHP"), False, i18n.t("未检测到版本"), "php", ""))
        for s in ov.redis:
            rows.append((i18n.t("Redis") + " " + s.name, s.running, s.detail, "redis", s.name))
        for s in ov.mysql:
            rows.append((i18n.t("MySQL") + " " + s.name, s.running, s.detail, "mysql", s.name))

        self.svc_text.configure(state="normal")
        self.svc_text.delete("1.0", "end")
        for name, running, detail, kind, rname in rows:
            tag = "ok" if running else "err"
            mark = "● " if running else "○ "
            self.svc_text.insert("end", mark + name, tag)
            if detail:
                self.svc_text.insert("end", "  " + detail, "mute")
            extra = _res_text(kind, rname) if running else ""
            if extra:
                self.svc_text.insert("end", " " + extra, "mute")
            self.svc_text.insert("end", "\n")

        # 合计资源占用（F12）
        if metrics:
            tot = metrics.get("totals", {})
            if tot:
                mem = _format_rss(tot.get("rss_bytes", 0))
                cpu = (f"{tot['cpu_percent']:.0f}%" if tot.get("cpu_percent", -1) >= 0
                       else i18n.t("未知"))
                line = i18n.t("合计 · {n} 个服务运行中 · 内存 {mem} · CPU {cpu}",
                             n=tot.get("service_count", 0), mem=mem, cpu=cpu)
                self.svc_text.insert("end", "\n" + line, "mute")
        self.svc_text.configure(state="disabled")

    def _fill(self, txt: tk.Text, items: list, default_tag: str = "") -> None:
        txt.configure(state="normal")
        txt.delete("1.0", "end")
        for i, line in enumerate(items):
            tag = default_tag
            low = line.lower()
            if "✗" in line or i18n.t("未") in line or "缺失" in line or "未映射" in line:
                tag = "err"
            elif "!" in line or i18n.t("警告") in line:
                tag = "warn"
            if not tag:
                tag = default_tag
            txt.insert("end", ("• " + line + "\n") if tag != "mute" else ("  " + line + "\n"),
                       tag or "mute")
        txt.configure(state="disabled")

    # ------------------------------------------------------------------ #
    def _start_all(self) -> None:
        try:
            msg = self.services.start_all()
        except Exception as e:  # noqa: BLE001
            self.notify(i18n.t("全部启动失败：{err}", err=e))
            return
        self.notify(msg)
        self.auto_refresh()

    def _stop_all(self) -> None:
        try:
            msg = self.services.stop_all()
        except Exception as e:  # noqa: BLE001
            self.notify(i18n.t("全部停止失败：{err}", err=e))
            return
        self.notify(msg)
        self.auto_refresh()

    def _diagnose_all(self) -> None:
        def worker():
            try:
                err, warn, total = ovmod.diagnose_all(
                    self.config, self.vhost_mgr, self.php_mgr, self.nginx_mgr)
            except Exception as e:  # noqa: BLE001
                self._queue.put(("diag_error", i18n.t("体检失败：{err}", err=e)))
                return
            self._queue.put(("diag_result", (err, warn, total)))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()
