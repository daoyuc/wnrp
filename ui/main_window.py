# -*- coding: utf-8 -*-
"""主窗口：多页签（PHP 版本管理 / Nginx 管理 / 站点映射 / Nginx 日志 / 关于）+ 顶部 cmd php 状态 + 底部状态栏。"""
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from core import autostart, path_manager
from core.config import Config
from core.health_monitor import HealthMonitor
from core.nginx_manager import NginxManager
from core.php_manager import PhpManager
from core.vhost_manager import VhostManager
from .dialogs import CliSwitchDialog, CrashDialog
from .nginx_log_panel import NginxLogPanel
from .nginx_panel import NginxPanel
from .php_panel import PhpPanel
from .theme import BG, CARD_BG, ERR, FONT, GRAY, OK, PRIMARY, PRIMARY_LIGHT, TEXT, setup_style
from .tray import TrayIcon
from .vhost_panel import VhostPanel

APP_TITLE = "phpvm · PHP 版本管理器"
WNRP_ROOT_SHOW = r"C:\wnrp"
CRASH_POLL_TICKS = 8  # 崩溃检测频率 ≈ 8 × 8s = 64s 一次
# 崩溃自愈防抖 / 限次
RECOVER_MIN_INTERVAL = 60.0   # 同一版本两次自愈最小间隔（秒）
RECOVER_WINDOW = 3600.0       # 计数窗口（秒）


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
        self._crash_queue: queue.Queue = queue.Queue()
        self._tray_queue: queue.Queue = queue.Queue()
        self.health = HealthMonitor()
        self._crash_alert_active = False
        self._crash_tick = 0
        self._build()
        self._refresh_cli()
        self.after(8000, self._tick)
        # 启动后稍作延迟，回溯最近 24h 的 php-cgi 崩溃（不弹窗，仅状态栏/托盘提示）
        self.after(1500, self._check_crash_startup)

        self._tray = None
        self._init_tray()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 点最小化按钮 → 直接隐藏到系统托盘（恢复：托盘菜单/双击「显示 phpvm」）
        self.bind("<Unmap>", self._on_unmap)

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
        self.vhost_panel = VhostPanel(nb, VhostManager(self.config), self.set_log)
        self.log_panel = NginxLogPanel(nb, self.set_log)
        about = self._build_about(nb)
        nb.add(self.php_panel, text="  PHP 版本管理  ")
        nb.add(self.nginx_panel, text="  Nginx 管理  ")
        nb.add(self.vhost_panel, text="  站点映射  ")
        nb.add(self.log_panel, text="  Nginx 日志  ")
        nb.add(about, text="  关于  ")

        # 状态栏
        bar = ttk.Frame(self, style="Status.TFrame")
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self._log_var, style="Status.TLabel").pack(
            side="left", fill="x", expand=True, padx=10, pady=4
        )
        self._alert_label = tk.Label(
            bar, text="", font=(FONT, 9, "bold"), foreground=ERR,
            background=PRIMARY_LIGHT, cursor="hand2",
        )
        self._alert_label.pack(side="right", padx=10, pady=4)
        self._alert_label.bind("<Button-1>", lambda e: self._show_crash_detail())

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

        # 设置区：开机自启 + 崩溃自愈
        settings = ttk.LabelFrame(frame, text="设置", padding=12)
        settings.pack(fill="x", pady=(10, 0))
        self._autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(
            settings, text="开机自动启动 phpvm（当前用户）",
            variable=self._autostart_var, command=self._toggle_autostart,
        ).pack(anchor="w", pady=(0, 6))
        self._recover_var = tk.BooleanVar(value=bool(self.config.get_setting("auto_recover_crash", False)))
        ttk.Checkbutton(
            settings, text="php-cgi 崩溃后自动重启（自愈，默认关闭）",
            variable=self._recover_var, command=self._toggle_recover,
        ).pack(anchor="w")
        ttk.Label(
            settings,
            text="自愈防抖 60 秒、每版本每小时最多 3 次，防止崩溃循环刷进程。",
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))

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

    # 设置区开关（关于页）
    def _toggle_autostart(self) -> None:
        target = self._autostart_var.get()
        ok = autostart.enable() if target else autostart.disable()
        if not ok:
            self._autostart_var.set(autostart.is_enabled())
            messagebox.showerror("开机自启", "修改注册表失败，请检查权限", parent=self)
            return
        self.set_log("开机自启已启用" if target else "开机自启已关闭")

    def _toggle_recover(self) -> None:
        enabled = self._recover_var.get()
        self.config.set_setting("auto_recover_crash", enabled)
        self.set_log("崩溃自愈已开启" if enabled else "崩溃自愈已关闭")

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
            self.log_panel.auto_refresh()
        except Exception:  # noqa: BLE001
            pass
        # cmd php 版本号变化极少，降频刷新（每 4 轮 tick ≈ 32s 一次）
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        if self._tick_count % 4 == 0:
            self._refresh_cli()
        # 崩溃检测（低频轮询事件日志）
        self._crash_tick += 1
        if self._crash_tick >= CRASH_POLL_TICKS:
            self._crash_tick = 0
            self._poll_crash()
        self.after(8000, self._tick)

    # ------------------------------------------------------------------ #
    # 崩溃告警
    def _check_crash_startup(self) -> None:
        """启动回溯：查询最近 24h 崩溃，仅状态栏 + 托盘气泡提示。"""

        def worker():
            try:
                events = self.health.poll_new_crashes(24)
            except Exception:  # noqa: BLE001
                events = []
            self._crash_queue.put(("startup", events))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_crash_queue()

    def _poll_crash(self) -> None:
        """定时轮询新增崩溃事件。"""

        def worker():
            try:
                events = self.health.poll_new_crashes(24)
            except Exception:  # noqa: BLE001
                events = []
            self._crash_queue.put(("tick", events))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_crash_queue()

    def _poll_crash_queue(self) -> None:
        try:
            kind, events = self._crash_queue.get_nowait()
        except queue.Empty:
            self.after(120, self._poll_crash_queue)
            return
        if events:
            self._on_crash(events, startup=(kind == "startup"))

    def _on_crash(self, events: list[dict], startup: bool) -> None:
        """收到崩溃事件：状态栏告警 + 托盘气泡；运行中新崩溃额外弹详情。"""
        self._crash_alert_active = True
        n = len(events)
        summary = self._crash_summary(events)
        self._alert_label.configure(text=f"⚠ php-cgi 崩溃 {n} 次，点击查看")
        self.set_log(f"检测到 php-cgi 崩溃（{n} 次），详见状态栏告警")
        if self._tray is not None:
            try:
                self._tray.show_balloon("php-cgi 崩溃告警", summary)
            except Exception:  # noqa: BLE001
                pass
        if not startup:
            self._show_crash_detail(events)
            # 崩溃自愈（默认关闭，可在「关于」页开启）
            if self.config.get_setting("auto_recover_crash", False):
                self._auto_recover(events)

    def _auto_recover(self, events: list[dict]) -> None:
        """运行中检测到新崩溃时自动重启对应版本 php-cgi。

        防抖：同一版本两次自愈最小间隔 RECOVER_MIN_INTERVAL；
        限次：每 RECOVER_WINDOW 窗口内每版本最多 auto_recover_limit 次。
        """
        limit = int(self.config.get_setting("auto_recover_limit", 3) or 3)
        now = time.time()
        self._recover_log = getattr(self, "_recover_log", {})

        for e in events:
            ver = e.get("version")
            if not ver:
                continue
            v = next((x for x in self.php_mgr.versions if x.name == ver), None)
            if v is None:
                continue
            rec = self._recover_log.get(ver)
            if rec:
                last_ts, count, window_start = rec
                if now - last_ts < RECOVER_MIN_INTERVAL:
                    continue
                if now - window_start > RECOVER_WINDOW:
                    count, window_start = 0, now
                if count >= limit:
                    self.set_log(f"自愈已达上限（{limit} 次/小时），暂停自动重启 {ver}")
                    continue
            else:
                count, window_start = 0, now

            def do(v=v, ver=ver):
                try:
                    msg = self.php_mgr.start(v)
                    self.set_log(f"自动恢复：{ver} → {msg}")
                    if self._tray is not None:
                        try:
                            self._tray.show_balloon("php-cgi 崩溃自愈", f"{ver}\n{msg}")
                        except Exception:  # noqa: BLE001
                            pass
                except Exception as ex:  # noqa: BLE001
                    self.set_log(f"自动恢复失败 {ver}：{type(ex).__name__}：{ex}")

            threading.Thread(target=do, daemon=True).start()
            self._recover_log[ver] = (now, count + 1, window_start)

    def _crash_summary(self, events: list[dict]) -> str:
        lines = []
        for e in events[:3]:
            ver = f"[{e['version']}] " if e.get("version") else ""
            lines.append(f"{e['time']} {ver}{e['app']} 异常码 {e['exception']}")
        if len(events) > 3:
            lines.append(f"…共 {len(events)} 次")
        return "\n".join(lines) or "未知"

    def _show_crash_detail(self, events: list[dict] | None = None) -> None:
        CrashDialog(self, events or self.health.recent_crashes)

    # ------------------------------------------------------------------ #
    # 系统托盘 / 关闭行为
    def _init_tray(self) -> None:
        try:
            self._tray = TrayIcon(
                self.winfo_id(),
                tip=APP_TITLE,
                on_show=self._show_window,
                on_exit=self._real_quit,
                menu_builder=self._build_tray_menu,
            )
        except Exception:  # noqa: BLE001
            self._tray = None

    def _on_unmap(self, event) -> None:
        """最小化（iconic）时隐藏到托盘；withdraw/退出触发的 Unmap 不处理。"""
        try:
            if self.state() == "iconic":
                self.withdraw()
        except tk.TclError:
            pass

    def _show_window(self) -> None:
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()
        try:
            self.php_panel.auto_refresh()
            self.nginx_panel.auto_refresh()
            self.log_panel.auto_refresh()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # 托盘动态菜单：PHP 版本 + Nginx 快捷启停
    def _build_tray_menu(self) -> list[dict]:
        items: list[dict] = []
        # 隐藏到托盘（窗口可见时可用）
        items.append({
            "type": "item",
            "label": "隐藏到托盘",
            "enabled": self.state() == "normal",
            "cmd": self.withdraw,
        })
        items.append({"type": "sep"})

        # Nginx 快捷操作
        nginx_items: list[dict] = []
        try:
            running, pids = self.nginx_mgr.get_status()
        except Exception:  # noqa: BLE001
            running, pids = False, []
        if running:
            state_txt = f"运行中 · PID {pids[0]}" if pids else "运行中"
        else:
            state_txt = "已停止"
        nginx_items.append({"type": "item", "label": f"状态：{state_txt}", "enabled": False})
        nginx_items.append({"type": "sep"})
        nginx_items.append({"type": "item", "label": "启动", "enabled": not running,
                            "cmd": lambda: self._tray_action("nginx", "start")})
        nginx_items.append({"type": "item", "label": "停止", "enabled": running,
                            "cmd": lambda: self._tray_action("nginx", "stop")})
        nginx_items.append({"type": "item", "label": "重载配置", "enabled": running,
                            "cmd": lambda: self._tray_action("nginx", "reload")})
        nginx_items.append({"type": "item", "label": "配置检查",
                            "cmd": lambda: self._tray_action("nginx", "test_config")})
        items.append({"type": "submenu", "label": "Nginx", "items": nginx_items})

        # 各 PHP 版本快捷启停
        versions = self.php_mgr.versions or self.php_mgr.scan_versions()
        for v in versions:
            sub: list[dict] = []
            pid_txt = f" · PID {v.pid}" if v.running and v.pid else ""
            sub.append({
                "type": "item",
                "label": f"状态：{'运行中' if v.running else '已停止'}{pid_txt} · 端口 {v.port}",
                "enabled": False,
            })
            sub.append({"type": "sep"})
            sub.append({"type": "item", "label": "启动", "enabled": not v.running,
                        "cmd": lambda v=v: self._tray_action(v, "start")})
            sub.append({"type": "item", "label": "停止", "enabled": v.running,
                        "cmd": lambda v=v: self._tray_action(v, "stop")})
            sub.append({"type": "item", "label": "重启", "enabled": v.running,
                        "cmd": lambda v=v: self._tray_action(v, "restart")})
            display = f"（{v.display}）" if v.display else ""
            mark = "● " if v.running else ""
            items.append({"type": "submenu", "label": f"{mark}{v.name}{display}", "items": sub})

        return items

    def _tray_action(self, target, action: str) -> None:
        """托盘菜单操作：后台线程执行（start/stop 含等待探测），结果经队列回 UI。"""
        mgr = self.php_mgr if target != "nginx" else self.nginx_mgr
        arg = None if target == "nginx" else target

        def worker():
            try:
                if arg is None:
                    msg = getattr(mgr, action)()
                else:
                    msg = getattr(mgr, action)(arg)
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}：{e}"
            self._tray_queue.put(msg)

        threading.Thread(target=worker, daemon=True).start()
        self._poll_tray_queue()

    def _poll_tray_queue(self) -> None:
        try:
            msg = self._tray_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_tray_queue)
            return
        self.set_log(msg)
        if self._tray is not None:
            try:
                self._tray.show_balloon("phpvm", msg[:200])
            except Exception:  # noqa: BLE001
                pass
        # 操作完成 → 立即刷新各面板状态
        try:
            self.php_panel.auto_refresh()
            self.nginx_panel.auto_refresh()
        except Exception:  # noqa: BLE001
            pass

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
