# -*- coding: utf-8 -*-
"""F7 总览仪表盘测试：build_overview 聚合 + 告警推导 + diagnose_all（全假依赖，不触真实服务）。"""
import types
import unittest
from unittest import mock

from core import i18n as _i18n
from core import overview as ovmod


def _php(name, display, port, running):
    return types.SimpleNamespace(name=name, display=display, port=port, running=running)


def _inst(name, port, running, pids=None):
    return types.SimpleNamespace(name=name, port=port, running=running,
                                 pids=list(pids or []))


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

    def test_build_dashboard_probes_php_once(self):
        """总览 + 资源画像必须共用一轮 resolve，不能再双倍扫进程。"""
        calls = {"resolve": 0, "nginx": 0, "redis_refresh": 0}

        def resolve(**k):
            calls["resolve"] += 1
            return [_php("php82", "8.2.4", 9000, True)]

        nginx = types.SimpleNamespace(
            get_status=lambda: calls.__setitem__("nginx", calls["nginx"] + 1) or (True, [1]))
        php = types.SimpleNamespace(resolve=resolve)
        redis = types.SimpleNamespace(
            refresh_instances=lambda: calls.__setitem__(
                "redis_refresh", calls["redis_refresh"] + 1),
            get_status_all=lambda: None,
            instances=[_inst("redis", 6379, True)])
        mysql = types.SimpleNamespace(
            refresh_instances=lambda: None, instances=[])
        vhost = types.SimpleNamespace(scan=lambda: [])

        with mock.patch("core.resource_monitor.snapshot_pids", return_value={}):
            ov, metrics = ovmod.build_dashboard(
                None, php, nginx, redis, mysql, vhost, hosts_map={})

        self.assertEqual(calls["resolve"], 1)
        self.assertEqual(calls["nginx"], 1)
        self.assertEqual(calls["redis_refresh"], 1)
        self.assertEqual(ov.health, "ok")
        self.assertIsInstance(metrics, dict)
        self.assertIn("services", metrics)
        self.assertIn("totals", metrics)

    def test_https_cert_missing_is_noop(self):
        entry = types.SimpleNamespace(file="/definitely/missing.conf")
        with mock.patch("builtins.open", side_effect=AssertionError("不应读盘")):
            self.assertIsNone(ovmod._https_cert_missing(entry))


if __name__ == "__main__":
    unittest.main()
