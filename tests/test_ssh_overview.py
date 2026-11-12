# -*- coding: utf-8 -*-
"""系统概览采集：纯解析 + 复用 exec_command（不依赖真实 paramiko 连接）。"""
import unittest
from unittest import mock

from core import ssh_overview

# 模拟远端采集脚本的输出（字段顺序与 _COLLECT_SCRIPT 对应）
SAMPLE = """
UPTIME=123456
LOAD=0.50 0.30 0.20
MEM=8000000 2000000
SWAP=2000000 1900000
DISK=/ 1000000 5000000
DISK=/data 50000000 100000000
NET=75000000000 74000000000
"""


class ParseTest(unittest.TestCase):
    def test_parse_fields(self):
        d = ssh_overview.parse_overview(SAMPLE)
        self.assertEqual(d["uptime_seconds"], 123456)
        self.assertEqual(d["load"], [0.5, 0.3, 0.2])
        self.assertEqual(d["mem_total_kb"], 8000000)
        self.assertEqual(d["mem_avail_kb"], 2000000)
        self.assertEqual(d["swap_total_kb"], 2000000)
        self.assertEqual(d["swap_free_kb"], 1900000)
        self.assertEqual(len(d["disks"]), 2)
        self.assertEqual(d["disks"][0]["mount"], "/")
        self.assertEqual(d["disks"][1]["mount"], "/data")
        self.assertEqual(d["net_send_bytes"], 75000000000)
        self.assertEqual(d["net_recv_bytes"], 74000000000)

    def test_missing_fields_get_safe_defaults(self):
        d = ssh_overview.parse_overview("garbage")
        self.assertEqual(d["uptime_seconds"], 0)
        self.assertEqual(d["load"], [0.0, 0.0, 0.0])
        self.assertEqual(d["disks"], [])

    def test_text_has_all_sections(self):
        d = ssh_overview.parse_overview(SAMPLE)
        txt = ssh_overview.overview_text(d, "12:00:00")
        for needle in ("运行时长", "负载", "内存", "/data", "交换",
                       "网络累计发送", "（采集于 12:00:00）"):
            self.assertIn(needle, txt)

    def test_scale_picks_total_magnitude(self):
        used, total, unit = ssh_overview._fmt_pair(6000000, 8000000)
        self.assertEqual(unit, "GB")
        self.assertNotEqual(used, total)
        # 小数字应落到的单位
        _u, unit2 = ssh_overview._scale(4.0)
        self.assertEqual(unit2, "KB")


class CollectTest(unittest.TestCase):
    def test_collect_attaches_parsed_data(self):
        fake = {"ok": True, "stdout": SAMPLE, "target": "u@h:22"}
        with mock.patch.object(ssh_overview.ssh_manager, "exec_command",
                               return_value=fake) as m:
            res = ssh_overview.collect_overview({"host": "h"}, trust=True,
                                                timeout=5.0)
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["uptime_seconds"], 123456)
        # trust / timeout 透传给 exec_command
        _, kw = m.call_args
        self.assertEqual(kw["trust"], True)
        self.assertEqual(kw["timeout"], 5.0)

    def test_collect_passes_through_error(self):
        fake = {"ok": False, "error": "boom", "error_kind": "network"}
        with mock.patch.object(ssh_overview.ssh_manager, "exec_command",
                               return_value=fake):
            res = ssh_overview.collect_overview({"host": "h"})
        self.assertFalse(res["ok"])
        self.assertNotIn("data", res)


if __name__ == "__main__":
    unittest.main()
