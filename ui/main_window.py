# -*- coding: utf-8 -*-
"""主窗口：三页签（PHP 版本管理 / Nginx 管理 / 关于）+ 顶部 cmd php 状态 + 底部状态栏。"""
import queue
import threading
import tkinter as tk
from tkinter import ttk

from core import path_manager
from core.config import Config
from core.nginx_manager import NginxManager
from core.php_manager import PhpManager
from .dialogs import CliSwitchDialog
from .nginx_panel import NginxPanel
from .php_panel import PhpPanel
from .theme import BG, CARD_BG, ERR, FONT, GRAY, OK, PRIMARY, TEXT, setup_style
from .tray import TrayIcon

APP_TITLE = "phpvm · PHP 版本管理器"
WNRP_ROOT_SHOW = r"C:\wnrp"


class MainWindow(tk.Tk):
    def __init__(self, php_mgr: PhpManager, nginx_mgr: NginxManager, config: Config):
        super().__init__()
        self.php_mgr = php_mgr
        self.nginx_mgr = nginx_mgr
        self.config = config

        self.title(APP_TITLE)
        self.geometry("1080x680")
        self.minsize(960, 600)
        self.configure(bg=BG)
        setup_style(self)

        self._log_var = tk.StringVar(value="就绪")
        self._cli_queue: queue.Queue = queue.Queue()
        self._build()
        self._refresh_cli()
        self.after(8000, self._tick)

        self._tray = None
        self._init_tray()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        # 标题区
        header = ttk.Frame(self, style="Card.TFrame")
        header.pack(fill="x", padx=12, pady=(12, 8))
        ttk.Label(header, text="PHP 版本管理器", style="Title.TLabel").pack(
            side="left", padx=(16, 8), pady=12
        )
        ttk.Label(header, text=f"环境根目录 {WNRP_ROOT_SHOW}", style="SubTitle.TLabel").pack(
            side="left", pady=12
        )

        # 右侧：cmd php 命令版本状态 + 切换
        cli_box = ttk.Frame(header)
        cli_box.pack(side="right", padx=16, pady=10)
        self.cli_dot = tk.Label(
            cli_box, text="●", font=(FONT, 12), background=CARD_BG, foreground=GRAY
        )
        self.cli_dot.pack(side="left", padx=(0, 6))
        self.cli_label = tk.Label(
            cli_box, text="CMD php：检测中…", font=(FONT, 9, "bold"),
            background=CARD_BG, foreground=TEXT,
        )
        self.cli_label.pack(side="left", padx=(0, 10))
        self.btn_cli = ttk.Button(cli_box, text="切换", command=self._open_cli_switch)
        self.btn_cli.pack(side="left")

        # 页签
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.php_panel = PhpPanel(nb, self.php_mgr, self.config, self.set_log)
        self.nginx_panel = NginxPanel(nb, self.nginx_mgr, self.set_log)
        about = self._build_about(nb)
        nb.add(self.php_panel, text="  PHP 版本管理  ")
        nb.add(self.nginx_panel, text="  Nginx 管理  ")
        nb.add(about, text="  关于  ")

        # 状态栏
        bar = ttk.Frame(self, style="Status.TFrame")
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self._log_var, style="Status.TLabel").pack(
            side="left", fill="x", expand=True, padx=10, pady=4
        )

    def _build_about(self, master) -> ttk.Frame:
        frame = ttk.Frame(master, padding=18)
        ttk.Label(frame, text="phpvm · PHP 版本管理器", style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(
            frame,
            text="管理 C:\\wnrp 下多个 PHP 版本的启动 / 停止 / 重启 / 状态 / 端口 / 配置，"
                 "并附带 Nginx 管理。",
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(0, 14))

        info = ttk.LabelFrame(frame, text="环境信息", padding=12)
        info.pack(fill="x")
        rows = [
            ("环境根目录", WNRP_ROOT_SHOW),
            ("PHP FastCGI 配置", "php82/php85 → php-web.ini，其余 → php.ini"),
            ("FastCGI 监听", "127.0.0.1:端口（按版本配置，见 PHP 版本管理页）"),
            ("Nginx 前缀", r"C:\wnrp\nginx"),
            ("隐藏启动器", r"C:\wnrp\RunHiddenConsole.exe"),
            ("配置持久化", r"C:\wnrp\phpvm\config.json"),
        ]
        for i, (k, v) in enumerate(rows):
            ttk.Label(info, text=f"{k}：", font=(FONT, 9, "bold"), background=CARD_BG).grid(
                row=i, column=0, sticky="w", padx=(8, 4), pady=3
            )
            ttk.Label(info, text=v, font=(FONT, 9), background=CARD_BG).grid(
                row=i, column=1, sticky="w", pady=3
            )

        ttk.Label(
            frame,
            text="\n提示：修改端口后需同步修改对应 nginx vhost 的 fastcgi_pass 才会生效。\n"
                 "phpvm 按端口精确启停，不会像旧的 start_phpXX.bat 那样误杀其它版本进程。",
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(14, 0))
        return frame

    # ------------------------------------------------------------------ #
    def set_log(self, msg: str) -> None:
        self._log_var.set(msg)

    # cmd php 版本展示 / 切换
    def _open_cli_switch(self) -> None:
        CliSwitchDialog(self, self.php_mgr, on_switched=self._refresh_cli)

    def _refresh_cli(self) -> None:
        """后台读取当前 cmd php 生效版本（含 User/Machine PATH 顺序），避免阻塞 UI。"""
        def worker():
            try:
                info = path_manager.get_cli_info()
            except Exception:  # noqa: BLE001
                info = {"dir": None, "name": None, "version": None}
            self._cli_queue.put(info)

        threading.Thread(target=worker, daemon=True).start()
        self._poll_cli()

    def _poll_cli(self) -> None:
        try:
            info = self._cli_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_cli)
            return
        name, version = info.get("name"), info.get("version")
        if not name:
            self.cli_dot.configure(foreground=GRAY)
            self.cli_label.configure(text="CMD php：未启用 wnrp 版本")
            return
        running_fg = OK if version != "未知" else ERR
        self.cli_dot.configure(foreground=running_fg)
        self.cli_label.configure(text=f"CMD php：{name} · PHP {version}")

    def _tick(self) -> None:
        # 自动轻量刷新状态（面板内部自行排队异步执行）
        try:
            self.php_panel.auto_refresh()
            self.nginx_panel.auto_refresh()
        except Exception:  # noqa: BLE001
            pass
        # cmd php 版本号变化极少，降频刷新（每 4 轮 tick ≈ 32s 一次）
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        if self._tick_count % 4 == 0:
            self._refresh_cli()
        self.after(8000, self._tick)

    # ------------------------------------------------------------------ #
    # 系统托盘 / 关闭行为
    def _init_tray(self) -> None:
        try:
            self._tray = TrayIcon(
                self.winfo_id(),
                tip=APP_TITLE,
                on_show=self._show_window,
                on_exit=self._real_quit,
            )
        except Exception:  # noqa: BLE001
            self._tray = None

    def _show_window(self) -> None:
        self.deiconify()
        self.lift()
        self.focus_force()

    def _on_close(self) -> None:
        """点击关闭按钮：弹确认框，可选最小化到托盘 / 退出 / 取消。"""
        dlg = tk.Toplevel(self)
        dlg.title("关闭 phpvm")
        dlg.geometry("320x150")
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        dlg.configure(bg=BG)
        setup_style(dlg)

        ttk.Label(
            dlg, text="要如何关闭 phpvm？", style="Title.TLabel"
        ).pack(pady=(14, 6))
        ttk.Label(
            dlg, text="可最小化到系统托盘后台运行，或完全退出。",
            style="SubTitle.TLabel",
        ).pack(pady=(0, 10))

        def choose(action):
            dlg.destroy()
            if action == "tray":
                self.withdraw()
            elif action == "exit":
                self._real_quit()

        frm = ttk.Frame(dlg)
        frm.pack(pady=(0, 10))
        ttk.Button(frm, text="最小化到托盘", command=lambda: choose("tray")).pack(
            side="left", padx=6
        )
        ttk.Button(frm, text="退出", command=lambda: choose("exit")).pack(
            side="left", padx=6
        )
        ttk.Button(frm, text="取消", command=dlg.destroy).pack(side="left", padx=6)

        dlg.wait_window()

    def _real_quit(self) -> None:
        if self._tray:
            self._tray.remove()
            self._tray = None
        self.destroy()
