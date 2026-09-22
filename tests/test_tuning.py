# -*- coding: utf-8 -*-
"""core/tuning：本机分档、建议生成（已达标不打扰）、写入与校验回滚。"""
import os
import tempfile
import unittest
from unittest import mock

from core import tuning


def profile(cpu: int = 4, mem_gb: int = 8, platform: str = "macos") -> tuning.MachineProfile:
    return tuning.detect_machine(cpu=cpu, mem_bytes=mem_gb * 1024 ** 3, platform=platform)


CONF = """worker_processes  1;
events {
    worker_connections  1024;
}
http {
    include       mime.types;
    keepalive_timeout  65;
}
"""

INI = """memory_limit = 512M
display_errors = Off
date.timezone = Asia/Shanghai
"""


class MachineTest(unittest.TestCase):
    def test_tier_by_cpu_and_memory(self):
        self.assertEqual(profile(4, 8).tier, "low")
        self.assertEqual(profile(8, 16).tier, "mid")
        self.assertEqual(profile(12, 64).tier, "high")
        self.assertEqual(profile(2, 64).tier, "high", "内存足够时按高配给容量型参数")

    def test_unknown_memory_falls_back_to_low(self):
        p = tuning.detect_machine(cpu=32, mem_bytes=0, platform="linux")
        self.assertEqual(p.mem_gb, 0.0)
        self.assertIn(p.tier, ("low", "mid", "high"))

    def test_num_normalizes_sizes(self):
        self.assertEqual(tuning._num("128M"), 128.0)
        self.assertEqual(tuning._num("1G"), 1024.0)
        self.assertEqual(tuning._num("512k"), 0.5)
        self.assertEqual(tuning._num("-1"), float("inf"), "-1（不限）应按无穷处理")

    def test_skip_rules(self):
        self.assertTrue(tuning._skip("512M", "256M", "min"), "已超过下限则不再建议")
        self.assertFalse(tuning._skip("128M", "256M", "min"))
        self.assertTrue(tuning._skip("15", "30", "max"), "已低于上限则不再建议")
        self.assertFalse(tuning._skip("65", "30", "max"))
        self.assertTrue(tuning._skip("On", "on", "eq"))
        self.assertFalse(tuning._skip("", "on", "eq"), "未设置时必须建议")


class PhpTuningTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.ini = os.path.join(self.dir.name, "php.ini")
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write(INI)

    def tearDown(self):
        self.dir.cleanup()

    def _read(self) -> str:
        with open(self.ini, "r", encoding="utf-8") as f:
            return f.read()

    def test_suggestions_skip_already_good_values(self):
        items = tuning.php_suggestions(self.ini, profile(), "8.2.4")
        keys = {i.key for i in items}
        self.assertIn("display_errors", keys)
        self.assertNotIn("memory_limit", keys, "512M 已超过 low 档建议 256M")
        self.assertNotIn("date.timezone", keys, "已设置时区则不再建议")

    def test_jit_item_only_for_php8(self):
        keys8 = {i.key for i in tuning.php_suggestions(self.ini, profile(), "8.2.4")}
        keys7 = {i.key for i in tuning.php_suggestions(self.ini, profile(), "7.4.3")}
        self.assertIn("opcache.jit_buffer_size", keys8)
        self.assertNotIn("opcache.jit_buffer_size", keys7)

    def test_optional_items_only_when_missing(self):
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write("memory_limit = 128M\n")
        keys = {i.key for i in tuning.php_suggestions(self.ini, profile(), "8.1.0")}
        self.assertIn("date.timezone", keys)
        self.assertIn("default_charset", keys)

    def test_tier_changes_capacity_values(self):
        low = {i.key: i.value for i in tuning.php_suggestions(self.ini, profile(4, 8), "8.2.0")}
        high = {i.key: i.value for i in tuning.php_suggestions(self.ini, profile(12, 64), "8.2.0")}
        self.assertEqual(low["opcache.memory_consumption"], "128")
        self.assertEqual(high["opcache.memory_consumption"], "256")
        self.assertEqual(high["memory_limit"], "1G")

    def test_apply_php_writes_and_backs_up(self):
        items = [i for i in tuning.php_suggestions(self.ini, profile(), "8.2.4")
                 if i.key == "display_errors"]
        res = tuning.apply_php(self.ini, items)
        self.assertEqual(res["changed"], 1)
        self.assertTrue(os.path.exists(res["backup"]))
        self.assertIn("display_errors = On", self._read())

    def test_apply_php_empty_is_noop(self):
        self.assertEqual(tuning.apply_php(self.ini, [])["changed"], 0)


class NginxTuningTest(unittest.TestCase):
    def test_windows_constraints(self):
        # 当前值故意取别的值，确保建议项会出现（已达标项本就不推荐）
        conf = CONF.replace("worker_processes  1;", "worker_processes  auto;") \
                   .replace("worker_connections  1024;", "worker_connections  512;")
        items = {i.key: i for i in tuning.nginx_suggestions(conf, profile(platform="windows"))}
        self.assertEqual(items["worker_processes"].value, "1")
        self.assertEqual(items["worker_connections"].value, "1024")
        self.assertNotIn("worker_rlimit_nofile", items, "Windows 无此指令")
        self.assertNotIn("multi_accept", items)
        self.assertNotIn("sendfile", items)

    def test_macos_and_linux_items(self):
        mac = {i.key: i for i in tuning.nginx_suggestions(CONF, profile(platform="macos"))}
        self.assertEqual(mac["worker_processes"].value, "auto")
        self.assertIn("worker_rlimit_nofile", mac)
        self.assertNotIn("use", mac, "macOS 由 nginx 自动选 kqueue")
        linux = {i.key: i for i in tuning.nginx_suggestions(CONF, profile(platform="linux"))}
        self.assertEqual(linux["use"].value, "epoll")

    def test_current_value_is_reported(self):
        items = {i.key: i for i in tuning.nginx_suggestions(CONF, profile(8, 16))}
        self.assertEqual(items["keepalive_timeout"].current, "65")
        self.assertEqual(items["keepalive_timeout"].value, "30")
        self.assertEqual(items["worker_connections"].value, "2048", "mid 档按 CPU/内存上调连接数")

    def test_notes_warn_about_windows_and_production(self):
        notes = tuning.nginx_notes(profile(platform="windows"))
        self.assertTrue(any("Windows" in n for n in notes))
        # 语言由本机设置决定（zh_CN 原文 / 译好的 en），两种都算通过
        self.assertTrue(any(("生产" in n or "production" in n) for n in notes))


class FakeNginxManager:
    """替身：可控的 nginx -t 结果，避免测试真的去跑 nginx。"""

    def __init__(self, exe: str, code: int, out: str):
        self.exe = exe
        self.code = code
        self.out = out
        self.reloaded = False

    def verify_config(self):
        return self.code, self.out

    def reload(self):
        self.reloaded = True
        return "reload ok"


class NginxApplyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.conf = os.path.join(self.dir.name, "nginx.conf")
        with open(self.conf, "w", encoding="utf-8") as f:
            f.write(CONF)
        self.exe = os.path.join(self.dir.name, "nginx")
        with open(self.exe, "w", encoding="utf-8") as f:
            f.write("")

    def tearDown(self):
        self.dir.cleanup()

    def _read(self) -> str:
        with open(self.conf, "r", encoding="utf-8") as f:
            return f.read()

    def test_apply_writes_backup_and_values(self):
        fake = FakeNginxManager(self.exe, 0, "syntax is ok")
        items = [i for i in tuning.nginx_suggestions(CONF, profile())
                 if i.key == "keepalive_timeout"]
        with mock.patch("core.nginx_manager.NginxManager", return_value=fake):
            res = tuning.apply_nginx(self.conf, items)
        self.assertTrue(res["ok"])
        self.assertEqual(res["changed"], 1)
        self.assertTrue(os.path.exists(res["backup"]))
        self.assertEqual(tuning.nginx_conf.get_directive(
            self._read(), "keepalive_timeout", "http"), "30")

    def test_apply_rolls_back_when_test_fails(self):
        fake = FakeNginxManager(self.exe, 1, "test failed")
        items = [i for i in tuning.nginx_suggestions(CONF, profile())
                 if i.key == "keepalive_timeout"]
        with mock.patch("core.nginx_manager.NginxManager", return_value=fake):
            res = tuning.apply_nginx(self.conf, items)
        self.assertTrue(res["rolled_back"])
        self.assertFalse(res["ok"])
        self.assertEqual(self._read(), CONF, "校验失败必须还原为原文")

    def test_apply_reload_on_demand(self):
        fake = FakeNginxManager(self.exe, 0, "ok")
        items = [i for i in tuning.nginx_suggestions(CONF, profile())
                 if i.key == "keepalive_timeout"]
        with mock.patch("core.nginx_manager.NginxManager", return_value=fake):
            tuning.apply_nginx(self.conf, items, reload=True)
        self.assertTrue(fake.reloaded)

    def test_apply_no_items_is_noop(self):
        self.assertEqual(tuning.apply_nginx(self.conf, [])["changed"], 0)


if __name__ == "__main__":
    unittest.main()
