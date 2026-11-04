# -*- coding: utf-8 -*-
"""访问日志聚合：行解析 / 聚合口径 / 报告与只读边界。"""
import os
import tempfile
import unittest

from core.plugins.access_insights import aggregate


def _line(ip="127.0.0.1", path="/index.php", status=200, method="GET",
          when="04/Nov/2026:10:00:00 +0800", ua="-", size=10, tail=""):
    return (f'{ip} - - [{when}] "{method} {path} HTTP/1.1" '
            f'{status} {size} "-" "{ua}"{tail}')


class ParseTest(unittest.TestCase):
    def test_combined_line(self):
        row = aggregate.parse_line(_line())
        self.assertIsNotNone(row)
        self.assertEqual(row["ip"], "127.0.0.1")
        self.assertEqual(row["method"], "GET")
        self.assertEqual(row["route"], "/index.php")
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["bytes"], 10)
        self.assertEqual(row["time"][3], 10)

    def test_route_drops_query_and_fragment(self):
        row = aggregate.parse_line(_line(path="/api/list?id=1#x"))
        self.assertEqual(row["path"], "/api/list?id=1#x")
        self.assertEqual(row["route"], "/api/list")

    def test_request_time_tail_is_parsed(self):
        row = aggregate.parse_line(_line(tail=" 0.123"))
        self.assertEqual(row["seconds"], 0.123)

    def test_garbage_lines_return_none(self):
        for text in ("", "garbage", "- - -",
                     '127.0.0.1 - - [not a date] "GET / HTTP/1.1" 200 1'):
            self.assertIsNone(aggregate.parse_line(text), text)

    def test_unknown_method_becomes_other(self):
        row = aggregate.parse_line(_line(method="\\X111A0\\X99\\X1AN"))
        self.assertEqual(row["method"], "OTHER")

    def test_dash_user_agent_is_empty(self):
        row = aggregate.parse_line(_line())
        self.assertEqual(row["ua"], "")

    def test_time_without_zone_still_parses(self):
        row = aggregate.parse_line(_line(when="04/Nov/2026:23:05:00"))
        self.assertIsNotNone(row["time"])
        self.assertEqual(row["time"][3], 23)


class AggregateTest(unittest.TestCase):
    def _rows(self):
        return [
            aggregate.parse_line(_line(path="/a", status=200)),
            aggregate.parse_line(_line(path="/a", status=200)),
            aggregate.parse_line(_line(path="/a?x=1", status=500)),
            aggregate.parse_line(_line(path="/b", status=404, ip="10.0.0.2")),
            aggregate.parse_line(_line(path="/b", status=500, ip="10.0.0.2",
                                       when="04/Nov/2026:23:30:00",
                                       tail=" 1.5")),
        ]

    def test_totals_and_status_classes(self):
        data = aggregate.aggregate(self._rows())
        self.assertEqual(data["requests"], 5)
        self.assertEqual(data["unique_ips"], 2)
        self.assertEqual(data["status_classes"]["2xx"], 2)
        self.assertEqual(data["status_classes"]["4xx"], 1)
        self.assertEqual(data["status_classes"]["5xx"], 2)
        self.assertEqual(data["error_rate"], 40.0)

    def test_top_paths_are_grouped_by_route(self):
        data = aggregate.aggregate(self._rows())
        self.assertEqual(data["top_paths"][0],
                         {"path": "/a", "count": 3, "pct": 60.0})

    def test_hourly_buckets(self):
        data = aggregate.aggregate(self._rows())
        hours = {it["hour"]: it["count"] for it in data["hourly"]}
        self.assertEqual(len(hours), 24)
        self.assertEqual(hours[10], 4)
        self.assertEqual(hours[23], 1)

    def test_errors_are_newest_first_and_limited(self):
        data = aggregate.aggregate(self._rows(), error_limit=2)
        self.assertEqual(len(data["errors"]), 2)
        self.assertEqual(data["errors"][0]["status"], 500)
        self.assertEqual(data["errors"][0]["time"], "2026-11-04 23:30:00")

    def test_slowest_and_average(self):
        data = aggregate.aggregate(self._rows())
        self.assertEqual(data["slowest"][0]["seconds"], 1.5)
        self.assertEqual(data["avg_seconds"], 1.5)

    def test_empty_input(self):
        data = aggregate.aggregate([])
        self.assertEqual(data["requests"], 0)
        self.assertEqual(data["error_rate"], 0.0)
        self.assertIsNone(data["avg_seconds"])
        self.assertEqual(data["errors"], [])
        self.assertEqual(len(data["hourly"]), 24)


class ReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name, lines):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def test_report_reads_default_file(self):
        self._write("access.log", [_line(), _line(path="/x", status=404)])
        data = aggregate.report(dir_path=self.dir)
        self.assertTrue(data["ok"])
        self.assertEqual(data["file"], "access.log")
        self.assertEqual(data["read"], 2)
        self.assertEqual(data["parsed"], 2)
        self.assertEqual(data["skipped"], 0)
        self.assertEqual(data["requests"], 2)
        self.assertEqual(data["available"], ["access.log"])

    def test_garbage_lines_are_counted_as_skipped(self):
        self._write("access.log", [_line(), "broken-line", _line()])
        data = aggregate.report(dir_path=self.dir)
        self.assertEqual(data["read"], 3)
        self.assertEqual(data["parsed"], 2)
        self.assertEqual(data["skipped"], 1)

    def test_lines_limit_keeps_the_tail(self):
        self._write("access.log", [_line(path="/a"), _line(path="/b"),
                                   _line(path="/c")])
        data = aggregate.report(dir_path=self.dir, lines=2)
        self.assertEqual(data["parsed"], 2)
        self.assertEqual(data["top_paths"][0]["path"], "/b")

    def test_named_file_and_rotation(self):
        self._write("access.log", [_line()])
        self._write("access.log.1", [_line(path="/old")])
        self._write("error.log", ["boom"])
        names = [it["name"] for it in aggregate.access_logs(self.dir)]
        self.assertEqual(names, ["access.log", "access.log.1"])
        data = aggregate.report("access.log.1", dir_path=self.dir)
        self.assertEqual(data["top_paths"][0]["path"], "/old")

    def test_unknown_file_lists_candidates(self):
        self._write("access.log", [_line()])
        data = aggregate.report("nope.log", dir_path=self.dir)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "not_found")
        self.assertEqual(data["available"], ["access.log"])

    def test_missing_directory_is_not_an_error(self):
        data = aggregate.report(dir_path=os.path.join(self.dir, "absent"))
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "no_access_log")
        self.assertEqual(data["available"], [])

    def test_read_lines_of_missing_file_is_empty(self):
        self.assertEqual(
            aggregate.read_lines(os.path.join(self.dir, "nope.log")), [])

    def test_access_logs_ignores_non_access_files(self):
        self._write("error.log", ["boom"])
        self._write("notes.txt", ["hi"])
        self.assertEqual(aggregate.access_logs(self.dir), [])


if __name__ == "__main__":
    unittest.main()
