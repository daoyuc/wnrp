# -*- coding: utf-8 -*-
"""F7 总览仪表盘测试：build_overview 聚合 + 告警推导 + diagnose_all（全假依赖，不触真实服务）。"""
import types
import unittest
from unittest import mock

from core import i18n as _i18n
from core import overview as ovmod


def _php(name, display, port, running):
    return types.SimpleNamespace(name=name, display=display, port=port, running=running)


def _inst(name, port, running):
    return types.SimpleNamespace(name=name, port=port, running=running)


class OverviewTest(unittest.TestCase):
    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")

    def tearDown(self):
        _i18n.set_language(self._lang)

    def _fakes(self, php_running=False, hosts_map=None):
        nginx = types.SimpleNamespace(get_status=lambda: (True, [123]))
        php = types.SimpleNamespace(
            resolve=lambda **k: [_php("php82", "8.2.4", 9000, php_running)])
        redis = types.SimpleNamespace(
            refresh_instances=lambda: None, get_status_all=lambda: None,
            instances=[_inst("redis", 6379, True)])
        mysql = types.SimpleNamespace(
            refresh_instances=lambda: None,
            instances=[_inst("mysql", 3306, False)])
        entry = types.SimpleNamespace(
            server_name="app.test", port=9000, php_version="php82",
            root="/no/such/root", file="/no/such.conf", file_rel="app.test.conf",
            note="", disabled=False)
        vhost = types.SimpleNamespace(
            scan=lambda: [entry],
            include_exists=lambda: True,
            vhost_dir="/no/such/vhost")
        return nginx, php, redis, mysql, vhost, (hosts_map if hosts_map is not None else {})

    def test_build_overview_services(self):
        nginx, php, redis, mysql, vhost, hm = self._fakes()
        ov = ovmod.build_overview(None, php, nginx, redis, mysql, vhost, hosts_map=hm)
        self.assertTrue(ov.nginx.running)
        self.assertEqual(len(ov.php), 1)
        self.assertEqual(ov.php[0].name, "php82")
        self.assertEqual(len(ov.redis), 1)
        self.assertEqual(len(ov.mysql), 1)
        self.assertEqual(ov.site_count, 1)

    def test_build_overview_alerts(self):
        # hosts 全空 + php 未运行 → 触发「未映射 hosts」「端口未映射」两类告警
        nginx, php, redis, mysql, vhost, hm = self._fakes(php_running=False, hosts_map={})
        ov = ovmod.build_overview(None, php, nginx, redis, mysql, vhost, hosts_map=hm)
        joined = " | ".join(ov.alerts)
        self.assertIn("未映射 hosts：app.test", joined)
        self.assertIn("端口未映射：9000（php82）", joined)
        self.assertEqual(ov.health, "warn")

    def test_build_overview_health_err_when_nginx_down(self):
        nginx = types.SimpleNamespace(get_status=lambda: (False, []))
        php = types.SimpleNamespace(resolve=lambda **k: [])
        vhost = types.SimpleNamespace(scan=lambda: [])
        ov = ovmod.build_overview(None, php, nginx, None, None, vhost, hosts_map={})
        self.assertEqual(ov.health, "err")

    def test_diagnose_all_counts(self):
        nginx, php, redis, mysql, vhost, hm = self._fakes(php_running=False, hosts_map={})
        err, warn, total = ovmod.diagnose_all(None, vhost, php, nginx, hosts_map=hm)
        self.assertEqual(total, 1)
        self.assertGreaterEqual(warn, 1)   # 未映射 hosts → warn
        self.assertGreaterEqual(err, 1)    # 根目录不存在 → err

    def test_to_dict_roundtrip(self):
        nginx, php, redis, mysql, vhost, hm = self._fakes()
        ov = ovmod.build_overview(None, php, nginx, redis, mysql, vhost, hosts_map=hm)
        d = ov.to_dict()
        self.assertIn("nginx", d)
        self.assertIn("alerts", d)
        self.assertIn("health", d)


if __name__ == "__main__":
    unittest.main()
