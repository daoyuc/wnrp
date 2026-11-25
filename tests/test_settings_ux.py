# -*- coding: utf-8 -*-
"""W2 UX：设置选即生效、开机 PHP 预览接线、About/端口过时文案。"""
import inspect
import unittest

from core import i18n as _i18n
from ui import dialogs, main_window as mw, php_panel


class _Lang(unittest.TestCase):
    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")

    def tearDown(self):
        _i18n.set_language(self._lang)


class SettingsImmediateApplyTest(_Lang):
    def test_scope_theme_lang_bind_combobox_selected(self):
        src = inspect.getsource(mw.MainWindow._fill_settings_general)
        self.assertIn("<<ComboboxSelected>>", src)
        self.assertIn("_apply_service_scope_box", src)
        self.assertIn("_apply_theme_box", src)
        self.assertIn("_apply_lang_box", src)
        # 选即生效：不再为这三项挂「应用」按钮
        self.assertNotIn('text=t("应用"), command=self._apply_service_scope_box', src)
        self.assertNotIn('text=t("应用"), command=self._apply_theme_box', src)
        self.assertNotIn('text=t("应用"), command=self._apply_lang_box', src)

    def test_settings_shows_boot_preview_var(self):
        src = inspect.getsource(mw.MainWindow._fill_settings_general)
        self.assertIn("_php_boot_preview_var", src)
        self.assertIn("_refresh_php_boot_preview", src)

    def test_apply_scope_refreshes_preview(self):
        src = inspect.getsource(mw.MainWindow._apply_service_scope_box)
        self.assertIn("_refresh_php_boot_preview", src)

    def test_autostart_change_refreshes_preview(self):
        src = inspect.getsource(mw.MainWindow._on_php_autostart_change)
        self.assertIn("_refresh_php_boot_preview", src)


class PhpBootPreviewWiringTest(_Lang):
    def test_php_panel_accepts_services_and_preview(self):
        src = inspect.getsource(php_panel.PhpPanel.__init__)
        self.assertIn("services", src)
        build = inspect.getsource(php_panel.PhpPanel._build)
        self.assertIn("_boot_preview_var", build)
        self.assertIn("_refresh_boot_preview", build)

    def test_main_window_passes_services(self):
        src = inspect.getsource(mw.MainWindow._build)
        self.assertIn("services=self.services", src)


class AboutPortCopyTest(_Lang):
    def test_about_points_at_sync_dialog(self):
        src = inspect.getsource(mw.MainWindow._build_about)
        self.assertIn("一键同步", src)
        self.assertNotIn("需同步修改对应 nginx vhost", src)
        self.assertNotIn("请手动", src)

    def test_port_dialog_no_longer_asks_manual_sync(self):
        src = inspect.getsource(dialogs.PortDialog.__init__)
        self.assertIn("保存后会提示同步站点配置", src)
        self.assertNotIn("需同步修改 nginx vhost", src)
        save = inspect.getsource(dialogs.PortDialog._save)
        self.assertNotIn("请手动同步", save)


if __name__ == "__main__":
    unittest.main()
