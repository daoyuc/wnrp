# -*- coding: utf-8 -*-
"""站点巡检插件测试：目标展开、单站点探测、汇总口径。

一律用本机回环 HTTP 服务与注入的 ``VhostEntry``，不访问外网、不读真实 nginx 配置。
"""
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from core.plugins.site_health import MANIFEST, probe
from core.vhost_manager import VhostEntry


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path == "/slow":
            time.sleep(0.4)
            self._reply(200)
        elif self.path == "/missing":
            self._reply(404)
        else:
            self._reply(200)

    def _reply(self, code: int) -> None:
        body = b"hello"
        try:
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            # 客户端（超时用例）提前断开，属于预期，不打印噪音
            pass

    def log_message(self, *args):  # noqa: D102 - 静默测试噪音
        pass


class _ServerMixin:
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ManifestTest(unittest.TestCase):
    def test_manifest_shape(self):
        self.assertEqual(MANIFEST.key, "site_health")
        self.assertTrue(MANIFEST.panel)
        self.assertIn("win32", MANIFEST.platforms)


class TargetsTest(unittest.TestCase):
    def test_expands_multi_domain_and_skips_unusable(self):
        entries = [
            VhostEntry(server_name="gmlbm.test www.gmlbm.test", file="a.conf"),
            VhostEntry(server_name="*.wild.test", file="b.conf"),
            VhostEntry(server_name="_", file="c.conf"),
            VhostEntry(server_name="gmlbm.test", file="d.conf"),
            VhostEntry(server_name="off.test", file="e.conf", disabled=True),
            VhostEntry(server_name="bad..name", file="f.conf"),
        ]
        got = [it["domain"] for it in probe.targets(entries)]
        self.assertEqual(got, ["gmlbm.test", "www.gmlbm.test"])

    def test_include_disabled_keeps_site(self):
        entries = [VhostEntry(server_name="off.test", file="e.conf",
                              disabled=True)]
        self.assertEqual(probe.targets(entries), [])
        got = probe.targets(entries, include_disabled=True)
        self.assertEqual([it["domain"] for it in got], ["off.test"])

    def test_sites_accepts_injected_entries(self):
        entries = [VhostEntry(server_name="a.test", file="a.conf",
                              root="/www/a", php_version="php82")]
        got = probe.sites(entries=entries)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["root"], "/www/a")
        self.assertEqual(got[0]["php_version"], "php82")


class ProbeTest(_ServerMixin, unittest.TestCase):
    def test_ok(self):
        res = probe.probe(self.url("/"), timeout=2)
        self.assertEqual(res["status"], 200)
        self.assertEqual(res["state"], "ok")
        self.assertTrue(res["ok"])
        self.assertEqual(res["bytes"], 5)
        self.assertEqual(res["error"], "")
        self.assertGreaterEqual(res["ms"], 0)

    def test_404_is_warn(self):
        res = probe.probe(self.url("/missing"), timeout=2)
        self.assertEqual(res["status"], 404)
        self.assertEqual(res["state"], "warn")
        self.assertTrue(res["ok"])
        self.assertEqual(res["error"], "")

    def test_refused(self):
        # Windows 上连未监听端口可能先超时再报 WSAECONNREFUSED，两种都算「连不上」
        res = probe.probe(f"http://127.0.0.1:{_closed_port()}/", timeout=3)
        self.assertEqual(res["state"], "down")
        self.assertFalse(res["ok"])
        self.assertIn(res["error"], ("refused", "timeout"))
        self.assertIsNone(res["status"])

    def test_timeout(self):
        res = probe.probe(self.url("/slow"), timeout=0.1)
        self.assertEqual(res["state"], "down")
        self.assertIn(res["error"], ("timeout", "network"))

    def test_bad_url(self):
        for url in ("ftp://x/", "nonsense", ""):
            res = probe.probe(url, timeout=1)
            self.assertEqual(res["error"], "bad_url")

    def test_error_codes_are_translatable(self):
        for code, text in probe.ERROR_MESSAGES.items():
            self.assertIsInstance(code, str)
            self.assertTrue(text, code)


class ReportTest(unittest.TestCase):
    ENTRIES = [
        VhostEntry(server_name="a.test", file="a.conf", php_version="php82"),
        VhostEntry(server_name="b.test", file="b.conf"),
        VhostEntry(server_name="c.test", file="c.conf", disabled=True),
    ]

    @staticmethod
    def _row(url: str, status, state: str, ms: int) -> dict:
        return {
            "url": url, "scheme": url.split(":")[0], "host": "", "port": None,
            "status": status, "state": state, "ok": state != "down",
            "ms": ms, "bytes": 10, "location": "", "error": "",
            "insecure": False, "cert_days": None,
        }

    def _patch(self, calls: list):
        def fake(url, timeout=probe.DEFAULT_TIMEOUT):
            calls.append(url)
            if "a.test" in url:
                return self._row(url, 200, "ok", 10)
            return self._row(url, 500, "down", 30)

        return mock.patch.object(probe, "probe", side_effect=fake)

    def test_summary_and_skip_disabled(self):
        calls: list[str] = []
        with self._patch(calls):
            data = probe.report(entries=self.ENTRIES, workers=2)
        self.assertEqual(data["total"], 2)  # 禁用站点不探测
        self.assertEqual(data["up"], 1)
        self.assertEqual(data["down"], 1)
        self.assertEqual(data["avg_ms"], 10)  # 只统计「有响应」的站点
        self.assertEqual(sorted(calls),
                         ["http://a.test/", "http://b.test/"])

    def test_only_and_limit(self):
        calls: list[str] = []
        with self._patch(calls):
            data = probe.report(entries=self.ENTRIES, only="a")
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["sites"][0]["domain"], "a.test")

        calls.clear()
        with self._patch(calls):
            data = probe.report(entries=self.ENTRIES, limit=1)
        self.assertEqual(data["total"], 1)

    def test_https_scheme(self):
        calls: list[str] = []
        with self._patch(calls):
            data = probe.report(entries=self.ENTRIES, scheme="https",
                                limit=1)
        self.assertEqual(data["scheme"], "https")
        self.assertTrue(calls[0].startswith("https://"))

    def test_unknown_scheme_falls_back_to_http(self):
        with self._patch([]):
            data = probe.report(entries=self.ENTRIES, scheme="gopher",
                                limit=1)
        self.assertEqual(data["scheme"], "http")


if __name__ == "__main__":
    unittest.main()
