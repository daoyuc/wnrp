# -*- coding: utf-8 -*-
"""证书到期插件测试：日期解析、vhost 扫描、汇总分级与 CLI 入口。"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

import cli
from core.plugins.cert_watch import MANIFEST, watch


class ParseTest(unittest.TestCase):
    def test_not_after_variants(self):
        stamp = watch.parse_not_after("notAfter=Nov 12 23:59:59 2026 GMT")
        self.assertIsNotNone(stamp)
        self.assertEqual(time.strftime("%Y-%m-%d", time.gmtime(stamp)),
                         "2026-11-12")
        # 单位数日期在 openssl 输出里是双空格
        self.assertIsNotNone(
            watch.parse_not_after("notAfter=Nov  2 00:00:00 2030 GMT"))

    def test_not_after_garbage(self):
        self.assertIsNone(watch.parse_not_after(""))
        self.assertIsNone(watch.parse_not_after("notAfter=nonsense"))

    def test_parse_x509(self):
        text = ("notAfter=Nov 12 23:59:59 2026 GMT\n"
                "subject=CN=example.com\n"
                "issuer=C=US, O=Let's Encrypt\n")
        parsed = watch.parse_x509(text)
        self.assertEqual(parsed["subject"], "CN=example.com")
        self.assertIn("Let's Encrypt", parsed["issuer"])
        self.assertTrue(parsed["not_after_raw"].startswith("Nov 12"))

    def test_levels(self):
        self.assertEqual(watch.level_of_days(-1), "expired")
        self.assertEqual(watch.level_of_days(0), "critical")
        self.assertEqual(watch.level_of_days(7), "critical")
        self.assertEqual(watch.level_of_days(8), "warn")
        self.assertEqual(watch.level_of_days(30), "warn")
        self.assertEqual(watch.level_of_days(31), "ok")


class FindTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.conf = os.path.join(self._tmp.name, "a.com.conf")
        with open(self.conf, "w", encoding="utf-8") as f:
            f.write("server {\n  server_name a.com www.a.com;\n"
                    "  ssl_certificate certs/a.crt;\n"
                    "  ssl_certificate_key certs/a.key;\n"
                    "  ssl_certificate $dynamic;\n}\n")

    def test_finds_relative_cert_and_skips_variables(self):
        rows = watch.find_cert_paths(
            [SimpleNamespace(server_name="a.com www.a.com", file=self.conf)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["site"], "a.com")
        self.assertEqual(rows[0]["path"],
                         os.path.join(self._tmp.name, "certs", "a.crt"))

    def test_missing_conf_is_ignored(self):
        rows = watch.find_cert_paths(
            [SimpleNamespace(server_name="x", file="/no/such.conf")])
        self.assertEqual(rows, [])

    def test_dedupes_same_path(self):
        entry = SimpleNamespace(server_name="a.com", file=self.conf)
        self.assertEqual(len(watch.find_cert_paths([entry, entry])), 1)


class ReportTest(unittest.TestCase):
    NOW = 1_800_000_000.0  # 固定时间，避免跨日抖动

    def certs(self):
        day = 86400
        return [
            {"site": "ok.com", "path": "/c/ok.crt",
             "not_after": self.NOW + 90 * day},
            {"site": "warn.com", "path": "/c/warn.crt",
             "not_after": self.NOW + 20 * day},
            {"site": "crit.com", "path": "/c/crit.crt",
             "not_after": self.NOW + 3 * day},
            {"site": "old.com", "path": "/c/old.crt",
             "not_after": self.NOW - 5 * day},
        ]

    def test_counts_and_order(self):
        data = watch.report(certs=self.certs(), now=self.NOW)
        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 4)
        self.assertEqual((data["expired"], data["critical"],
                          data["warn"], data["valid"]), (1, 1, 1, 1))
        self.assertEqual([it["site"] for it in data["items"]],
                         ["old.com", "crit.com", "warn.com", "ok.com"])
        self.assertEqual(data["nearest"]["site"], "old.com")
        self.assertEqual(data["items"][1]["days_left"], 3)

    def test_within_filter_keeps_expired(self):
        data = watch.report(certs=self.certs(), now=self.NOW, within=30)
        self.assertEqual([it["site"] for it in data["items"]],
                         ["old.com", "crit.com", "warn.com"])

    def test_only_filter(self):
        data = watch.report(certs=self.certs(), now=self.NOW, only="warn")
        self.assertEqual([it["site"] for it in data["items"]], ["warn.com"])

    def test_empty_injected_is_ok(self):
        data = watch.report(certs=[], now=self.NOW)
        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 0)
        self.assertIsNone(data["nearest"])


@unittest.skipUnless(shutil.which("openssl"), "需要系统 openssl")
class OpenSslTest(unittest.TestCase):
    def test_reads_self_signed_certificate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert = os.path.join(tmp, "a.crt")
            key = os.path.join(tmp, "a.key")
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-days", "30", "-subj", "/CN=cert-watch.test",
                 "-keyout", key, "-out", cert],
                check=True, capture_output=True)
            info = watch.cert_info(cert)
            self.assertIsNotNone(info)
            self.assertGreater(info["not_after"], time.time())
            self.assertIn("cert-watch.test", info.get("subject", ""))

    def test_broken_file_returns_none(self):
        with tempfile.NamedTemporaryFile("w", suffix=".crt",
                                         delete=False) as f:
            f.write("not a certificate")
            path = f.name
        try:
            self.assertIsNone(watch.cert_info(path))
        finally:
            os.unlink(path)


class CliTest(unittest.TestCase):
    def test_parser_accepts_options(self):
        ns = cli.build_parser().parse_args(
            ["certs", "list", "--within", "30", "--only", "a.com"])
        self.assertEqual(ns.within, 30)
        self.assertEqual(ns.only, "a.com")

    def test_manifest_declared(self):
        self.assertEqual(MANIFEST.key, "cert_watch")
        self.assertEqual(MANIFEST.nav_group, "diag")
        self.assertTrue(MANIFEST.platform_supported(sys.platform))


if __name__ == "__main__":
    unittest.main()
