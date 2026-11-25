# -*- coding: utf-8 -*-
"""W0 UX：全部停止确认统一、关窗按钮权重、重启 CTA 文案契约。"""
import inspect
import unittest
from unittest import mock

from core import i18n as _i18n
from ui import confirm, overview_panel
from ui import main_window as mw


class _Lang(unittest.TestCase):
    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")

    def tearDown(self):
        _i18n.set_language(self._lang)


class ConfirmStopAllTest(_Lang):
    def test_shared_copy_constants(self):
        self.assertIn("Nginx", confirm.STOP_ALL_BODY)
        self.assertIn("确定继续", confirm.STOP_ALL_BODY)

    def test_confirm_stop_all_uses_askyesno(self):
        with mock.patch("ui.confirm.messagebox.askyesno",
                        return_value=True) as ask:
            self.assertTrue(confirm.confirm_stop_all(None))
            ask.assert_called_once()
            args, kwargs = ask.call_args
            self.assertEqual(args[0], "全部停止")
            self.assertIn("全部不可访问", args[1])

    def test_confirm_stop_all_cancel(self):
        with mock.patch("ui.confirm.messagebox.askyesno", return_value=False):
            self.assertFalse(confirm.confirm_stop_all(None))

    def test_overview_stop_calls_shared_confirm(self):
        src = inspect.getsource(overview_panel.OverviewPanel._stop_all)
        self.assertIn("confirm_stop_all", src)

    def test_main_window_stop_calls_shared_confirm(self):
        src = inspect.getsource(mw.MainWindow._all_services)
        self.assertIn("confirm_stop_all", src)
        self.assertNotIn("askyesno", src)


class OfferRestartTest(_Lang):
    def test_offer_restart_appends_question(self):
        with mock.patch("ui.confirm.messagebox.askyesno",
                        return_value=False) as ask:
            confirm.offer_restart_now(
                None, title="语言已切换", detail="界面语言已保存为 English。")
            args, _ = ask.call_args
            self.assertEqual(args[0], "语言已切换")
            self.assertIn("立即重启 phpvm 以使改动生效？", args[1])

    def test_lang_handler_offers_restart(self):
        src = inspect.getsource(mw.MainWindow._on_lang_selected)
        self.assertIn("_offer_restart_now", src)
        self.assertNotIn("showinfo", src)


class CloseDialogWeightTest(unittest.TestCase):
    def test_restart_is_not_accent(self):
        src = inspect.getsource(mw.MainWindow._close_dialog)
        # 重启不得再用 Accent；取消或托盘才是安全默认
        self.assertNotIn('text=t("重启"), style="Accent.TButton"', src)
        self.assertIn('text=t("重启")', src)
        self.assertIn("Accent.TButton", src)  # 托盘或取消仍可用 Accent


class OfferPhpRestartTest(_Lang):
    def test_not_running_shows_info_only(self):
        with mock.patch("ui.confirm.messagebox.showinfo") as info, \
             mock.patch("ui.confirm.messagebox.askyesno") as ask:
            self.assertFalse(confirm.offer_php_restart(
                None, name="php82", detail="done", running=False))
            info.assert_called_once()
            ask.assert_not_called()

    def test_running_asks_yesno(self):
        with mock.patch("ui.confirm.messagebox.askyesno",
                        return_value=True) as ask:
            self.assertTrue(confirm.offer_php_restart(
                None, name="php82", detail="done", running=True))
            args, _ = ask.call_args
            self.assertIn("php82", args[1])
            self.assertIn("立即重启", args[1])


class ConfirmRedisDangerTest(_Lang):
    def test_flush_requires_ack_checkbox_in_source(self):
        src = inspect.getsource(confirm.confirm_redis_danger)
        self.assertIn("我知道会清空", src)
        self.assertIn("DB{db}", src)


if __name__ == "__main__":
    unittest.main()
