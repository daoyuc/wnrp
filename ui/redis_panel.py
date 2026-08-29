# -*- coding: utf-8 -*-
"""Redis 管理页：多实例选择 + 状态卡片 + 控制按钮 + 命令输出日志区。"""
import os
import queue
import re
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from core.redis_manager import RedisInstance, RedisManager  # pyright: ignore[reportImplicitRelativeImport]
from .theme import ERR, FONT, GRAY, LOG_ACCENT, LOG_BG, LOG_FG, OK, PRIMARY_DARK, WARN


class RedisPanel(ttk.Frame):
    def __init__(self, master, redis_mgr: RedisManager, notify):
        super().__init__(master, padding=8)
        self.redis_mgr = redis_mgr
        self.notify = notify

        self._queue: queue.Queue[Any] = queue.Queue()
        self._busy = False
        self._pending_refresh = False

        self._build()
        self.refresh_status()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        # 顶部：实例选择
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Redis 实例：", font=(FONT, 9, "bold")).pack(side="left")
        self.instance_var = tk.StringVar()
        self.instance_cb = ttk.Combobox(top, textvariable=self.instance_var, state="readonly",
                                        width=24, font=(FONT, 9))
        self.instance_cb.pack(side="left", padx=(4, 10))
        self.instance_cb.bind("<<ComboboxSelected>>", lambda e: self._on_select())
        self.instance_cb["values"] = [i.name for i in self.redis_mgr.instances]
        self.instance_var.set(self._default_name())
        ttk.Button(top, text="刷新状态", command=self.refresh_status).pack(side="left")

        # 左侧：状态卡片 + 控制按钮
        left = ttk.Frame(self)
        left.pack(side="left", fill="y", padx=(0, 10))

        card = ttk.LabelFrame(left, text="运行状态", padding=14)
        card.pack(fill="x")
        self.dot_label = ttk.Label(card, text="●", font=(FONT, 16, "bold"), foreground=GRAY)
        self.dot_label.pack(anchor="w")
        self.state_label = ttk.Label(card, text="检测中…", font=(FONT, 12, "bold"),
                                     foreground=PRIMARY_DARK)
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
        self.btn_start = ttk.Button(btns, text="启动 Redis", style="Accent.TButton",
                                    command=lambda: self._run("start"))
        self.btn_start.pack(fill="x", pady=(0, 6))
        self.btn_restart = ttk.Button(btns, text="重启", command=lambda: self._run("restart"))
        self.btn_restart.pack(fill="x", pady=(0, 6))
        self.btn_stop = ttk.Button(btns, text="停止 Redis", style="Danger.TButton",
                                   command=lambda: self._run("stop"))
        self.btn_stop.pack(fill="x", pady=(0, 6))
        self.btn_ping = ttk.Button(btns, text="测试连接 (PING)", command=lambda: self._run("ping"))
        self.btn_ping.pack(fill="x", pady=(0, 6))
        self.btn_conf = ttk.Button(btns, text="打开配置文件", command=self._open_conf)
        self.btn_conf.pack(fill="x", pady=(0, 6))
        self.btn_rrefresh = ttk.Button(btns, text="刷新状态", command=self.refresh_status)
        self.btn_rrefresh.pack(fill="x")

        ttk.Label(
            left,
            text="提示：Redis 监听端口在各自配置文件中\n（port 项），修改后重启 Redis 生效。",
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

        self._append_log("== phpvm Redis 管理器 ==", "info")
        if self.redis_mgr.instances:
            for inst in self.redis_mgr.instances:
                self._append_log(
                    f"发现实例 [{inst.name}]：{inst.server}（端口 {inst.port}）", "info"
                )
        else:
            self._append_log("未在 C:\\wnrp 下找到 redis-server.exe 实例", "warn")

    # ------------------------------------------------------------------ #
    # 实例选择
    # ------------------------------------------------------------------ #
    @staticmethod
    def _version_key(name: str) -> tuple[int, ...]:
        """从实例目录名提取版本号用于排序，如 Redis-8.4.4 → (8,4,4)。"""
        nums = re.findall(r"\d+", name)
        return tuple(int(x) for x in nums) if nums else (0,)

    def _default_name(self) -> str:
        """默认选中实例：版本号最高的目录（Redis-8.4.4 > Redis）。"""
        if not self.redis_mgr.instances:
            return ""
        return max(self.redis_mgr.instances, key=lambda i: self._version_key(i.name)).name

    def _instance(self) -> RedisInstance | None:
        name = self.instance_var.get()
        for inst in self.redis_mgr.instances:
            if inst.name == name:
                return inst
        # 当前值不在实例列表中（如尚未设置）→ 回退默认高版本
        default = self._default_name()
        for inst in self.redis_mgr.instances:
            if inst.name == default:
                return inst
        return self.redis_mgr.instances[0] if self.redis_mgr.instances else None

    def _on_select(self) -> None:
        inst = self._instance()
        if inst is None:
            return
        self._append_log(f"已选择实例 [{inst.name}]（端口 {inst.port}）", "info")
        self.refresh_status()

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def refresh_status(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        if not self.redis_mgr.instances:
            self._set_busy(False)
            return

        def worker():
            try:
                inst = self._instance()
                data: dict[str, tuple[bool, list[int]]] = {}
                for i in self.redis_mgr.instances:
                    running, pids = self.redis_mgr.get_status(i)
                    data[i.name] = (running, pids)
                ver = self.redis_mgr.get_version(inst) if inst else ""
                self._queue.put(("status", (inst.name if inst else "", data, ver)))
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
            sel_name, data, ver = payload
            names = [i.name for i in self.redis_mgr.instances]
            if not self.instance_var.get() or self.instance_var.get() not in names:
                self.instance_var.set(self._default_name())
            self._render_status(sel_name, data, ver)
        elif kind == "error":
            messagebox.showerror("错误", payload, parent=self)

    def _render_status(self, sel_name: str, data: dict[str, tuple[bool, list[int]]], ver: str) -> None:
        inst = self._instance()
        if inst is None or inst.name not in data:
            return
        running, pids = data[inst.name]
        if running:
            self.dot_label.configure(text="●", foreground=OK)
            self.state_label.configure(text="运行中", foreground=OK)
            self.info_vars["PID"].set(", ".join(map(str, pids)) if pids else "—")
        else:
            self.dot_label.configure(text="○", foreground=GRAY)
            self.state_label.configure(text="已停止", foreground=GRAY)
            self.info_vars["PID"].set("—")
        self.info_vars["版本"].set(ver)
        self.info_vars["端口"].set(str(inst.port))
        self.info_vars["配置"].set(inst.conf or "—")
        self.info_vars["数据目录"].set(inst.dir)

    # ------------------------------------------------------------------ #
    def auto_refresh(self) -> None:
        if self._busy or self._pending_refresh or not self.redis_mgr.instances:
            return
        self._pending_refresh = True

        def worker():
            try:
                data = {}
                for i in self.redis_mgr.instances:
                    running, pids = self.redis_mgr.get_status(i)
                    data[i.name] = (running, pids)
                self._queue.put(("auto", data))
            except Exception:  # noqa: BLE001
                self._queue.put(("auto", {}))

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
            inst = self._instance()
            if inst is not None and inst.name in payload:
                running, pids = payload[inst.name]
                self._render_status(inst.name, payload, self.info_vars["版本"].get())

    # ------------------------------------------------------------------ #
    # 操作
    # ------------------------------------------------------------------ #
    def _run(self, action: str) -> None:
        if self._busy:
            return
        inst = self._instance()
        if inst is None:
            messagebox.showwarning("提示", "未发现 Redis 实例。", parent=self)
            return
        self._set_busy(True)

        def worker():
            try:
                msg = getattr(self.redis_mgr, action)(inst)
                if action == "ping" and not msg:
                    msg = "未找到 redis-cli.exe，无法测试连接"
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
        if action == "ping":
            tag = "ok" if "PONG" in msg else "warn"
            self._append_log(f"[PING] {msg}", tag)
        else:
            ok = "成功" in msg and "失败" not in msg
            tag = "ok" if ok else ("warn" if "未在运行" in msg or "已在运行" in msg else "err")
            self._append_log(f"[{action}] {msg}", tag)
        self.notify(msg)
        self.refresh_status()

    # ------------------------------------------------------------------ #
    def _open_conf(self) -> None:
        inst = self._instance()
        if inst is None or not inst.conf:
            messagebox.showwarning("提示", "未找到配置文件。", parent=self)
            return
        try:
            os.startfile(inst.conf)  # noqa: S606 - 用系统默认程序打开文本文件
        except OSError as e:
            messagebox.showerror("错误", f"打开配置失败：{e}", parent=self)

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
        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_ping,
                  self.btn_conf, self.btn_rrefresh):
            b.configure(state="disabled" if busy else "normal")


INFO_ROWS = [("PID", ""), ("版本", ""), ("端口", ""), ("配置", ""), ("数据目录", "")]
