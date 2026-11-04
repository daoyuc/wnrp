# -*- coding: utf-8 -*-
"""「项目启动器」页签：项目列表 + 站点 URL / 命令清单 + 一键打开浏览器或终端。

契约（主窗口 / 主题系统）：

- ``auto_refresh()``：8 秒 tick 调用，扫描走 worker + queue，主线程只 drain；
- ``refresh_theme()``：主题切换时重设 Treeview 斑马纹配色。

安全边界：命令只来自内置模板白名单或用户手动添加的自定义命令；站点 URL 只来自
nginx vhost 的 ``server_name``。点击才打开一次浏览器 / 终端，没有任何自动执行，
也不读取 ``.phpvm.json`` 里的命令或域名当 URL。
"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core import run_log
from core.i18n import t
from core.plugins.project_launcher import commands, projects, supported
from .. import theme
from ..window_utils import rearm_poll


class ProjectLauncherPanel(ttk.Frame):
    """项目启动器面板。"""

    def __init__(self, master, config, notify):
        super().__init__(master, padding=8)
        self.config = config
        self.notify = notify
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._all: list = []
        self._view: list = []
        self._commands: list[dict] = []
        self._selected_key = ""
        self._search = tk.StringVar()
        self._status = tk.StringVar(value=t("就绪"))
        self._build()
        self.refresh()
        self._poll()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text=t("项目启动器"), style="Title.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self._status,
                  style="CardDim.TLabel").pack(side="left", padx=(theme.PAD_MD, 0))
        ttk.Button(bar, text=t("刷新"), command=self.refresh,
                   style="Toolbar.TButton").pack(side="right")
        ttk.Entry(bar, textvariable=self._search, width=22).pack(
            side="right", padx=(0, theme.PAD_SM))
        self._search.trace_add("write", lambda *_: self._render_projects())

        self._body = ttk.Frame(self)
        self._body.pack(fill="both", expand=True, pady=(theme.PAD_MD, 0))
        self._cols = ttk.Frame(self._body)
        self._cols.pack(fill="both", expand=True)

        left = ttk.Frame(self._cols)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text=t("项目"), style="Section.TLabel").pack(anchor="w")
        self._ptree = ttk.Treeview(left, columns=("php", "domains"),
                                   show="tree headings", selectmode="browse",
                                   height=14)
        self._ptree.heading("#0", text=t("项目"))
        self._ptree.heading("php", text=t("PHP"))
        self._ptree.heading("domains", text=t("域名"))
        self._ptree.column("#0", width=150)
        self._ptree.column("php", width=80)
        self._ptree.column("domains", width=150)
        self._ptree.pack(fill="both", expand=True, side="left")
        psb = ttk.Scrollbar(left, orient="vertical", command=self._ptree.yview)
        psb.pack(side="right", fill="y")
        self._ptree.configure(yscrollcommand=psb.set)

        right = ttk.Frame(self._cols)
        right.pack(side="left", fill="both", expand=True,
                   padx=(theme.PAD_MD, 0))
        ttk.Label(right, text=t("命令"), style="Section.TLabel").pack(anchor="w")
        self._ctree = ttk.Treeview(right, columns=("command", "source"),
                                   show="tree headings", selectmode="browse",
                                   height=14)
        self._ctree.heading("#0", text=t("命令"))
        self._ctree.heading("command", text=t("命令行"))
        self._ctree.heading("source", text=t("来源"))
        self._ctree.column("#0", width=150)
        self._ctree.column("command", width=210)
        self._ctree.column("source", width=70, anchor="center")
        self._ctree.pack(fill="both", expand=True, side="left")
        csb = ttk.Scrollbar(right, orient="vertical", command=self._ctree.yview)
        csb.pack(side="right", fill="y")
        self._ctree.configure(yscrollcommand=csb.set)

        self._empty = ttk.Label(self._body, text=t("暂无项目"),
                                style="CardDim.TLabel")

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(theme.PAD_MD, 0))
        self._browse_btn = ttk.Button(
            actions, text=t("在浏览器打开"), command=self._on_browse,
            style="Accent.TButton")
        self._browse_btn.pack(side="left")
        ttk.Button(actions, text=t("在终端运行"), command=self._on_run).pack(
            side="left", padx=(theme.PAD_SM, 0))
        ttk.Button(actions, text=t("打开目录"), command=self._on_open_dir).pack(
            side="left", padx=(theme.PAD_SM, 0))
        ttk.Button(actions, text=t("添加命令"), command=self._on_add).pack(
            side="left", padx=(theme.PAD_SM, 0))
        ttk.Button(actions, text=t("删除命令"), command=self._on_remove,
                   style="Danger.TButton").pack(side="left",
                                                padx=(theme.PAD_SM, 0))
        ttk.Label(actions, text=t("站点地址来自站点映射（vhost）配置"),
                  style="CardDim.TLabel").pack(side="right")
        if not supported():
            ttk.Label(actions, text=t("当前平台不支持"),
                      style="CardDim.TLabel").pack(side="right")

        self._menu = tk.Menu(self, tearoff=False)
        self._ptree.bind("<<TreeviewSelect>>", lambda e: self._render_commands())
        self._ptree.bind("<Double-1>", lambda e: self._on_browse())
        self._ptree.bind("<Button-3>", self._show_menu)
        self._ctree.bind("<Double-1>", lambda e: self._on_run())
        self.refresh_theme()

    # ------------------------------------------------------------------ #
    # 数据刷新（worker + 队列回主线程）
    # ------------------------------------------------------------------ #
    def auto_refresh(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        try:
            found = projects.scan(self.config)
            self._queue.put(("data", found))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("error", f"{type(e).__name__}: {e}"))

    def _poll(self) -> None:
        while True:
            try:
                msg = self._queue.get_nowait()
            except queue.Empty:
                break
            if msg[0] == "data":
                self._busy = False
                self._all = msg[1]
                self._render_projects()
                self._status.set(t("共 {n} 个项目", n=len(self._all)))
            elif msg[0] == "launch":
                result = msg[1]
                if result.get("ok"):
                    self._status.set(t("已打开终端：{cmd}",
                                       cmd=result.get("command", "")))
                else:
                    self._status.set(t("打开终端失败：{err}",
                                       err=result.get("error", "")))
                    self.notify(t("打开终端失败"), "error")
            elif msg[0] == "browse":
                result = msg[1]
                if result.get("ok"):
                    self._status.set(t("已打开站点：{url}",
                                       url=result.get("url", "")))
                else:
                    self._status.set(t("打开浏览器失败：{err}",
                                       err=result.get("error", "")))
                    self.notify(t("打开浏览器失败"), "error")
            else:
                self._busy = False
                self._status.set(t("扫描失败"))
                run_log.error("plugin", t("项目启动器扫描失败：{err}", err=msg[1]))
        rearm_poll(self, self._poll, 150)

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def refresh_theme(self) -> None:
        for tree in (getattr(self, "_ptree", None), getattr(self, "_ctree", None)):
            if tree is None:
                continue
            tree.tag_configure("odd", background=theme.ROW_ALT)
            tree.tag_configure("even", background=theme.CARD_BG)

    def _render_projects(self) -> None:
        q = self._search.get().strip().lower()
        self._view = [p for p in self._all
                      if not q or q in p.name.lower() or q in p.php.lower()
                      or any(q in d.lower() for d in p.domains)]
        self._ptree.delete(*self._ptree.get_children())
        for i, p in enumerate(self._view):
            self._ptree.insert(
                "", "end", iid=p.key, text=p.name,
                values=(p.php or "—", " ".join(p.domains) or "—"),
                tags=("odd" if i % 2 else "even",))

        if self._view:
            self._empty.pack_forget()
            self._cols.pack(fill="both", expand=True)
            key = self._selected_key if self._ptree.exists(self._selected_key) \
                else self._view[0].key
            self._selected_key = key
            self._ptree.selection_set(key)
        else:
            self._cols.pack_forget()
            self._empty.pack(expand=True)
        self._render_commands()

    def _render_commands(self) -> None:
        project = self._selected_project()
        self._commands = commands.for_project(self.config, project) if project else []
        self._ctree.delete(*self._ctree.get_children())
        for i, c in enumerate(self._commands):
            self._ctree.insert(
                "", "end", iid=c["id"], text=t(c["label"]),
                values=(c["command"], t("内置") if c["builtin"] else t("自定义")),
                tags=("odd" if i % 2 else "even",))
        children = self._ctree.get_children()
        if children:
            self._ctree.selection_set(children[0])
        btn = getattr(self, "_browse_btn", None)
        if btn is not None:
            btn.configure(state="normal" if projects.site_urls(project)
                          else "disabled")

    def _selected_project(self):
        sel = self._ptree.selection()
        if not sel:
            return None
        self._selected_key = sel[0]
        return next((p for p in self._view if p.key == sel[0]), None)

    def _selected_command(self) -> dict | None:
        sel = self._ctree.selection()
        return commands.find(self._commands, sel[0]) if sel else None

    # ------------------------------------------------------------------ #
    # 操作
    # ------------------------------------------------------------------ #
    def _show_menu(self, event) -> None:
        """项目列表右键：打开站点 / 目录，或逐个选择域名、复制 URL。"""
        iid = self._ptree.identify_row(event.y)
        if iid:
            self._ptree.selection_set(iid)
        project = self._selected_project()
        if project is None:
            return
        self._menu.delete(0, "end")
        self._menu.add_command(label=t("在浏览器打开"), command=self._on_browse)
        self._menu.add_command(label=t("打开目录"), command=self._on_open_dir)
        urls = projects.site_urls(project)
        if urls:
            self._menu.add_separator()
            for url in urls:
                self._menu.add_command(
                    label=url, command=lambda u=url: self._browse_url(u))
            self._menu.add_separator()
            self._menu.add_command(label=t("复制站点 URL"),
                                   command=lambda u=urls[0]: self._copy_url(u))
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _copy_url(self, url: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(url)
        self._status.set(t("已复制 URL：{url}", url=url))

    def _on_browse(self) -> None:
        project = self._selected_project()
        if project is None:
            return
        urls = projects.site_urls(project)
        if not urls:
            msg = t("该项目没有可访问的站点（需在站点映射里配置域名）")
            self._status.set(msg)
            self.notify(msg, "warn")
            return
        self._browse_url(urls[0])

    def _browse_url(self, url: str) -> None:
        project = self._selected_project()
        if project is None:
            return
        self._status.set(t("正在打开浏览器…"))

        def worker():
            try:
                result = commands.open_site(project, url)
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self._queue.put(("browse", result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_run(self) -> None:
        if not supported():
            self.notify(t("当前平台不支持"), "warn")
            return
        project = self._selected_project()
        command = self._selected_command()
        if not project or not command:
            self._status.set(t("请先选择项目与命令"))
            return
        if not command["builtin"] and not messagebox.askyesno(
                t("确认运行"),
                t("即将在终端运行自定义命令：\n{cmd}\n\n请确认该命令由你自己添加。",
                  cmd=command["command"])):
            return
        self._status.set(t("正在打开终端…"))

        def worker():
            try:
                result = commands.launch(self.config, project, command["id"])
            except Exception as e:  # noqa: BLE001
                result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            self._queue.put(("launch", result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_open_dir(self) -> None:
        project = self._selected_project()
        if not project:
            return
        try:
            if os.name == "nt":
                os.startfile(project.root)  # noqa: S606 - 只打开用户已选中的项目目录
            elif sys.platform == "darwin":
                subprocess.Popen(["open", project.root])
            else:
                subprocess.Popen(["xdg-open", project.root])
        except Exception as e:  # noqa: BLE001
            self.notify(t("打开目录失败：{err}", err=e), "error")

    def _on_add(self) -> None:
        project = self._selected_project()
        dlg = tk.Toplevel(self)
        dlg.title(t("添加命令"))
        dlg.transient(self.winfo_toplevel())
        dlg.resizable(False, False)
        dlg.grab_set()

        name = tk.StringVar()
        cmd = tk.StringVar()
        only_this = tk.BooleanVar(value=False)
        body = ttk.Frame(dlg, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=t("名称")).grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=name, width=34).grid(row=0, column=1, pady=4)
        ttk.Label(body, text=t("命令行")).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=cmd, width=34).grid(row=1, column=1, pady=4)
        ttk.Checkbutton(body, text=t("仅当前项目可用"),
                        variable=only_this).grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(body, text=t("命令不会自动执行，需在此面板手动点击运行。"),
                  style="CardDim.TLabel").grid(row=3, column=0, columnspan=2,
                                              sticky="w", pady=(8, 0))

        def submit():
            entry, err = commands.add_custom(
                self.config, name.get(), cmd.get(),
                project.root if (only_this.get() and project) else "")
            if err:
                messagebox.showerror(t("添加失败"), err, parent=dlg)
                return
            dlg.destroy()
            self._status.set(t("已添加命令：{name}", name=entry["label"]))
            self._render_commands()

        btns = ttk.Frame(body)
        btns.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text=t("取消"), command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text=t("确定"), command=submit,
                   style="Accent.TButton").pack(side="right", padx=(0, 6))

    def _on_remove(self) -> None:
        command = self._selected_command()
        if not command:
            return
        if command["builtin"]:
            self.notify(t("内置命令不可删除"), "warn")
            return
        if not messagebox.askyesno(
                t("删除命令"),
                t("确定删除自定义命令「{name}」？", name=command["label"])):
            return
        if commands.remove_custom(self.config, command["id"]):
            self._status.set(t("已删除命令"))
            self._render_commands()
