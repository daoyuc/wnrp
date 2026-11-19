# -*- coding: utf-8 -*-
"""端口占用插件测试：文本解析、合并口径、真实本机采集与 CLI 入口。

解析用例全部用固定文本喂入，不依赖本机进程；真实采集只在本机临时监听一个
端口后断言它出现在结果里，不依赖任何外部服务。
"""
import os
import socket
import sys
import unittest

import cli
from core.plugins.port_scan import MANIFEST, scan

NETSTAT_SAMPLE = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:80             0.0.0.0:0              LISTENING       4321
  TCP    0.0.0.0:443            0.0.0.0:0              LISTENING       4321
  TCP    [::]:80                [::]:0                 LISTENING       4321
  TCP    127.0.0.1:9081         0.0.0.0:0              LISTENING       5678
  TCP    127.0.0.1:6379         127.0.0.1:50000        ESTABLISHED     9999
"""

LSOF_SAMPLE = """p4321
cnginx
n*:80
n*:443
p5678
cphp-cgi
n127.0.0.1:9081
"""

TASKLIST_SAMPLE = ('"nginx.exe","4321","Console","1","20,000 K"\r\n'
                   '"php-cgi.exe","5678","Console","1","15,000 K"\r\n'
                   '"not a row"\r\n')


class ParseTest(unittest.TestCase):
    def test_netstat_keeps_only_listening_rows(self):
        rows = scan.parse_netstat(NETSTAT_SAMPLE)
        ports = {r["port"] for r in rows}
        self.assertEqual(ports, {80, 443, 9081})
        self.assertNotIn(6379, ports)  # ESTABLISHED 不是监听
        self.assertEqual(rows[0]["pid"], 4321)

    def test_lsof_groups_by_pid_and_command(self):
        rows = scan.parse_lsof(LSOF_SAMPLE)
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["pid"] for r in rows}, {4321, 5678})
        self.assertEqual({r["process"] for r in rows}, {"nginx", "php-cgi"})
        self.assertIn(443, {r["port"] for r in rows})

    def test_tasklist_csv(self):
        names = scan.parse_tasklist(TASKLIST_SAMPLE)
        self.assertEqual(names, {4321: "nginx.exe", 5678: "php-cgi.exe"})

    def test_split_addr_handles_ipv6_and_bad_input(self):
        self.assertEqual(scan.split_addr("[::1]:9081"), ("[::1]", 9081))
        self.assertEqual(scan.split_addr("*:80"), ("*", 80))
        self.assertEqual(scan.split_addr("nonsense"), ("nonsense", None))
        self.assertEqual(scan.split_addr("host:99999"), ("host:99999", None))

    def test_process_kind_does_not_match_editors(self):
        self.assertEqual(scan.process_kind("nginx.exe"), "nginx")
        self.assertEqual(scan.process_kind("php-cgi.exe"), "php")
        self.assertEqual(scan.process_kind("phpstorm64.exe"), "other")
        self.assertEqual(scan.process_kind("redis-server"), "redis")
        self.assertEqual(scan.process_kind("mysqld.exe"), "mysql")
        self.assertEqual(scan.process_kind(""), "other")


class MergeTest(unittest.TestCase):
    def test_merge_dedupes_addresses_and_fills_names(self):
        rows = scan.merge_rows(scan.parse_netstat(NETSTAT_SAMPLE),
                               {4321: "nginx.exe"})
        port80 = [r for r in rows if r["port"] == 80]
        self.assertEqual(len(port80), 1)
        self.assertEqual(port80[0]["addr"], "0.0.0.0, [::]")
        self.assertEqual(port80[0]["kind"], "nginx")
        # 9081 没给名字时保留空进程名，归属落 other
        port9081 = [r for r in rows if r["port"] == 9081][0]
        self.assertEqual(port9081["process"], "")
        self.assertEqual(port9081["kind"], "other")

    def test_merge_ignores_invalid_rows(self):
        self.assertEqual(scan.merge_rows([{"port": 0}, {"port": "x"}]), [])


class ReportTest(unittest.TestCase):
    ROWS = [
        {"port": 80, "pid": 1, "process": "nginx.exe", "addr": "0.0.0.0",
         "kind": "nginx"},
        {"port": 9081, "pid": 2, "process": "php-cgi.exe",
         "addr": "127.0.0.1", "kind": "php"},
        {"port": 3306, "pid": 3, "process": "mysqld.exe",
         "addr": "0.0.0.0", "kind": "mysql"},
        {"port": 8080, "pid": 4, "process": "node.exe",
         "addr": "127.0.0.1", "kind": "other"},
    ]

    def test_summary_counts_and_conflicts(self):
        rows = list(self.ROWS) + [
            {"port": 80, "pid": 9, "process": "other.exe", "addr": "127.0.0.1",
             "kind": "other"}]
        data = scan.report(rows=rows)
        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 5)
        self.assertEqual(data["services"], 3)
        self.assertEqual(data["other"], 2)
        self.assertEqual(data["conflicts"], [80])
        self.assertEqual(data["counts"]["php"], 1)

    def test_same_process_multi_worker_is_not_conflict(self):
        # nginx / php-cgi 多 worker 共监听同一端口是正常形态
        rows = [
            {"port": 80, "pid": 10, "process": "nginx", "addr": "*",
             "kind": "nginx"},
            {"port": 80, "pid": 11, "process": "nginx", "addr": "*",
             "kind": "nginx"},
        ]
        self.assertEqual(scan.report(rows=rows)["conflicts"], [])

    def test_filter_by_kind_and_needle(self):
        data = scan.report(rows=self.ROWS, kind="php")
        self.assertEqual([it["port"] for it in data["items"]], [9081])
        self.assertEqual(data["conflicts"], [])

        data = scan.report(rows=self.ROWS, only="node")
        self.assertEqual([it["port"] for it in data["items"]], [8080])

        data = scan.report(rows=self.ROWS, only="3306")
        self.assertEqual([it["port"] for it in data["items"]], [3306])

    def test_limit(self):
        data = scan.report(rows=self.ROWS, limit=2)
        self.assertEqual([it["port"] for it in data["items"]], [80, 9081])

    def test_error_is_reported(self):
        data = scan.report(rows=[], )
        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 0)


class RealScanTest(unittest.TestCase):
    def test_local_listener_shows_up(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            rows, error = scan.listening_ports()
            if error:
                self.skipTest(f"本机缺少查询工具：{error}")
            self.assertIn(port, {r["port"] for r in rows})
            mine = [r for r in rows if r["port"] == port]
            self.assertIn(os.getpid(), {r["pid"] for r in mine})
        finally:
            srv.close()

    def test_report_never_raises(self):
        data = scan.report()
        self.assertIn("items", data)
        self.assertIsInstance(data["total"], int)


class CliTest(unittest.TestCase):
    def run_cli(self, argv):
        ns = cli.build_parser().parse_args(argv)
        result = cli.Result()
        ns.func(ns, result)
        return result

    def test_ports_list_json_payload(self):
        ns = cli.build_parser().parse_args(["ports", "list", "--limit", "1"])
        result = cli.Result()
        ns.func(ns, result)
        self.assertIn("items", result.data)
        self.assertLessEqual(result.data["total"], 1)

    def test_filter_without_match_is_not_an_error(self):
        result = self.run_cli(["ports", "list", "--only",
                               "no-such-process-xyz"])
        self.assertTrue(result.ok)
        self.assertEqual(result.data["total"], 0)

    def test_manifest_declared(self):
        self.assertEqual(MANIFEST.key, "port_scan")
        self.assertEqual(MANIFEST.nav_group, "diag")
        self.assertTrue(MANIFEST.platform_supported(sys.platform))


if __name__ == "__main__":
    unittest.main()
