# -*- coding: utf-8 -*-
"""Nginx 管理页：状态卡片 + 控制按钮 + 命令输出日志区。"""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core.nginx_manager import NginxManager
from .theme import ERR, FONT, GRAY, LOG_ACCENT, LOG_BG, LOG_FG, OK, PRIMARY_DARK, WARN


class NginxPanel(ttk.Frame):
    def __init__(self, master, nginx_mgr: NginxManager, notify):
        super().__init__(master, padding=8)
        self.nginx_mgr = nginx_mgr
        self.notify = notify

        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._pending_refresh = False
        self._running = False

        self._build()
        self.refresh_status()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        # 左侧：状态卡片 + 控制按钮
        left = ttk.Frame(self)
        left.pack(side="left", fill="y", padx=(0, 10))

        card = ttk.LabelFrame(left, text="运行状态", padding=14)
        card.pack(fill="x")
        self.dot_label = ttk.Label(card, text="●", font=(FONT, 16, "bold"), foreground=GRAY)
        self.dot_label.pack(anchor="w")
        self.state_label = ttk.Label(card, text="检测中…", font=(FONT, 12, "bold"), foreground=PRIMARY_DARK)
        self.state_label.pack(anchor="w", pady=(4, 8))

        info_grid = ttk.Frame(card)
        info_grid.pack(anchor="w")
        self.info_vars = {}
        for i, (k, _) in enumerate(INFO_ROWS):
            ttk.Label(info_grid, text=f"{k}：", font=(FONT, 9, "bold")).grid(
                row=i, column=0, sticky="e", pady=2
            )
            var = tk.StringVar(value="—")
            self.info_vars[k] = var
            ttk.Label(info_grid, textvariable=var, font=(FONT, 9)).grid(
                row=i, column=1, sticky="w", padx=(6, 0), pady=2
            )

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(10, 0))
        self.btn_start = ttk.Button(btns, text="启动 Nginx", style="Accent.TButton", command=lambda: self._run("start"))
        self.btn_start.pack(fill="x", pady=(0, 6))
        self.btn_reload = ttk.Button(btns, text="平滑重载", command=lambda: self._run("reload"))
        self.btn_reload.pack(fill="x", pady=(0, 6))
        self.btn_test = ttk.Button(btns, text="配置检查 (nginx -t)", command=lambda: self._run("test"))
        self.btn_test.pack(fill="x", pady=(0, 6))
        self.btn_stop = ttk.Button(btns, text="停止 Nginx", style="Danger.TButton", command=lambda: self._run("stop"))
        self.btn_stop.pack(fill="x", pady=(0, 6))
        self.btn_nrefresh = ttk.Button(btns, text="刷新状态", command=self.refresh_status)
        self.btn_nrefresh.pack(fill="x")

        ttk.Label(
            left,
            text="提示：修改 vhost 配置后点「配置检查」\n验证通过，再点「平滑重载」生效。",
            style="SubTitle.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(12, 0))

        # 右侧：命令输出日志
        right = ttk.Frame(self)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="命令输出", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        log_wrap = ttk.Frame(right)
        log_wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            log_wrap, wrap="word", font=("Consolas", 9),
            background=LOG_BG, foreground=LOG_FG,
            relief="flat", padx=10, pady=8, state="disabled",
        )
        vsb = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.log_text.tag_configure("ok", foreground=OK)
        self.log_text.tag_configure("err", foreground=ERR)
        self.log_text.tag_configure("warn", foreground=WARN)
        self.log_text.tag_configure("info", foreground=LOG_ACCENT)

        self._append_log("== phpvm Nginx 管理器 ==", "info")
        self._append_log(f"可执行文件：{self.nginx_mgr.exe}", "info")
        self._append_log(f"前缀目录  ：{self.nginx_mgr.prefix}", "info")

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
                self._queue.put(("error", f"状态获取失败：{e}"))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_status()

    def _poll_status(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            self.after(80, self._poll_status)
            return
        self._set_busy(False)
        if kind == "status":
            running, pids, version = payload
            self._render_status(running, pids, version)
        elif kind == "error":
            messagebox.showerror("错误", payload, parent=self)

    def _render_status(self, running: bool, pids: list[int], version: str) -> None:
        self._running = running
        if running:
            self.dot_label.configure(text="●", foreground=OK)
            self.state_label.configure(text="运行中", foreground=OK)
            self.info_vars["PID"].set(", ".join(map(str, pids)) if pids else "—")
        else:
            self.dot_label.configure(text="○", foreground=GRAY)
            self.state_label.configure(text="已停止", foreground=GRAY)
            self.info_vars["PID"].set("—")
        self.info_vars["版本"].set(version)
        self.info_vars["前缀"].set(self.nginx_mgr.prefix)

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
        self._poll_auto()

    def _poll_auto(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            self.after(80, self._poll_auto)
            return
        self._pending_refresh = False
        if kind == "auto":
            running, pids = payload
            self._render_status(running, pids, self.info_vars["版本"].get())

    # ------------------------------------------------------------------ #
    def _run(self, action: str) -> None:
        if self._busy:
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
                self._queue.put(("error", f"{action} 失败：{e}"))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_op()

    def _poll_op(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            self.after(80, self._poll_op)
            return
        self._set_busy(False)
        if kind == "error":
            messagebox.showerror("错误", payload, parent=self)
            self._append_log(payload, "err")
            return
        action, msg = payload
        if action == "test":
            # 配置检查：输出全量写入日志，并判断结果
            self._append_log("── 配置检查 (nginx -t) ──", "info")
            self._append_log(msg, "ok" if "failed" not in msg.lower() else "err")
        else:
            ok = "成功" in msg and "失败" not in msg
            tag = "ok" if ok else ("warn" if "未在运行" in msg or "已在运行" in msg else "err")
            self._append_log(f"[{action}] {msg}", tag)
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


INFO_ROWS = [("PID", ""), ("版本", ""), ("前缀", "")]
