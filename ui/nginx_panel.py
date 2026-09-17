# -*- coding: utf-8 -*-
"""Nginx 管理页：状态卡片 + 控制按钮 + 命令输出日志区。"""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core.i18n import t
from core.nginx_manager import NginxManager
from . import theme

# 左侧信息卡：行 ID -> (显示标签 msgid, 占位)
_INFO_ROWS = [("pid", "PID"), ("ver", "版本"), ("prefix", "前缀")]


class NginxPanel(ttk.Frame):
    def __init__(self, master, nginx_mgr: NginxManager, notify, on_new_site=None):
        super().__init__(master, padding=8)
        self.nginx_mgr = nginx_mgr
        self.notify = notify
        self.on_new_site = on_new_site

        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._pending_refresh = False
        self._running = False
        self._draining = False  # 队列 drain 是否已启动（唯一消费者）

        self._build()
        self.refresh_status()
        self._start_drain()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        # 左侧：状态卡片 + 控制按钮
        left = ttk.Frame(self)
        left.pack(side="left", fill="y", padx=(0, 10))

        card = ttk.LabelFrame(left, text=t("运行状态"), padding=14)
        card.pack(fill="x")
        self.dot_label = ttk.Label(card, text="●", font=(theme.FONT, 16, "bold"), foreground=theme.GRAY)
        self.dot_label.pack(anchor="w")
        self.state_label = ttk.Label(card, text=t("检测中…"), font=(theme.FONT, 12, "bold"),
                                     foreground=theme.PRIMARY_DARK)
        self.state_label.pack(anchor="w", pady=(4, 8))

        info_grid = ttk.Frame(card)
        info_grid.pack(anchor="w")
        self.info_vars = {}
        for i, (rid, label) in enumerate(_INFO_ROWS):
            ttk.Label(info_grid, text=f"{t(label)}：", font=(theme.FONT, 9, "bold")).grid(
                row=i, column=0, sticky="e", pady=2
            )
            var = tk.StringVar(value="—")
            self.info_vars[rid] = var
            ttk.Label(info_grid, textvariable=var, font=(theme.FONT, 9)).grid(
                row=i, column=1, sticky="w", padx=(6, 0), pady=2
            )

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(10, 0))
        if self.on_new_site:
            self.btn_new_site = ttk.Button(btns, text=t("＋ 新建站点向导…"),
                                           style="Accent.TButton",
                                           command=self.on_new_site)
            self.btn_new_site.pack(fill="x", pady=(0, 6))
        self.btn_start = ttk.Button(btns, text=t("启动 Nginx"), style="Accent.TButton",
                                    command=lambda: self._run("start"))
        self.btn_start.pack(fill="x", pady=(0, 6))
        self.btn_reload = ttk.Button(btns, text=t("平滑重载"), command=lambda: self._run("reload"))
        self.btn_reload.pack(fill="x", pady=(0, 6))
        self.btn_test = ttk.Button(btns, text=t("配置检查 (nginx -t)"),
                                   command=lambda: self._run("test"))
        self.btn_test.pack(fill="x", pady=(0, 6))
        self.btn_stop = ttk.Button(btns, text=t("停止 Nginx"), style="Danger.TButton",
                                   command=lambda: self._run("stop"))
        self.btn_stop.pack(fill="x", pady=(0, 6))
        self.btn_nrefresh = ttk.Button(btns, text=t("刷新状态"), command=self.refresh_status)
        self.btn_nrefresh.pack(fill="x")

        ttk.Label(
            left,
            text=t("提示：修改 vhost 配置后点「配置检查」\n验证通过，再点「平滑重载」生效。"),
            style="SubTitle.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(12, 0))

        # 右侧：命令输出日志
        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text=t("命令输出"), style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        log_wrap = ttk.Frame(right)
        log_wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            log_wrap, wrap="word", font=theme.mono(),
            background=theme.LOG_BG, foreground=theme.LOG_FG,
            relief="flat", padx=10, pady=8, state="disabled",
        )
        vsb = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.log_text.tag_configure("ok", foreground=theme.OK)
        self.log_text.tag_configure("err", foreground=theme.ERR)
        self.log_text.tag_configure("warn", foreground=theme.WARN)
        self.log_text.tag_configure("info", foreground=theme.LOG_ACCENT)

        self._append_log(t("== phpvm Nginx 管理器 =="), "info")
        self._append_log(f"{t('可执行文件')}：{self.nginx_mgr.exe}", "info")
        self._append_log(f"{t('前缀目录')}  ：{self.nginx_mgr.prefix}", "info")

    # ------------------------------------------------------------------ #
    def refresh_status(self) -> None:
        if self._busy:
            return
        self._set_busy(True)

        def worker():
            try:
                running, pids = self.nginx_mgr.get_status()
                version = self.nginx_mgr.get_version()
                self._queue.put(("status", (running, pids, version)))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", t("状态获取失败：{err}", err=e)))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    # ------------------------------------------------------------------ #
    # 队列分发：worker 线程只负责 put，UI 操作全部收敛到主线程的 drain
    # 循环（唯一消费者）—— 此前自动刷新 / 手动刷新 / 启停三个 after 轮询
    # 抢同一个队列，会互相吞掉消息，导致状态永久刷不出来。
    # ------------------------------------------------------------------ #
    def _start_drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        self.after(80, self._drain)

    def _drain(self) -> None:
        if not self.winfo_exists():
            self._draining = False
            return
        try:
            while True:
                self._dispatch(*self._queue.get_nowait())
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _dispatch(self, kind: str, payload) -> None:
        if kind == "status":
            self._set_busy(False)
            running, pids, version = payload
            self._render_status(running, pids, version)
        elif kind == "auto":
            self._pending_refresh = False
            running, pids = payload
            self._render_status(running, pids, self.info_vars["ver"].get())
        elif kind == "op":
            self._set_busy(False)
            self._on_op_done(*payload)
        elif kind == "error":
            self._set_busy(False)
            messagebox.showerror(t("错误"), payload, parent=self)
            self._append_log(payload, "err")

    def _render_status(self, running: bool, pids: list[int], version: str) -> None:
        self._running = running
        if running:
            self.dot_label.configure(text="●", foreground=theme.OK)
            self.state_label.configure(text=t("运行中"), foreground=theme.OK)
            self.info_vars["pid"].set(", ".join(map(str, pids)) if pids else "—")
        else:
            self.dot_label.configure(text="○", foreground=theme.GRAY)
            self.state_label.configure(text=t("已停止"), foreground=theme.GRAY)
            self.info_vars["pid"].set("—")
        self.info_vars["ver"].set(version)
        self.info_vars["prefix"].set(self.nginx_mgr.prefix)

    # ------------------------------------------------------------------ #
    def auto_refresh(self) -> None:
        if self._busy or self._pending_refresh:
            return
        self._pending_refresh = True

        def worker():
            try:
                running, pids = self.nginx_mgr.get_status()
                self._queue.put(("auto", (running, pids)))
            except Exception:  # noqa: BLE001
                self._queue.put(("auto", (False, [])))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    # ------------------------------------------------------------------ #
    def _run(self, action: str) -> None:
        if self._busy:
            return
        if action == "stop" and not messagebox.askyesno(
                t("停止 Nginx"),
                t("停止 Nginx 后本机全部站点将不可访问，确定继续？"), parent=self):
            return
        self._set_busy(True)

        def worker():
            try:
                if action == "start":
                    msg = self.nginx_mgr.start()
                elif action == "stop":
                    msg = self.nginx_mgr.stop()
                elif action == "reload":
                    msg = self.nginx_mgr.reload()
                else:
                    msg = self.nginx_mgr.test_config()
                self._queue.put(("op", (action, msg)))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", t("{action} 失败：{err}", action=action, err=e)))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    def _on_op_done(self, action: str, msg: str) -> None:
        action_disp = {"start": t("启动"), "stop": t("停止"),
                       "reload": t("平滑重载"), "test": t("配置检查")}.get(action, action)
        if action == "test":
            # 配置检查：输出全量写入日志，并判断结果
            self._append_log("── " + t("配置检查 (nginx -t)") + " ──", "info")
            self._append_log(msg, "ok" if "failed" not in msg.lower() else "err")
        else:
            ok = t("成功") in msg and t("失败") not in msg
            tag = "ok" if ok else ("warn" if t("未在运行") in msg or t("已在运行") in msg else "err")
            self._append_log(f"[{action_disp}] {msg}", tag)
        self.notify(msg)
        self.refresh_status()

    # ------------------------------------------------------------------ #
    def _append_log(self, text: str, tag: str = "") -> None:
        self.log_text.configure(state="normal")
        start = self.log_text.index("end-1c")
        self.log_text.insert("end", text + "\n")
        if tag:
            self.log_text.tag_add(tag, start, "end-1c")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for b in (self.btn_start, self.btn_stop, self.btn_reload, self.btn_test, self.btn_nrefresh):
            b.configure(state="disabled" if busy else "normal")
