# -*- coding: utf-8 -*-
"""F1 诊断纯函数测试：用假 VhostEntry / 假依赖注入，不触真实 Nginx/PHP/hosts。"""
import os
import tempfile
import types
import unittest

from core import diag
from core import i18n as _i18n
from core.vhost_manager import VhostEntry

NS = types.SimpleNamespace


class _ZhCN:
    """本机 locale 为英文时 `t()` 返回英译，而测试断言中文源串；
    强制 zh_CN 让 `t()` 回落原文，tearDown 还原以免影响其它模块。"""

    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")

    def tearDown(self):
        _i18n.set_language(self._lang)


def _entry(**kw) -> VhostEntry:
    base = dict(server_name="app.test", file="/tmp/app.conf", root="",
                port=None, php_version=None, note="", disabled=False)
    base.update(kw)
    return VhostEntry(**base)


class DiagChecksTest(_ZhCN, unittest.TestCase):
    def test_nginx_running(self):
        self.assertEqual(diag.check_nginx_running(True).level, diag.LEVEL_OK)
        it = diag.check_nginx_running(False)
        self.assertEqual(it.level, diag.LEVEL_ERR)
        self.assertEqual(it.fix_key, "nginx")

    def test_include(self):
        self.assertEqual(diag.check_include(True).level, diag.LEVEL_OK)
        it = diag.check_include(False, "/x/vhost")
        self.assertEqual(it.level, diag.LEVEL_WARN)
        self.assertEqual(it.fix_key, "include")

    def test_vhost_loaded(self):
        self.assertEqual(diag.check_vhost_loaded(_entry()).level, diag.LEVEL_OK)
        it = diag.check_vhost_loaded(_entry(disabled=True))
        self.assertEqual(it.level, diag.LEVEL_ERR)
        self.assertEqual(it.fix_key, "enable")

    def test_hosts(self):
        e = _entry(server_name="app.test www.app.test")
        self.assertEqual(diag.check_hosts(e, {"app.test": "127.0.0.1",
                                             "www.app.test": "127.0.0.1"}).level, diag.LEVEL_OK)
        it = diag.check_hosts(e, {"app.test": "10.0.0.1"})
        self.assertEqual(it.level, diag.LEVEL_WARN)
        self.assertEqual(it.fix_key, "hosts")

    def test_port_mapping(self):
        # 无 fastcgi_pass → 不适用（静态站点）
        self.assertEqual(diag.check_port_mapping(_entry(), NS(ports={})).level, diag.LEVEL_SKIP)
        # 端口已映射版本 → ok
        self.assertEqual(
            diag.check_port_mapping(_entry(port=9000, php_version="php82"),
                                   NS(ports={"php82": 9000})).level, diag.LEVEL_OK)
        # 端口未映射任何版本 → err（含当前映射表）
        it = diag.check_port_mapping(_entry(port=9000), NS(ports={"php82": 9000}))
        self.assertEqual(it.level, diag.LEVEL_ERR)
        self.assertIn("php82", it.detail)

    def test_php_running(self):
        e = _entry(port=9000, php_version="php82")
        self.assertEqual(
            diag.check_php_running(e, [NS(name="php82", running=True)]).level, diag.LEVEL_OK)
        it = diag.check_php_running(e, [NS(name="php82", running=False)])
        self.assertEqual(it.level, diag.LEVEL_ERR)
        self.assertEqual(it.fix_key, "php")
        # 版本未安装 → warn
        self.assertEqual(
            diag.check_php_running(e, [NS(name="php74", running=True)]).level, diag.LEVEL_WARN)
        # 无版本关联 → skip
        self.assertEqual(
            diag.check_php_running(_entry(port=None, php_version=None), []).level, diag.LEVEL_SKIP)

    def test_root(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "index.php"), "w") as f:
            f.write("<?php")
        self.assertEqual(diag.check_root(_entry(root=tmp)).level, diag.LEVEL_OK)
        miss = os.path.join(tmp, "nope")
        self.assertEqual(diag.check_root(_entry(root=miss)).level, diag.LEVEL_ERR)
        empty = tempfile.mkdtemp()
        self.assertEqual(diag.check_root(_entry(root=empty)).level, diag.LEVEL_WARN)

    def test_https_cert(self):
        tmp = tempfile.mkdtemp()
        plain = os.path.join(tmp, "plain.conf")
        with open(plain, "w") as f:
            f.write("server { listen 80; server_name app.test; }")
        self.assertEqual(diag.check_https_cert(_entry(file=plain)).level, diag.LEVEL_SKIP)

        cert = os.path.join(tmp, "app.pem")
        with open(cert, "w") as f:
            f.write("x")
        https = os.path.join(tmp, "https.conf")
        with open(https, "w") as f:
            f.write(f"server {{ listen 443 ssl; ssl_certificate {cert}; }}")
        self.assertEqual(diag.check_https_cert(_entry(file=https)).level, diag.LEVEL_OK)

        https_bad = os.path.join(tmp, "https_bad.conf")
        with open(https_bad, "w") as f:
            f.write("server { listen 443 ssl; ssl_certificate /no/such.pem; }")
        self.assertEqual(diag.check_https_cert(_entry(file=https_bad)).level, diag.LEVEL_WARN)

    def test_error_log(self):
        tmp = tempfile.mkdtemp()
        log = os.path.join(tmp, "error.log")
        with open(log, "w") as f:
            f.write("2026/01/01 00:00:00 [error] app.test 502 Bad Gateway\n")
            f.write("2026/01/01 00:00:01 [info] something else\n")
        it = diag.check_error_log(_entry(server_name="app.test"), tmp)
        self.assertIn(it.level, (diag.LEVEL_ERR, diag.LEVEL_WARN))
        self.assertIn("502", it.detail)

        clean_dir = tempfile.mkdtemp()
        clean_log = os.path.join(clean_dir, "error.log")
        with open(clean_log, "w") as f:
            f.write("2026/01/01 [info] app.test ok\n")
        self.assertEqual(diag.check_error_log(_entry(server_name="app.test"), clean_dir,
                                             max_lines=80).level, diag.LEVEL_OK)

        self.assertEqual(diag.check_error_log(_entry(server_name="app.test"),
                                             "/no/log/dir").level, diag.LEVEL_SKIP)


class DiagOrchestrateTest(_ZhCN, unittest.TestCase):
    def _dep(self, tmp, php_running=True):
        entry = _entry(port=9000, php_version="php82", root=tmp)
        with open(os.path.join(tmp, "index.php"), "w") as f:
            f.write("<?php")
        return diag.diagnose_site(
            entry,
            config=NS(ports={"php82": 9000}),
            vhost_mgr=NS(include_exists=lambda: True, vhost_dir="/vhost"),
            php_versions=[NS(name="php82", running=php_running)],
            nginx_running=True,
            hosts_map={"app.test": "127.0.0.1"},
            logs_dir=tmp,
        )

    def test_diagnose_all_ok(self):
        tmp = tempfile.mkdtemp()
        items = self._dep(tmp, php_running=True)
        self.assertEqual(diag.summarize(items), diag.LEVEL_OK)
        # 关键检查都在
        names = {it.name for it in items}
        self.assertIn("Nginx 运行", names)
        self.assertIn("fastcgi 端口映射", names)
        self.assertIn("PHP 版本运行", names)

    def test_diagnose_php_down_is_err(self):
        tmp = tempfile.mkdtemp()
        items = self._dep(tmp, php_running=False)
        self.assertEqual(diag.summarize(items), diag.LEVEL_ERR)


if __name__ == "__main__":
    unittest.main()
