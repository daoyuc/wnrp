# -*- coding: utf-8 -*-
"""数据库面板：模块登记、导入与自动刷新契约。"""
import os
import tempfile
import unittest
from unittest import mock

from core import i18n as _i18n
from core import modules
from core.db import store


class DbModuleTest(unittest.TestCase):
    def test_db_is_registered_in_site_group(self):
        keys = {m["key"] for m in modules.MODULES}
        self.assertIn("db", keys)
        self.assertEqual(modules.group_of("db"), "site")
        self.assertFalse(modules.MODULES[
            next(i for i, m in enumerate(modules.MODULES) if m["key"] == "db")
        ]["required"])


class DbPanelSmokeTest(unittest.TestCase):
    """无显示环境时跳过；有显示时真建一次面板。"""

    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "PHPVM_DB_CONNECTIONS": os.path.join(self._tmp.name, "db_connections.json"),
            "PHPVM_DB_AUDIT_DIR": os.path.join(self._tmp.name, "db_audit"),
        })
        env.start()
        self.addCleanup(env.stop)
        mock.patch.object(store, "set_secret_permissions", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        _i18n.set_language(self._lang)

    def test_panel_builds_and_reloads(self):
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
        except Exception:  # noqa: BLE001
            self.skipTest("no display")
            return
        try:
            from ui.db_panel import DbPanel
            store.add({"name": "app", "engine": "sqlite",
                       "path": os.path.join(self._tmp.name, "missing.sqlite")})
            # path 文件可不存在：列表只读本地清单
            notes = []
            panel = DbPanel(root, lambda msg, level="info": notes.append((msg, level)))
            self.assertTrue(hasattr(panel, "auto_refresh"))
            self.assertTrue(hasattr(panel, "refresh_theme"))
            panel.auto_refresh()
            self.assertEqual(len(panel._connections), 1)
            panel.refresh_theme()
            panel.destroy()
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
