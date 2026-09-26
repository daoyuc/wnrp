# -*- coding: utf-8 -*-
"""F12 轻量资源监控测试：字节可读化 / ps 时间解析 / 进程采样 / 服务级聚合。

全部用假 manager + 打桩 ps 输出，不触达真实进程；语言固定 zh_CN 避免英文
locale 下断言中文文案失败（见项目 i18n 测试约定）。
"""
import types
import unittest
from unittest import mock

from core import i18n as _i18n
from core import resource_monitor as rm


_FAKE_PS = (
    "12345 1024 0.0 0:01.00 /usr/bin/nginx -p /x\n"
    "99999 2048 1.5 0:05.00 /opt/php82/php-cgi -b 127.0.0.1:9000\n"
    "77777 4096 0.2 0:02.00 redis-server *:6379\n"
)


def _fake_run_cmd(args, timeout=15):
    # 只拦 resource_monitor 的 ps 调用；其余返回空，避免副作用
    if args and args[0] == "ps":
        return 0, _FAKE_PS, ""
    return 0, "", ""


def _nginx(status=(True, [12345])):
    return types.SimpleNamespace(get_status=lambda: status)


def _php(versions):
    return types.SimpleNamespace(
        resolve=lambda **k: versions)


def _redis(instances):
    return types.SimpleNamespace(
        refresh_instances=lambda: None,
        get_status_all=lambda: None,
        instances=instances)


def _mysql(instances):
    return types.SimpleNamespace(
        refresh_instances=lambda: None,
        instances=instances)


class ResourceMonitorTest(unittest.TestCase):
    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")
        rm.reset_cpu_baseline()

    def tearDown(self):
        _i18n.set_language(self._lang)
        rm.reset_cpu_baseline()

    def test_human_bytes(self):
        self.assertEqual(rm.human_bytes(0), "0 B")
        self.assertEqual(rm.human_bytes(512), "512 B")
        self.assertEqual(rm.human_bytes(1536), "1.5 KB")
        self.assertEqual(rm.human_bytes(1536 * 1024), "1.5 MB")
        self.assertEqual(rm.human_bytes(2 * 1024 * 1024 * 1024), "2.00 GB")

    def test_parse_ps_time(self):
        self.assertAlmostEqual(rm._parse_ps_time("0:00.05"), 0.05, places=4)
        self.assertAlmostEqual(rm._parse_ps_time("1:02:03"), 3723.0, places=4)
        self.assertAlmostEqual(rm._parse_ps_time("2-03:04:05"), 183845.0, places=2)
        self.assertEqual(rm._parse_ps_time(""), 0.0)

    def test_snapshot_pids_parses_ps(self):
        with mock.patch.object(rm.pu, "run_cmd", _fake_run_cmd):
            samples = rm.snapshot_pids([12345, 99999, 77777, 1])
        self.assertEqual(samples[12345].rss_bytes, 1024 * 1024)
        self.assertEqual(samples[12345].name, "/usr/bin/nginx")
        # 首次采样无基线 → 回退 ps 的 lifetime %cpu
        self.assertAlmostEqual(samples[99999].cpu_percent, 1.5, places=4)
        self.assertAlmostEqual(samples[77777].cpu_percent, 0.2, places=4)
        # 不存在的 PID 被忽略
        self.assertNotIn(1, samples)

    def test_snapshot_pids_cpu_delta(self):
        with mock.patch.object(rm.pu, "run_cmd", _fake_run_cmd):
            first = rm.snapshot_pids([12345])
            # 累计 CPU 时间不变（ps 输出固定）→ 二次采样 delta≈0
            second = rm.snapshot_pids([12345])
        self.assertIsInstance(second[12345].cpu_percent, float)

    def test_collect_service_metrics_aggregates(self):
        nginx = _nginx((True, [12345]))
        php = _php([types.SimpleNamespace(name="php82", running=True, pid=99999, port=9000)])
        redis = _redis([types.SimpleNamespace(name="redis@6.2", running=True, pids=[77777])])
        mysql = _mysql([types.SimpleNamespace(name="mysql", running=False, pids=[])])

        with mock.patch.object(rm.pu, "run_cmd", _fake_run_cmd):
            data = rm.collect_service_metrics(None, php, nginx, redis, mysql)

        # 服务清单：nginx / php / redis / mysql 各一
        kinds = [s["kind"] for s in data["services"]]
        self.assertEqual(kinds, ["nginx", "php", "redis", "mysql"])
        # 内存合计 = (1024 + 2048 + 4096) KB
        self.assertEqual(data["totals"]["rss_bytes"], (1024 + 2048 + 4096) * 1024)
        # CPU 合计 = 0.0 + 1.5 + 0.2（mysql 未运行不计）
        self.assertAlmostEqual(data["totals"]["cpu_percent"], 1.7, places=4)
        self.assertEqual(data["totals"]["service_count"], 3)
        # 未运行服务 running=False 且 rss/cpu 为 0
        mysql_svc = next(s for s in data["services"] if s["kind"] == "mysql")
        self.assertFalse(mysql_svc["running"])
        self.assertEqual(mysql_svc["rss_bytes"], 0)

    def test_collect_service_metrics_skips_none_managers(self):
        nginx = _nginx((False, []))
        php = _php([types.SimpleNamespace(name="php82", running=False, pid=None, port=9000)])
        with mock.patch.object(rm.pu, "run_cmd", _fake_run_cmd):
            data = rm.collect_service_metrics(None, php, nginx, None, None)
        self.assertEqual(len(data["services"]), 2)
        self.assertEqual(data["totals"]["service_count"], 0)


if __name__ == "__main__":
    unittest.main()
