# -*- coding: utf-8 -*-
"""core/hosts_manager：托管块写入、冲突保护、备份/还原与移除。

通过把 SystemRoot 指到临时目录来重定向 hosts 路径（hosts_manager.hosts_path()
在调用时才读环境变量），全程不触碰真实系统 hosts。
"""
import os
import tempfile
import unittest
from unittest import mock

from core import hosts_manager
from core.config import IS_WIN

HOSTS = """# 测试用 hosts
127.0.0.1   localhost
10.0.0.5    other.test
"""


@unittest.skipUnless(IS_WIN, "hosts 写入路径仅 Windows 有实现（posix 为 /etc/hosts）")
class HostsManagerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.etc = os.path.join(self.dir.name, "System32", "drivers", "etc")
        os.makedirs(self.etc, exist_ok=True)
        self.hosts = os.path.join(self.etc, "hosts")
        with open(self.hosts, "w", encoding="utf-8") as f:
            f.write(HOSTS)
        self.env = mock.patch.dict(os.environ, {"SystemRoot": self.dir.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def _read(self) -> str:
        with open(self.hosts, "r", encoding="utf-8") as f:
            return f.read()

    def test_ensure_entries_appends_managed_block(self):
        self.assertEqual(hosts_manager.hosts_path(), self.hosts)
        res = hosts_manager.ensure_entries(["a.test"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["added"], ["a.test"])
        text = self._read()
        self.assertIn("# >>> phpvm-managed", text)
        self.assertIn("127.0.0.1 a.test", text)
        self.assertIn("10.0.0.5    other.test", text, "原有行必须原样保留")

    def test_ensure_entries_does_not_touch_local_or_conflicting(self):
        res = hosts_manager.ensure_entries(["localhost", "other.test"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["added"], [])
        self.assertEqual(res["already"], ["localhost"])
        self.assertEqual(res["conflict"], ["other.test"])
        self.assertNotIn(">>>", self._read(), "无需修改时不应写入托管块")

    def test_backup_then_restore_round_trip(self):
        before = self._read()
        ok, backup = hosts_manager.create_backup()
        self.assertTrue(ok)
        self.assertEqual(backup, hosts_manager.backup_path())
        self.assertTrue(hosts_manager.has_backup())
        with open(self.hosts, "a", encoding="ascii") as f:
            f.write("127.0.0.1 manual.test\n")
        ok, msg = hosts_manager.restore_backup()
        self.assertTrue(ok, msg)
        self.assertEqual(self._read(), before)

    def test_remove_entries_only_removes_managed_domains(self):
        hosts_manager.ensure_entries(["a.test", "b.test"])
        res = hosts_manager.remove_entries(["a.test"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["removed"], ["a.test"])
        text = self._read()
        self.assertNotIn("a.test", text)
        self.assertIn("b.test", text, "托管块内其它域名应保留")
        self.assertIn("10.0.0.5    other.test", text, "非托管行必须保留")

    def test_remove_entries_reports_missing_without_writing(self):
        before = self._read()
        res = hosts_manager.remove_entries(["not-there.test"])
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["removed"], [])
        self.assertEqual(res["missing"], ["not-there.test"])
        self.assertEqual(self._read(), before)


if __name__ == "__main__":
    unittest.main()
