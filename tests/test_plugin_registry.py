# -*- coding: utf-8 -*-
"""core/plugins 注册表回归测试。

守护三件事：插件能被发现并声明面板 / 后台、插件并入模块开关（设置页与 CLI 自动覆盖）、
单个插件损坏不会拖垮注册表。
"""
import unittest
from unittest import mock

from core import modules, plugins
from core.plugins.base import PluginSpec


class _Cfg:
    """最小 Config 替身：只实现插件用到的两个方法。"""

    def __init__(self, data=None):
        self.data = dict(data or {})

    def get_setting(self, key, default=None):
        return self.data.get(key, default)

    def set_setting(self, key, value):
        self.data[key] = value


class PluginRegistryTest(unittest.TestCase):
    def setUp(self):
        plugins.reload()

    def test_keymouse_is_registered(self):
        spec = plugins.get("keymouse")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.panel_module, "ui.plugins.keymouse_panel")
        self.assertEqual(spec.panel_class, "KeymousePanel")
        self.assertTrue(spec.daemon_script.endswith("daemon.py"))
        self.assertEqual(spec.platforms, ("win32", "darwin"))

    def test_plugins_merge_into_module_switch(self):
        keys = [m["key"] for m in modules.MODULES]
        self.assertIn("keymouse", keys)
        # 插件默认启用
        self.assertTrue(modules.is_enabled("keymouse", _Cfg()))

    def test_disabled_plugin_is_not_enabled(self):
        """停用后不返回该插件；注册表有多个插件时逐个验证，不假设只有一个。"""
        all_keys = [spec.key for spec in plugins.discover()]
        self.assertIn("keymouse", all_keys)
        for key in all_keys:
            cfg = _Cfg({"disabled_modules": [key]})
            self.assertFalse(modules.is_enabled(key, cfg))
            self.assertNotIn(key, [s.key for s in plugins.enabled_specs(cfg)])
        cfg = _Cfg({"disabled_modules": all_keys})
        self.assertEqual(plugins.enabled_specs(cfg), [])

    def test_normalize_accepts_plugin_key(self):
        cfg = _Cfg()
        cleaned = modules.set_disabled(cfg, ["keymouse", "not-a-plugin"])
        self.assertEqual(cleaned, ["keymouse"])

    def test_broken_plugin_is_skipped(self):
        """某个插件导入抛错时，注册表只丢它自己。"""
        real_import = plugins.importlib.import_module

        def fake_import(name, *args, **kwargs):
            if name.endswith(".keymouse"):
                raise ImportError("boom")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(plugins.importlib, "import_module", fake_import):
            specs = plugins.discover(refresh=True)
        self.assertNotIn("keymouse", [s.key for s in specs])

    def test_platform_supported(self):
        spec = PluginSpec(key="x", name="X", platforms=("win32",))
        self.assertTrue(spec.platform_supported("win32"))
        self.assertFalse(spec.platform_supported("darwin"))


if __name__ == "__main__":
    unittest.main()
