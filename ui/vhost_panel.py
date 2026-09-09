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

from core import hosts_manager, process_utils as pu
from core.vhost_manager import VhostEntry, VhostManager
from .site_wizard import SiteWizardDialog
from .theme import CARD_BG, ERR, GRAY, OK, TEXT, WARN

COLUMNS = [
    ("server_name", "域名", 230, "w"),
    ("hosts", "hosts 映射", 150, "w"),
    ("file", "配置文件", 130, "w"),
    ("port", "端口", 70, "center"),
    ("php", "PHP 版本", 90, "center"),
    ("root", "项目根目录", 300, "w"),
    ("note", "说明", 150, "w"),
]
_OK_MARK = "✔"
_WARN_MARK = "⚠"


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
        self.btn_new = ttk.Button(bar, text="＋ 新建站点…", style="Accent.TButton",
                                  command=self._open_wizard)
        self.btn_new.pack(side="left", padx=(0, 6))
        self.btn_refresh = ttk.Button(bar, text="刷新", command=self.refresh)
        self.btn_open_dir = ttk.Button(bar, text="打开 vhost 目录", command=self._open_dir)
        self.btn_open_dir.pack(side="left", padx=(0, 6))
        self.btn_refresh.pack(side="left", padx=(0, 6))
        ttk.Label(
            bar,
            text="「新建站点」按向导生成 Laravel/WordPress/ThinkPHP 等配置，并自动写 hosts；"
                 "双击行打开配置文件",
            style="SubTitle.TLabel",
        ).pack(side="left", padx=(4, 0))

        # 生效 nginx.conf 是否 include 站点目录：自动检测状态行
        self._inc_wrap = tk.Frame(self, bg=CARD_BG)
        self._inc_wrap.pack(fill="x", pady=(0, 6))
        self._inc_label = tk.Label(
            self._inc_wrap, anchor="w", justify="left", wraplength=760,
            font=("", 10), bg=CARD_BG, fg=TEXT,
        )
        self._inc_label.pack(side="left", fill="x", expand=True, padx=6, pady=4)
        self._btn_fix_inc = ttk.Button(
            self._inc_wrap, text="自动补 include 并校验", command=self._fix_include
        )
        self._btn_fix_inc.pack_forget()  # 默认隐藏，仅在「未 include」时显示
        self._inc_status: dict | None = None

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
                inc = self.vhost_mgr.include_status()
                self._queue.put(("data", (entries, inc)))
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
        if kind == "data":
            entries, inc = payload
            self._render(entries)
            self._render_include_status(inc)
            self.notify(f"已扫描 {len(entries)} 个 server 块")
        else:
            self._render_include_status(None)
            messagebox.showerror("扫描失败", payload, parent=self)
            self.notify("扫描失败")

    def _render(self, entries: list[VhostEntry]) -> None:
        self._entries = entries
        self.tree.delete(*self.tree.get_children())
        # 一次性读取 hosts，避免逐行重读
        all_doms = [d for e in entries for d in e.server_name.split()
                    if not d.startswith("*.")]
        mapping = hosts_manager.mapping_for_domains(all_doms)
        for i, e in enumerate(entries):
            is_warn = bool(e.note) or (e.port is not None and not e.php_version)
            tags = ["warn" if is_warn else "ok", "odd" if i % 2 else "even"]
            self.tree.insert(
                "", "end",
                values=(
                    e.server_name,
                    self._hosts_cell(e.server_name, mapping),
                    e.file_rel,
                    e.port if e.port is not None else "—",
                    e.php_version or "—",
                    e.root or "—",
                    e.note or "—",
                ),
                tags=tags,
            )

    @staticmethod
    def _hosts_cell(server_name: str, mapping: dict) -> str:
        """域名 → hosts 状态摘要（✓本机 / ✗缺失 / 指向其它 IP）。"""
        parts = []
        for d in server_name.split():
            if d.startswith("*."):
                parts.append(d.replace("*.", "*") + " 泛解析")
                continue
            ip = mapping.get(d)
            if ip == "127.0.0.1":
                parts.append("✓ 本机")
            elif ip is None:
                parts.append("✗ 未映射")
            else:
                parts.append(f"⚠ {ip}")
        return ", ".join(parts) or "—"

    # ------------------------------------------------------------------ #
    def _open_wizard(self) -> None:
        """打开「新建站点」可视化向导，完成后刷新列表。"""
        SiteWizardDialog(self.winfo_toplevel(), self.vhost_mgr.config,
                         on_done=self.refresh)

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
            pu.open_path(path)
        else:
            messagebox.showwarning("文件不存在", f"配置文件不存在：\n{path}", parent=self)

    def _open_dir(self) -> None:
        """打开站点配置所在目录（已建则 vhost，否则主配置所在目录）。"""
        target = self.vhost_mgr.vhost_dir if os.path.isdir(self.vhost_mgr.vhost_dir) \
            else self.vhost_mgr.conf_dir
        if not os.path.isdir(target):
            target = self.vhost_mgr.nginx.prefix
        pu.open_path(target)

    # ------------------------------------------------------------------ #
    # include 状态检测 / 一键修复
    # ------------------------------------------------------------------ #
    def _render_include_status(self, st: dict | None) -> None:
        """根据检测结果刷新状态行：✔已加载 / ⚠未 include(可修复) / ⚠配置缺失。"""
        self._inc_status = st
        self._btn_fix_inc.pack_forget()
        if st is None:
            self._inc_label.configure(fg=GRAY, text="正在检测站点目录加载状态…")
            return
        main = st.get("main_conf") or ""
        lines = "、".join(st.get("lines") or []) or "（无）"
        if st.get("covered"):
            self._inc_label.configure(
                fg=OK,
                text=f"{_OK_MARK} 生效主配置已 include 站点目录 {st['vhost_dir']}\n{main}  → include：{lines}",
            )
        elif not os.path.exists(main):
            self._inc_label.configure(fg=WARN, text=f"{_WARN_MARK} {st['reason']}")
        else:
            self._inc_label.configure(
                fg=ERR,
                text=f"{_WARN_MARK} 站点目录未被 nginx 加载，新建/修改站点不会生效！\n{st['reason']}",
            )
            self._btn_fix_inc.pack(side="right", padx=6, pady=4)

    def _fix_include(self) -> None:
        """自动补 include → nginx -t 校验 → 询问是否平滑重载。"""
        if self._inc_status is None:
            return
        self._btn_fix_inc.state(["disabled"])
        try:
            if self.vhost_mgr.include_status().get("covered"):
                self.notify("站点目录已被主配置 include，无需修复")
                self.refresh()
                return
            self.notify("正在自动补 include 并校验…")
            res = self.vhost_mgr.ensure_include()
            final = res["message"]
            cfg_ok = not res["ok"]
            if res["ok"]:
                out = (self.vhost_mgr.nginx.test_config() or "").strip()
                if out:
                    final = f"{final}\n{out}"
                cfg_ok = "successful" in out or out == "配置检查通过"
            self._render_include_status(self.vhost_mgr.include_status())
            if not res["ok"]:
                messagebox.showerror("修复失败", final, parent=self)
            elif not cfg_ok:
                messagebox.showwarning("配置校验未通过", final, parent=self)
            else:
                messagebox.showinfo("已修复", final, parent=self)
                running, _ = self.vhost_mgr.nginx.get_status()
                if running and messagebox.askyesno(
                    "include 已补上",
                    "需要平滑重载 nginx 才会加载站点目录。\n是否立即重载？",
                    parent=self,
                ):
                    self.notify(self.vhost_mgr.nginx.reload())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("修复失败", str(e), parent=self)
        finally:
            self._btn_fix_inc.state(["!disabled"])
            self.refresh()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_refresh.configure(state="disabled" if busy else "normal")
