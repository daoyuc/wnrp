# -*- coding: utf-8 -*-
"""core/vhost_manager：站点命名 helper、server 块定位改写、include 补全、端口同步与回滚。

全部在临时目录 + 假 nginx 上进行，不读写真实 nginx 配置。
"""
import os
import tempfile
import unittest

from core.vhost_manager import (VhostManager, nginx_path, safe_conf_base, valid_domain)
from tests.fakes import FAILED, SUCCESS, FakeConfig, make_layout, read

CONF_TWO_SITES = """server {
    listen 80;
    server_name app.test;
    root D:/www9/app/public;

    location ~ \\.php$ {
        fastcgi_pass 127.0.0.1:9000;
    }
}

server {
    listen 80;
    server_name other.test;
    root D:/www9/other/public;

    location ~ \\.php$ {
        fastcgi_pass 127.0.0.1:9080;
    }
}
"""


class SiteHelpersTest(unittest.TestCase):
    def test_valid_domain(self):
        for ok in ("app.test", "localhost", "*.dev.test", "a-b.test", "x.y.z"):
            self.assertTrue(valid_domain(ok), ok)
        for bad in ("", "app test", "a--b.test", "*", "app_test", ".test"):
            self.assertFalse(valid_domain(bad), bad)

    def test_safe_conf_base(self):
        self.assertEqual(safe_conf_base("app.test"), "app.test")
        # 通配符被过滤、首尾的 . _ - 被去掉（现有行为，CLI 与向导共用）
        self.assertEqual(safe_conf_base("*.dev.test"), "dev.test")
        self.assertEqual(safe_conf_base("///"), "site")

    def test_nginx_path(self):
        self.assertEqual(nginx_path(r"D:\www9\app"), "D:/www9/app")


class VhostManagerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name
        self.ng = make_layout(self.root)
        self.vm = VhostManager(FakeConfig({"php82": 9000, "php8": 9080}))
        self.vm.nginx = self.ng
        self.conf = os.path.join(self.ng.vhost_dir, "app.test.conf")
        with open(self.conf, "w", encoding="utf-8") as f:
            f.write(CONF_TWO_SITES)

    def tearDown(self):
        self.dir.cleanup()

    # ---------------- 站点级操作 ---------------- #
    def test_set_site_php_only_touches_target_block(self):
        res = self.vm.set_site_php(self.conf, "app.test", 9085)
        self.assertTrue(res["ok"], res)
        text = read(self.conf)
        self.assertIn("fastcgi_pass 127.0.0.1:9085;", text)
        self.assertIn("fastcgi_pass 127.0.0.1:9080;", text, "同文件其它站点不应被改")
        self.assertTrue(res["backup"] and os.path.exists(res["backup"]))

    def test_set_site_php_rolls_back_when_test_fails(self):
        self.ng.test_output = FAILED
        res = self.vm.set_site_php(self.conf, "app.test", 9085)
        self.assertFalse(res["ok"])
        self.assertEqual(read(self.conf), CONF_TWO_SITES, "校验失败应还原原文件")

    def test_set_site_php_reports_static_site(self):
        plain = os.path.join(self.ng.vhost_dir, "static.test.conf")
        with open(plain, "w", encoding="utf-8") as f:
            f.write("server {\n    listen 80;\n    server_name static.test;\n}\n")
        res = self.vm.set_site_php(plain, "static.test", 9085)
        self.assertFalse(res["ok"])
        self.assertIn("fastcgi_pass", res["message"])

    def test_set_site_enabled_rename_and_rollback(self):
        res = self.vm.set_site_enabled(self.conf, False)
        self.assertTrue(res["ok"], res)
        disabled = self.conf + VhostManager.DISABLED_SUFFIX
        self.assertTrue(os.path.exists(disabled))
        self.assertFalse(os.path.exists(self.conf))

        self.ng.test_output = FAILED
        res = self.vm.set_site_enabled(disabled, True)
        self.assertFalse(res["ok"])
        self.assertTrue(os.path.exists(disabled), "校验失败应改回禁用名")
        self.ng.test_output = SUCCESS
        res = self.vm.set_site_enabled(disabled, True)
        self.assertTrue(res["ok"], res)
        self.assertTrue(os.path.exists(self.conf))

    # ---------------- include 检测 / 补全 ---------------- #
    def test_include_status_not_covered_has_reason(self):
        """回归：include_status 曾因循环变量遮蔽 t() 而在未 include 时抛 TypeError。"""
        info = self.vm.include_status()
        self.assertFalse(info["covered"])
        self.assertTrue(info["reason"], "未 include 时必须给出可展示的原因")

    def test_ensure_include_appends_once(self):
        res = self.vm.ensure_include()
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["changed"])
        self.assertIn(nginx_path(self.ng.vhost_dir) + "/*.conf", read(self.ng.main_conf))
        again = self.vm.ensure_include()
        self.assertFalse(again["changed"], "已 include 时不应重复写入")
        self.assertTrue(self.vm.include_exists())

    # ---------------- 端口一键同步 ---------------- #
    def test_sync_port_replaces_and_keeps_backup(self):
        results = self.vm.sync_port(9000, 9085)
        self.assertEqual(len(results), 1, results)
        self.assertTrue(results[0]["ok"], results)
        self.assertEqual(results[0]["replaced"], 1)
        self.assertIn("fastcgi_pass 127.0.0.1:9085;", read(self.conf))
        self.assertIn("fastcgi_pass 127.0.0.1:9080;", read(self.conf))

    def test_sync_port_restores_on_failed_test(self):
        self.ng.test_output = FAILED
        results = self.vm.sync_port(9000, 9085)
        self.assertFalse(results[0]["ok"], results)
        self.assertEqual(read(self.conf), CONF_TWO_SITES)
        self.assertFalse(os.path.exists(self.conf + ".bak"), "回滚后应清理备份")

    def test_sync_port_reports_unchanged_port(self):
        results = self.vm.sync_port(9000, 9000)
        self.assertTrue(all(r["ok"] for r in results), results)


if __name__ == "__main__":
    unittest.main()
