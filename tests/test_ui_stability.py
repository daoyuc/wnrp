# -*- coding: utf-8 -*-
"""UI 稳定性守护：主线程队列 drain 的「自愈契约」。

覆盖三类真实发生过的稳定性缺陷（都是在 macOS 开发、Windows 上潜伏的那类）：

1. **单条消息把 drain 打断**：``_dispatch`` 抛异常 → 异常冒出 Tk ``after`` 回调，
   末尾的续排不再执行 → drain 链断裂、``_draining`` 永为 ``True``，之后所有
   worker 结果被静默丢弃（busy 不复位、自动复检停摆、面板看着「卡住了」）。
   对应 ``ui/redis_panel.py`` 的正确范式：续排必须包在 ``try`` 里。
2. **worker 线程里碰 Tk**：``MainWindow._post`` 若在 worker 里调 ``after()``，
   启动期（主循环未进）会抛 ``RuntimeError: main thread is not in main loop``，
   且 ``_ui_draining`` 已置 True → 之后所有投递永久静默丢弃。
3. **窗口销毁竞态**：worker 仍在跑时关掉弹窗/面板，``after`` 与 ``winfo_exists()``
   都会抛 ``TclError``，需要 ``window_utils.rearm_poll`` 兜住。

无显示环境自动跳过（探测方式与 GUI 冒烟一致：只判断环境，不创建窗口）。
"""
import threading
import time
import unittest
from unittest import mock

from tests.test_main_window_smoke import MainWindow, _has_display

try:
    import tkinter as tk

    from core import modules as modules_mod
    from core.config import Config
    from core.mysql_manager import MysqlManager
    from core.nginx_manager import NginxManager
    from core.php_manager import PhpManager
    from core.redis_manager import RedisManager
    from ui.site_wizard import SiteWizardDialog
    from ui.window_utils import rearm_poll
except Exception:  # noqa: BLE001 - 极简安装 / 无 tkinter 时整体跳过
    _IMPORT_ERROR = True
else:
    _IMPORT_ERROR = False


@unittest.skipUnless(_has_display() and not _IMPORT_ERROR,
                     "无可用 Tk 显示环境（或未安装 tkinter）")
class DrainContractTest(unittest.TestCase):
    """真实建一次主窗口，验证 drain / 投递 / 续排的契约。"""

    app = None

    def setUp(self):
        self.cfg = Config()
        real_is_enabled = modules_mod.is_enabled
        # mail 面板是本文件的主要观察对象，本机 config.json 可能停用了它
        forced = ("overview", "mail")
        patcher = mock.patch.object(
            modules_mod, "is_enabled",
            lambda key, cfg: True if key in forced else real_is_enabled(key, cfg))
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(MainWindow, "_init_tray", lambda self: None), \
                mock.patch("ui.update_dialog.UpdateBanner.start", lambda self: None):
            self.app = MainWindow(PhpManager(self.cfg), NginxManager(),
                                  RedisManager(), MysqlManager(), self.cfg)

    def tearDown(self):
        if self.app is not None:
            try:
                self.app.destroy()
            except Exception:  # noqa: BLE001
                pass
            self.app = None

    def _pump(self, seconds: float = 3.0) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            time.sleep(0.02)

    def test_dispatch_error_keeps_drain_alive(self):
        """单条消息处理抛异常不得冒到 Tk 回调，drain 链必须继续。"""
        panel = self.app.mail_panel
        # _start_drain 幂等：已挂起时不能再排一个循环（两个循环会互相吞消息）
        panel._start_drain()
        self.assertTrue(panel._draining)
        pending = len(panel.tk.call("after", "info"))
        panel._start_drain()
        self.assertEqual(len(panel.tk.call("after", "info")), pending)

        with mock.patch.object(panel, "_dispatch", side_effect=RuntimeError("boom")):
            panel._queue.put(("data", None))
            panel._drain()  # 同步跑一轮：不得抛异常
        # 链未断：仍处于「已挂起」状态，且队列已被排空（坏消息没卡住后续）
        self.assertTrue(panel._draining, "drain 链因异常断裂：后续消息会被永久丢弃")
        self.assertTrue(panel._queue.empty())

        # 恢复正常后，同一个循环仍能消费新消息
        seen = []
        panel._queue.put(("__test__", None))
        with mock.patch.object(panel, "_dispatch", side_effect=lambda k, p: seen.append(k)):
            panel._drain()
        self.assertEqual(seen, ["__test__"])

    def test_post_from_worker_thread_needs_no_after(self):
        """worker 线程调用 _post：不得抛异常，且回调最终在主线程执行。"""
        done = threading.Event()
        errors: list = []

        def worker():
            try:
                self.app._post(lambda: done.set())
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(5)
        self.assertEqual(errors, [], "worker 里投递回调不应抛异常")
        self._pump(3.0)
        self.assertTrue(done.is_set(), "回调未被主线程 drain 执行")

    def test_crash_queue_is_drained_fully_and_rearmed(self):
        """崩溃队列必须一次排空并续排，否则告警要等下一轮（≈64s）才显示。"""
        seen: list = []
        with mock.patch.object(self.app, "_on_crash",
                               side_effect=lambda ev, startup=False: seen.append((ev, startup))):
            self.app._crash_queue.put(("wd", "守护进程已启动"))
            self.app._crash_queue.put(("tick", [{"version": "php"}]))
            self.app._poll_crash_queue()
        self.assertEqual(len(seen), 1, "wd 分支不应把已排队的崩溃事件压住")
        self.assertTrue(self.app.tk.call("after", "info"), "处理完必须续排轮询")

    def test_rearm_poll_is_safe_after_destroy(self):
        """窗口销毁后续排必须安静返回 False，而不是抛 TclError。"""
        win = tk.Toplevel(self.app)
        win.destroy()
        self.assertFalse(rearm_poll(win, lambda: None))


class SiteWizardFatalTest(unittest.TestCase):
    """向导 worker 未兜异常时的收尾（不建窗口，纯逻辑）。"""

    @unittest.skipUnless(not _IMPORT_ERROR, "未安装 tkinter")
    def test_on_fatal_releases_busy_and_buttons(self):
        fake = mock.MagicMock()
        SiteWizardDialog._on_fatal(fake, "OSError: boom")
        fake._set_busy.assert_called_once_with(False)
        fake.btn_next.configure.assert_called_once()
        fake.btn_cancel.configure.assert_called_once_with(state="normal")


if __name__ == "__main__":
    unittest.main()
