# -*- coding: utf-8 -*-
"""GUI 启动冒烟：主窗口能真正构建出来，且「总览」页签被放在第一位。

为什么要有这份测试：`ttk.Notebook.insert(0, ...)` 对**空** notebook 会抛
``TclError: Slave index 0 out of bounds``（Tk 8.6，Windows/Linux 均可复现），
而 `overview` 模块默认启用 → 主窗口构造即崩、应用完全无法启动。
core 层测试覆盖不到这类问题，故这里真正建一次窗口。

无显示环境（CI / Linux 无 X）自动跳过。
"""
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
    if MainWindow is None:
        return False
    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001
        return False
    root.destroy()
    return True


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

    def tearDown(self):
        if self.app is not None:
            try:
                # 先撤掉挂起的定时器（_tick / _check_crash_startup …），
                # 否则窗口销毁后它们仍会触发，Tk 会打 "invalid command name ..." 噪音
                for aid in self.app.tk.call("after", "info"):
                    try:
                        self.app.after_cancel(aid)
                    except Exception:  # noqa: BLE001
                        pass
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
        nb = app.overview_panel.master
        self.assertEqual(nb.index(app.overview_panel), 0, "总览应是第一个页签")
        self.assertGreaterEqual(len(nb.tabs()), 3, "其余主页面签应至少存在")

    def test_build_is_repeatable(self):
        """连续建两次（覆盖「已有页签时 insert(0)」与「空 notebook」两条分支）。"""
        app = self._build()
        self.assertEqual(app.overview_panel.master.index(app.overview_panel), 0)
        self.tearDown()
        app2 = self._build()
        self.assertEqual(app2.overview_panel.master.index(app2.overview_panel), 0)

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

        nb = app.overview_panel.master
        nb.select(app.mail_panel)          # 邮件页：Windows 下要渲染 SMTP 提示行
        end = time.time() + 3.0
        while time.time() < end:
            app.update()
            time.sleep(0.05)

        self.assertEqual(thread_errors, [], "worker 线程内抛错：%s" % thread_errors)
        self.assertTrue(app.mail_panel._state.cget("text"), "邮件页状态行应已渲染")


if __name__ == "__main__":
    unittest.main()
