# -*- coding: utf-8 -*-
"""core/net_ssl.py 回归测试。

守护三件事：证书校验不降级、新 CA 包确实被加载、core/ 下各 HTTPS 入口
（含扩展安装委托 php_downloader 的链路）都带上了共享 SSL 上下文（否则会
退回系统过期根，复现 'certificate has expired'）。
"""
import os
import ssl
import tempfile
import unittest
from unittest import mock

from core import net_ssl, php_downloader, php_extension, updater


class SslContextTest(unittest.TestCase):
    def test_verification_stays_on(self):
        """不能靠关掉校验来绕开过期根。"""
        ctx = net_ssl.ssl_context()
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_ca_bundle_loaded(self):
        """随仓库分发的 CA 包必须存在且被载入（否则又落回系统过期根）。"""
        self.assertTrue(os.path.isfile(net_ssl._CA_BUNDLE))
        self.assertGreater(len(net_ssl.ssl_context().get_ca_certs()), 100)

    def test_context_is_cached(self):
        self.assertIs(net_ssl.ssl_context(), net_ssl.ssl_context())


class _FakeResponse:
    """最小 urlopen 返回值：支持 with / read / headers。"""

    def __init__(self, data=b"", headers=None):
        self._data = data
        self.headers = headers or {}
        self._done = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n=-1):
        if self._done:
            return b""
        self._done = True
        return self._data


class HttpsCallSitesPassContext(unittest.TestCase):
    """各下载入口的 urlopen 调用都必须传入共享 SSL 上下文。"""

    def _assert_context(self, module, call, *args, **kwargs):
        with mock.patch.object(module.urllib.request, "urlopen",
                               return_value=_FakeResponse()) as m:
            try:
                call(*args, **kwargs)
            except Exception:
                pass  # 假响应不完整，只关心 urlopen 的入参
        self.assertIn("context", m.call_args.kwargs)
        self.assertIs(m.call_args.kwargs["context"], net_ssl.ssl_context())

    def test_http_get(self):
        self._assert_context(php_downloader, php_downloader._http_get,
                             "https://example.test")

    def test_download_with_progress(self):
        with tempfile.TemporaryDirectory() as d:
            self._assert_context(
                php_downloader, php_downloader.download_with_progress,
                "https://example.test", os.path.join(d, "f"))

    def test_extension_download(self):
        # _download 已委托 php_downloader，urlopen 调用点也在该模块
        with tempfile.TemporaryDirectory() as d:
            self._assert_context(php_downloader, php_extension._download,
                                 "https://example.test", os.path.join(d, "f.dll"))

    def test_updater_open(self):
        self._assert_context(updater, updater._open, "https://example.test")


if __name__ == "__main__":
    unittest.main()
