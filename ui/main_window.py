# -*- coding: utf-8 -*-
"""主窗口：多页签（PHP 版本管理 / Nginx 管理 / Redis 管理 / 站点映射 / SQLite 数据库 / Nginx 日志 / 关于）+ 顶部 cmd php 状态 + 底部状态栏。"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core import autostart, crash_watchdog, path_manager
from core.config import Config, IS_WIN, WNRP_ROOT
from core.i18n import LANGS, t
from core.health_monitor import HealthMonitor
from core.mysql_manager import MysqlManager
from core.nginx_manager import NginxManager
from core.php_manager import PhpManager
from core.redis_manager import RedisManager
from core.service_group import ServiceGroup
from core.sqlite_manager import SqliteManager
from core.vhost_manager import VhostManager
from .dialogs import CliSwitchDialog, CrashDialog
from .mysql_panel import MysqlPanel
from .nginx_log_panel import NginxLogPanel
from .nginx_panel import NginxPanel
from .php_panel import PhpPanel
from .redis_panel import RedisPanel
from .site_wizard import SiteWizardDialog
from .sqlite_panel import SqlitePanel
from .theme import BG, CARD_BG, ERR, FONT, GRAY, OK, PRIMARY, PRIMARY_LIGHT, TEXT, setup_style
from .vhost_panel import VhostPanel
from .window_utils import fit_window

APP_TITLE = t("phpvm · PHP 版本管理器")
WNRP_ROOT_SHOW = WNRP_ROOT
CLI_PREFIX = "CMD php" if IS_WIN else t("终端 php")
CRASH_POLL_TICKS = 8  # 崩溃检测频率 ≈ 8 × 8s = 64s 一次（仅告警展示用）


class MainWindow(tk.Tk):
    def __init__(self, php_mgr: PhpManager, nginx_mgr: NginxManager,
                 redis_mgr: RedisManager, mysql_mgr: MysqlManager, config: Config):
        super().__init__()
        self.php_mgr = php_mgr
        self.nginx_mgr = nginx_mgr
        self.redis_mgr = redis_mgr
        self.mysql_mgr = mysql_mgr
        self.config = config
        self.services = ServiceGroup(php_mgr, nginx_mgr, redis_mgr, mysql_mgr)

        self.title(APP_TITLE)
        self.configure(bg=BG)
        setup_style(self)
        self._build_menubar()

        self._log_var = tk.StringVar(value=t("就绪"))
        self._cli_queue: queue.Queue = queue.Queue()
        self._crash_queue: queue.Queue = queue.Queue()
        self._tray_queue: queue.Queue = queue.Queue()
        self.health = HealthMonitor()
        self._crash_alert_active = False
        self._crash_tick = 0
        self._build()
        # 按屏幕可用工作区收敛窗口尺寸：保证底部状态栏与各页签操作按钮不被 Dock/任务栏遮挡
        fit_window(self, None, width=1080, height=680, min_width=960, min_height=600)
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
    def _build_menubar(self) -> None:
        """顶部菜单栏：目前提供界面语言切换（重启后生效）。"""
        menubar = tk.Menu(self)
        lang_menu = tk.Menu(menubar, tearoff=0)
        self._lang_var = tk.StringVar(value=self.config.get_lang())
        for code, name in LANGS.items():
            lang_menu.add_radiobutton(
                label=name, value=code, variable=self._lang_var,
                command=lambda c=code: self._on_lang_selected(c),
            )
        menubar.add_cascade(label=t("语言"), menu=lang_menu)
        # 注意：实例属性 self.config 是 Config 对象（覆盖了 tk 的 .config 别名），
        # 这里必须用 .configure 才能给根窗口挂上菜单栏。
        self.configure(menu=menubar)

    def _on_lang_selected(self, code: str) -> None:
        """保存语言选择；界面文本在重启后切换，因此仅提示。"""
        self._lang_var.set(code)
        if code == self.config.get_lang():
            return
        self.config.set_lang(code)
        messagebox.showinfo(
            t("语言已切换"),
            t("界面语言已保存为 {name}。重启 phpvm 后生效。", name=LANGS[code]),
            parent=self,
        )

    def _build(self) -> None:
        # 状态栏：先 pack 到底部，窗口被压小时优先保留（不被页签内容挤出）
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

        # 标题区
        header = ttk.Frame(self, style="Card.TFrame")
        header.pack(fill="x", padx=12, pady=(12, 8))
        ttk.Label(header, text=t("PHP 版本管理器"), style="Title.TLabel").pack(
            side="left", padx=(16, 8), pady=12
        )
        ttk.Label(header, text=t("环境根目录 {dir}", dir=WNRP_ROOT_SHOW),
                  style="SubTitle.TLabel").pack(
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
            cli_box, text=t("{prefix}：检测中…", prefix=CLI_PREFIX),
            font=(FONT, 9, "bold"), background=CARD_BG, foreground=TEXT,
        )
        self.cli_label.pack(side="left", padx=(0, 10))
        self.btn_cli = ttk.Button(cli_box, text=t("切换"), command=self._open_cli_switch)
        self.btn_cli.pack(side="left")

        # 页签
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=12, pady=(0, 6))
        self.php_panel = PhpPanel(nb, self.php_mgr, self.config, self.set_log)
        self.nginx_panel = NginxPanel(nb, self.nginx_mgr, self.set_log,
                                      on_new_site=self._open_site_wizard)
        self.redis_panel = RedisPanel(nb, self.redis_mgr, self.set_log)
        self.mysql_panel = MysqlPanel(nb, self.mysql_mgr, self.set_log)
        self.vhost_mgr = VhostManager(self.config)
        self.vhost_panel = VhostPanel(nb, self.vhost_mgr, self.set_log)
        # SQLite 查询页：复用 vhost 管理器，从站点 root 里发现站点自带的数据库
        self.sqlite_panel = SqlitePanel(nb, SqliteManager(self.config), self.vhost_mgr,
                                        self.set_log)
        self.log_panel = NginxLogPanel(nb, self.set_log, self.nginx_mgr)
        about = self._build_about(nb)
        nb.add(self.php_panel, text=f"  {t('PHP 版本管理')}  ")
        nb.add(self.nginx_panel, text=f"  {t('Nginx 管理')}  ")
        nb.add(self.redis_panel, text=f"  {t('Redis 管理')}  ")
        nb.add(self.mysql_panel, text=f"  {t('MySQL 管理')}  ")
        nb.add(self.vhost_panel, text=f"  {t('站点映射')}  ")
        nb.add(self.sqlite_panel, text=f"  {t('SQLite 数据库')}  ")
        nb.add(self.log_panel, text=f"  {t('Nginx 日志')}  ")
        nb.add(about, text=f"  {t('关于')}  ")

    def _build_about(self, master) -> ttk.Frame:
        frame = ttk.Frame(master, padding=18)
        ttk.Label(frame, text=APP_TITLE, style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(
            frame,
            text=t("管理 {root} 下多个 PHP 版本的启动 / 停止 / 重启 / 状态 / 端口 / 配置，"
                    "并附带 Nginx 与 Redis 管理。", root=WNRP_ROOT),
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(0, 14))

        info = ttk.LabelFrame(frame, text=t("环境信息"), padding=12)
        info.pack(fill="x")
        rows = [
            (t("环境根目录"), WNRP_ROOT_SHOW),
            (t("PHP FastCGI 配置"), t("php82/php85 → php-web.ini，其余 → php.ini")),
            (t("FastCGI 监听"), t("127.0.0.1:端口（按版本配置，见 PHP 版本管理页）")),
            (t("Nginx 前缀"), os.path.join(WNRP_ROOT, "nginx")),
            (t("配置持久化"), os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")),
        ]
        if IS_WIN:
            rows.insert(4, (t("隐藏启动器"), os.path.join(WNRP_ROOT, "RunHiddenConsole.exe")))
        for i, (k, v) in enumerate(rows):
            ttk.Label(info, text=f"{k}：", font=(FONT, 9, "bold"), background=CARD_BG).grid(
                row=i, column=0, sticky="w", padx=(8, 4), pady=3
            )
            ttk.Label(info, text=v, font=(FONT, 9), background=CARD_BG).grid(
                row=i, column=1, sticky="w", pady=3
            )

        # 设置区：开机自启 + 崩溃自愈 + 界面语言
        settings = ttk.LabelFrame(frame, text=t("设置"), padding=12)
        settings.pack(fill="x", pady=(10, 0))
        self._autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(
            settings, text=t("开机自动启动 phpvm（当前用户）"),
            variable=self._autostart_var, command=self._toggle_autostart,
        ).pack(anchor="w", pady=(0, 6))
        # 开机自动启动整套服务（Windows：写入「启动」目录脚本；其它平台暂不支持）
        self._svc_autostart_var = tk.BooleanVar(value=autostart.services_enabled())
        self._cb_svc_autostart = ttk.Checkbutton(
            settings, text=t("开机自动启动全部服务（Nginx + PHP + Redis + MySQL）"),
            variable=self._svc_autostart_var, command=self._toggle_service_autostart,
            state="normal" if IS_WIN else "disabled",
        )
        self._cb_svc_autostart.pack(anchor="w", pady=(0, 6))
        if not IS_WIN:
            ttk.Label(
                settings,
                text=t("该能力当前仅支持 Windows（写入用户「启动」目录）。"),
                style="SubTitle.TLabel",
            ).pack(anchor="w", pady=(0, 6))
        self._recover_var = tk.BooleanVar(value=bool(self.config.get_setting("auto_recover_crash", False)))
        ttk.Checkbutton(
            settings, text=t("php-cgi 崩溃后自动重启（自愈，默认关闭）"),
            variable=self._recover_var, command=self._toggle_recover,
        ).pack(anchor="w")
        ttk.Label(
            settings,
            text=t("自愈防抖 60 秒、每版本每小时最多 3 次，防止崩溃循环刷进程。"),
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))
        lang_row = ttk.Frame(settings)
        lang_row.pack(anchor="w", pady=(8, 0))
        ttk.Label(lang_row, text=t("界面语言："), font=(FONT, 9, "bold"),
                  background=CARD_BG).pack(side="left")
        self._lang_box = ttk.Combobox(
            lang_row, state="readonly", width=16,
            values=[f"{name}（{code}）" for code, name in LANGS.items()],
        )
        self._lang_box.current(list(LANGS).index(self.config.get_lang()))
        self._lang_box.pack(side="left", padx=(4, 0))
        ttk.Button(lang_row, text=t("应用"), command=self._apply_lang_box).pack(
            side="left", padx=(8, 0)
        )

        # 服务编排：一键启停整套环境
        group_row = ttk.Frame(settings)
        group_row.pack(anchor="w", pady=(10, 0), fill="x")
        ttk.Label(group_row, text=t("服务编排："), font=(FONT, 9, "bold"),
                  background=CARD_BG).pack(side="left")
        ttk.Button(group_row, text=t("全部启动"), style="Accent.TButton",
                   command=lambda: self._all_services("start")).pack(side="left", padx=(4, 6))
        ttk.Button(group_row, text=t("全部停止"), style="Danger.TButton",
                   command=lambda: self._all_services("stop")).pack(side="left")
        ttk.Label(
            settings,
            text=t("启动顺序 PHP → Redis → MySQL → Nginx，停止反之；已运行的服务自动跳过。"),
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        ttk.Label(
            frame,
            text=t("\n提示：修改端口后需同步修改对应 nginx vhost 的 fastcgi_pass 才会生效。\n"
                   "phpvm 按端口精确启停，不会像旧的 start_phpXX.bat 那样误杀其它版本进程。"),
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(14, 0))
        return frame

    def _apply_lang_box(self) -> None:
        """关于页语言下拉：应用所选语言并提示重启生效。"""
        codes = list(LANGS)
        idx = self._lang_box.current()
        if idx < 0 or idx >= len(codes):
            return
        code = codes[idx]
        self._lang_var.set(code)
        if code == self.config.get_lang():
            return
        self.config.set_lang(code)
        messagebox.showinfo(
            t("语言已切换"),
            t("界面语言已保存为 {name}。重启 phpvm 后生效。", name=LANGS[code]),
            parent=self,
        )

    # ------------------------------------------------------------------ #
    def set_log(self, msg: str) -> None:
        self._log_var.set(msg)

    def _all_services(self, action: str) -> None:
        """一键启停整套服务（Nginx + 各 PHP + Redis + MySQL），后台执行。"""
        title = t("全部启动") if action == "start" else t("全部停止")
        if action == "stop" and not messagebox.askyesno(
                title,
                t("将停止 Nginx、全部 PHP 版本、Redis 与 MySQL，"
                  "本机站点会全部不可访问。\n确定继续？"),
                parent=self):
            return
        self.set_log(t("正在{title}…", title=title))

        def worker():
            try:
                msg = (self.services.start_all() if action == "start"
                       else self.services.stop_all())
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}：{e}"
            self._tray_queue.put(msg)

        threading.Thread(target=worker, daemon=True).start()
        self._poll_tray_queue()

    def _open_site_wizard(self) -> None:
        """打开「新建站点」向导（Nginx 管理页入口），完成后刷新站点映射列表。"""
        SiteWizardDialog(self, self.config, on_done=lambda: self.vhost_panel.refresh())

    # 设置区开关（关于页）
    def _toggle_autostart(self) -> None:
        target = self._autostart_var.get()
        ok = autostart.enable() if target else autostart.disable()
        if not ok:
            self._autostart_var.set(autostart.is_enabled())
            messagebox.showerror(t("开机自启"), t("修改注册表失败，请检查权限"), parent=self)
            return
        self.set_log(t("开机自启已启用") if target else t("开机自启已关闭"))

    def _toggle_service_autostart(self) -> None:
        """开机自动启动整套服务开关。"""
        target = self._svc_autostart_var.get()
        ok = autostart.enable_services() if target else autostart.disable_services()
        if not ok:
            self._svc_autostart_var.set(autostart.services_enabled())
            messagebox.showerror(
                t("开机启动服务"),
                t("写入启动项失败：{path}", path=autostart.services_script_path()),
                parent=self,
            )
            return
        self.set_log(t("已开启开机启动全部服务") if target
                     else t("已关闭开机启动全部服务"))

    def _toggle_recover(self) -> None:
        """自愈开关：开启 → 拉起独立守护进程；关闭 → 守护进程下轮自行退出。

        自愈由 core.crash_watchdog 常驻执行（与 GUI 生命周期解耦），
        关闭时无需杀进程，守护进程读到配置即退出。
        """
        enabled = self._recover_var.get()
        self.config.set_setting("auto_recover_crash", enabled)
        if enabled:
            self.set_log(t("崩溃自愈已开启，正在启动守护进程…"))

            def spawn():
                ok, msg = crash_watchdog.spawn()
                self._crash_queue.put(("wd", msg if ok else t("自愈守护异常：{msg}", msg=msg)))

            threading.Thread(target=spawn, daemon=True).start()
        else:
            self.set_log(t("崩溃自愈已关闭（守护进程将自动退出）"))

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
            self.cli_label.configure(text=t("{prefix}：未启用 wnrp 版本", prefix=CLI_PREFIX))
            return
        running_fg = OK if version != t("未知") else ERR
        self.cli_dot.configure(foreground=running_fg)
        self.cli_label.configure(
            text=t("{prefix}：{name} · PHP {version}",
                   prefix=CLI_PREFIX, name=name, version=version))

    def _tick(self) -> None:
        # 自动轻量刷新状态（面板内部自行排队异步执行）
        try:
            self.php_panel.auto_refresh()
            self.nginx_panel.auto_refresh()
            self.redis_panel.auto_refresh()
            self.log_panel.auto_refresh()
        except Exception:  # noqa: BLE001
            pass
        # cmd php 版本号变化极少，降频刷新（每 4 轮 tick ≈ 32s 一次）
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        if self._tick_count % 4 == 0:
            self._refresh_cli()
            # MySQL 状态查询涉及服务枚举，同样降频
            try:
                self.mysql_panel.auto_refresh()
            except Exception:  # noqa: BLE001
                pass
        # 崩溃检测（低频轮询事件日志，仅用于告警展示）+ 自愈守护保活
        self._crash_tick += 1
        if self._crash_tick >= CRASH_POLL_TICKS:
            self._crash_tick = 0
            self._poll_crash()
            self._ensure_recover_daemon()
        self.after(8000, self._tick)

    def _ensure_recover_daemon(self) -> None:
        """若开启了崩溃自愈，确保独立守护进程存活（后台探测/拉起，不阻塞 UI）。"""

        def worker():
            try:
                if not self.config.get_setting("auto_recover_crash", False):
                    return
                if crash_watchdog.is_running():
                    return
                ok, msg = crash_watchdog.spawn()
                if not ok:
                    self._crash_queue.put(("wd", t("自愈守护进程异常：{msg}", msg=msg)))
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=worker, daemon=True).start()

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
        if kind == "wd":
            # 守护进程操作反馈（自愈开关/保活线程回传）
            self.set_log(events)
            return
        if events:
            self._on_crash(events, startup=(kind == "startup"))

    def _on_crash(self, events: list[dict], startup: bool) -> None:
        """收到崩溃事件：状态栏告警 + 托盘气泡；运行中新崩溃额外弹详情。"""
        self._crash_alert_active = True
        n = len(events)
        summary = self._crash_summary(events)
        self._alert_label.configure(text=t("⚠ php-cgi 崩溃 {n} 次，点击查看", n=n))
        self.set_log(t("检测到 php-cgi 崩溃（{n} 次），详见状态栏告警", n=n))
        if self._tray is not None:
            try:
                self._tray.show_balloon(t("php-cgi 崩溃告警"), summary)
            except Exception:  # noqa: BLE001
                pass
        if not startup:
            self._show_crash_detail(events)
            # 崩溃自愈由独立守护进程执行（core/crash_watchdog，与 GUI 解耦）；
            # 此处仅确保守护进程存活。故障若未产生崩溃事件（如进程被清理），
            # 守护进程的失联探测仍会兜底恢复。
            if self.config.get_setting("auto_recover_crash", False):
                self._ensure_recover_daemon()

    def _crash_summary(self, events: list[dict]) -> str:
        lines = []
        for e in events[:3]:
            ver = f"[{e['version']}] " if e.get("version") else ""
            lines.append(t("{time} {ver}{app} 异常码 {code}",
                           time=e["time"], ver=ver, app=e["app"], code=e["exception"]))
        if len(events) > 3:
            lines.append(t("…共 {count} 次", count=len(events)))
        return "\n".join(lines) or t("未知")

    def _show_crash_detail(self, events: list[dict] | None = None) -> None:
        CrashDialog(self, events or self.health.recent_crashes, on_clear=self._clear_crash_history)

    def _clear_crash_history(self) -> None:
        """清空崩溃告警：重置检测游标 + 关闭状态栏红色告警。"""
        self.health.reset()
        self._crash_alert_active = False
        self._alert_label.configure(text="")
        self.set_log(t("已清空崩溃告警记录"))

    # ------------------------------------------------------------------ #
    # 系统托盘 / 关闭行为
    def _init_tray(self) -> None:
        """初始化系统托盘。仅 Windows 支持；macOS/Linux 暂以 Dock 驻留代替。"""
        if not IS_WIN:
            self._tray = None
            return
        from .tray import TrayIcon  # 延迟导入：tray 模块依赖 Win32，仅 Windows 可用

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
        """最小化（iconic）时：有托盘则隐藏到托盘；无托盘交给 Dock 正常最小化。"""
        try:
            if self._tray is not None and self.state() == "iconic":
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
            self.redis_panel.auto_refresh()
            self.mysql_panel.auto_refresh()
            self.log_panel.auto_refresh()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # 托盘动态菜单：PHP 版本 + Nginx + Redis + MySQL 快捷启停
    def _build_tray_menu(self) -> list[dict]:
        items: list[dict] = []
        # 一键启停整套服务
        items.append({"type": "item", "label": t("全部启动服务"),
                      "cmd": lambda: self._all_services("start")})
        items.append({"type": "item", "label": t("全部停止服务"),
                      "cmd": lambda: self._all_services("stop")})
        items.append({"type": "sep"})
        # 隐藏到托盘（窗口可见时可用）
        items.append({
            "type": "item",
            "label": t("隐藏到托盘"),
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
            state_txt = t("状态：运行中 · PID {pid}", pid=pids[0]) if pids else t("状态：运行中")
        else:
            state_txt = t("状态：已停止")
        nginx_items.append({"type": "item", "label": state_txt, "enabled": False})
        nginx_items.append({"type": "sep"})
        nginx_items.append({"type": "item", "label": t("启动"), "enabled": not running,
                            "cmd": lambda: self._tray_action("nginx", "start")})
        nginx_items.append({"type": "item", "label": t("停止"), "enabled": running,
                            "cmd": lambda: self._tray_action("nginx", "stop")})
        nginx_items.append({"type": "item", "label": t("重载配置"), "enabled": running,
                            "cmd": lambda: self._tray_action("nginx", "reload")})
        nginx_items.append({"type": "item", "label": t("配置检查"),
                            "cmd": lambda: self._tray_action("nginx", "test_config")})
        items.append({"type": "submenu", "label": "Nginx", "items": nginx_items})

        # Redis 快捷启停（多实例时每个实例一个子菜单）
        redis_items: list[dict] = []
        try:
            self.redis_mgr.get_status_all()
        except Exception:  # noqa: BLE001
            pass
        redis_insts = self.redis_mgr.instances
        if redis_insts:
            def _redis_submenu(inst) -> list[dict]:
                if inst.running and inst.pids:
                    state_txt = t("状态：运行中 · PID {pids} · 端口 {port}",
                                  pids=", ".join(map(str, inst.pids)), port=inst.port)
                elif inst.running:
                    state_txt = t("状态：运行中 · 端口 {port}", port=inst.port)
                else:
                    state_txt = t("状态：已停止 · 端口 {port}", port=inst.port)
                sub = [
                    {"type": "item", "label": state_txt, "enabled": False},
                    {"type": "sep"},
                    {"type": "item", "label": t("启动"), "enabled": not inst.running,
                     "cmd": lambda i=inst: self._tray_action("redis", "start", i)},
                    {"type": "item", "label": t("停止"), "enabled": inst.running,
                     "cmd": lambda i=inst: self._tray_action("redis", "stop", i)},
                    {"type": "item", "label": t("重启"),
                     "cmd": lambda i=inst: self._tray_action("redis", "restart", i)},
                ]
                return sub

            def _redis_label(inst) -> str:
                return f"Redis [{inst.name}]"

            if len(redis_insts) == 1:
                inst = redis_insts[0]
                items.append({"type": "submenu", "label": _redis_label(inst),
                              "items": _redis_submenu(inst)})
            else:
                for inst in redis_insts:
                    items.append({"type": "submenu",
                                  "label": _redis_label(inst),
                                  "items": _redis_submenu(inst)})
        else:
            items.append({"type": "item", "label": t("Redis：未发现实例"), "enabled": False})

        # MySQL 快捷启停（实例为 Windows 服务时需管理员权限）
        mysql_insts = self.mysql_mgr.instances
        if mysql_insts:
            for mi in mysql_insts:
                if mi.running and mi.pids:
                    m_state = t("状态：运行中 · PID {pids} · 端口 {port}",
                                pids=", ".join(map(str, mi.pids)), port=mi.port)
                elif mi.running:
                    m_state = t("状态：运行中 · 端口 {port}", port=mi.port)
                else:
                    m_state = t("状态：已停止 · 端口 {port}", port=mi.port)
                items.append({
                    "type": "submenu", "label": f"MySQL [{mi.name}]",
                    "items": [
                        {"type": "item", "label": m_state, "enabled": False},
                        {"type": "sep"},
                        {"type": "item", "label": t("启动"), "enabled": not mi.running,
                         "cmd": lambda i=mi: self._tray_action("mysql", "start", i)},
                        {"type": "item", "label": t("停止"), "enabled": mi.running,
                         "cmd": lambda i=mi: self._tray_action("mysql", "stop", i)},
                        {"type": "item", "label": t("重启"),
                         "cmd": lambda i=mi: self._tray_action("mysql", "restart", i)},
                    ],
                })
        else:
            items.append({"type": "item", "label": t("MySQL：未发现实例"), "enabled": False})

        # 各 PHP 版本快捷启停
        versions = self.php_mgr.versions or self.php_mgr.scan_versions()
        for v in versions:
            sub: list[dict] = []
            if v.running and v.pid:
                state_txt = t("状态：运行中 · PID {pid} · 端口 {port}", pid=v.pid, port=v.port)
            elif v.running:
                state_txt = t("状态：运行中 · 端口 {port}", port=v.port)
            else:
                state_txt = t("状态：已停止 · 端口 {port}", port=v.port)
            sub.append({"type": "item", "label": state_txt, "enabled": False})
            sub.append({"type": "sep"})
            sub.append({"type": "item", "label": t("启动"), "enabled": not v.running,
                        "cmd": lambda v=v: self._tray_action(v, "start")})
            sub.append({"type": "item", "label": t("停止"), "enabled": v.running,
                        "cmd": lambda v=v: self._tray_action(v, "stop")})
            sub.append({"type": "item", "label": t("重启"), "enabled": v.running,
                        "cmd": lambda v=v: self._tray_action(v, "restart")})
            display = f"（{v.display}）" if v.display else ""
            mark = "● " if v.running else ""
            items.append({"type": "submenu", "label": f"{mark}{v.name}{display}", "items": sub})

        return items

    def _tray_action(self, target, action: str, arg=None) -> None:
        """托盘菜单操作：后台线程执行（start/stop 含等待探测），结果经队列回 UI。

        target：php 版本对象 / "nginx" / "redis"；redis 时 arg 为实例对象。
        """
        if target == "nginx":
            mgr, call_arg = self.nginx_mgr, None
        elif target == "redis":
            mgr, call_arg = self.redis_mgr, arg
        elif target == "mysql":
            mgr, call_arg = self.mysql_mgr, arg
        else:
            mgr, call_arg = self.php_mgr, target

        def worker():
            try:
                if call_arg is None:
                    msg = getattr(mgr, action)()
                else:
                    msg = getattr(mgr, action)(call_arg)
            except Exception as e:  # noqa: BLE001
                msg = t("{name}：{text}", name=type(e).__name__, text=str(e))
            # 手动停止 → 解除守护看护；启动/重启 → 纳入守护看护
            if not isinstance(target, str):
                try:
                    if action == "stop":
                        if t("已停止") in msg or t("未在运行") in msg:
                            crash_watchdog.unwatch(target.name)
                    elif action in ("start", "restart"):
                        crash_watchdog.watch_version(target.name)
                except Exception:  # noqa: BLE001
                    pass
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
            self.redis_panel.auto_refresh()
            self.mysql_panel.auto_refresh()
        except Exception:  # noqa: BLE001
            pass

    def _on_close(self) -> None:
        """点击关闭按钮：弹「重启 / 退出」选择框。

        macOS/Linux 无托盘：重启 / 退出 / 取消；
        Windows 有托盘时额外提供「最小化到托盘」。
        """
        self._close_dialog(show_tray=self._tray is not None)

    def _close_dialog(self, show_tray: bool) -> None:
        dlg = tk.Toplevel(self)
        dlg.title(t("关闭 phpvm"))
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()
        dlg.configure(bg=BG)
        setup_style(dlg)

        def choose(action: str) -> None:
            dlg.destroy()
            if action == "tray":
                self.withdraw()
            elif action == "restart":
                self._do_restart()
            elif action == "exit":
                self._real_quit()

        # 按钮栏先 pack 到底部：屏幕可用高度不足时按钮仍优先可见
        frm = ttk.Frame(dlg)
        frm.pack(side="bottom", pady=(0, 12))

        ttk.Label(dlg, text=t("要如何关闭 phpvm？"), style="Title.TLabel").pack(
            pady=(16, 6)
        )
        if show_tray:
            ttk.Label(
                dlg, text=t("可最小化到系统托盘后台运行，或完全退出。"),
                style="SubTitle.TLabel",
            ).pack(pady=(0, 12))

        if show_tray:
            ttk.Button(frm, text=t("最小化到托盘"), command=lambda: choose("tray")).pack(
                side="left", padx=6
            )
        ttk.Button(frm, text=t("重启"), command=lambda: choose("restart")).pack(
            side="left", padx=6
        )
        ttk.Button(frm, text=t("退出"), command=lambda: choose("exit")).pack(
            side="left", padx=6
        )
        ttk.Button(frm, text=t("取消"), command=dlg.destroy).pack(side="left", padx=6)

        # 按可用工作区收敛尺寸并定位：按钮栏已固定底部，保证右下角按钮不被遮挡
        fit_window(dlg, self, width=430 if show_tray else 330,
                   height=160 if show_tray else 150)

        dlg.wait_window()

    def _do_restart(self) -> None:
        """重启 phpvm（整体应用）：先拉起全新进程，再退出当前实例。

        新进程通过环境变量 PHPVM_RESTART=1 告知 main.py 这是「重启」拉起，
        在遇到单例锁（socket / 互斥体）被旧实例占用时短暂轮询等待，
        避开退出竞态（见 main.py）。
        """
        exe = sys.executable
        if IS_WIN:
            # GUI 应用：优先用 pythonw，避免弹出黑色控制台窗口
            pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.exists(pyw):
                exe = pyw
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"
        )
        env = dict(os.environ, PHPVM_RESTART="1")
        try:
            subprocess.Popen([exe, script], env=env)
        except OSError as e:
            messagebox.showerror(
                t("重启"), t("无法启动新进程：{msg}", msg=e), parent=self
            )
            return
        self._real_quit()

    def _real_quit(self) -> None:
        if self._tray:
            self._tray.remove()
            self._tray = None
        self.destroy()
