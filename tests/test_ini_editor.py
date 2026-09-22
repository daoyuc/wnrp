# -*- coding: utf-8 -*-
"""core/ini_editor：关键配置项读写（备份、精确行替换、未命中键追加）。"""
import os
import tempfile
import unittest

from core import ini_editor

INI = """[PHP]
memory_limit = 128M
; memory_limit = 64M
upload_max_filesize = 2M
date.timezone = UTC
"""


class IniEditorTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ini = os.path.join(self.dir.name, "php.ini")
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write(INI)

    def tearDown(self):
        self.dir.cleanup()

    def _read(self) -> str:
        with open(self.ini, "r", encoding="utf-8") as f:
            return f.read()

    def test_save_values_replaces_line_and_keeps_backup(self):
        changed, backup = ini_editor.save_values(self.ini, {"memory_limit": "512M"})
        self.assertEqual(changed, 1)
        self.assertTrue(os.path.exists(backup))
        text = self._read()
        self.assertIn("memory_limit = 512M", text)
        self.assertIn("; memory_limit = 64M", text, "注释行不应被改写")

    def test_save_values_appends_missing_key(self):
        ini_editor.save_values(self.ini, {"opcache.enable": "1"})
        self.assertIn("opcache.enable = 1", self._read())

    def test_load_values_ignores_commented_lines(self):
        values = ini_editor.load_values(self.ini)
        self.assertEqual(values.get("memory_limit"), "128M")
        self.assertEqual(values.get("date.timezone"), "UTC")

    def test_save_values_matches_key_case_insensitively(self):
        ini_editor.save_values(self.ini, {"MEMORY_LIMIT": "256M"})
        self.assertIn("memory_limit = 256M", self._read())


if __name__ == "__main__":
    unittest.main()
