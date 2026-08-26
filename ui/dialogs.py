# -*- coding: utf-8 -*-
"""对话框：端口编辑（校验 + 保存 + 一键同步 vhost）、配置查看/编辑、自检、cmd php 切换。"""
import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core import path_manager, recover_history
from core.config import Config
from core.php_manager import PhpManager, PhpVersion
from core.vhost_manager import VhostManager
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

        old_port = self.version.port
        self.config.set_port(self.version.name, port)
        self.version.port = port
        self.destroy()

        # 保存后检查引用旧端口的 vhost，弹出一键同步对话框
        try:
            vm = VhostManager(self.config)
        except Exception:  # noqa: BLE001
            vm = None
        if vm is not None:
            VhostSyncDialog(self.master, vm, old_port, port)
        else:
            messagebox.showinfo(
                "端口已修改",
                f"[{self.version.name}] 端口已改为 {port} 并保存。\n\n"
                f"若 nginx 配置引用了旧端口 {old_port}，请手动同步 fastcgi_pass。",
                parent=self.master,
            )
        if self.on_saved:
            self.on_saved()


class VhostSyncDialog(tk.Toplevel):
    """改端口后的一键同步：列出受影响配置文件 → 备份替换 → nginx -t 校验 → 可重载。

    所有耗时操作（扫描、同步、校验、重载）均后台执行，结果经 queue 回传。
    """

    def __init__(self, master, vhost_mgr: VhostManager, old_port: int, new_port: int):
        super().__init__(master)
        self.vhost_mgr = vhost_mgr
        self.old_port = old_port
        self.new_port = new_port
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._synced_ok = False
        self._file_domains: dict[str, str] = {}

        self.title(f"同步 vhost 端口 · {old_port} → {new_port}")
        self.geometry("780x540")
        self.minsize(660, 440)
        self.configure(bg=CARD_BG)
        self.transient(master)
        self.grab_set()

        header = ttk.Frame(self, padding=(16, 14, 16, 4))
        header.pack(fill="x")
        ttk.Label(header, text=f"一键同步 FastCGI 端口 {old_port} → {new_port}",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="以下配置文件中的 fastcgi_pass 仍指向旧端口。一键同步会备份原文件（.bak）、"
                 "替换端口并执行 nginx -t 校验，校验失败自动还原所有备份。",
            style="SubTitle.TLabel", wraplength=720,
        ).pack(anchor="w", pady=(4, 0))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=16, pady=10)
        self.tree = ttk.Treeview(
            wrap, columns=("file", "domains", "status", "detail"),
            show="headings", selectmode="browse",
        )
        for col, text, w in (("file", "配置文件", 280), ("domains", "域名", 160),
                             ("status", "状态", 70), ("detail", "详情", 260)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=w, anchor="w", stretch=(col == "file"))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("ok", foreground=OK)
        self.tree.tag_configure("err", foreground=ERR)
        self.tree.tag_configure("wait", foreground=GRAY)

        ttk.Label(self, text="nginx -t 校验输出：", style="SubTitle.TLabel").pack(anchor="w", padx=16)
        self.result_text = tk.Text(
            self, height=7, wrap="char", font=("Consolas", 9),
            background="#FFFFFF", foreground=TEXT, relief="flat", padx=8, pady=6, state="disabled",
        )
        self.result_text.pack(fill="x", padx=16, pady=(2, 8))

        btns = ttk.Frame(self, padding=(16, 0, 16, 14))
        btns.pack(fill="x")
        ttk.Button(btns, text="打开 vhost 目录", command=self._open_dir).pack(side="left")
        self.btn_reload = ttk.Button(btns, text="重载 Nginx", state="disabled", command=self._reload)
        self.btn_reload.pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="right")
        self.btn_sync = ttk.Button(btns, text="一键同步", style="Accent.TButton", command=self._sync)
        self.btn_sync.pack(side="right", padx=(0, 8))

        self._center(master)
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        """后台扫描引用旧端口的配置文件。"""
        self._set_busy(True)
        self._append_result("正在扫描引用旧端口 {} 的配置文件…".format(self.old_port))

        def worker():
            try:
                files = self.vhost_mgr._find_files_with_port(self.old_port)
                domains = {}
                for e in self.vhost_mgr.entries_with_port(self.old_port):
                    domains.setdefault(e.file, []).append(e.server_name)
                self._queue.put(("files", (files, domains)))
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
        if kind == "files":
            files, domains = payload
            self._file_domains = domains
            if not files:
                self._append_result("没有配置文件引用旧端口，无需同步。")
                self.btn_sync.configure(state="disabled")
                return
            for path in files:
                rel = os.path.relpath(path, VHOST_DIR)
                if rel.startswith(".."):
                    rel = path
                dm = ", ".join(domains.get(path, [])) or "—"
                self.tree.insert("", "end", values=(rel, dm, "待同步", ""),
                                 tags=("wait",))
            self._append_result("共 {} 个文件引用旧端口 {}{}。".format(
                len(files), self.old_port,
                "，点击「一键同步」执行" if not self._synced_ok else ""))
        elif kind == "done":
            self._on_done(payload)
        elif kind == "reloaded":
            ok, msg = payload
            self._set_busy(False)
            if ok:
                self._append_result("[重载] " + msg)
            else:
                self._append_result("[重载失败] " + msg)
            messagebox.showinfo("重载 Nginx", msg, parent=self)
        else:
            self._set_busy(False)
            self._append_result("错误：" + str(payload))
            messagebox.showerror("同步失败", str(payload), parent=self)

    def _sync(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._append_result("正在替换端口并校验 nginx 配置…")
        self.btn_sync.configure(state="disabled")

        def worker():
            try:
                results = self.vhost_mgr.sync_port(self.old_port, self.new_port)
                output = self.vhost_mgr.nginx.test_config()
                self._queue.put(("done", (results, output)))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self._poll()

    def _on_done(self, payload) -> None:
        results, output = payload
        self._set_busy(False)
        self.btn_sync.configure(state="normal")
        self._append_result("nginx -t 输出：\n" + (output or "(无输出)"))
        all_ok = True
        for r in results:
            path = r["file"]
            rel = os.path.relpath(path, VHOST_DIR)
            if rel.startswith(".."):
                rel = path
            if r["ok"]:
                status, tag, detail = f"已替换 {r['replaced']} 处", "ok", r["message"]
            else:
                status, tag, detail = "失败", "err", r["message"]
                all_ok = False
            self.tree.insert("", "end", values=(rel, self._file_domains.get(path, "—"),
                                                status, detail), tags=(tag,))
        self._synced_ok = all_ok
        if all_ok:
            self.btn_reload.configure(state="normal")
            self._append_result("全部同步完成：备份保留于各文件 .bak，可点击「重载 Nginx」生效。")
            messagebox.showinfo(
                "同步完成",
                "vhost 配置已全部同步到新端口，并已通过 nginx -t 校验。\n\n"
                "请点击「重载 Nginx」使新配置立即生效。",
                parent=self,
            )
        else:
            self._append_result("存在失败的同步（已自动还原备份），请检查上方详情。")

    def _reload(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self.btn_reload.configure(state="disabled")

        def worker():
            try:
                msg = self.vhost_mgr.nginx.reload()
                self._queue.put(("reloaded", (True, msg)))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("reloaded", (False, str(e))))

        threading.Thread(target=worker, daemon=True).start()
        self._poll()

    def _open_dir(self) -> None:
        try:
            os.startfile(VHOST_DIR)
        except OSError as e:
            messagebox.showerror("无法打开目录", str(e), parent=self)

    def _append_result(self, text: str) -> None:
        self.result_text.configure(state="normal")
        self.result_text.insert("end", text + "\n")
        self.result_text.configure(state="disabled")
        self.result_text.see("end")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy

    def _center(self, master) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")


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


class CrashDialog(tk.Toplevel):
    """php-cgi 崩溃事件详情（Windows 事件日志 Application/1000）。

    两个页签：崩溃事件 + 自愈历史（recover_history.json 可视化）。
    """

    def __init__(self, master, events: list[dict], on_clear=None):
        super().__init__(master)
        self.events = events
        self.on_clear = on_clear
        self.title("php-cgi 崩溃事件")
        self.geometry("860x660")
        self.minsize(720, 540)
        self.configure(bg=CARD_BG)
        self.transient(master)

        header = ttk.Frame(self, padding=(16, 14, 16, 4))
        header.pack(fill="x")
        ttk.Label(header, text="php-cgi 崩溃事件（事件日志 Application/1000）",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="以下记录来自 Windows 事件日志。异常码 0xc0000005（访问冲突）通常是扩展/JIT/"
                 "代码段错误导致，崩溃后站点会 502。",
            style="SubTitle.TLabel", wraplength=800,
        ).pack(anchor="w", pady=(4, 0))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=16, pady=8)

        crash_tab = ttk.Frame(nb, padding=4)
        recover_tab = ttk.Frame(nb, padding=4)
        nb.add(crash_tab, text=" 崩溃事件 ")
        nb.add(recover_tab, text=" 自愈历史 ")

        self._build_crash_tab(crash_tab)
        self._build_recover_tab(recover_tab)

        btns = ttk.Frame(self, padding=(16, 0, 16, 14))
        btns.pack(fill="x")
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="right")
        self.clear_btn = ttk.Button(btns, text="清空记录", command=self._clear)
        self.clear_btn.pack(side="right", padx=(0, 8))
        if not events:
            self.clear_btn.configure(state="disabled")
        self._center(master)

    def _build_crash_tab(self, parent) -> None:
        cols = [("time", "崩溃时间", 150), ("version", "版本", 60), ("app", "进程", 110),
                ("module", "故障模块", 140), ("exception", "异常码", 95), ("offset", "偏移", 160)]
        self.tree = ttk.Treeview(parent, columns=[c[0] for c in cols], show="headings", height=7)
        for cid, text, w in cols:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor="w", stretch=(cid == "module"))
        vsb = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        detail_wrap = ttk.Frame(parent)
        detail_wrap.pack(fill="both", expand=True, pady=(4, 0))
        self.detail = tk.Text(detail_wrap, height=9, wrap="char", font=("Consolas", 9),
                              background="#FFFFFF", foreground=TEXT, relief="flat",
                              padx=10, pady=8, state="disabled")
        dvsb = ttk.Scrollbar(detail_wrap, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=dvsb.set)
        self.detail.pack(side="left", fill="both", expand=True)
        dvsb.pack(side="right", fill="y")

        if not self.events:
            self.tree.insert("", "end", values=("—", "—", "—", "—", "—", "—"))
            self._set_detail("（没有崩溃记录）")
        else:
            for e in self.events:
                self.tree.insert("", "end", values=(
                    e.get("time", "—"), e.get("version") or "—", e.get("app", "—"),
                    e.get("module", "—"), e.get("exception", "—"), e.get("offset", "—"),
                ))
            self._set_detail(self.events[0].get("message", ""))
        self.tree.bind("<<TreeviewSelect>>", lambda ev: self._on_select())

    def _build_recover_tab(self, parent) -> None:
        """自愈历史：recover_history.json 最新在前，失败行红色标记。"""
        info = ttk.Label(parent, text="自愈操作历史（防抖/限次/重启/失败，存于 recover_history.json）",
                         style="SubTitle.TLabel")
        info.pack(anchor="w", pady=(0, 4))
        cols = [("time", "时间", 150), ("version", "版本", 60), ("action", "动作", 90), ("detail", "详情", 480)]
        self.rec_tree = ttk.Treeview(parent, columns=[c[0] for c in cols], show="headings")
        for cid, text, w in cols:
            self.rec_tree.heading(cid, text=text)
            self.rec_tree.column(cid, width=w, anchor="w", stretch=(cid == "detail"))
        rvsb = ttk.Scrollbar(parent, orient="vertical", command=self.rec_tree.yview)
        self.rec_tree.configure(yscrollcommand=rvsb.set)
        self.rec_tree.pack(side="left", fill="both", expand=True)
        rvsb.pack(side="right", fill="y")
        self.rec_tree.tag_configure("fail", foreground=ERR)
        self.rec_tree.tag_configure("ok", foreground=OK)

        rows = recover_history.load()
        if not rows:
            self.rec_tree.insert("", "end", values=("—", "—", "—", "（暂无自愈记录）"))
        for r in rows:
            action = r.get("action", "")
            tag = "fail" if action == "fail" else ("ok" if action == "start" else "")
            self.rec_tree.insert("", "end", values=(
                r.get("time", "—"), r.get("version", "—"),
                recover_history.ACTION_LABELS.get(action, action), r.get("detail", ""),
            ), tags=(tag,) if tag else ())

    def _clear(self) -> None:
        """清空已读崩溃告警：回调主窗口重置游标并关闭状态栏告警。"""
        if self.on_clear is not None:
            self.on_clear()
        self.destroy()

    def _on_select(self) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        idx = self.tree.index(sel[0])
        if idx < len(self.events):
            self._set_detail(self.events[idx].get("message", ""))

    def _set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        # 完整展示；事件消息中的字段分隔符还原为换行，逐项可读
        self.detail.insert("1.0", text.replace(" | ", "\n"))
        self.detail.configure(state="disabled")

    def _center(self, master) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")


class SelfCheckDialog(tk.Toplevel):
    """版本一键自检：php -v / 关键扩展 / 配置加载，结果分级展示。"""

    def __init__(self, master, version: PhpVersion, health):
        super().__init__(master)
        self.version = version
        self.health = health
        self._queue: queue.Queue = queue.Queue()

        self.title(f"版本自检 · {version.name} (PHP {version.display})")
        self.geometry("680x420")
        self.minsize(560, 360)
        self.configure(bg=CARD_BG)
        self.transient(master)

        header = ttk.Frame(self, padding=(16, 14, 16, 4))
        header.pack(fill="x")
        ttk.Label(header, text=f"自检 [{version.name}] · PHP {version.display}",
                  style="Title.TLabel").pack(anchor="w")
        self.state_label = ttk.Label(header, text="正在检测…", style="SubTitle.TLabel")
        self.state_label.pack(anchor="w", pady=(4, 0))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=16, pady=10)
        self.tree = ttk.Treeview(wrap, columns=("name", "status", "detail"),
                                 show="headings", selectmode="browse")
        for cid, text, w in (("name", "检查项", 200), ("status", "结果", 70), ("detail", "详情", 340)):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor="w", stretch=(cid == "detail"))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("ok", foreground=OK)
        self.tree.tag_configure("err", foreground=ERR)

        btns = ttk.Frame(self, padding=(16, 0, 16, 14))
        btns.pack(fill="x")
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="重新检测", command=self._run).pack(side="right", padx=(0, 8))
        self._center(master)
        self._run()

    def _run(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.state_label.configure(text="正在检测…")

        def worker():
            try:
                result = self.health.self_check(self.version)
                self._queue.put(("result", result))
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
        if kind == "error":
            self.state_label.configure(text=f"自检失败：{payload}")
            return
        result = payload
        for c in result["checks"]:
            status = "正常" if c["ok"] else "异常"
            tag = "ok" if c["ok"] else "err"
            self.tree.insert("", "end", values=(c["name"], status, c["detail"]), tags=(tag,))
        total = "全部通过" if result["ok"] else "存在异常"
        self.state_label.configure(
            text=f"{total} · 关键扩展核对 {len(result['checks'])} 项"
        )
        if not result["ok"]:
            self.state_label.configure(foreground=ERR)

    def _center(self, master) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")


class IniEditDialog(tk.Toplevel):
    """ini 关键配置项表单编辑：读写安全（备份 + 精确行替换），改后提示重启生效。"""

    def __init__(self, master, version: PhpVersion, php_mgr: PhpManager):
        super().__init__(master)
        self.version = version
        self.php_mgr = php_mgr
        self._queue: queue.Queue = queue.Queue()
        self._vars: dict[str, tk.Variable] = {}

        self.title(f"编辑配置 · {version.name}")
        self.geometry("680x520")
        self.minsize(560, 420)
        self.configure(bg=CARD_BG)
        self.transient(master)

        header = ttk.Frame(self, padding=(16, 14, 16, 4))
        header.pack(fill="x")
        ttk.Label(header, text=f"编辑常用配置项 · {os.path.basename(version.ini)}",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="保存前自动备份原文件（.bak）；修改后需重启对应版本（FastCGI）才生效。",
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=16, pady=10)
        canvas = tk.Canvas(wrap, background=CARD_BG, highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inner_id, width=e.width))
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._form = inner

        btns = ttk.Frame(self, padding=(16, 0, 16, 14))
        btns.pack(fill="x")
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="保存", style="Accent.TButton", command=self._save).pack(
            side="right", padx=(0, 8))
        self._build_form()
        self._center(master)

    def _build_form(self) -> None:
        from core import ini_editor
        current = ini_editor.load_values(self.version.ini)
        for i, meta in enumerate(ini_editor.INI_ITEMS_META):
            row = ttk.Frame(self._form)
            row.pack(fill="x", pady=4, padx=8)
            ttk.Label(row, text=meta["key"], font=(FONT, 9, "bold"),
                      background=CARD_BG, width=24, anchor="w").grid(
                row=0, column=0, sticky="w")
            var = tk.StringVar(value=current.get(meta["key"], ""))
            self._vars[meta["key"]] = var
            if meta.get("type") in ("onoff", "enum"):
                options = meta.get("options") or ["On", "Off"]
                cb = ttk.Combobox(row, textvariable=var, values=options,
                                  state="readonly", width=30)
                cb.grid(row=0, column=1, sticky="w")
            else:
                entry = ttk.Entry(row, textvariable=var, width=32)
                entry.grid(row=0, column=1, sticky="w")
            ttk.Label(row, text=meta.get("hint", ""), style="SubTitle.TLabel",
                      background=CARD_BG).grid(row=0, column=2, sticky="w", padx=(8, 0))

    def _save(self) -> None:
        from core import ini_editor
        changes: dict[str, str] = {}
        for meta in ini_editor.INI_ITEMS_META:
            key, value = meta["key"], self._vars[key].get().strip()
            err = ini_editor.validate_value(meta, value)
            if err:
                messagebox.showerror("校验失败", f"{key}：{err}", parent=self)
                return
            changes[key] = value

        def worker():
            try:
                count, backup = ini_editor.save_values(self.version.ini, changes)
                self._queue.put(("saved", (count, backup)))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_save()

    def _poll_save(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            self.after(80, self._poll_save)
            return
        if kind == "error":
            messagebox.showerror("保存失败", str(payload), parent=self)
            return
        count, backup = payload
        messagebox.showinfo(
            "保存成功",
            f"已更新 {count} 项配置，备份保留于：\n{backup}\n\n"
            f"重启 [{self.version.name}]（停止后启动）即可生效。",
            parent=self,
        )
        self.destroy()

    def _center(self, master) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
