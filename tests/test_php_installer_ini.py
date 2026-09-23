# -*- coding: utf-8 -*-
"""新版本安装的 ini 生成：只产出一份 php.ini（CLI 与 FastCGI 共用）。"""
import os
import tempfile
import unittest

from core.php_installer import FASTCGI_EXTRA, generate_ini

DEV_TEMPLATE = """\
; php.ini-development
extension_dir = "ext"
;extension=curl
;extension=gd
;extension=snmp
date.timezone = UTC
memory_limit = 128M
"""


class GenerateIniTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.php_dir = self.dir.name
        self._write("php.ini-development", DEV_TEMPLATE)

    def tearDown(self):
        self.dir.cleanup()

    def _write(self, name: str, content: str) -> str:
        p = os.path.join(self.php_dir, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def _read(self, name: str) -> str:
        with open(os.path.join(self.php_dir, name), "r", encoding="utf-8") as f:
            return f.read()

    def test_only_php_ini_is_generated(self):
        generate_ini(self.php_dir)
        self.assertTrue(os.path.exists(os.path.join(self.php_dir, "php.ini")))
        self.assertFalse(os.path.exists(os.path.join(self.php_dir, "php-web.ini")),
                         "CLI 与 FastCGI 已统一为一份 php.ini，不应再生成 php-web.ini")

    def test_common_tweaks_and_extensions_applied(self):
        generate_ini(self.php_dir)
        body = self._read("php.ini")
        self.assertIn('extension_dir = "ext"', body)
        self.assertIn("date.timezone = Asia/Shanghai", body)
        self.assertIn("extension=curl", body)
        self.assertIn("extension=gd", body)
        self.assertIn(";extension=snmp", body, "不在启用清单里的扩展应保持注释")

    def test_fastcgi_extras_merged_into_php_ini(self):
        """php-cgi 直连所需的关键项必须落在唯一的 php.ini 里（末尾覆盖先前值）。"""
        generate_ini(self.php_dir)
        body = self._read("php.ini")
        for line in FASTCGI_EXTRA:
            self.assertIn(line, body, "缺少 FastCGI 关键项：%s" % line)
        self.assertLess(body.index("; ---------- phpvm FastCGI ----------"),
                        len(body) - 1, "附加块应位于文件末尾")

    def test_falls_back_to_minimal_ini(self):
        os.remove(os.path.join(self.php_dir, "php.ini-development"))
        generate_ini(self.php_dir)
        body = self._read("php.ini")
        self.assertIn('extension_dir = "ext"', body)
        self.assertIn("cgi.force_redirect=0", body)


if __name__ == "__main__":
    unittest.main()
