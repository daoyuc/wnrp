# -*- coding: utf-8 -*-
"""F4 既有站点一键 HTTPS：site_service.secure_site / unsecure_site。

用假 nginx 布局 + 打桩证书生成，不触碰真实 nginx 配置、证书与系统 hosts。
"""
import os
import tempfile
import types
import unittest
from unittest import mock

from core import site_service
from core.vhost_manager import VhostManager
from tests.fakes import FAILED, FakeConfig, make_layout, read

VHOST_80 = """server {
    listen 80;
    server_name app.test;
    root "/tmp/app/public";

    location ~ \\.php$ {
        fastcgi_pass 127.0.0.1:9000;
    }
}
"""

VHOST_BOTH = """server {
    listen 80;
    server_name app.test;
    root "/tmp/app/public";
}

server {
    listen 443 ssl;
    http2 on;
    server_name app.test;
    ssl_certificate     /tmp/app.test.crt;
    ssl_certificate_key /tmp/app.test.key;
}
"""


class SiteSecureTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name
        self.ng = make_layout(self.root)
        os.makedirs(self.ng.vhost_dir, exist_ok=True)
        self.conf = os.path.join(self.ng.vhost_dir, "app.test.conf")
        self._write(VHOST_80)

    def tearDown(self):
        self.dir.cleanup()

    def _write(self, text: str) -> None:
        with open(self.conf, "w", encoding="utf-8") as f:
            f.write(text)

    def _entry(self):
        return types.SimpleNamespace(file=self.conf, server_name="app.test")

    def _patch(self, *, cert_ok=True):
        def factory(config=None):
            vm = VhostManager(FakeConfig())
            vm.nginx = self.ng
            return vm

        p = mock.patch.object(site_service, "VhostManager", factory)
        p.start()
        self.addCleanup(p.stop)
        cert = {"ok": cert_ok, "cert": "/tmp/app.test.crt", "key": "/tmp/app.test.key",
                "created": True, "message": "cert"}
        cm = mock.patch.object(site_service.cert_manager, "ensure_site_cert",
                               return_value=dict(cert))
        self.addCleanup(cm.stop)
        return cm.start()

    def test_secure_appends_443_and_keeps_80(self):
        cert = self._patch()
        res = site_service.secure_site(self._entry(), FakeConfig())
        self.assertTrue(res["ok"], res)
        after = read(self.conf)
        self.assertIn("listen 80", after)
        self.assertIn("listen 443 ssl", after)
        self.assertEqual(after.count("server_name app.test"), 2)
        cert.assert_called_once_with("app.test")

    def test_secure_idempotent_when_already_https(self):
        self._write(VHOST_BOTH)
        cert = self._patch()
        res = site_service.secure_site(self._entry(), FakeConfig())
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("already"))
        cert.assert_not_called()

    def test_secure_rolls_back_when_test_fails(self):
        self.ng.test_output = FAILED
        before = read(self.conf)
        self._patch()
        res = site_service.secure_site(self._entry(), FakeConfig())
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("rolled_back"))
        self.assertEqual(read(self.conf), before, "校验失败应还原到修改前")

    def test_secure_rejects_disabled_site(self):
        disabled = self.conf + ".disabled"
        os.rename(self.conf, disabled)
        self._patch()
        res = site_service.secure_site(
            types.SimpleNamespace(file=disabled, server_name="app.test"), FakeConfig())
        self.assertFalse(res["ok"])

    def test_secure_reports_cert_failure(self):
        self._patch(cert_ok=False)
        res = site_service.secure_site(self._entry(), FakeConfig())
        self.assertFalse(res["ok"])
        self.assertNotIn("listen 443", read(self.conf), "证书失败时不应改动配置")

    def test_unsecure_removes_443_block(self):
        self._write(VHOST_BOTH)
        self._patch()
        res = site_service.unsecure_site(self._entry(), FakeConfig())
        self.assertTrue(res["ok"], res)
        after = read(self.conf)
        self.assertNotIn("listen 443", after)
        self.assertIn("listen 80", after)

    def test_secure_rejects_main_conf(self):
        self._patch()
        res = site_service.secure_site(
            types.SimpleNamespace(file=self.ng.main_conf, server_name="localhost"),
            FakeConfig())
        self.assertFalse(res["ok"])
        self.assertNotIn("listen 443", read(self.ng.main_conf))

    def test_unsecure_noop_when_not_https(self):
        self._patch()
        res = site_service.unsecure_site(self._entry(), FakeConfig())
        self.assertTrue(res["ok"])
        self.assertTrue(res.get("unchanged"))


if __name__ == "__main__":
    unittest.main()
