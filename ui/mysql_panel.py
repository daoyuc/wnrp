# -*- coding: utf-8 -*-
"""MySQL 管理页：实例选择 + 运行状态卡片 + 启停控制 + 操作/错误日志。

- Windows 上若该实例已注册为服务（如 MySQL），启停走服务接口；未注册则进程模式；
- 服务启停需要管理员权限：非管理员时禁用启停按钮并给出提示，不静默失败。
"""
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from core import process_utils as pu
from core.i18n import t
from core.mysql_manager import MysqlInstance, MysqlManager, is_admin
from .theme import (
    CARD_BG, ERR, FONT, GRAY, LOG_ACCENT, LOG_BG, LOG_FG, OK,
    PRIMARY, TEXT, TEXT_DIM, WARN,
)

# 状态卡信息行：(内部键, 标签 msgid)
_INFO_ROWS = [
    ("version", "版本"),
    ("port", "端口"),
    ("pid", "PID"),
    ("mode", "运行方式"),
    ("service", "服务名"),
    ("start_type", "启动类型"),
    ("conf", "配置文件"),
    ("datadir", "数据目录"),
]

_OP_LABELS = {
    "start": "启动 MySQL",
    "stop": "停止 MySQL",
    "restart": "重启 MySQL",
}


class MysqlPanel(ttk.Frame):
    # 控件跨方法创建，提前声明类型（同 redis_panel）
    instance_var: tk.StringVar
    instance_cb: ttk.Combobox
    dot_label: tk.Label
    state_label: ttk.Label
    info_vars: dict[str, tk.StringVar]
    hint_label: ttk.Label
    log_text: tk.Text
    btn_start: ttk.Button
    btn_stop: ttk.Button
    btn_restart: ttk.Button

    def __init__(self, master, mysql_mgr: MysqlManager, notify):
        super().__init__(master, padding=8)
        self.mysql_mgr = mysql_mgr
        self.notify = notify
        self._queue: queue.Queue[Any] = queue.Queue()
        self._busy = False
        self._build()
        self.refresh_status()
        self._drain()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text=t("MySQL 实例："), font=(FONT, 9, "bold")).pack(side="left")
        self.instance_var = tk.StringVar()
        self.instance_cb = ttk.Combobox(top, textvariable=self.instance_var,
                                        state="readonly", width=20, font=(FONT, 9))
        self.instance_cb.pack(side="left", padx=(4, 10))
        self.instance_cb.bind("<<ComboboxSelected>>", lambda e: self.refresh_status())
        self.instance_cb["values"] = [i.name for i in self.mysql_mgr.instances]
        if self.mysql_mgr.instances:
            self.instance_var.set(self.mysql_mgr.default_instance().name)
        ttk.Button(top, text=t("刷新状态"), command=self.refresh_status).pack(side="left")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        left = ttk.Frame(body)
        left.pack(side="left", fill="y", padx=(0, 10))

        card = ttk.LabelFrame(left, text=t("运行状态"), padding=14)
        card.pack(fill="x")
        head = ttk.Frame(card)
        head.pack(fill="x")
        self.dot_label = tk.Label(head, text="●", font=(FONT, 16, "bold"),
                                  background=CARD_BG, foreground=GRAY)
        self.dot_label.pack(side="left")
        self.state_label = ttk.Label(head, text=t("检测中…"), font=(FONT, 10, "bold"))
        self.state_label.pack(side="left", padx=(8, 0))

        # 信息行用独立容器承载：避免与上方 head 的 pack 混用同一父容器
        grid_box = ttk.Frame(card)
        grid_box.pack(fill="x", pady=(8, 0))
        self.info_vars = {}
        for i, (key, label) in enumerate(_INFO_ROWS):
            ttk.Label(grid_box, text=f"{t(label)}：", font=(FONT, 9, "bold"),
                      background=CARD_BG, width=10, anchor="w").grid(
                row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar(value="—")
            self.info_vars[key] = var
            ttk.Label(grid_box, textvariable=var, font=(FONT, 9),
                      background=CARD_BG, foreground=TEXT_DIM, wraplength=320,
                      justify="left").grid(row=i, column=1, sticky="w", pady=2)

        self.hint_label = ttk.Label(left, text="", style="SubTitle.TLabel",
                                    wraplength=360, justify="left", foreground=WARN)
        self.hint_label.pack(anchor="w", pady=(8, 0))

        btns = ttk.Frame(left)
        btns.pack(anchor="w", pady=(10, 0))
        self.btn_start = ttk.Button(btns, text=t(_OP_LABELS["start"]),
                                    style="Accent.TButton",
                                    command=lambda: self._operate("start"))
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(btns, text=t(_OP_LABELS["stop"]),
                                   command=lambda: self._operate("stop"))
        self.btn_stop.pack(side="left", padx=(0, 6))
        self.btn_restart = ttk.Button(btns, text=t(_OP_LABELS["restart"]),
                                      command=lambda: self._operate("restart"))
        self.btn_restart.pack(side="left", padx=(0, 6))

        btns2 = ttk.Frame(left)
        btns2.pack(anchor="w", pady=(8, 0))
        ttk.Button(btns2, text=t("打开配置文件"), command=self._open_conf).pack(
            side="left", padx=(0, 6))
        ttk.Button(btns2, text=t("打开数据目录"), command=self._open_datadir).pack(
            side="left", padx=(0, 6))
        ttk.Button(btns2, text=t("查看错误日志"), command=self._show_error_log).pack(
            side="left")

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text=t("操作与日志"), style="Section.TLabel").pack(anchor="w")
        self.log_text = tk.Text(
            right, wrap="word", font=("Consolas", 9), background=LOG_BG,
            foreground=LOG_FG, relief="flat", height=18, padx=8, pady=6,
        )
        self.log_text.pack(fill="both", expand=True, pady=(4, 0))
        self.log_text.tag_configure("ok", foreground=OK)
        self.log_text.tag_configure("err", foreground=ERR)
        self.log_text.tag_configure("info", foreground=LOG_ACCENT)
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # 状态刷新
    # ------------------------------------------------------------------ #
    def refresh_status(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        name = self.instance_var.get()

        def worker():
            try:
                if not self.mysql_mgr.instances:
                    self.mysql_mgr.refresh_instances()
                inst = self._find(name) or self.mysql_mgr.default_instance()
                if inst is None:
                    self._queue.put(("status", None))
                    return
                running, pids = self.mysql_mgr.get_status(inst)
                self._queue.put(("status", (inst, running, pids)))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _render_status(self, payload) -> None:
        if payload is None:
            self.dot_label.configure(foreground=GRAY)
            self.state_label.configure(text=t("未发现 MySQL 实例"))
            for key, _ in _INFO_ROWS:
                self.info_vars[key].set("—")
            self.hint_label.configure(
                text=t("未找到含 bin/mysqld 的 mysql 目录（环境根：{root}）。",
                       root=self.mysql_mgr.root))
            self._set_ctrl(False)
            return
        inst, running, pids = payload
        self.dot_label.configure(foreground=OK if running else GRAY)
        self.state_label.configure(
            text=t("运行中") if running else t("已停止"))
        self.info_vars["version"].set(inst.version or "—")
        self.info_vars["port"].set(str(inst.port))
        self.info_vars["pid"].set(", ".join(map(str, pids)) if pids else "—")
        self.info_vars["mode"].set(t("Windows 服务") if inst.mode == "service"
                                   else t("独立进程"))
        self.info_vars["service"].set(inst.service or "—")
        self.info_vars["start_type"].set(inst.start_type or "—")
        self.info_vars["conf"].set(inst.conf or "—")
        self.info_vars["datadir"].set(inst.datadir or "—")

        if inst.mode == "service" and not is_admin():
            self.hint_label.configure(
                text=t("该实例为 Windows 服务，启停需要管理员权限。\n"
                       "请以「管理员身份运行」phpvm 后再操作（当前仅可查看状态）。"))
            self._set_ctrl(False)
        else:
            self.hint_label.configure(text="")
            self._set_ctrl(True)

    def _set_ctrl(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.btn_start.configure(state=state)
        self.btn_stop.configure(state=state)
        self.btn_restart.configure(state=state)

    # ------------------------------------------------------------------ #
    # 操作
    # ------------------------------------------------------------------ #
    def _operate(self, action: str) -> None:
        inst = self._find(self.instance_var.get()) or self.mysql_mgr.default_instance()
        if inst is None:
            return
        label = t(_OP_LABELS[action])
        if action == "stop" and not messagebox.askyesno(
                label, t("停止 MySQL 会影响本机全部站点，确定继续？"), parent=self):
            return
        self._set_busy(True)
        self._append_log(f"> {label}", "info")
        self.notify(t("正在{label}…", label=label))

        def worker():
            try:
                msg = getattr(self.mysql_mgr, action)(inst)
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}：{e}"
            self._queue.put(("op", msg))

        threading.Thread(target=worker, daemon=True).start()

    def _open_conf(self) -> None:
        inst = self._current()
        if not inst:
            return
        if inst.conf and os.path.exists(inst.conf):
            pu.open_path(inst.conf)
        else:
            messagebox.showinfo(t("配置文件"), t("未找到配置文件：{path}",
                                              path=inst.conf or "—"), parent=self)

    def _open_datadir(self) -> None:
        inst = self._current()
        if not inst:
            return
        path = inst.datadir or os.path.join(inst.dir, "data")
        if os.path.isdir(path):
            pu.open_path(path)
        else:
            messagebox.showinfo(t("数据目录"), t("目录不存在：{path}", path=path),
                                parent=self)

    def _show_error_log(self) -> None:
        inst = self._current()
        if not inst:
            return
        self._set_busy(True)
        self._append_log(t("> 读取错误日志…"), "info")

        def worker():
            try:
                text = self.mysql_mgr.tail_error_log(inst)
            except Exception as e:  # noqa: BLE001
                text = f"{type(e).__name__}：{e}"
            self._queue.put(("log", text))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    # 队列 / 辅助
    # ------------------------------------------------------------------ #
    def _drain(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            self.after(120, self._drain)
            return
        self._set_busy(False)
        if kind == "status":
            self._render_status(payload)
        elif kind == "op":
            self._append_log(payload, "err" if t("失败") in payload else "ok")
            self.notify(payload.replace("\n", " ")[:120])
            if t("失败") in payload or t("需要管理员权限") in payload:
                messagebox.showwarning(t("MySQL"), payload, parent=self)
            self.refresh_status()
        elif kind == "log":
            self._append_log(payload, "info")
        elif kind == "error":
            self.state_label.configure(text=t("检测失败"))
            self._append_log(payload, "err")
        self.after(120, self._drain)

    def _append_log(self, text: str, tag: str = "") -> None:
        self.log_text.configure(state="normal")
        start = self.log_text.index("end-1c")
        self.log_text.insert("end", text + "\n")
        if tag:
            self.log_text.tag_add(tag, start, "end-1c")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _find(self, name: str) -> MysqlInstance | None:
        for i in self.mysql_mgr.instances:
            if i.name == name:
                return i
        return None

    def _current(self) -> MysqlInstance | None:
        return self._find(self.instance_var.get()) or self.mysql_mgr.default_instance()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.instance_cb.configure(state="disabled" if busy else "readonly")

    def auto_refresh(self) -> None:
        """主窗口心跳调用（低频，避免频繁 sc 查询）。"""
        if not self.mysql_mgr.instances:
            return
        self.refresh_status()
