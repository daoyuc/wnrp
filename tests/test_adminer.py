# -*- coding: utf-8 -*-
"""F9 Adminer：文件校验 / PHP 版本选择 / 安装编排（全假依赖，不下载、不建站）。"""
import os
import tempfile
import types
import unittest
from unittest import mock

from core import adminer


class FakeConfig:
    def __init__(self, ports=None):
        self.ports = dict(ports or {})


class FakePhpManager:
    def __init__(self, versions):
        self._v = versions

    def scan_versions(self):
        return self._v


class FakeSiteResult:
    def __init__(self, fatal=False, failed_tags=()):
        self.fatal = fatal
        self._failed = set(failed_tags)
        self.steps = [("file", {"ok": True}), ("test", {"ok": True, "skip": True})]
        self.path = "/tmp/adminer.test.conf"

    def failed(self, tag):
        return tag in self._failed


def _php_file(path: str, body: str = "<?php\n// adminer\n") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body + ("x" * 2048))


class AdminerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.tmp = self.dir.name
        self.file = os.path.join(self.tmp, "adminer", adminer.FILE_NAME)
        p1 = mock.patch.object(adminer, "adminer_dir", return_value=os.path.join(self.tmp, "adminer"))
        p1.start()
        self.addCleanup(p1.stop)

    def tearDown(self):
        self.dir.cleanup()

    def test_is_installed_requires_php_file(self):
        self.assertFalse(adminer.is_installed())
        _php_file(self.file)
        self.assertTrue(adminer.is_installed())

    def test_is_installed_rejects_non_php(self):
        _php_file(self.file, body="<html>nope</html>")
        self.assertFalse(adminer.is_installed())

    def test_pick_php_by_name(self):
        cfg = FakeConfig({"php82": 9000})
        with mock.patch.object(adminer, "PhpManager",
                               lambda c: FakePhpManager([types.SimpleNamespace(name="php82", port=9000)])):
            self.assertEqual(adminer.pick_php(cfg, "php82"), ("php82", 9000))
            self.assertEqual(adminer.pick_php(cfg, "php99"), (None, None))

    def test_pick_php_prefers_running_then_newest(self):
        versions = [types.SimpleNamespace(name="php74", port=9074, running=False),
                    types.SimpleNamespace(name="php85", port=9085, running=True),
                    types.SimpleNamespace(name="php82", port=9000, running=False)]
        with mock.patch.object(adminer, "PhpManager", lambda c: FakePhpManager(versions)):
            # php85 在跑 → 优先
            self.assertEqual(adminer.pick_php(FakeConfig()), ("php85", 9085))

    def test_install_dry_run(self):
        with mock.patch.object(adminer, "PhpManager",
                               lambda c: FakePhpManager([types.SimpleNamespace(name="php82", port=9000)])):
            res = adminer.install(FakeConfig({"php82": 9000}), dry_run=True)
        self.assertTrue(res["ok"])
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["php"], "php82")

    def test_install_download_failure_propagates(self):
        with mock.patch.object(adminer, "PhpManager",
                               lambda c: FakePhpManager([types.SimpleNamespace(name="php82", port=9000)])), \
                mock.patch.object(adminer, "is_installed", return_value=False), \
                mock.patch.object(adminer, "download",
                                  return_value={"ok": False, "message": "offline"}):
            res = adminer.install(FakeConfig({"php82": 9000}))
        self.assertFalse(res["ok"])
        self.assertEqual(res["message"], "offline")

    def test_install_creates_site(self):
        _php_file(self.file)
        captured = {}

        def fake_create(plan, config):
            captured["plan"] = plan
            return FakeSiteResult()

        with mock.patch.object(adminer, "PhpManager",
                               lambda c: FakePhpManager([types.SimpleNamespace(name="php82", port=9000)])), \
                mock.patch.object(adminer.site_service, "create_site", fake_create):
            res = adminer.install(FakeConfig({"php82": 9000}))
        self.assertTrue(res["ok"], res)
        self.assertEqual(captured["plan"].docroot, os.path.join(self.tmp, "adminer"))
        self.assertEqual(captured["plan"].domains, [adminer.DEFAULT_DOMAIN])
        self.assertEqual(captured["plan"].port, 9000)

    def test_install_reports_site_failure(self):
        _php_file(self.file)
        with mock.patch.object(adminer, "PhpManager",
                               lambda c: FakePhpManager([types.SimpleNamespace(name="php82", port=9000)])), \
                mock.patch.object(adminer.site_service, "create_site",
                                  lambda plan, config: FakeSiteResult(fatal=True, failed_tags=["test"])):
            res = adminer.install(FakeConfig({"php82": 9000}))
        self.assertFalse(res["ok"])
        self.assertIn("test", res["message"])

    def test_no_php_available(self):
        with mock.patch.object(adminer, "PhpManager", lambda c: FakePhpManager([])):
            res = adminer.install(FakeConfig())
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
