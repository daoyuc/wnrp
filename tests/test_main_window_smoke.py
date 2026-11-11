# -*- coding: utf-8 -*-
"""GUI 启动冒烟：主窗口能真正构建出来，且「总览」在左侧导航里是第一个页面。

为什么要有这份测试：主窗口 14 个页面全部由模块开关动态构建，任一面板构造
失败就是「应用直接起不来」，core 层测试覆盖不到；同时导航从横向页签改成
竖向导航后，必须保证「只有当前页被映射」（否则各面板的自动刷新会全部跑起来，
既卡界面又会让 _panel_visible() 的可见性判断失效）。

无显示环境（CI / Linux 无 X）自动跳过。
"""
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from core.config import Config
from core.mysql_manager import MysqlManager
from core.nginx_manager import NginxManager
from core.php_manager import PhpManager
from core.redis_manager import RedisManager

try:  # 无 tkinter（极简安装）时整个模块跳过
    import tkinter as tk

    from core import modules as modules_mod
    from ui.main_window import MainWindow

    _TK_IMPORT_ERROR = ""
except Exception as e:  # noqa: BLE001 - pragma: no cover
    _TK_IMPORT_ERROR = str(e)
    MainWindow = None  # type: ignore[assignment]


def _has_display() -> bool:
    """是否有可用的 Tk 显示环境（**只做环境判断，绝不创建窗口**）。

    macOS + Tk 9 上不能用「建一个 root 再销毁」来探测：那样本进程就已经用过
    一次 Tcl 解释器，随后 MainWindow 建的是第二个 root，而 macOS Aqua 下第二个
    解释器里调用 ``update_idletasks()``（``fit_window`` 第一行）会直接
    **SIGSEGV** —— 进程当场死掉，排在它后面的所有用例都不会被执行
    （表现为 ``unittest discover`` 只跑一部分就退出码 139）。
    """
    if MainWindow is None:
        return False
    if sys.platform == "win32":
        return True
    if sys.platform == "darwin":
        # macOS：Aqua 依赖 GUI 会话（WindowServer）；纯 SSH 登录时 Tk() 会直接中止
        try:
            return subprocess.run(["pgrep", "-qx", "WindowServer"],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode == 0
        except OSError:  # pragma: no cover - pgrep 缺失时保守放行
            return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


@unittest.skipUnless(_has_display(), "无可用 Tk 显示环境（或未安装 tkinter）")
class MainWindowSmokeTest(unittest.TestCase):
    app = None

    def setUp(self):
        self.cfg = Config()
        real_is_enabled = modules_mod.is_enabled
        # 强制开启 overview / mail：本机 config.json 可能停用了它们，
        # 而这两块正是要覆盖的对象（overview 是启动崩溃的必要条件，mail 是 Windows SMTP 提示行）
        forced = ("overview", "mail")
        patcher = mock.patch.object(
            modules_mod, "is_enabled",
            lambda key, cfg: True if key in forced else real_is_enabled(key, cfg))
        patcher.start()
        self.addCleanup(patcher.stop)
        # 「记住上次页面」会在 800ms 后落盘：测试里屏蔽真实写盘，避免污染本机
        # config.json（运行期文件）；需要断言时直接看 config.settings 内存值
        flush = mock.patch.object(MainWindow, "_flush_page_setting", lambda self: None)
        flush.start()
        self.addCleanup(flush.stop)
        # SSH 面板构造时会预置内置命令片段（写 settings.ssh_snippets）：测试里
        # 屏蔽掉，别把 20 条预置片段写进本机 config.json
        seed = mock.patch("core.ssh_store.ensure_builtin_snippets",
                          lambda cfg: [])
        seed.start()
        self.addCleanup(seed.stop)

    def tearDown(self):
        if self.app is not None:
            try:
                # 直接 destroy：Tk 会顺带撤销挂起的定时器（_tick /
                # _check_crash_startup …），不会留下 "invalid command name" 噪音。
                # 反过来先手动 after_cancel 掉 "after info" 里的全部 id，会把 Tk
                # 内部命令一起注销，destroy 时反抛
                # TclError: can't delete Tcl command，窗口销毁失败、残留到下一个用例。
                self.app.destroy()
            except Exception:  # noqa: BLE001
                pass
            self.app = None

    def _build(self):
        cfg = self.cfg
        with mock.patch.object(MainWindow, "_init_tray", lambda self: None), \
                mock.patch("ui.update_dialog.UpdateBanner.start", lambda self: None):
            self.app = MainWindow(PhpManager(cfg), NginxManager(),
                                  RedisManager(), MysqlManager(), cfg)
        return self.app

    def test_window_builds_and_overview_tab_is_first(self):
        app = self._build()
        self.assertIsNotNone(app.overview_panel, "overview 模块启用时应创建总览面板")
        nb = app.nav
        self.assertEqual(nb.index(app.overview_panel), 0, "总览应是第一个导航页")
        self.assertGreaterEqual(len(nb.tabs()), 3, "其余主页面签应至少存在")

    def test_only_current_page_is_mapped(self):
        """导航切换后必须**只有一个**页面处于映射状态。

        回归点：旧实现用 Notebook，不可见页由 Tk 隐藏；换成竖向导航后若忘记
        ``pack_forget()``，所有页面会叠在一起，``_panel_visible()`` 恒为真 →
        每个 8 秒 tick 都会把所有面板的扫描跑一遍（子进程/线程成倍）。
        """
        app = self._build()
        nav = app.nav
        pages = nav.tabs()
        self.assertGreaterEqual(len(pages), 3)
        for page in pages:
            nav.select(page)
            for _ in range(2):
                app.update()
            mapped = [p for p in pages if p.winfo_ismapped()]
            self.assertEqual(len(mapped), 1,
                             "同时有 %d 个页面被映射（应只在当前页）" % len(mapped))
            self.assertIs(mapped[0], page)

    def test_last_page_is_remembered_and_restored(self):
        """切页记住 key，下次启动回到该页；页面被停用时回落第一页。"""
        app = self._build()
        mail = getattr(app, "mail_panel", None)
        self.assertIsNotNone(mail, "mail 模块在本用例被强制开启")
        app.nav.select(mail)
        app.update()
        self.assertEqual(app.nav.current_key(), "mail")
        self.assertEqual(app.config.settings.get("last_page"), "mail")
        self.tearDown()

        # 重建：应当直接停在 mail
        app2 = self._build()
        self.assertEqual(app2.nav.current_key(), "mail")
        self.tearDown()

        # 页面被停用（把 last_page 指向一个不存在的 key）→ 回落第一个页面
        self.cfg.settings["last_page"] = "disabled-page"
        app3 = self._build()
        self.assertEqual(app3.nav.index(app3.nav.current_page()), 0,
                         "上次页面已停用时应回落到第一个页面")

    def test_status_bar_shows_system_memory_slot(self):
        app = self._build()
        self.assertTrue(app._mem_label.winfo_exists())
        self.assertTrue(app._mem_var.get(), "状态栏内存槽位应有初始占位文案")
        self.assertEqual(str(app._mem_label.cget("style")),
                         "Memory.Status.TLabel")

    def test_project_launcher_tab_has_browser_action(self):
        """插件页签必须挂上，且「在浏览器打开」按钮真实存在（可见入口）。"""
        real = modules_mod.is_enabled
        with mock.patch.object(
                modules_mod, "is_enabled",
                lambda key, cfg: True if key == "project_launcher"
                else real(key, cfg)):
            app = self._build()
        panels = [p for p in getattr(app, "_plugin_panels", [])
                  if type(p).__name__ == "ProjectLauncherPanel"]
        self.assertEqual(len(panels), 1, "项目启动器页签应被挂载")
        self.assertTrue(panels[0]._browse_btn.winfo_exists())

    def test_access_insights_tab_has_core_widgets(self):
        """访问日志聚合页签挂上后，文件下拉与小时分布画布必须存在。"""
        real = modules_mod.is_enabled
        with mock.patch.object(
                modules_mod, "is_enabled",
                lambda key, cfg: True if key == "access_insights"
                else real(key, cfg)):
            app = self._build()
        panels = [p for p in getattr(app, "_plugin_panels", [])
                  if type(p).__name__ == "AccessInsightsPanel"]
        self.assertEqual(len(panels), 1, "访问日志聚合页签应被挂载")
        panel = panels[0]
        self.assertTrue(panel._file_combo.winfo_exists())
        self.assertTrue(panel._hour_canvas.winfo_exists())
        self.assertIn(panel._lines, (2000, 20000, 100000))

    def test_build_is_repeatable(self):
        """连续建两次：导航分组树与页面顺序都必须能重建（不能残留上一次的 item）。"""
        app = self._build()
        self.assertEqual(app.nav.index(app.overview_panel), 0)
        self.tearDown()
        app2 = self._build()
        self.assertEqual(app2.nav.index(app2.overview_panel), 0)

    def test_about_tab_scrolls_instead_of_being_clipped(self):
        """关于页必须「可滚动看全 + 横向不溢出」。

        回归点：内容（环境信息 / 模块开关 / 一堆设置项）远超一屏，且长文本默认
        不换行会横向撑破布局 —— 用户只能手动拉大窗口才看得到，且边角被裁切。
        """
        app = self._build()
        nb = app.nav
        nb.select(nb.tabs()[-1])          # 关于页是最后一个页签
        for _ in range(5):
            app.update()

        canvas = app._about_scroll.canvas
        first, last = canvas.yview()
        self.assertLess(float(last), 1.0, "内容应高于一屏：必须靠滚动（而不是拉窗口）看全")
        self.assertGreaterEqual(float(first), 0.0)

        # 内容宽度不得超过可视宽度，否则右侧会被页签边界裁掉
        canvas_w = canvas.winfo_width()
        self.assertGreater(canvas_w, 1, "canvas 应已完成布局")
        self.assertLessEqual(app._about_scroll.content.winfo_reqwidth(), canvas_w,
                             "关于页内容横向溢出：长文本没有按容器宽度换行")

        # 滚到底再回顶：验证滚动条区间与滚轮通道可用
        canvas.yview_moveto(1.0)
        app.update()
        canvas.yview_moveto(0.0)
        app.update()

    def test_worker_results_reach_main_thread_without_errors(self):
        """泵事件若干秒：各面板的 worker 结果必须能正常回主线程渲染。

        回归点：worker 曾直接 ``self.after(0, ...)``，主线程还没进 ``mainloop()``
        时会抛 ``RuntimeError: main thread is not in main loop``（本测试用
        ``update()`` 而非 ``mainloop()``，正是当时暴露该问题的场景）。
        worker 里的异常不会走 Tk 的 ``report_callback_exception``，
        所以这里挂 ``threading.excepthook`` 才能抓到。
        """
        app = self._build()
        thread_errors: list = []
        old_hook = threading.excepthook
        threading.excepthook = lambda args: thread_errors.append(args.exc_value)
        self.addCleanup(setattr, threading, "excepthook", old_hook)

        nb = app.nav
        nb.select(app.mail_panel)          # 邮件页：Windows 下要渲染 SMTP 提示行
        end = time.time() + 3.0
        while time.time() < end:
            app.update()
            time.sleep(0.05)

        self.assertEqual(thread_errors, [], "worker 线程内抛错：%s" % thread_errors)
        self.assertTrue(app.mail_panel._state.cget("text"), "邮件页状态行应已渲染")


if __name__ == "__main__":
    unittest.main()
