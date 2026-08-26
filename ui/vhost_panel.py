# -*- coding: utf-8 -*-
"""站点映射页：nginx server 块 ↔ FastCGI 端口 ↔ PHP 版本矩阵。

- 展示域名 / 配置文件 / fastcgi_pass 端口 / 反查 PHP 版本 / 项目 root；
- 异常项红色高亮：端口未映射到任何已配置 PHP 版本；
- 双击行打开对应 nginx 配置文件；工具栏支持刷新与打开 vhost 目录。
"""
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core.config import WNRP_ROOT
from core.vhost_manager import VhostEntry, VhostManager
from .theme import CARD_BG, ERR, TEXT

COLUMNS = [
    ("server_name", "域名", 230, "w"),
    ("file", "配置文件", 130, "w"),
    ("port", "端口", 70, "center"),
    ("php", "PHP 版本", 90, "center"),
    ("root", "项目根目录", 330, "w"),
    ("note", "说明", 170, "w"),
]
NGINX_CONF_DIR = os.path.join(WNRP_ROOT, "nginx", "conf")


class VhostPanel(ttk.Frame):
    """站点映射页签。"""

    def __init__(self, master, vhost_mgr: VhostManager, notify):
        super().__init__(master, padding=8)
        self.vhost_mgr = vhost_mgr
        self.notify = notify
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._entries: list[VhostEntry] = []

        self._build()
        self.refresh()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        self.btn_refresh = ttk.Button(bar, text="刷新", command=self.refresh)
        self.btn_open_dir = ttk.Button(bar, text="打开 vhost 目录", command=self._open_dir)
        self.btn_open_dir.pack(side="left", padx=(0, 6))
        self.btn_refresh.pack(side="left", padx=(0, 6))
        ttk.Label(
            bar,
            text="nginx 中 fastcgi_pass 端口 ↔ phpvm 各版本端口映射；双击行打开配置文件",
            style="SubTitle.TLabel",
        ).pack(side="left", padx=(4, 0))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            wrap, columns=[c[0] for c in COLUMNS], show="headings", selectmode="browse"
        )
        for col, text, width, anchor in COLUMNS:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "root"), minwidth=50)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("ok", foreground=TEXT)
        self.tree.tag_configure("warn", foreground=ERR)
        self.tree.tag_configure("odd", background="#FAFBFC")
        self.tree.tag_configure("even", background=CARD_BG)
        self.tree.bind("<Double-1>", lambda e: self._open_config())

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self.notify("正在扫描 nginx 配置…")

        def worker():
            try:
                entries = self.vhost_mgr.scan()
                self._queue.put(("entries", entries))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self._poll()

    def _poll(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            self.after(80, self._poll)
            return
        self._set_busy(False)
        if kind == "entries":
            self._render(payload)
            self.notify(f"已扫描 {len(payload)} 个 server 块")
        else:
            messagebox.showerror("扫描失败", payload, parent=self)
            self.notify("扫描失败")

    def _render(self, entries: list[VhostEntry]) -> None:
        self._entries = entries
        self.tree.delete(*self.tree.get_children())
        for i, e in enumerate(entries):
            is_warn = bool(e.note) or (e.port is not None and not e.php_version)
            tags = ["warn" if is_warn else "ok", "odd" if i % 2 else "even"]
            self.tree.insert(
                "", "end",
                values=(
                    e.server_name,
                    e.file_rel,
                    e.port if e.port is not None else "—",
                    e.php_version or "—",
                    e.root or "—",
                    e.note or "—",
                ),
                tags=tags,
            )

    # ------------------------------------------------------------------ #
    def _open_config(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if idx >= len(self._entries):
            return
        path = self._entries[idx].file
        if os.path.exists(path):
            try:
                os.startfile(path)  # noqa: S606 —— 用默认关联程序打开
            except OSError as e:
                messagebox.showerror("打开失败", str(e), parent=self)
        else:
            messagebox.showwarning("文件不存在", f"配置文件不存在：\n{path}", parent=self)

    def _open_dir(self) -> None:
        target = NGINX_CONF_DIR if os.path.isdir(NGINX_CONF_DIR) else WNRP_ROOT
        try:
            os.startfile(target)  # noqa: S606
        except OSError as e:
            messagebox.showerror("打开失败", str(e), parent=self)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_refresh.configure(state="disabled" if busy else "normal")
