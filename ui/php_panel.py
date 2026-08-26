# -*- coding: utf-8 -*-
"""PHP 版本管理页：版本列表（状态灯/版本号/端口/PID/配置）+ 启停/重启/端口/配置操作。

所有耗时操作（扫描、php -v 解析、启停、状态刷新）均在后台线程执行，
通过 queue 回传 UI 线程，避免界面卡死。
"""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core.config import Config
from core.health_monitor import HealthMonitor
from core.php_manager import PhpManager, PhpVersion, PortConflictError
from .dialogs import IniDialog, IniEditDialog, PortDialog, SelfCheckDialog
from .theme import CARD_BG, ERR, FONT, GRAY, OK, PRIMARY, TEXT

COLUMNS = [
    ("status", "状态", 70, "center"),
    ("name", "版本目录", 110, "w"),
    ("ver", "PHP 版本", 90, "center"),
    ("port", "端口", 80, "center"),
    ("pid", "PID", 90, "center"),
    ("ini", "配置文件", 300, "w"),
]


class PhpPanel(ttk.Frame):
    def __init__(self, master, php_mgr: PhpManager, config: Config, notify):
        super().__init__(master, padding=8)
        self.php_mgr = php_mgr
        self.config = config
        self.notify = notify

        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._versions: list[PhpVersion] = []
        self._name_to_iid: dict[str, str] = {}
        self._pending_row_refresh = False

        self._build()
        self.refresh_versions()

    # ------------------------------------------------------------------ #
    # 界面构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        self.btn_start = ttk.Button(bar, text="启动", style="Accent.TButton", command=lambda: self._operate("start"))
        self.btn_stop = ttk.Button(bar, text="停止", style="Danger.TButton", command=lambda: self._operate("stop"))
        self.btn_restart = ttk.Button(bar, text="重启", command=lambda: self._operate("restart"))
        self.btn_port = ttk.Button(bar, text="编辑端口", command=self._edit_port)
        self.btn_ini = ttk.Button(bar, text="查看配置", command=self._view_ini)
        self.btn_edit = ttk.Button(bar, text="编辑配置", command=self._edit_ini)
        self.btn_check = ttk.Button(bar, text="自检", command=self._self_check)
        self.btn_refresh = ttk.Button(bar, text="刷新", command=self.refresh_versions)
        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_port,
                  self.btn_ini, self.btn_edit, self.btn_check, self.btn_refresh):
            b.pack(side="left", padx=(0, 6))
        ttk.Label(bar, text="选中版本后操作 · 双击行查看配置", style="SubTitle.TLabel").pack(
            side="left", padx=(4, 0)
        )

        # 表格
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            wrap, columns=[c[0] for c in COLUMNS], show="headings", selectmode="browse"
        )
        for col, text, width, anchor in COLUMNS:
            self.tree.heading(col, text=text)
            self.tree.column(
                col, width=width, anchor=anchor,
                stretch=(col == "ini"), minwidth=60,
            )
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("dot_run", foreground=OK)
        self.tree.tag_configure("dot_stop", foreground=GRAY)
        self.tree.tag_configure("dot_err", foreground=ERR)
        self.tree.tag_configure("odd", background="#FAFBFC")
        self.tree.tag_configure("even", background=CARD_BG)
        self.tree.bind("<Double-1>", lambda e: self._view_ini())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())

    # ------------------------------------------------------------------ #
    # 数据加载 / 渲染
    # ------------------------------------------------------------------ #
    def refresh_versions(self) -> None:
        """全量扫描 + 版本解析 + 状态刷新（后台线程）。"""
        if self._busy:
            return
        self._set_busy(True)
        self.notify("正在扫描 PHP 版本…")

        def worker():
            try:
                versions = self.php_mgr.scan_versions()
                versions = self.php_mgr.resolve(refresh_status=True, fast=True)
                self._queue.put(("versions", versions))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", f"扫描失败：{e}"))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_versions()

    def _poll_versions(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            self.after(80, self._poll_versions)
            return
        self._set_busy(False)
        if kind == "versions":
            self._render(payload)
            self.notify(f"已发现 {len(payload)} 个 PHP 版本")
        else:
            messagebox.showerror("错误", payload, parent=self)
            self.notify("扫描失败")

    def _render(self, versions: list[PhpVersion]) -> None:
        self._versions = versions
        self._name_to_iid.clear()
        self.tree.delete(*self.tree.get_children())
        for i, v in enumerate(versions):
            running, pid = v.running, v.pid
            dot, tag = ("●", "dot_run") if running else ("○", "dot_stop")
            tags = [tag, "odd" if i % 2 else "even"]
            iid = self.tree.insert(
                "", "end",
                values=(
                    dot, v.name, v.display, v.port,
                    pid if pid else "—",
                    v.ini,
                ),
                tags=tags,
            )
            self._name_to_iid[v.name] = iid
        self._update_buttons()

    # ------------------------------------------------------------------ #
    # 启停 / 重启
    # ------------------------------------------------------------------ #
    def _operate(self, action: str) -> None:
        v = self._selected()
        if v is None:
            messagebox.showinfo("提示", "请先在列表中选择一个 PHP 版本。", parent=self)
            return
        if self._busy:
            return
        action_text = {"start": "启动", "stop": "停止", "restart": "重启"}[action]
        self._set_busy(True)
        self.notify(f"正在{action_text} [{v.name}] …")

        def worker():
            try:
                if action == "start":
                    msg = self.php_mgr.start(v)
                elif action == "stop":
                    msg = self.php_mgr.stop(v)
                else:
                    msg = self.php_mgr.restart(v)
                self._queue.put(("op", (v.name, msg)))
            except PortConflictError as e:
                self._queue.put(("conflict", (v.name, str(e))))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", (v.name, f"{action_text}失败：{e}")))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_op()

    def _poll_op(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            self.after(80, self._poll_op)
            return
        self._set_busy(False)
        name, msg = payload
        if kind == "op":
            self.notify(msg)
            messagebox.showinfo("操作完成", msg, parent=self)
        elif kind == "conflict":
            self.notify(msg)
            messagebox.showwarning("端口冲突", msg, parent=self)
        else:
            self.notify(msg)
            messagebox.showerror("操作失败", msg, parent=self)
        self._refresh_row(name)

    # ------------------------------------------------------------------ #
    # 轻量状态刷新（定时器）
    # ------------------------------------------------------------------ #
    def auto_refresh(self) -> None:
        if self._busy or not self._versions or self._pending_row_refresh:
            return
        self._pending_row_refresh = True

        def worker():
            # 批量刷新：一次 TCP 快照 + 一次进程快照内完成全部版本状态判定
            try:
                self.php_mgr.refresh_all_status(self._versions, fast=True)
                results = {v.name: (v.running, v.pid) for v in self._versions}
            except Exception:  # noqa: BLE001
                results = {}
            self._queue.put(("status", results))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_status()

    def _poll_status(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            self.after(80, self._poll_status)
            return
        self._pending_row_refresh = False
        if kind != "status":
            return
        results = payload
        changed = False
        for name, (running, pid) in results.items():
            v = next((x for x in self._versions if x.name == name), None)
            if v and (v.running != running or v.pid != pid):
                v.running, v.pid = running, pid
                changed = True
        if changed:
            self._update_rows()

    def _refresh_row(self, name: str) -> None:
        def worker():
            v = next((x for x in self._versions if x.name == name), None)
            if v is None:
                return
            try:
                v.running, v.pid = self.php_mgr.get_status(v, fast=True)
            except Exception:  # noqa: BLE001
                pass
            self._queue.put(("row", v))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_row()

    def _poll_row(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            self.after(80, self._poll_row)
            return
        if kind == "row":
            self._update_rows()

    def _update_rows(self) -> None:
        for i, v in enumerate(self._versions):
            iid = self._name_to_iid.get(v.name)
            if not iid:
                continue
            running, pid = v.running, v.pid
            dot, tag = ("●", "dot_run") if running else ("○", "dot_stop")
            tags = [tag, "odd" if i % 2 else "even"]
            self.tree.item(
                iid,
                values=(dot, v.name, v.display, v.port, pid if pid else "—", v.ini),
                tags=tags,
            )
        self._update_buttons()

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def _selected(self) -> PhpVersion | None:
        sel = self.tree.selection()
        if not sel:
            return None
        for v in self._versions:
            if self._name_to_iid.get(v.name) == sel[0]:
                return v
        return None

    def _update_buttons(self) -> None:
        has_sel = self._selected() is not None and not self._busy
        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_port,
                  self.btn_ini, self.btn_edit, self.btn_check):
            b.configure(state="normal" if has_sel else "disabled")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_refresh.configure(state="disabled" if busy else "normal")
        self._update_buttons()

    # ------------------------------------------------------------------ #
    # 端口 / 配置
    # ------------------------------------------------------------------ #
    def _edit_port(self) -> None:
        v = self._selected()
        if v is None:
            return
        PortDialog(self, v, self.config, on_saved=self.refresh_versions)

    def _view_ini(self) -> None:
        v = self._selected()
        if v is None:
            return
        IniDialog(self, v, self.php_mgr)

    def _edit_ini(self) -> None:
        v = self._selected()
        if v is None:
            return
        IniEditDialog(self, v, self.php_mgr)

    def _self_check(self) -> None:
        v = self._selected()
        if v is None:
            return
        SelfCheckDialog(self, v, HealthMonitor())
