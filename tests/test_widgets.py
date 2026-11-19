# -*- coding: utf-8 -*-
"""ui.widgets 冒烟：在无显示环境可跳过；有 Tk 时建一次原语。"""
import os
import unittest

from core import theme as core_theme


class ThemeRolesForWidgetsTest(unittest.TestCase):
    def test_success_warn_err_bg_roles_exist(self):
        for mode in (core_theme.LIGHT, core_theme.DARK):
            keys = set(core_theme.PALETTES[mode])
            self.assertIn("success_bg", keys)
            self.assertIn("warn_bg", keys)
            self.assertIn("err_bg", keys)


def _tk_available() -> bool:
    if os.environ.get("PHPVM_SKIP_GUI") == "1":
        return False
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


@unittest.skipUnless(_tk_available(), "无 Tk 显示环境")
class WidgetsSmokeTest(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from ui import theme
        from ui import widgets

        self.tk = tk
        self.theme = theme
        self.widgets = widgets
        self.root = tk.Tk()
        self.root.withdraw()
        theme.set_mode("light")
        theme.setup_style(self.root)

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def test_page_header_toolbar_empty_metric_pill(self):
        w = self.widgets
        host = self.tk.Frame(self.root)
        host.pack()
        header = w.PageHeader(host, title="标题", subtitle="说明")
        self.assertEqual(header.title_var.get(), "标题")
        tb = w.Toolbar(
            host,
            primary=[{"text": "启动", "command": lambda: None}],
            secondary=[{"text": "刷新", "command": lambda: None}],
            overflow=[{"text": "更多项", "command": lambda: None}],
        )
        self.assertGreaterEqual(len(tb.buttons), 2)
        empty = w.EmptyState(host, title="空", hint="提示")
        empty.show()
        empty.hide()
        card = w.MetricCard(host, label="Nginx", value="运行中", hint="80")
        card.set("已停止", hint="")
        pill = w.status_pill(host, state=w.PILL_OK, text="健康")
        pill.set_state(w.PILL_ERR, "异常")
        banner = w.HealthBanner(host)
        banner.set(w.PILL_WARN, "有 2 项告警", "告警")
        btn = tb.buttons[0]
        w.set_busy([btn], True)
        self.assertEqual(str(btn.cget("state")), "disabled")
        w.set_busy([btn], False)


if __name__ == "__main__":
    unittest.main()
