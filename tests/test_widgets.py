# -*- coding: utf-8 -*-
"""ui.widgets 冒烟：在无显示环境可跳过；有 Tk 时建一次原语。"""
import os
import unittest

from core import theme as core_theme
from ui.widgets import parse_metric_number


class ThemeRolesForWidgetsTest(unittest.TestCase):
    def test_success_warn_err_bg_roles_exist(self):
        for mode in (core_theme.LIGHT, core_theme.DARK):
            keys = set(core_theme.PALETTES[mode])
            self.assertIn("success_bg", keys)
            self.assertIn("warn_bg", keys)
            self.assertIn("err_bg", keys)


class ParseMetricNumberTest(unittest.TestCase):
    def test_plain_int_and_percent(self):
        self.assertEqual(parse_metric_number("12"), (12.0, ""))
        self.assertEqual(parse_metric_number("12%"), (12.0, "%"))
        self.assertEqual(parse_metric_number(" 6.8 "), (6.8, ""))

    def test_suffix_and_reject(self):
        self.assertEqual(parse_metric_number("-3.5ms"), (-3.5, "ms"))
        self.assertEqual(parse_metric_number("12 个"), (12.0, " 个"))
        self.assertIsNone(parse_metric_number("运行中"))
        self.assertIsNone(parse_metric_number(""))
        self.assertIsNone(parse_metric_number("mem 12"))


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
        card.set("已停止", hint="", animate=False)
        pill = w.status_pill(host, state=w.PILL_OK, text="健康")
        pill.set_state(w.PILL_ERR, "异常")
        banner = w.HealthBanner(host)
        banner.set(w.PILL_WARN, "有 2 项告警", "告警")
        btn = tb.buttons[0]
        w.set_busy([btn], True)
        self.assertEqual(str(btn.cget("state")), "disabled")
        w.set_busy([btn], False)

    def test_metric_same_value_is_noop(self):
        w = self.widgets
        host = self.tk.Frame(self.root)
        card = w.MetricCard(host, label="CPU", value="10%", hint="ok")
        card.set("10%", hint="ok", animate=True)
        self.assertIsNone(card._anim_after)
        self.assertEqual(card.value_lbl.cget("style"), "MetricValue.TLabel")

    def test_metric_number_animates_to_end(self):
        w = self.widgets
        host = self.tk.Frame(self.root)
        card = w.MetricCard(host, label="CPU", value="10%")
        card.set("40%", animate=True)
        self.assertIsNotNone(card._anim_after)
        # 泵满动画时长 + 余量，终点必须落到目标值
        deadline = self.root.tk.call("clock", "milliseconds") + 500
        while card._anim_after is not None:
            self.root.update()
            if self.root.tk.call("clock", "milliseconds") > deadline:
                break
        self.root.update()
        self.assertEqual(card.value_var.get(), "40%")

    def test_metric_anim_cancel_lands_on_latest(self):
        w = self.widgets
        host = self.tk.Frame(self.root)
        card = w.MetricCard(host, label="N", value="0")
        card.set("100", animate=True)
        card.set("50", animate=True)  # 取消前一段，改追 50
        deadline = self.root.tk.call("clock", "milliseconds") + 500
        while card._anim_after is not None:
            self.root.update()
            if self.root.tk.call("clock", "milliseconds") > deadline:
                break
        self.root.update()
        self.assertEqual(card.value_var.get(), "50")


if __name__ == "__main__":
    unittest.main()
