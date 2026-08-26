# -*- coding: utf-8 -*-
"""「下载 PHP 版本」对话框：官方源候选列表 → 自动适配 → 下载安装。

- 候选列表：releases.json 为主源 + archives/ 补充 7.x 旧版，标注 已安装/可更新/可安装
- 线程安全模式：默认 NTS（FastCGI 官方推荐），可手动切换 TS
- 下载进度条 + 阶段文字；完成后回调刷新版本列表
"""
import queue
import threading
import tkinter as tk
import tkinter.ttk as ttk
from tkinter import messagebox

from core.config import Config
from core.php_downloader import (
    build_candidates,
    detect_arch,
    version_key,
)
from core.php_installer import default_port_for, install, install_dir_for
from core.php_manager import PhpManager
from .theme import CARD_BG, GRAY, PRIMARY_LIGHT, TEXT, WARN, setup_style

STATE_INSTALLED = "已安装"
STATE_UPDATE = "可更新"
STATE_READY = "可安装"

# 进度阶段 → 界面文案
_STAGE_TEXT = {
    "下载": "正在下载",
    "校验": "正在校验 SHA-256",
    "解压": "正在解压",
    "落盘": "正在落盘到目标目录",
    "生成配置": "正在生成 php.ini / php-web.ini",
    "验证": "正在运行 php -v 验证",
}


class DownloadDialog(tk.Toplevel):
    """下载并安装新版 PHP 的模态对话框。"""

    def __init__(self, master, php_mgr: PhpManager, config: Config, on_installed=None):
        super().__init__(master)
        self.php_mgr = php_mgr
        self.config = config
        self.on_installed = on_installed

        self.arch = detect_arch()
        self.ts_mode = tk.StringVar(value="nts")
        self.queue = queue.Queue()
        self._candidates = []
        self._installed = {}        # name -> display（已装版本号，可能为空串）
        self._selected = None       # 当前选中的 SeriesCandidate
        self._selected_state = STATE_READY
        self._busy = False

        self.title("下载 PHP 版本")
        self.geometry("680x600")
        self.minsize(620, 540)
        self.resizable(True, True)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._center()
        self._start_load()
        self.after(80, self._poll)

    # ------------------------------------------------------------------ UI #
    def _build_ui(self):
        st = setup_style(self)
        root = ttk.Frame(self, style="Card.TFrame", padding=18)
        root.pack(fill="both", expand=True)

        # 标题区
        head = ttk.Frame(root, style="Card.TFrame")
        head.pack(fill="x", pady=(0, 12))
        ttk.Label(head, text="下载 PHP 版本", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            head,
            text="数据源 windows.php.net，自动适配当前系统架构",
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))
        arch_badge = tk.Label(
            head, text=f"本机 {self.arch.upper()}",
            bg=PRIMARY_LIGHT, fg=TEXT, font=("Microsoft YaHei", 9, "bold"),
            padx=12, pady=4,
        )
        arch_badge.pack(side="right", anchor="n")

        # 候选列表区
        list_frame = ttk.Frame(root, style="Card.TFrame")
        list_frame.pack(fill="both", expand=True, pady=(0, 12))
        self._hint_bar = ttk.Frame(list_frame, style="Card.TFrame")
        self._hint_bar.pack(fill="x", pady=(0, 6))
        self._hint = ttk.Label(self._hint_bar, text="正在获取可用版本…",
                               style="SubTitle.TLabel", anchor="w")
        self._hint.pack(side="left")
        self._retry_btn = ttk.Button(self._hint_bar, text="重试",
                                     command=self._on_retry)
        self._retry_btn.pack(side="right")
        self._retry_btn.pack_forget()

        cols = ("series", "ts", "compiler", "size", "state")
        widths = {"series": 150, "ts": 70, "compiler": 90, "size": 90, "state": 110}
        self._tree = ttk.Treeview(
            list_frame, columns=cols, show="headings", selectmode="browse",
            height=10,
        )
        for col in cols:
            self._tree.heading(col, text={"series": "PHP 版本", "ts": "线程",
                                          "compiler": "编译器", "size": "大小",
                                          "state": "状态"}[col])
            self._tree.column(col, width=widths[col], anchor="w" if col == "series" else "center",
                              stretch=col in ("series",))
        self._tree.tag_configure("installed", foreground=GRAY)
        self._tree.tag_configure("update", foreground=WARN, font=("Microsoft YaHei", 9, "bold"))
        self._tree.tag_configure("ready", foreground=TEXT)
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        # 配置区（卡片）
        cfg = ttk.Frame(root, style="Card.TFrame", relief="solid", padding=12)
        cfg.pack(fill="x", pady=(0, 12))
        ttk.Label(cfg, text="安装配置", style="Section.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ttk.Label(cfg, text="线程安全模式：", style="SubTitle.TLabel").grid(
            row=1, column=0, sticky="w")
        ts_frame = ttk.Frame(cfg, style="Card.TFrame")
        ts_frame.grid(row=1, column=1, sticky="w")
        for val, label in (("nts", "NTS（FastCGI 推荐）"), ("ts", "TS（线程安全）")):
            rb = tk.Radiobutton(
                ts_frame, text=label, value=val, variable=self.ts_mode,
                command=self._on_ts_change, bg=CARD_BG, fg=TEXT,
                activebackground=CARD_BG, activeforeground=TEXT,
                selectcolor="#FFFFFF", font=("Microsoft YaHei", 9),
                highlightthickness=0, bd=0,
            )
            rb.pack(side="left", padx=(0, 14))

        self._summary = ttk.Label(cfg, text="请选择一个版本", style="SubTitle.TLabel",
                                  background=CARD_BG, anchor="w")
        self._summary.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # 进度区
        prog = ttk.Frame(root, style="Card.TFrame")
        prog.pack(fill="x", pady=(0, 12))
        self._prog = ttk.Progressbar(prog, maximum=100, mode="determinate")
        self._prog.pack(fill="x")
        self._prog_text = ttk.Label(prog, text="", style="SubTitle.TLabel", anchor="w")
        self._prog_text.pack(fill="x", pady=(4, 0))

        # 操作区
        actions = ttk.Frame(root, style="Card.TFrame")
        actions.pack(fill="x")
        self._btn_install = ttk.Button(actions, text="开始下载", style="Accent.TButton",
                                       command=self._on_install)
        self._btn_install.pack(side="right")
        ttk.Button(actions, text="关闭", command=self._on_close).pack(side="right", padx=(0, 10))
        self._btn_install.state(["disabled"])

    def _center(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = self.winfo_screenwidth() // 2 - w // 2
        y = self.winfo_screenheight() // 2 - h // 2 - 40
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    # ---------------------------------------------------------- 加载候选列表 #
    def _start_load(self):
        t = threading.Thread(target=self._load_worker, daemon=True)
        t.start()

    def _on_retry(self):
        self._retry_btn.pack_forget()
        self._hint.config(text="正在获取可用版本…")
        self._start_load()

    def _load_worker(self):
        try:
            candidates = build_candidates(include_legacy=True)
            versions = self.php_mgr.scan_versions()  # 轻量扫描，不解析版本号
            installed = {v.name: v.display for v in versions}
            self.queue.put(("loaded", candidates, installed))
        except Exception as e:  # noqa: BLE001
            self.queue.put(("load_error", f"{type(e).__name__}: {e}"))

    def _render_candidates(self):
        self._tree.delete(*self._tree.get_children())
        for cand in self._candidates:
            name = install_dir_for(cand.series)
            pkg = cand.package("nts", self.arch) or cand.package("ts", self.arch)
            if pkg is None:
                continue
            installed_disp = self._installed.get(name, "")
            state, tag = STATE_READY, "ready"
            if name in self._installed:
                if installed_disp and version_key(installed_disp) < version_key(cand.version):
                    state, tag = STATE_UPDATE, "update"
                else:
                    state, tag = STATE_INSTALLED, "installed"
            ts = "NTS" if pkg.ts_mode == "nts" else "TS"
            self._tree.insert(
                "", "end",
                iid=cand.series,
                values=(cand.version, ts, pkg.compiler.upper(), pkg.size or "-", state),
                tags=(tag,),
            )
        self._hint.config(text=f"共 {len(self._candidates)} 个可用版本（含历史归档）"
                           if self._candidates else "暂无可安装的版本")

    def _on_select(self, _evt=None):
        sel = self._tree.selection()
        if not sel:
            self._selected = None
            self._btn_install.state(["disabled"])
            self._summary.config(text="请选择一个版本")
            return
        series = sel[0]
        cand = next((c for c in self._candidates if c.series == series), None)
        if cand is None:
            return
        self._selected = cand
        name = install_dir_for(cand.series)
        self._selected_state = STATE_INSTALLED if name in self._installed else STATE_READY
        if self._selected_state == STATE_INSTALLED:
            self._btn_install.state(["disabled"])
        else:
            self._btn_install.state(["!disabled"])
        self._update_summary()

    def _on_ts_change(self):
        if self._selected:
            self._update_summary()

    def _update_summary(self):
        cand = self._selected
        if cand is None:
            self._summary.config(text="请选择一个版本")
            return
        name = install_dir_for(cand.series)
        port = default_port_for(name, self.config)
        pkg = cand.package(self.ts_mode.get(), self.arch)
        if pkg is None:
            pkg = cand.package("ts" if self.ts_mode.get() == "nts" else "nts", self.arch)
        size = pkg.size if pkg else "-"
        if self._selected_state == STATE_UPDATE:
            inst = self._installed.get(name, "?")
            status = f"（已装 {inst}，可更新到 {cand.version}）"
        elif self._selected_state == STATE_INSTALLED:
            status = f"（已安装 {self._installed.get(name, cand.version)}）"
        else:
            status = "（全新安装）"
        self._summary.config(
            text=f"将安装 PHP {cand.version} {self.ts_mode.get().upper()} → 目录 {name}，"
                 f"默认端口 {port}，包大小 {size} {status}")

    # ------------------------------------------------------------ 下载安装 #
    def _on_install(self):
        cand = self._selected
        if cand is None or self._busy:
            return
        if self._selected_state == STATE_INSTALLED:
            messagebox.showinfo("提示", "该版本已安装，请选择其他版本。", parent=self)
            return
        pkg = cand.package(self.ts_mode.get(), self.arch)
        if pkg is None:
            messagebox.showerror("错误", "该版本在当前架构下没有可用安装包。", parent=self)
            return
        self._set_busy(True)
        self._prog.configure(mode="determinate", value=0)
        self._prog_text.config(text="正在下载… 0%")
        t = threading.Thread(target=self._install_worker, args=(pkg,), daemon=True)
        t.start()

    def _install_worker(self, pkg):
        try:
            result = install(pkg, self.config, progress=self._on_progress)
            self.queue.put(("installed", result))
        except Exception as e:  # noqa: BLE001
            self.queue.put(("install_error", f"安装失败：{type(e).__name__}: {e}"))

    def _on_progress(self, stage: str, ratio: float | None):
        self.queue.put(("progress", stage, ratio))

    # ------------------------------------------------------------ 事件轮询 #
    def _poll(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                kind = msg[0]
                if kind == "loaded":
                    _, candidates, installed = msg
                    self._candidates = candidates
                    self._installed = installed
                    self._render_candidates()
                    if candidates:
                        # 默认选中第一个未安装的版本
                        for cand in candidates:
                            if install_dir_for(cand.series) not in installed:
                                self._tree.selection_set(cand.series)
                                self._tree.see(cand.series)
                                self._on_select()
                                break
                elif kind == "load_error":
                    _, err = msg
                    self._hint.config(text=f"获取版本列表失败：{err}（请检查网络后重试）")
                    self._tree.delete(*self._tree.get_children())
                    self._retry_btn.pack(side="right")
                elif kind == "progress":
                    _, stage, ratio = msg
                    self._render_progress(stage, ratio)
                elif kind == "installed":
                    self._handle_result(msg[1])
                elif kind == "install_error":
                    self._set_busy(False)
                    self._prog.configure(mode="determinate", value=0)
                    self._prog_text.config(text="")
                    messagebox.showerror("安装失败", msg[1], parent=self)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._poll)

    def _render_progress(self, stage: str, ratio: float | None):
        base = _STAGE_TEXT.get(stage, stage)
        if ratio is None:
            self._prog.configure(mode="determinate", value=100)
            self._prog_text.config(text=f"{base}…")
        else:
            pct = max(0.0, min(100.0, ratio * 100))
            self._prog.configure(mode="determinate", value=pct)
            self._prog_text.config(text=f"{base}… {pct:.1f}%")

    def _handle_result(self, result):
        self._set_busy(False)
        if result.ok:
            msg = (f"{result.message}\n\n已自动完成：ini 配置生成、端口分配（{result.port}）。"
                   f"可在主界面直接启动该版本。")
            messagebox.showinfo("安装完成", msg, parent=self)
            if callable(self.on_installed):
                try:
                    self.on_installed()
                except Exception:  # noqa: BLE001
                    pass
            self._safe_destroy()
        else:
            messagebox.showerror("安装失败", result.message, parent=self)
            self._set_busy(False)
            self._prog.configure(mode="determinate", value=0)
            self._prog_text.config(text="")

    # ---------------------------------------------------------------- 辅助 #
    def _set_busy(self, busy: bool):
        self._busy = busy
        state = ["disabled"] if busy else ["!disabled"]
        if not busy and (self._selected is None or self._selected_state == STATE_INSTALLED):
            state = ["disabled"]
        self._btn_install.state(state)
        self._tree.state(["disabled"] if busy else ["!disabled"])

    def _on_close(self):
        if self._busy:
            messagebox.showwarning("提示", "下载安装进行中，请稍候…", parent=self)
            return
        self._safe_destroy()

    def _safe_destroy(self):
        if self.winfo_exists():
            self.destroy()
