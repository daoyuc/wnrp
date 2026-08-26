# -*- coding: utf-8 -*-
"""对话框：端口编辑（校验 + 保存 + 提示同步 vhost）、php.ini 配置查看、cmd php 版本切换。"""
import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core import path_manager
from core.config import Config
from core.php_manager import PhpManager, PhpVersion
from .theme import CARD_BG, ERR, FONT, GRAY, OK, PRIMARY, PRIMARY_DARK, TEXT

VHOST_DIR = r"C:\wnrp\nginx\conf\vhost"


class PortDialog(tk.Toplevel):
    """编辑某 PHP 版本的 FastCGI 端口。"""

    def __init__(self, master, version: PhpVersion, config: Config, on_saved=None):
        super().__init__(master)
        self.version = version
        self.config = config
        self.on_saved = on_saved

        self.title(f"编辑端口 · {version.name}")
        self.resizable(False, False)
        self.configure(bg=CARD_BG)
        self.transient(master)
        self.grab_set()

        body = ttk.Frame(self, padding=18)
        body.pack()

        ttk.Label(
            body,
            text=f"[{version.name}]  PHP {version.display}",
            style="Title.TLabel",
        ).pack(anchor="w")

        row = ttk.Frame(body)
        row.pack(fill="x", pady=(14, 4))
        ttk.Label(row, text="FastCGI 端口：", font=(FONT, 9, "bold"), background=CARD_BG).pack(side="left")
        self.var = tk.StringVar(value=str(version.port))
        entry = ttk.Entry(row, textvariable=self.var, width=10, font=(FONT, 11))
        entry.pack(side="left", padx=(8, 0))
        entry.focus_set()
        entry.select_range(0, "end")
        entry.bind("<Return>", lambda e: self._save())

        ttk.Label(
            body,
            text="修改后需同步修改 nginx vhost 中的 fastcgi_pass 才会生效",
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(16, 0))
        ttk.Button(btns, text="确定", style="Accent.TButton", command=self._save).pack(side="right")
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=(0, 8))

        self._center(master)

    def _center(self, master) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{x}+{y}")

    def _save(self) -> None:
        raw = self.var.get().strip()
        try:
            port = int(raw)
        except ValueError:
            messagebox.showerror("端口不合法", "端口必须是整数。", parent=self)
            return
        err = self.config.validate_port(port)
        if err:
            messagebox.showerror("端口不合法", err, parent=self)
            return
        err = self.config.validate_unique(self.version.name, port)
        if err:
            messagebox.showerror("端口冲突", err, parent=self)
            return

        self.config.set_port(self.version.name, port)
        self.version.port = port
        self.destroy()

        answer = messagebox.askyesno(
            "端口已修改",
            f"[{self.version.name}] 端口已改为 {port} 并保存。\n\n"
            f"是否打开 nginx vhost 目录，同步修改对应配置文件的 fastcgi_pass ？",
            parent=self.master,
        )
        if answer:
            try:
                os.startfile(VHOST_DIR)
            except OSError as e:
                messagebox.showerror("无法打开目录", str(e), parent=self.master)
        if self.on_saved:
            self.on_saved()


class IniDialog(tk.Toplevel):
    """查看 PHP 版本的核心配置与完整 ini 内容。"""

    def __init__(self, master, version: PhpVersion, php_mgr: PhpManager):
        super().__init__(master)
        self.version = version
        self.php_mgr = php_mgr

        self.title(f"PHP 配置 · {version.name} (PHP {version.display})")
        self.geometry("780x560")
        self.minsize(640, 460)
        self.configure(bg=CARD_BG)
        self.transient(master)

        header = ttk.Frame(self, padding=(14, 12, 14, 4))
        header.pack(fill="x")
        ttk.Label(header, text=f"配置文件：{version.ini}", style="SubTitle.TLabel").pack(
            side="left"
        )
        ttk.Button(header, text="用编辑器打开", command=self._open_ini).pack(side="right")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(8, 12))
        nb.add(self._build_key_frame(nb), text="  关键配置  ")
        nb.add(self._build_ext_frame(nb), text="  已启用扩展  ")
        nb.add(self._build_full_frame(nb), text="  完整内容  ")

        self._center(master)

    # ------------------------------------------------------------------ #
    def _build_key_frame(self, master) -> ttk.Frame:
        frame = ttk.Frame(master, padding=10)
        tree = ttk.Treeview(frame, columns=("key", "value"), show="headings", selectmode="browse")
        tree.heading("key", text="配置项")
        tree.heading("value", text="值")
        tree.column("key", width=240, anchor="w", stretch=False)
        tree.column("value", width=400, anchor="w")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        data = self.php_mgr.read_key_ini(self.version)
        if "__error__" in data:
            tree.insert("", "end", values=("读取失败", data["__error__"]))
            return frame
        for key in data:
            if key == "__extensions__":
                continue
            tree.insert("", "end", values=(key, data[key]))
        return frame

    def _build_ext_frame(self, master) -> ttk.Frame:
        frame = ttk.Frame(master, padding=10)
        text = tk.Text(
            frame, wrap="char", font=("Consolas", 9),
            background="#FFFFFF", foreground=TEXT, relief="flat", padx=8, pady=6,
        )
        vsb = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=vsb.set)
        text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        data = self.php_mgr.read_key_ini(self.version)
        exts = data.get("__extensions__", [])
        if not exts:
            text.insert("1.0", "（未在 php.ini 中启用任何 extension 指令）")
        else:
            text.insert("1.0", "\n".join(f"{i + 1}. {e}" for i, e in enumerate(exts)))
        text.configure(state="disabled")
        return frame

    def _build_full_frame(self, master) -> ttk.Frame:
        frame = ttk.Frame(master, padding=10)
        text = tk.Text(
            frame, wrap="none", font=("Consolas", 9),
            background="#1E1E1E", foreground="#C8C8C8", relief="flat", padx=8, pady=6,
        )
        hs = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        vs = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(xscrollcommand=hs.set, yscrollcommand=vs.set)
        text.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        text.insert("1.0", self.php_mgr.read_ini(self.version))
        text.configure(state="disabled")
        return frame

    # ------------------------------------------------------------------ #
    def _open_ini(self) -> None:
        try:
            os.startfile(self.version.ini)
        except OSError as e:
            messagebox.showerror("无法打开文件", str(e), parent=self)

    def _center(self, master) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")


class CliSwitchDialog(tk.Toplevel):
    """切换系统 cmd / 终端 中的 php 命令版本（修改用户 PATH，新窗口生效）。"""

    COLS = [
        ("name", "版本目录", 110, "w"),
        ("ver", "PHP 版本", 90, "center"),
        ("port", "端口", 70, "center"),
        ("status", "FastCGI", 80, "center"),
        ("mark", "cmd 生效", 100, "center"),
    ]

    def __init__(self, master, php_mgr: PhpManager, on_switched=None):
        super().__init__(master)
        self.php_mgr = php_mgr
        self.on_switched = on_switched
        self._queue: queue.Queue = queue.Queue()
        self._versions: list[PhpVersion] = []
        self._name_to_iid: dict[str, str] = {}
        self._effective = path_manager.get_effective_php_dir()

        self.title("切换 cmd php 命令版本")
        self.geometry("660x440")
        self.minsize(580, 380)
        self.configure(bg=CARD_BG)
        self.transient(master)

        header = ttk.Frame(self, padding=(16, 14, 16, 4))
        header.pack(fill="x")
        ttk.Label(header, text="切换系统 cmd / 终端 中的 php 命令版本", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="原理：修改用户环境变量 PATH（User 优先级高于系统），将所选版本置顶。"
                 "新打开的 cmd 生效，无需管理员权限。",
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=16, pady=10)
        self.tree = ttk.Treeview(
            wrap, columns=[c[0] for c in self.COLS], show="headings", selectmode="browse"
        )
        for col, text, width, anchor in self.COLS:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "mark"), minwidth=60)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("dot_run", foreground=OK)
        self.tree.tag_configure("dot_stop", foreground=GRAY)
        self.tree.tag_configure("mark_now", foreground=OK)
        self.tree.bind("<Double-1>", lambda e: self._apply())

        btns = ttk.Frame(self, padding=(16, 0, 16, 14))
        btns.pack(fill="x")
        ttk.Button(btns, text="在新窗口测试 php -v", command=self._test_cmd).pack(side="left")
        ttk.Label(btns, text="已打开的 cmd 不会自动切换，需重开窗口", style="SubTitle.TLabel").pack(
            side="left", padx=(10, 0)
        )
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="设为当前", style="Accent.TButton", command=self._apply).pack(
            side="right", padx=(0, 8)
        )

        self._load()
        self._center(master)

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        def worker():
            try:
                versions = self.php_mgr.scan_versions()
                versions = self.php_mgr.resolve(refresh_status=True, fast=True)
                self._queue.put(("versions", versions))
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
        if kind == "versions":
            self._render(payload)
        else:
            messagebox.showerror("加载失败", payload, parent=self)

    def _render(self, versions: list[PhpVersion]) -> None:
        self._versions = versions
        self._name_to_iid.clear()
        self.tree.delete(*self.tree.get_children())
        for i, v in enumerate(versions):
            running, pid = v.running, v.pid
            dot = "●" if running else "○"
            dot_tag = "dot_run" if running else "dot_stop"
            eff = v.dir == self._effective
            mark = "当前生效" if eff else ""
            iid = self.tree.insert(
                "",
                "end",
                values=(v.name, v.display, v.port, f"{dot} {'运行中' if running else '已停止'}",
                        mark),
                tags=[dot_tag] + (["mark_now"] if eff else []),
            )
            self._name_to_iid[v.name] = iid
            if eff:
                self.tree.selection_set(iid)

    def _selected(self) -> PhpVersion | None:
        sel = self.tree.selection()
        if not sel:
            return None
        for v in self._versions:
            if self._name_to_iid.get(v.name) == sel[0]:
                return v
        return None

    def _apply(self) -> None:
        v = self._selected()
        if v is None:
            messagebox.showinfo("提示", "请先选择一个 PHP 版本。", parent=self)
            return
        try:
            path_manager.set_cli_php(v.dir)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("切换失败", str(e), parent=self)
            return
        self._effective = v.dir
        self._render(self._versions)
        messagebox.showinfo(
            "切换成功",
            f"已切换 cmd php 命令 → [{v.name}]（PHP {v.display}）\n\n"
            f"注意：已打开的 cmd / 终端不会自动感知，请新开窗口执行 php -v 验证。",
            parent=self,
        )
        if self.on_switched:
            self.on_switched()

    def _test_cmd(self) -> None:
        """新开一个 cmd 窗口执行 php -v，直观验证当前生效版本。"""
        try:
            subprocess.Popen(
                ["cmd", "/k", "php", "-v"],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except OSError as e:
            messagebox.showerror("无法打开 cmd", str(e), parent=self)

    def _center(self, master) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
