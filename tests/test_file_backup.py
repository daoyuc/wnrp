# -*- coding: utf-8 -*-
"""core/file_backup：备份、还原、备份清理与「源不存在」语义。"""
import os
import tempfile
import unittest

from core import file_backup


class FileBackupTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "nginx.conf")
        self._write("v1")

    def tearDown(self):
        self.dir.cleanup()

    def _write(self, text: str) -> None:
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def _read(self) -> str:
        with open(self.path, "r", encoding="utf-8") as f:
            return f.read()

    def test_backup_and_restore_removes_backup(self):
        bak = file_backup.backup(self.path)
        self.assertEqual(bak, self.path + ".bak")
        self.assertTrue(os.path.exists(bak))
        self._write("v2")
        self.assertTrue(file_backup.restore(bak, self.path))
        self.assertEqual(self._read(), "v1")
        self.assertFalse(os.path.exists(bak), "默认还原后应删除备份（保持历史行为）")

    def test_restore_keeps_backup_when_asked(self):
        bak = file_backup.backup(self.path)
        self._write("v2")
        self.assertTrue(file_backup.restore(bak, self.path, remove_backup=False))
        self.assertEqual(self._read(), "v1")
        self.assertTrue(os.path.exists(bak))

    def test_restore_derives_target_from_backup_name(self):
        bak = file_backup.backup(self.path)
        self._write("v2")
        self.assertTrue(file_backup.restore(bak))  # 不传 path，按后缀反推
        self.assertEqual(self._read(), "v1")

    def test_backup_missing_source_returns_empty(self):
        self.assertEqual(file_backup.backup(os.path.join(self.dir.name, "nope")), "")
        self.assertFalse(file_backup.has_backup(os.path.join(self.dir.name, "nope")))

    def test_custom_suffix_like_hosts(self):
        bak = file_backup.backup(self.path, ".phpvm.bak")
        self.assertTrue(bak.endswith(".phpvm.bak"))
        self.assertTrue(file_backup.has_backup(self.path, ".phpvm.bak"))
        self._write("v2")
        self.assertTrue(file_backup.restore(bak))  # 反推原路径
        self.assertEqual(self._read(), "v1")

    def test_restore_without_backup_is_false_and_silent(self):
        self.assertFalse(file_backup.restore(os.path.join(self.dir.name, "x.bak"), self.path))
        self.assertEqual(self._read(), "v1")


if __name__ == "__main__":
    unittest.main()
