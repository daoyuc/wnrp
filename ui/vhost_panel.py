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
import webbrowser
from tkinter import messagebox, ttk

from core import hosts_manager, process_utils as pu
from core.i18n import t
from core.vhost_manager import VhostEntry, VhostManager
from .site_wizard import SiteWizardDialog
from .theme import CARD_BG, ERR, GRAY, OK, TEXT, WARN

COLUMNS = [
    ("server_name", t("域名"), 230, "w"),
    ("hosts", t("hosts 映射"), 150, "w"),
    ("file", t("配置文件"), 130, "w"),
    ("port", t("端口"), 70, "center"),
    ("php", t("PHP 版本"), 90, "center"),
    ("root", t("项目根目录"), 300, "w"),
    ("note", t("说明"), 150, "w"),
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
        self.btn_new = ttk.Button(bar, text=t("＋ 新建站点…"), style="Accent.TButton",
                                  command=self._open_wizard)
        self.btn_new.pack(side="left", padx=(0, 6))
        self.btn_refresh = ttk.Button(bar, text=t("刷新"), command=self.refresh)
        self.btn_open_dir = ttk.Button(bar, text=t("打开 vhost 目录"), command=self._open_dir)
        self.btn_open_dir.pack(side="left", padx=(0, 6))
        self.btn_refresh.pack(side="left", padx=(0, 6))
        ttk.Label(
            bar,
            text=t("「新建站点」按向导生成 Laravel/WordPress/ThinkPHP 等配置，并自动写 hosts；"
                   "双击行打开配置文件"),
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
            self._inc_wrap, text=t("自动补 include 并校验"), command=self._fix_include
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
        self.tree.tag_configure("disabled", foreground=GRAY)
        self.tree.tag_configure("odd", background="#FAFBFC")
        self.tree.tag_configure("even", background=CARD_BG)
        self.tree.bind("<Double-1>", lambda e: self._open_config())
        # 右键菜单（Windows/Linux 为 Button-3，macOS 触控板为 Button-2）
        self.tree.bind("<Button-3>", self._show_menu)
        self.tree.bind("<Button-2>", self._show_menu)
        self._menu = tk.Menu(self, tearoff=0)
        self._php_menu = tk.Menu(self._menu, tearoff=0)

    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self.notify(t("正在扫描 nginx 配置…"))

        def worker():
            try:
                # 含已禁用站点（.conf.disabled），便于重新启用
                entries = self.vhost_mgr.scan(include_disabled=True)
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
        if kind == "op":
            res = payload or {}
            if res.get("ok"):
                self.notify(res.get("message") or t("操作完成"))
            else:
                messagebox.showerror(t("操作失败"), res.get("message") or "", parent=self)
            self.refresh()
            return
        if kind == "data":
            entries, inc = payload
            self._render(entries)
            self._render_include_status(inc)
            self.notify(t("已扫描 {count} 个 server 块", count=len(entries)))
        else:
            self._render_include_status(None)
            messagebox.showerror(t("扫描失败"), payload, parent=self)
            self.notify(t("扫描失败"))

    def _render(self, entries: list[VhostEntry]) -> None:
        self._entries = entries
        self.tree.delete(*self.tree.get_children())
        # 一次性读取 hosts，避免逐行重读
        all_doms = [d for e in entries for d in e.server_name.split()
                    if not d.startswith("*.")]
        mapping = hosts_manager.mapping_for_domains(all_doms)
        for i, e in enumerate(entries):
            is_warn = bool(e.note) or (e.port is not None and not e.php_version)
            state_tag = "disabled" if e.disabled else ("warn" if is_warn else "ok")
            tags = [state_tag, "odd" if i % 2 else "even"]
            self.tree.insert(
                "", "end",
                values=(
                    e.server_name,
                    self._hosts_cell(e.server_name, mapping),
                    e.file_rel + (t(" （已禁用）") if e.disabled else ""),
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
                parts.append(d.replace("*.", "*") + t(" 泛解析"))
                continue
            ip = mapping.get(d)
            if ip == "127.0.0.1":
                parts.append(t("✓ 本机"))
            elif ip is None:
                parts.append(t("✗ 未映射"))
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
            messagebox.showwarning(t("文件不存在"), t("配置文件不存在：\n{path}", path=path),
                                   parent=self)

    def _open_dir(self) -> None:
        """打开站点配置所在目录（已建则 vhost，否则主配置所在目录）。"""
        target = self.vhost_mgr.vhost_dir if os.path.isdir(self.vhost_mgr.vhost_dir) \
            else self.vhost_mgr.conf_dir
        if not os.path.isdir(target):
            target = self.vhost_mgr.nginx.prefix
        pu.open_path(target)

    # ------------------------------------------------------------------ #
    # 右键菜单：站点级操作（打开 / 启用禁用 / 切换 PHP / hosts 清理）
    # ------------------------------------------------------------------ #
    def _selected(self) -> VhostEntry | None:
        """返回当前选中行对应的条目。"""
        sel = self.tree.selection()
        if not sel:
            return None
        idx = self.tree.index(sel[0])
        return self._entries[idx] if idx < len(self._entries) else None

    def _show_menu(self, event) -> None:
        """右键：先选中所在行，再按该站点状态重建菜单。"""
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
        entry = self._selected()
        if entry is None:
            return
        self._menu.delete(0, "end")
        self._menu.add_command(label=t("打开站点（浏览器）"), command=self._open_site)
        self._menu.add_command(label=t("打开项目根目录"), command=self._open_root)
        self._menu.add_command(label=t("打开配置文件"), command=self._open_config)
        self._menu.add_separator()

        self._php_menu.delete(0, "end")
        ports = dict(self.vhost_mgr.config.ports)
        if entry.port is None:
            self._php_menu.add_command(label=t("（该站点无 fastcgi_pass）"),
                                       state="disabled")
        else:
            for name in sorted(ports):
                port = ports[name]
                mark = "✔ " if port == entry.port else ""
                self._php_menu.add_command(
                    label=f"{mark}{name} · {port}",
                    command=lambda p=port: self._set_php(p))
        self._menu.add_cascade(label=t("切换 PHP 版本"), menu=self._php_menu)
        self._menu.add_command(
            label=t("启用站点") if entry.disabled else t("禁用站点"),
            command=self._toggle_enabled)
        self._menu.add_separator()
        self._menu.add_command(label=t("从 hosts 移除映射"), command=self._remove_hosts)
        self._menu.add_separator()
        self._menu.add_command(label=t("刷新"), command=self.refresh)
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _open_site(self) -> None:
        entry = self._selected()
        if not entry:
            return
        url = VhostManager.site_url(entry)
        if not url:
            messagebox.showinfo(t("无法打开"), t("该站点没有可直接访问的域名（仅泛解析）"),
                                parent=self)
            return
        webbrowser.open(url)
        self.notify(t("已在浏览器打开 {url}", url=url))

    def _open_root(self) -> None:
        entry = self._selected()
        if not entry:
            return
        root = entry.root
        if root and os.path.isdir(root):
            pu.open_path(root)
            self.notify(t("已打开目录 {path}", path=root))
        else:
            messagebox.showinfo(t("目录不存在"),
                                t("项目根目录不存在或未配置：{path}", path=root or "—"),
                                parent=self)

    def _toggle_enabled(self) -> None:
        entry = self._selected()
        if not entry:
            return
        enable = entry.disabled
        if not enable and not messagebox.askyesno(
                t("禁用站点"),
                t("禁用后该站点配置不再被 nginx 加载，访问将失败。\n确定禁用 {name}？",
                  name=entry.server_name),
                parent=self):
            return
        self._run_op(lambda: self.vhost_mgr.set_site_enabled(entry.file, enable),
                     t("正在启用站点…") if enable else t("正在禁用站点…"))

    def _set_php(self, port: int) -> None:
        entry = self._selected()
        if not entry:
            return
        self._run_op(
            lambda: self.vhost_mgr.set_site_php(entry.file, entry.server_name, port),
            t("正在切换 {name} 的 PHP 版本…", name=entry.server_name))

    def _remove_hosts(self) -> None:
        entry = self._selected()
        if not entry:
            return
        doms = [d for d in entry.server_name.split() if d and not d.startswith("*.")]
        if not doms:
            messagebox.showinfo(t("无需处理"), t("该站点没有可直接映射的域名（仅泛解析）"),
                                parent=self)
            return
        if not messagebox.askyesno(
                t("移除 hosts 映射"),
                t("将从 hosts 的 phpvm 托管块中移除：{doms}\n"
                  "（不会改动用户手写的其它映射）确定继续？", doms="、".join(doms)),
                parent=self):
            return
        self._run_op(lambda: hosts_manager.remove_entries(doms), t("正在移除 hosts 映射…"))

    def _run_op(self, fn, tip: str) -> None:
        """后台执行站点操作（可能触发 nginx -t 或 hosts 提权），结果回主线程提示。"""
        self._set_busy(True)
        self.notify(tip)

        def worker():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "message": f"{type(e).__name__}：{e}"}
            self._queue.put(("op", res))

        threading.Thread(target=worker, daemon=True).start()
        self._poll()

    # ------------------------------------------------------------------ #
    # include 状态检测 / 一键修复
    # ------------------------------------------------------------------ #
    def _render_include_status(self, st: dict | None) -> None:
        """根据检测结果刷新状态行：✔已加载 / ⚠未 include(可修复) / ⚠配置缺失。"""
        self._inc_status = st
        self._btn_fix_inc.pack_forget()
        if st is None:
            self._inc_label.configure(fg=GRAY, text=t("正在检测站点目录加载状态…"))
            return
        main = st.get("main_conf") or ""
        lines = "、".join(st.get("lines") or []) or t("（无）")
        if st.get("covered"):
            self._inc_label.configure(
                fg=OK,
                text=t("{mark} 生效主配置已 include 站点目录 {dir}\n{main}  → include：{lines}",
                       mark=_OK_MARK, dir=st["vhost_dir"], main=main, lines=lines),
            )
        elif not os.path.exists(main):
            self._inc_label.configure(fg=WARN, text=f"{_WARN_MARK} {st['reason']}")
        else:
            self._inc_label.configure(
                fg=ERR,
                text=t("{mark} 站点目录未被 nginx 加载，新建/修改站点不会生效！\n{reason}",
                       mark=_WARN_MARK, reason=st["reason"]),
            )
            self._btn_fix_inc.pack(side="right", padx=6, pady=4)

    def _fix_include(self) -> None:
        """自动补 include → nginx -t 校验 → 询问是否平滑重载。"""
        if self._inc_status is None:
            return
        self._btn_fix_inc.state(["disabled"])
        try:
            if self.vhost_mgr.include_status().get("covered"):
                self.notify(t("站点目录已被主配置 include，无需修复"))
                self.refresh()
                return
            self.notify(t("正在自动补 include 并校验…"))
            res = self.vhost_mgr.ensure_include()
            final = res["message"]
            cfg_ok = not res["ok"]
            if res["ok"]:
                out = (self.vhost_mgr.nginx.test_config() or "").strip()
                if out:
                    final = f"{final}\n{out}"
                cfg_ok = "successful" in out or out == t("配置检查通过")
            self._render_include_status(self.vhost_mgr.include_status())
            if not res["ok"]:
                messagebox.showerror(t("修复失败"), final, parent=self)
            elif not cfg_ok:
                messagebox.showwarning(t("配置校验未通过"), final, parent=self)
            else:
                messagebox.showinfo(t("已修复"), final, parent=self)
                running, _ = self.vhost_mgr.nginx.get_status()
                if running and messagebox.askyesno(
                    t("include 已补上"),
                    t("需要平滑重载 nginx 才会加载站点目录。\n是否立即重载？"),
                    parent=self,
                ):
                    self.notify(self.vhost_mgr.nginx.reload())
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(t("修复失败"), str(e), parent=self)
        finally:
            self._btn_fix_inc.state(["!disabled"])
            self.refresh()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_refresh.configure(state="disabled" if busy else "normal")
