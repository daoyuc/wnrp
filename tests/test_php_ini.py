# -*- coding: utf-8 -*-
"""php.ini 初始化：目标路径判定、模板优先级、骨架生成、已存在不覆盖。"""
import os
import tempfile
import unittest

from core.php_manager import INI_SKELETON, PhpManager, PhpVersion

from .fakes import FakeConfig


def _version(d: str, ini: str = "") -> PhpVersion:
    return PhpVersion(name="php82", display="8.2.0",
                      dir=d, cgi=os.path.join(d, "php-cgi"), ini=ini, port=9082)


class InitIniTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.mgr = PhpManager(FakeConfig())

    def _write(self, name: str, content: str) -> str:
        p = os.path.join(self.root, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    # ---------------------------------------------------------------- #
    def test_target_falls_back_to_version_dir(self):
        """未解析到配置（posix 常见）时，目标是版本目录内的 php.ini。"""
        v = _version(self.root, "")
        self.assertEqual(self.mgr.ini_target(v), os.path.join(self.root, "php.ini"))

    def test_target_keeps_resolved_path(self):
        v = _version(self.root, os.path.join(self.root, "php-web.ini"))
        self.assertEqual(self.mgr.ini_target(v), os.path.join(self.root, "php-web.ini"))

    def test_ready_reflects_file_existence(self):
        """Windows 上 v.ini 恒为「应有路径」，故必须按文件是否存在判定。"""
        v = _version(self.root, os.path.join(self.root, "php.ini"))
        self.assertFalse(self.mgr.ini_ready(v))
        self._write("php.ini", "a = 1\n")
        self.assertTrue(self.mgr.ini_ready(v))

    # ---------------------------------------------------------------- #
    def test_prefers_production_template(self):
        self._write("php.ini-production", "memory_limit = 256M\n; production\n")
        self._write("php.ini-development", "memory_limit = 128M\n")
        v = _version(self.root, "")
        ok, msg = self.mgr.init_ini(v)
        self.assertTrue(ok, msg)
        self.assertIn("production", self._read(v.ini))
        self.assertEqual(v.ini, self.mgr.ini_target(v), "成功后应同步 v.ini")

    def test_falls_back_to_development_template(self):
        self._write("php.ini-development", "memory_limit = 128M\n")
        v = _version(self.root, "")
        ok, msg = self.mgr.init_ini(v)
        self.assertTrue(ok, msg)
        self.assertIn("memory_limit = 128M", self._read(v.ini))
        self.assertIn("development", msg)

    def test_skeleton_when_no_template(self):
        v = _version(self.root, "")
        self.assertEqual(self.mgr.ini_template(v)[0], "", "无模板时不应返回路径")
        ok, msg = self.mgr.init_ini(v)
        self.assertTrue(ok, msg)
        body = self._read(v.ini)
        self.assertIn("memory_limit = 128M", body)
        self.assertIn("display_errors = On", body)
        self.assertTrue(body.strip().endswith(";extension=openssl"))
        self.assertEqual(INI_SKELETON.strip().splitlines()[0], body.splitlines()[0])

    def test_does_not_overwrite_existing(self):
        src = self._write("php.ini-production", "memory_limit = 256M\n")
        self._write("php.ini", "keep = me\n")
        v = _version(self.root, os.path.join(self.root, "php.ini"))
        ok, msg = self.mgr.init_ini(v)
        self.assertFalse(ok)
        self.assertIn("keep = me", self._read(v.ini))
        self.assertNotEqual(self._read(v.ini), self._read(src))

    def test_creates_missing_parent_dir(self):
        target_dir = os.path.join(self.root, "conf")
        v = _version(self.root, os.path.join(target_dir, "php.ini"))
        ok, msg = self.mgr.init_ini(v)
        self.assertTrue(ok, msg)
        self.assertTrue(os.path.exists(os.path.join(target_dir, "php.ini")))

    def _read(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()


if __name__ == "__main__":
    unittest.main()
