# -*- coding: utf-8 -*-
"""PhpManager 版本探测：``php -v`` 失败时给出可读原因（如 Homebrew 升级 icu 后老 keg 缺库）。

替代方案是只显示「未知」，用户无法区分「版本解析不出」与「二进制跑不起来」。
"""
import os
import tempfile
import unittest
from unittest import mock

from core import i18n as _i18n
from core import php_manager
from core.php_manager import PhpManager, PhpVersion, error_hint
from tests.fakes import FakeConfig

DYLD = ("dyld[60651]: Library not loaded: @loader_path/../../../../opt/icu4c/lib/"
        "libicui18n.74.dylib\n"
        "  Referenced from: /opt/homebrew/Cellar/php@5.6/5.6.40_10/bin/php\n"
        "  Reason: tried: '...' (no such file)\n")


class _LangFixture(unittest.TestCase):
    """断言中文文案：显式切到 zh_CN，避免英文 locale 下环境性失败。"""

    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")

    def tearDown(self):
        _i18n.set_language(self._lang)


class ErrorHintTest(_LangFixture):
    def test_dyld_missing_library_is_summarized(self):
        self.assertEqual(error_hint(DYLD), "缺少动态库：libicui18n.74.dylib")

    def test_generic_output_falls_back_to_first_line(self):
        self.assertEqual(error_hint("\n\nkaboom: bad things\nmore"), "kaboom: bad things")

    def test_empty_output(self):
        self.assertEqual(error_hint(""), "")
        self.assertEqual(error_hint(None), "")


class ProbeVersionTest(_LangFixture):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.exe = os.path.join(self.dir.name, php_manager.CLI_NAME)
        with open(self.exe, "wb") as f:
            f.write(b"")
        self.v = PhpVersion(name="php56", display="", dir=self.dir.name,
                            cgi=self.exe, ini="", port=9056)
        php_manager.PhpManager._VERSION_CACHE.clear()

    def _patch(self, out, err):
        p = mock.patch.object(php_manager.pu, "run_cmd", return_value=(0, out, err))
        p.start()
        self.addCleanup(p.stop)

    def _resolve(self):
        pm = PhpManager(FakeConfig())
        pm.versions = [self.v]
        pm.resolve(refresh_status=False)
        return pm

    def test_failure_records_reason_and_unknown_display(self):
        self._patch("", DYLD)
        self._resolve()
        self.assertEqual(self.v.display, php_manager.t("未知"))
        self.assertEqual(self.v.error, "缺少动态库：libicui18n.74.dylib")

    def test_success_clears_reason(self):
        self._patch("PHP 5.6.40 (cli) (built: Sep 27 2024)\n", "")
        self._resolve()
        self.assertEqual(self.v.display, "5.6.40")
        self.assertEqual(self.v.error, "")

    def test_parse_version_still_returns_string(self):
        self._patch("PHP 7.4.33 (cli)\n", "")
        pm = PhpManager(FakeConfig())
        self.assertEqual(pm.parse_version(self.v), "7.4.33")


if __name__ == "__main__":
    unittest.main()
