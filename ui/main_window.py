# -*- coding: utf-8 -*-
"""主窗口：多页签（PHP 版本管理 / Nginx 管理 / Redis 管理 / 站点映射 / SQLite 数据库 / Nginx 日志 / 关于）+ 顶部 cmd php 状态 + 底部状态栏。"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core import (app_paths, autostart, backup_bundle, crash_watchdog,
                  path_manager, run_log, updater)
from core import theme as theme_prefs
from core.config import Config, IS_WIN, WNRP_ROOT
from core.i18n import LANGS, t
from core.health_monitor import HealthMonitor
from core import modules
from core.mysql_manager import MysqlManager
from core.nginx_manager import NginxManager
from core.php_manager import PhpManager
from core.redis_manager import RedisManager
from core.service_group import PHP_SCOPE_NEWEST, PHP_SCOPES, ServiceGroup, scope_label
from core.vhost_manager import VhostManager
from .dialogs import CliSwitchDialog, CrashDialog
from .mysql_panel import MysqlPanel
from .nginx_log_panel import LogPanel
from .nginx_panel import NginxPanel
from .php_panel import PhpPanel
from .redis_panel import RedisPanel
from .run_log_panel import RunLogPanel
from .site_wizard import SiteWizardDialog
from . import theme
from .update_dialog import UpdateBanner, UpdateDialog
from .vhost_panel import VhostPanel
from .window_utils import fit_window

APP_TITLE = t("phpvm · PHP 版本管理器")
WNRP_ROOT_SHOW = WNRP_ROOT
CLI_PREFIX = "CMD php" if IS_WIN else t("终端 php")
CRASH_POLL_TICKS = 8  # 崩溃检测频率 ≈ 8 × 8s = 64s 一次（仅告警展示用）
# 外观模式顺序（与下拉框/菜单一致）：浅色 / 深色 / 跟随系统
THEME_MODES = (theme_prefs.LIGHT, theme_prefs.DARK, theme_prefs.SYSTEM)


def theme_label(mode: str) -> str:
    """外观模式的展示名（经 i18n 翻译）。"""
    return t(theme_prefs.MODE_LABELS.get(theme_prefs.normalize(mode), "跟随系统"))


class MainWindow(tk.Tk):
    # 控件跨方法创建（_build / _build_about 等），提前声明类型以满足静态检查
    _module_vars: dict[str, tk.BooleanVar]

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
        # 主题：先按配置（默认跟随系统）载入调色板，再建样式与界面
        theme.set_mode(theme_prefs.get_mode(self.config))
        self.configure(bg=theme.BG)
        theme.setup_style(self)
        self._build_menubar()

        self._log_var = tk.StringVar(value=t("就绪"))
        self._cli_queue: queue.Queue = queue.Queue()
        self._crash_queue: queue.Queue = queue.Queue()
        self._tray_queue: queue.Queue = queue.Queue()
        self._tray_draining = False  # 托盘结果队列的唯一消费者是否已启动
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
        # 启动静默检查更新（可在「关于 → 设置」关闭；发现新版本只做状态栏提示）
        self._update_banner = UpdateBanner(self, self.config, self._on_update_found)
        self._update_banner.start()

        self._tray = None
        self._init_tray()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # 点最小化按钮 → 直接隐藏到系统托盘（恢复：托盘菜单/双击「显示 phpvm」）
        self.bind("<Unmap>", self._on_unmap)
        # 运行日志：启动快照 + 托盘/操作结果队列的唯一消费者
        self._log_startup_snapshot()
        self._start_tray_drain()
        # 「启动时自动启动全部服务」开启时延迟调起（结果写入运行日志）
        self.after(1200, self._maybe_start_services_on_launch)

    # ------------------------------------------------------------------ #
    def _build_menubar(self) -> None:
        """顶部菜单栏：外观主题（立即生效）+ 界面语言（重启后生效）。"""
        menubar = tk.Menu(self)
        # 外观：浅色 / 深色 / 跟随系统 —— 切换后立即重刷整个界面
        theme_menu = tk.Menu(menubar, tearoff=0)
        self._theme_var = tk.StringVar(value=theme_prefs.get_mode(self.config))
        for mode in THEME_MODES:
            theme_menu.add_radiobutton(
                label=theme_label(mode), value=mode, variable=self._theme_var,
                command=lambda m=mode: self._on_theme_selected(m),
            )
        menubar.add_cascade(label=t("外观"), menu=theme_menu)
        lang_menu = tk.Menu(menubar, tearoff=0)
        self._lang_var = tk.StringVar(value=self.config.get_lang())
        for code, name in LANGS.items():
            lang_menu.add_radiobutton(
                label=name, value=code, variable=self._lang_var,
                command=lambda c=code: self._on_lang_selected(c),
            )
        menubar.add_cascade(label=t("语言"), menu=lang_menu)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label=t("检查更新"), command=self.open_update_dialog)
        help_menu.add_command(label=t("下载缓存目录"), command=self._open_update_dir)
        menubar.add_cascade(label=t("帮助"), menu=help_menu)
        # 注意：实例属性 self.config 是 Config 对象（覆盖了 tk 的 .config 别名），
        # 这里必须用 .configure 才能给根窗口挂上菜单栏。
        self.configure(menu=menubar)

    # ------------------------------------------------------------------ #
    # 外观主题
    # ------------------------------------------------------------------ #
    def _on_theme_selected(self, mode: str) -> None:
        """切换外观：写入配置并立即应用到主窗口与所有已打开的对话框。"""
        mode = theme_prefs.normalize(mode)
        self._theme_var.set(mode)
        if mode != theme_prefs.get_mode(self.config):
            theme_prefs.set_mode(self.config, mode)
        theme.apply_mode(self, mode)
        self._sync_theme_box()
        self.set_log(t("外观已切换为{name}", name=theme_label(mode)))

    def _sync_theme_box(self) -> None:
        """关于页的主题下拉与当前模式保持一致。"""
        box = getattr(self, "_theme_box", None)
        if box is None:
            return
        mode = theme_prefs.get_mode(self.config)
        if mode in THEME_MODES:
            box.current(THEME_MODES.index(mode))

    def _apply_theme_box(self) -> None:
        """关于页下拉：应用所选外观（与菜单等效）。"""
        idx = self._theme_box.current()
        if 0 <= idx < len(THEME_MODES):
            self._on_theme_selected(THEME_MODES[idx])

    def _check_system_theme(self) -> None:
        """「跟随系统」时：系统外观变化后自动重刷主题（低频探测）。"""
        if theme_prefs.get_mode(self.config) != theme_prefs.SYSTEM:
            return
        dark = theme_prefs.system_prefers_dark()
        if dark is None:
            return
        want = theme_prefs.DARK if dark else theme_prefs.LIGHT
        if want != theme.current_mode():
            theme.apply_mode(self, theme_prefs.SYSTEM)
            self.set_log(t("外观已跟随系统切换为{name}", name=theme_label(want)))

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
            side="left", fill="x", expand=True, padx=theme.PAD_MD, pady=theme.PAD_SM
        )
        self._alert_label = ttk.Label(
            bar, text="", style="Alert.Status.TLabel", cursor="hand2",
        )
        self._alert_label.pack(side="right", padx=theme.PAD_MD, pady=theme.PAD_SM)
        self._alert_label.bind("<Button-1>", lambda e: self._show_crash_detail())

        # 新版本提示（点击打开更新对话框）
        self._update_label = ttk.Label(
            bar, text="", style="Link.Status.TLabel", cursor="hand2",
        )
        self._update_label.pack(side="right", padx=(0, theme.PAD_SM), pady=theme.PAD_SM)
        self._update_label.bind("<Button-1>", lambda e: self.open_update_dialog())
        # 状态栏上沿细线：与内容区明确分界
        theme.divider(self).pack(fill="x", side="bottom")

        # 标题区（卡片 + 左侧强调竖条）
        header = ttk.Frame(self, style="Panel.TFrame")
        header.pack(fill="x", padx=theme.PAD_LG, pady=(theme.PAD_LG, theme.PAD_SM))
        theme.accent_bar(header).pack(side="left", fill="y", padx=(0, theme.PAD_MD))
        ttk.Label(header, text=t("PHP 版本管理器"), style="Title.TLabel").pack(
            side="left", padx=(theme.PAD_MD, theme.PAD_SM), pady=theme.PAD_MD
        )
        ttk.Label(header, text=t("环境根目录 {dir}", dir=WNRP_ROOT_SHOW),
                  style="SubTitle.TLabel").pack(
            side="left", pady=theme.PAD_MD
        )

        # 右侧：cmd php 命令版本状态 + 切换
        cli_box = ttk.Frame(header, style="Card.TFrame")
        cli_box.pack(side="right", padx=theme.PAD_LG, pady=theme.PAD_MD)
        self.cli_dot = ttk.Label(cli_box, text="●", style="Dot.TLabel")
        self.cli_dot.pack(side="left", padx=(0, theme.PAD_SM))
        self.cli_label = ttk.Label(
            cli_box, text=t("{prefix}：检测中…", prefix=CLI_PREFIX), style="CardBold.TLabel",
        )
        self.cli_label.pack(side="left", padx=(0, theme.PAD_MD))
        self.btn_cli = ttk.Button(cli_box, text=t("切换"), command=self._open_cli_switch)
        self.btn_cli.pack(side="left")

        # 页签：按模块开关构建（停用的模块不实例化、不导入其面板代码）
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=theme.PAD_LG, pady=(0, theme.PAD_SM))
        self.notebook = nb
        # 切回某个页签时立即补一次刷新（不可见页签的自动刷新是暂停的）
        nb.bind("<<NotebookTabChanged>>", lambda e: self._refresh_panels())
        self.php_panel = PhpPanel(nb, self.php_mgr, self.config, self.set_log)
        self.nginx_panel = NginxPanel(nb, self.nginx_mgr, self.set_log,
                                      on_new_site=self._open_site_wizard)
        self.redis_panel = None
        if self.redis_mgr is not None:
            self.redis_panel = RedisPanel(nb, self.redis_mgr, self.set_log)
        self.mysql_panel = None
        if self.mysql_mgr is not None:
            self.mysql_panel = MysqlPanel(nb, self.mysql_mgr, self.set_log)
        self.vhost_mgr = VhostManager(self.config)
        self.vhost_panel = VhostPanel(nb, self.vhost_mgr, self.set_log)
        self.sqlite_panel = None
        if modules.is_enabled("sqlite", self.config):
            # 延迟导入：停用时不加载 sqlite 面板与管理器
            from core.sqlite_manager import SqliteManager
            from .sqlite_panel import SqlitePanel

            # SQLite 查询页：复用 vhost 管理器，从站点 root 里发现站点自带的数据库
            self.sqlite_panel = SqlitePanel(nb, SqliteManager(self.config),
                                            self.vhost_mgr, self.set_log)
        self.log_panel = None
        if modules.is_enabled("log", self.config):
            self.log_panel = LogPanel(nb, self.set_log, self.nginx_mgr,
                                      self.config, self.vhost_mgr)
        # 总览仪表盘（可选模块，置顶第一个页签）
        self.overview_panel = None
        if modules.is_enabled("overview", self.config):
            from .overview_panel import OverviewPanel
            self.overview_panel = OverviewPanel(
                nb, self.set_log, self.php_mgr, self.nginx_mgr,
                self.redis_mgr, self.mysql_mgr, self.vhost_mgr,
                self.config, self.services)
            nb.insert(0, self.overview_panel, text=t("总览"))
        about = self._build_about(nb)
        # 页签文字两侧留白由 TNotebook.Tab 的 padding 控制（不再用空格凑宽度）
        nb.add(self.php_panel, text=t("PHP 版本管理"))
        nb.add(self.nginx_panel, text=t("Nginx 管理"))
        if self.redis_panel is not None:
            nb.add(self.redis_panel, text=t("Redis 管理"))
        if self.mysql_panel is not None:
            nb.add(self.mysql_panel, text=t("MySQL 管理"))
        nb.add(self.vhost_panel, text=t("站点映射"))
        if self.sqlite_panel is not None:
            nb.add(self.sqlite_panel, text=t("SQLite 数据库"))
        if self.log_panel is not None:
            nb.add(self.log_panel, text=t("日志"))
        # 运行日志：全局记录（应用启停 / 服务启停结果 / 异常），不受模块开关影响
        self.run_panel = RunLogPanel(nb, self.set_log)
        nb.add(self.run_panel, text=t("运行日志"))
        nb.add(about, text=t("关于"))

    def _build_about(self, master) -> ttk.Frame:
        # 整页为一张卡片：分组框与内部文本统一走 Card.* 样式，避免底色不一致
        frame = ttk.Frame(master, style="Card.TFrame", padding=theme.PAD_XL)
        ttk.Label(frame, text=APP_TITLE, style="Title.TLabel").pack(
            anchor="w", pady=(0, theme.PAD_SM)
        )
        ttk.Label(
            frame,
            text=t("管理 {root} 下多个 PHP 版本的启动 / 停止 / 重启 / 状态 / 端口 / 配置，"
                    "并附带 Nginx 与 Redis 管理。", root=WNRP_ROOT),
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(0, theme.PAD_LG))

        info = ttk.LabelFrame(frame, text=t("环境信息"), padding=12,
                              style="Card.TLabelframe")
        info.pack(fill="x")
        rows = [
            (t("phpvm 版本"), f"v{updater.current_version()}"),
            (t("环境根目录"), WNRP_ROOT_SHOW),
            (t("PHP FastCGI 配置"), t("各版本目录内的 php.ini（CLI 与 FastCGI 共用）")),
            (t("FastCGI 监听"), t("127.0.0.1:端口（按版本配置，见 PHP 版本管理页）")),
            (t("Nginx 前缀"), os.path.join(WNRP_ROOT, "nginx")),
            (t("配置持久化"), self.config.config_path),
            (t("数据目录"), app_paths.data_dir()),
        ]
        if IS_WIN:
            rows.insert(5, (t("隐藏启动器"), os.path.join(WNRP_ROOT, "RunHiddenConsole.exe")))
        for i, (k, v) in enumerate(rows):
            ttk.Label(info, text=f"{k}：", style="CardBold.TLabel").grid(
                row=i, column=0, sticky="w", padx=(theme.PAD_XS, theme.PAD_XS), pady=3
            )
            ttk.Label(info, text=v, style="CardDim.TLabel").grid(
                row=i, column=1, sticky="w", pady=3
            )

        # 模块开关：取消勾选的可选模块在重启后不再加载
        mods = ttk.LabelFrame(frame, text=t("功能模块"), padding=12,
                              style="Card.TLabelframe")
        mods.pack(fill="x", pady=(theme.PAD_MD, 0))
        ttk.Label(
            mods,
            text=t("默认全部启用；取消勾选后需重启 phpvm 生效（该模块代码将不再加载）。"),
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(0, 6))
        self._module_vars: dict[str, tk.BooleanVar] = {}
        disabled = modules.disabled_modules(self.config)
        for meta in modules.MODULES:
            key = str(meta["key"])
            required = bool(meta["required"])
            var = tk.BooleanVar(value=(key not in disabled))
            self._module_vars[key] = var
            cb = ttk.Checkbutton(
                mods, text=modules.label(meta), variable=var, style="Card.TCheckbutton",
                state="disabled" if required else "normal",
                command=lambda k=key: self._toggle_module(k),
            )
            cb.pack(anchor="w", pady=1)

        # 设置区：开机自启 + 崩溃自愈 + 界面语言
        settings = ttk.LabelFrame(frame, text=t("设置"), padding=12,
                                  style="Card.TLabelframe")
        settings.pack(fill="x", pady=(theme.PAD_MD, 0))
        self._autostart_var = tk.BooleanVar(value=autostart.is_enabled())
        ttk.Checkbutton(
            settings, text=t("开机自动启动 phpvm（当前用户）"), style="Card.TCheckbutton",
            variable=self._autostart_var, command=self._toggle_autostart,
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
        # 开机自动启动整套服务（Windows：写入「启动」目录脚本；其它平台暂不支持）
        self._svc_autostart_var = tk.BooleanVar(value=autostart.services_enabled())
        self._cb_svc_autostart = ttk.Checkbutton(
            settings, text=t("开机自动启动全部服务（Nginx + PHP + Redis + MySQL）"),
            style="Card.TCheckbutton",
            variable=self._svc_autostart_var, command=self._toggle_service_autostart,
            state="normal" if IS_WIN else "disabled",
        )
        self._cb_svc_autostart.pack(anchor="w", pady=(0, theme.PAD_SM))
        if not IS_WIN:
            ttk.Label(
                settings,
                text=t("该能力当前仅支持 Windows（写入用户「启动」目录）。"),
                style="SubTitle.TLabel",
            ).pack(anchor="w", pady=(0, 6))
        # 启动 phpvm 时自动启动整套服务（跨平台；结果写入「运行日志」页签）
        self._svc_launch_var = tk.BooleanVar(
            value=bool(self.config.get_setting("start_services_on_launch", False)))
        ttk.Checkbutton(
            settings, text=t("启动 phpvm 时自动启动全部服务（PHP / Redis / MySQL / Nginx）"),
            style="Card.TCheckbutton",
            variable=self._svc_launch_var, command=self._toggle_service_launch,
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
        # 「自动启动服务」时启动哪些 PHP 版本（默认仅最新，避免一次拉起全部版本）
        scope_row = ttk.Frame(settings, style="Card.TFrame")
        scope_row.pack(anchor="w", pady=(0, theme.PAD_SM))
        ttk.Label(scope_row, text=t("自动启动服务的 PHP 版本："),
                  style="CardBold.TLabel").pack(side="left")
        self._svc_scope_box = ttk.Combobox(
            scope_row, state="readonly", width=22,
            values=[scope_label(s) for s in PHP_SCOPES],
        )
        self._sync_service_scope_box()
        self._svc_scope_box.pack(side="left", padx=(theme.PAD_XS, 0))
        ttk.Button(scope_row, text=t("应用"), command=self._apply_service_scope_box).pack(
            side="left", padx=(theme.PAD_SM, 0)
        )
        ttk.Label(
            settings,
            text=t("默认「仅最新版本」：开机自启/启动时不会一次拉起全部 PHP 版本，"
                   "其它版本可在 PHP 页手动启动。"),
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(0, theme.PAD_XS))
        self._recover_var = tk.BooleanVar(value=bool(self.config.get_setting("auto_recover_crash", False)))
        ttk.Checkbutton(
            settings, text=t("php-cgi 崩溃后自动重启（自愈，默认关闭）"),
            style="Card.TCheckbutton",
            variable=self._recover_var, command=self._toggle_recover,
        ).pack(anchor="w")
        ttk.Label(
            settings,
            text=t("自愈防抖 60 秒、每版本每小时最多 3 次，防止崩溃循环刷进程。"),
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(theme.PAD_XS, 0))
        lang_row = ttk.Frame(settings, style="Card.TFrame")
        lang_row.pack(anchor="w", pady=(theme.PAD_SM, 0))
        ttk.Label(lang_row, text=t("界面语言："), style="CardBold.TLabel").pack(side="left")
        self._lang_box = ttk.Combobox(
            lang_row, state="readonly", width=16,
            values=[f"{name}（{code}）" for code, name in LANGS.items()],
        )
        self._lang_box.current(list(LANGS).index(self.config.get_lang()))
        self._lang_box.pack(side="left", padx=(theme.PAD_XS, 0))
        ttk.Button(lang_row, text=t("应用"), command=self._apply_lang_box).pack(
            side="left", padx=(theme.PAD_SM, 0)
        )

        # 外观主题：浅色 / 深色 / 跟随系统（立即生效，无需重启）
        theme_row = ttk.Frame(settings, style="Card.TFrame")
        theme_row.pack(anchor="w", pady=(theme.PAD_SM, 0))
        ttk.Label(theme_row, text=t("外观主题："), style="CardBold.TLabel").pack(side="left")
        self._theme_box = ttk.Combobox(
            theme_row, state="readonly", width=16,
            values=[theme_label(m) for m in THEME_MODES],
        )
        self._theme_box.pack(side="left", padx=(theme.PAD_XS, 0))
        self._sync_theme_box()
        ttk.Button(theme_row, text=t("应用"), command=self._apply_theme_box).pack(
            side="left", padx=(theme.PAD_SM, 0)
        )
        ttk.Label(
            settings,
            text=t("深色/浅色切换立即生效；「跟随系统」会随系统外观自动切换。"),
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(theme.PAD_XS, 0))

        # 软件更新：版本显示 + 手动检查 + 启动自动检查开关
        upd_row = ttk.Frame(settings, style="Card.TFrame")
        upd_row.pack(anchor="w", pady=(theme.PAD_MD, 0))
        ttk.Label(upd_row, text=t("软件更新："), style="CardBold.TLabel").pack(side="left")
        ttk.Label(upd_row, text=t("当前版本 {ver}", ver=f"v{updater.current_version()}"),
                  style="CardDim.TLabel").pack(side="left",
                                               padx=(theme.PAD_XS, theme.PAD_MD))
        ttk.Button(upd_row, text=t("检查更新"),
                   command=self.open_update_dialog).pack(side="left")
        ttk.Button(upd_row, text=t("打开下载缓存目录"),
                   command=self._open_update_dir).pack(side="left", padx=(theme.PAD_SM, 0))
        self._update_autocheck_var = tk.BooleanVar(
            value=bool(self.config.get_setting("check_update_on_start", True)))
        ttk.Checkbutton(
            settings,
            text=t("启动时自动检查更新（发现新版本时仅状态栏提示）"),
            style="Card.TCheckbutton",
            variable=self._update_autocheck_var,
            command=self._toggle_update_autocheck,
        ).pack(anchor="w", pady=(theme.PAD_XS, 0))

        # 服务编排：一键启停整套环境
        group_row = ttk.Frame(settings, style="Card.TFrame")
        group_row.pack(anchor="w", pady=(theme.PAD_MD, 0), fill="x")
        ttk.Label(group_row, text=t("服务编排："), style="CardBold.TLabel").pack(side="left")
        ttk.Button(group_row, text=t("全部启动"), style="Accent.TButton",
                   command=lambda: self._all_services("start")).pack(side="left", padx=(4, 6))
        ttk.Button(group_row, text=t("全部停止"), style="Danger.TButton",
                   command=lambda: self._all_services("stop")).pack(side="left")
        ttk.Label(
            settings,
            text=t("启动顺序 PHP → Redis → MySQL → Nginx，停止反之；已运行的服务自动跳过。"),
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        # 环境备份与迁移（F6）：导出 / 恢复「整套配置」（不含数据库数据）
        bak_row = ttk.Frame(settings, style="Card.TFrame")
        bak_row.pack(anchor="w", pady=(theme.PAD_MD, 0), fill="x")
        ttk.Label(bak_row, text=t("环境备份："), style="CardBold.TLabel").pack(side="left")
        ttk.Button(bak_row, text=t("导出配置…"), command=self._export_backup).pack(
            side="left", padx=(4, 6))
        ttk.Button(bak_row, text=t("从备份恢复…"), command=self._restore_backup).pack(side="left")
        ttk.Label(
            settings,
            text=t("导出 config.json / 站点配置 / nginx.conf / 各版本 php.ini 为 zip；"
                   "恢复会先备份现有文件，nginx -t 失败整体回滚。不含数据库数据。"),
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
    # 软件更新
    # ------------------------------------------------------------------ #
    def open_update_dialog(self) -> None:
        """打开「软件更新」对话框（打开即检查一次）。"""
        self._update_label.configure(text="")
        UpdateDialog(self, self.config, on_status=self.set_log)

    def _open_update_dir(self) -> None:
        """打开安装包下载缓存目录。"""
        from core import process_utils as pu

        pu.open_path(updater.updates_dir())

    def _on_update_found(self, rel) -> None:
        """启动检查发现新版本：状态栏提示（不弹窗打扰）。"""
        self._update_label.configure(
            text=t("发现新版本 {ver}，点击升级", ver=rel.version))
        self.set_log(t("发现新版本 {ver}，可点击状态栏提示升级", ver=rel.version))

    def _toggle_update_autocheck(self) -> None:
        enabled = self._update_autocheck_var.get()
        self.config.set_setting("check_update_on_start", enabled)
        self.set_log(t("启动时自动检查更新已开启") if enabled
                     else t("启动时自动检查更新已关闭"))

    # ------------------------------------------------------------------ #
    # 环境备份与迁移（F6）
    # ------------------------------------------------------------------ #
    def _export_backup(self) -> None:
        """导出环境配置为 zip（后台执行，仅配置，不含数据库数据）。"""
        dest = filedialog.asksaveasfilename(
            parent=self, title=t("导出环境配置"),
            defaultextension=".zip", initialfile="phpvm-env.zip",
            filetypes=[(t("Zip 压缩包"), "*.zip")])
        if not dest:
            return
        self.set_log(t("正在导出环境配置…"))

        def worker():
            res = backup_bundle.export_bundle(dest, self.config)
            self.after(0, lambda: self._backup_done(res, True))

        threading.Thread(target=worker, daemon=True).start()

    def _restore_backup(self) -> None:
        """从 zip 恢复环境配置（改前备份；nginx -t 失败整体回滚）。"""
        src = filedialog.askopenfilename(
            parent=self, title=t("从备份恢复"),
            filetypes=[(t("Zip 压缩包"), "*.zip")])
        if not src:
            return
        if not messagebox.askyesno(
                t("从备份恢复"),
                t("将用备份覆盖当前环境配置（config.json / 站点配置 / nginx.conf / php.ini）。\n"
                  "恢复前会备份现有文件，nginx -t 失败会整体回滚。\n\n确定继续？"),
                parent=self):
            return
        self.set_log(t("正在恢复环境配置…"))

        def worker():
            res = backup_bundle.restore_bundle(src, self.config)
            self.after(0, lambda: self._backup_done(res, False))

        threading.Thread(target=worker, daemon=True).start()

    def _backup_done(self, res: dict, exporting: bool) -> None:
        msg = res.get("message", "")
        if res.get("ok"):
            self.set_log(msg, "ok")
            messagebox.showinfo(
                t("导出环境配置") if exporting else t("从备份恢复"), msg, parent=self)
            if not exporting:
                self._refresh_panels()
        else:
            self.set_log(msg, "error")
            messagebox.showerror(t("操作失败"), msg, parent=self)

    # ------------------------------------------------------------------ #
    def set_log(self, msg: str, level: str = "info") -> None:
        """状态栏消息；同时写入全局运行日志（「运行日志」页签可查历史）。"""
        self._log_var.set(msg)
        run_log.log(level, "ui", msg)

    def report_callback_exception(self, exc, val, tb) -> None:
        """Tk 回调内的未捕获异常：写运行日志（默认只打印到 stderr，容易被漏看）。"""
        run_log.error("ui", t("界面回调异常：{name}：{msg}", name=exc.__name__, msg=val))
        super().report_callback_exception(exc, val, tb)

    def _log_startup_snapshot(self) -> None:
        """启动快照写入运行日志，便于事后对照「当时是什么环境、哪些开关开着」。"""
        mods = [t(str(m["name"])) for m in modules.MODULES
                if modules.is_enabled(str(m["key"]), self.config)]

        def state(value: bool) -> str:
            return t("已开启") if value else t("已关闭")

        run_log.info("env", t("数据目录：{dir}", dir=app_paths.data_dir()))
        run_log.info("env", t("界面语言：{lang} · 外观主题：{theme}",
                              lang=self.config.get_lang(),
                              theme=theme_label(theme_prefs.get_mode(self.config))))
        run_log.info("env", t("已启用模块：{mods}", mods="、".join(mods) or t("无")))
        run_log.info("env", t(
            "开机自启 phpvm：{a} · 开机自启服务：{b} · 崩溃自愈：{c} · 启动时自动启动服务：{d}",
            a=state(autostart.is_enabled()),
            b=state(autostart.services_enabled()),
            c=state(bool(self.config.get_setting("auto_recover_crash", False))),
            d=state(bool(self.config.get_setting("start_services_on_launch", False)))))
        run_log.info("env", t("自动启动服务的 PHP 版本：{scope}",
                              scope=scope_label(self._autostart_php_scope())))

    def _toggle_service_launch(self) -> None:
        """「启动 phpvm 时自动启动全部服务」开关（下次启动生效）。"""
        enabled = self._svc_launch_var.get()
        self.config.set_setting("start_services_on_launch", enabled)
        self.set_log(t("启动时自动启动全部服务已开启（下次启动生效）") if enabled
                     else t("启动时自动启动全部服务已关闭"))

    def _autostart_php_scope(self) -> str:
        """自动启动服务时使用的 PHP 启动范围（取值非法/缺失时回落「仅最新版本」）。"""
        scope = self.config.get_setting("autostart_php_scope", PHP_SCOPE_NEWEST)
        return scope if scope in PHP_SCOPES else PHP_SCOPE_NEWEST

    def _sync_service_scope_box(self) -> None:
        """下拉框跟随配置（取值非法时按默认项显示）。"""
        self._svc_scope_box.current(PHP_SCOPES.index(self._autostart_php_scope()))

    def _apply_service_scope_box(self) -> None:
        """应用「自动启动服务的 PHP 版本」选择（写入 settings.autostart_php_scope）。"""
        idx = self._svc_scope_box.current()
        if idx < 0 or idx >= len(PHP_SCOPES):
            return
        scope = PHP_SCOPES[idx]
        self.config.set_setting("autostart_php_scope", scope)
        self.set_log(t("自动启动服务的 PHP 版本已设为：{scope}", scope=scope_label(scope)))

    def _maybe_start_services_on_launch(self) -> None:
        """设置里开启「启动时自动启动全部服务」时调起整套服务。

        PHP 只按 `autostart_php_scope`（默认仅最新版本）启动，不会一次拉起全部版本；
        逐项成败由 ServiceGroup 写入运行日志（core.run_log），此处只记录开始/异常。
        """
        if not self.config.get_setting("start_services_on_launch", False):
            return
        scope = self._autostart_php_scope()
        run_log.info("app", t("启动时自动启动全部服务：开始（PHP：{scope}）",
                              scope=scope_label(scope)))
        self.set_log(t("正在启动全部服务…"))

        def worker():
            try:
                msg = self.services.start_all(php_scope=scope)
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}：{e}"
                run_log.error("app", t("启动时自动启动全部服务失败：{msg}", msg=msg))
            self._tray_queue.put((msg, "info"))

        threading.Thread(target=worker, daemon=True).start()
        self._start_tray_drain()

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
                run_log.error("services", t("{title}失败：{msg}", title=title, msg=msg))
                self._tray_queue.put((msg, "error"))
                return
            self._tray_queue.put((msg, "info"))

        threading.Thread(target=worker, daemon=True).start()
        self._start_tray_drain()

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

    def _toggle_module(self, key: str) -> None:
        """模块开关：写入 settings.disabled_modules（刚需模块不可取消）。"""
        if key in modules.REQUIRED_KEYS:
            self._module_vars[key].set(True)
            return
        disabled = [k for k, v in self._module_vars.items()
                    if not v.get() and k not in modules.REQUIRED_KEYS]
        modules.set_disabled(self.config, disabled)
        state = t("停用") if key in disabled else t("启用")
        name = next((str(m["name"]) for m in modules.MODULES if str(m["key"]) == key), key)
        self.set_log(t("模块 {name} 已设为{state}，重启 phpvm 后生效",
                       name=t(name), state=state))

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
                if ok:
                    run_log.ok("recover", msg)
                else:
                    run_log.error("recover", t("自愈守护异常：{msg}", msg=msg))
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
        if not self.winfo_exists():  # 窗口已销毁：停止轮询
            return
        try:
            info = self._cli_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_cli)
            return
        name, version = info.get("name"), info.get("version")
        if not name:
            self.cli_dot.configure(foreground=theme.GRAY)
            self.cli_label.configure(text=t("{prefix}：未启用 wnrp 版本", prefix=CLI_PREFIX))
            return
        running_fg = theme.OK if version != t("未知") else theme.ERR
        self.cli_dot.configure(foreground=running_fg)
        self.cli_label.configure(
            text=t("{prefix}：{name} · PHP {version}",
                   prefix=CLI_PREFIX, name=name, version=version))

    def _tick(self) -> None:
        # 自动轻量刷新状态（面板内部自行排队异步执行）
        self._refresh_panels()
        # cmd php 版本号变化极少，降频刷新（每 4 轮 tick ≈ 32s 一次）
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        if self._tick_count % 4 == 0:
            self._refresh_cli()
            # 跟随系统外观时，探测系统是否切了浅色/深色
            self._check_system_theme()
            # MySQL 状态查询涉及服务枚举，同样降频（且只在页签可见时）
            if self.mysql_panel is not None and self._panel_visible(self.mysql_panel):
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
        if not self.winfo_exists():  # 窗口已销毁：停止轮询
            return
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
        run_log.error("crash", t("检测到 php-cgi 崩溃（{n} 次）：\n{detail}",
                                 n=n, detail=summary))
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
        self._refresh_panels()

    def _panel_visible(self, panel) -> bool:
        """面板是否可见（notebook 非当前页 / 窗口最小化时 widget 未映射）。"""
        try:
            return bool(panel.winfo_ismapped())
        except Exception:  # noqa: BLE001
            return False

    def _refresh_panels(self) -> None:
        """刷新已启用的面板（停用的模块不刷新、不触碰其代码）。

        只刷新当前可见的页签：不可见面板的状态扫描纯属白跑子进程/线程，
        切回时由 <<NotebookTabChanged>> 立即补一次，不会看到陈旧数据。
        """
        for panel in (self.overview_panel, self.php_panel, self.nginx_panel,
                      self.redis_panel, self.log_panel):
            if panel is None or not self._panel_visible(panel):
                continue
            try:
                panel.auto_refresh()
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

        # Redis 快捷启停（多实例时每个实例一个子菜单；模块停用时跳过）
        if self.redis_mgr is None:
            redis_insts = []
        else:
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

        # MySQL 快捷启停（实例为 Windows 服务时需管理员权限；模块停用时跳过）
        mysql_insts = self.mysql_mgr.instances if self.mysql_mgr is not None else []
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
            level = "info"
            try:
                if call_arg is None:
                    msg = getattr(mgr, action)()
                else:
                    msg = getattr(mgr, action)(call_arg)
            except Exception as e:  # noqa: BLE001
                msg = t("{name}：{text}", name=type(e).__name__, text=str(e))
                level = "error"
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
            self._tray_queue.put((msg, level))

        threading.Thread(target=worker, daemon=True).start()
        self._start_tray_drain()

    def _start_tray_drain(self) -> None:
        """启动托盘/操作结果队列的唯一消费者（幂等）。

        此前每次操作都新起一个 ``_poll_tray_queue`` 循环，多个循环并存会
        互相吞消息 —— 统一收敛到常驻 drain，与各面板的队列规范一致。
        """
        if self._tray_draining:
            return
        self._tray_draining = True
        self.after(100, self._drain_tray)

    def _drain_tray(self) -> None:
        if not self.winfo_exists():  # 窗口已销毁：停止轮询
            self._tray_draining = False
            return
        got = False
        try:
            while True:
                item = self._tray_queue.get_nowait()
                msg, level = item if isinstance(item, tuple) else (item, "info")
                self.set_log(msg, level)
                if self._tray is not None:
                    try:
                        self._tray.show_balloon("phpvm", str(msg)[:200])
                    except Exception:  # noqa: BLE001
                        pass
                got = True
        except queue.Empty:
            pass
        if got:
            # 操作完成 → 立即刷新各面板状态
            self._refresh_panels()
        self.after(100, self._drain_tray)

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
        dlg.configure(bg=theme.BG)
        theme.setup_style(dlg)

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
                side="left", padx=theme.PAD_SM
            )
        ttk.Button(frm, text=t("重启"), style="Accent.TButton",
                   command=lambda: choose("restart")).pack(side="left", padx=theme.PAD_SM)
        ttk.Button(frm, text=t("退出"), style="Danger.TButton",
                   command=lambda: choose("exit")).pack(side="left", padx=theme.PAD_SM)
        ttk.Button(frm, text=t("取消"), command=dlg.destroy).pack(side="left", padx=theme.PAD_SM)

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
            run_log.error("app", t("无法启动新进程：{msg}", msg=e))
            messagebox.showerror(
                t("重启"), t("无法启动新进程：{msg}", msg=e), parent=self
            )
            return
        run_log.info("app", t("正在重启 phpvm（拉起新进程后退出当前实例）"))
        self._real_quit()

    def _real_quit(self) -> None:
        run_log.info("app", t("用户退出 phpvm"))
        if self._tray:
            self._tray.remove()
            self._tray = None
        self.destroy()
