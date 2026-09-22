# -*- coding: utf-8 -*-
"""主题契约测试：色板键一致、模式规整与解析（不 import tkinter）。"""
import unittest

from core import theme


class _Cfg:
    """最小 Config 替身：只需 get_setting / set_setting。"""

    def __init__(self, value=None):
        self.value = value

    def get_setting(self, key, default=None):
        return default if self.value is None else self.value

    def set_setting(self, key, value):
        self.value = value


class PaletteTest(unittest.TestCase):
    def test_palettes_share_the_same_roles(self):
        """两套主题的角色键必须完全一致，否则切换主题会丢色。"""
        self.assertEqual(set(theme.PALETTES[theme.LIGHT]),
                         set(theme.PALETTES[theme.DARK]))

    def test_palette_is_a_copy(self):
        """palette() 必须返回副本，避免调用方改坏全局色板。"""
        p = theme.palette(theme.LIGHT)
        p["primary"] = "#000000"
        self.assertNotEqual(theme.palette(theme.LIGHT)["primary"], "#000000")

    def test_every_role_is_a_color_string(self):
        for mode in (theme.LIGHT, theme.DARK):
            for key, value in theme.PALETTES[mode].items():
                self.assertTrue(
                    isinstance(value, str) and value.startswith("#") and len(value) == 7,
                    f"{mode}.{key} 不是 #RRGGBB：{value!r}")


class ModeTest(unittest.TestCase):
    def test_normalize_aliases(self):
        self.assertEqual(theme.normalize("dark"), theme.DARK)
        self.assertEqual(theme.normalize("NIGHT"), theme.DARK)
        self.assertEqual(theme.normalize("白"), theme.SYSTEM)  # 未知输入回落到默认
        self.assertEqual(theme.normalize(""), theme.SYSTEM)
        self.assertEqual(theme.normalize("auto"), theme.SYSTEM)

    def test_resolve(self):
        self.assertEqual(theme.resolve(theme.DARK), theme.DARK)
        self.assertEqual(theme.resolve(theme.LIGHT), theme.LIGHT)
        # 跟随系统：按注入的探测结果解析（不依赖真实系统外观）
        self.assertEqual(theme.resolve(theme.SYSTEM, True), theme.DARK)
        self.assertEqual(theme.resolve(theme.SYSTEM, False), theme.LIGHT)
        # system_dark=None 表示「未注入」，会走真实系统探测（此处不断言具体值）

    def test_get_and_set_mode(self):
        cfg = _Cfg()
        self.assertEqual(theme.get_mode(cfg), theme.SYSTEM)  # 未保存 → 默认跟随系统
        theme.set_mode(cfg, "black")
        self.assertEqual(theme.get_mode(cfg), theme.DARK)
        theme.set_mode(cfg, theme.SYSTEM)
        self.assertEqual(theme.get_mode(cfg), theme.SYSTEM)


if __name__ == "__main__":
    unittest.main()
