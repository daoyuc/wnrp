# -*- coding: utf-8 -*-
"""PHP 错误日志插件测试：行解析、归一化、尾部读取、聚合与 CLI 入口。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

import cli
from core.plugins.php_errors import MANIFEST, aggregate

LARAVEL_LOG = """\
[2026-11-12 09:00:00] local.ERROR: Undefined variable $foo at /var/www/a/app/Http/Controller.php:42
[2026-11-12 09:05:00] local.ERROR: Undefined variable $foo at /var/www/a/app/Http/Controller.php:99
[2026-11-12 09:10:00] local.WARNING: Slow query detected
[2026-11-12 09:12:00] local.INFO: User logged in
stack trace line without bracket
"""

PHP_LOG = """\
[12-Nov-2026 09:20:00 UTC] PHP Warning:  Undefined array key "id" in /var/www/b/index.php on line 7
[12-Nov-2026 09:21:00 UTC] PHP Fatal error:  Uncaught Error: Call to undefined function in /var/www/b/index.php:12
"""


class ParseTest(unittest.TestCase):
    def test_laravel_line(self):
        row = aggregate.parse_line(
            "[2026-11-12 09:00:00] local.ERROR: Undefined variable $x")
        self.assertEqual(row["level"], "error")
        self.assertIn("Undefined variable", row["message"])

    def test_php_native_lines(self):
        warn = aggregate.parse_line(
            '[12-Nov-2026 09:20:00 UTC] PHP Warning:  Undefined array key')
        self.assertEqual(warn["level"], "warning")
        fatal = aggregate.parse_line(
            "[12-Nov-2026 09:21:00 UTC] PHP Fatal error:  Uncaught Error")
        self.assertEqual(fatal["level"], "fatal")

    def test_non_error_lines_are_skipped(self):
        self.assertIsNone(aggregate.parse_line("stack trace line"))
        self.assertIsNone(aggregate.parse_line(""))
        self.assertIsNone(aggregate.parse_line("[2026-11-12 09:00:00] EMPTY: "))

    def test_level_mapping(self):
        self.assertEqual(aggregate.level_of("CRITICAL"), "error")
        self.assertEqual(aggregate.level_of("Emergency"), "error")
        self.assertEqual(aggregate.level_of("Deprecated"), "deprecated")
        self.assertEqual(aggregate.level_of("Notice"), "notice")
        self.assertEqual(aggregate.level_of("whatever"), "other")

    def test_normalize_merges_numbers_and_paths(self):
        a = aggregate.normalize("Oops at /var/www/a/x.php on line 42")
        b = aggregate.normalize("Oops at /srv/b/y.php on line 7")
        self.assertEqual(a, b)
        self.assertIn("<path>", a)
        self.assertIn("N", a)


class ReadTailTest(unittest.TestCase):
    def test_reads_only_tail_and_drops_partial_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big.log")
            with open(path, "w", encoding="utf-8") as f:
                for i in range(2000):
                    f.write(f"line-{i:04d}-padding-padding-padding\n")
            text = aggregate.read_tail(path, max_bytes=200)
            self.assertLess(len(text), 400)
            self.assertTrue(text.startswith("line-"))
            self.assertNotIn("line-0000", text)
            self.assertTrue(text.endswith("\n"))

    def test_missing_file_is_empty(self):
        self.assertEqual(aggregate.read_tail("/no/such/file.log"), "")


class ReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.laravel = os.path.join(self._tmp.name, "laravel.log")
        self.php = os.path.join(self._tmp.name, "php_error.log")
        with open(self.laravel, "w", encoding="utf-8") as f:
            f.write(LARAVEL_LOG)
        with open(self.php, "w", encoding="utf-8") as f:
            f.write(PHP_LOG)
        self.sources = [
            {"path": self.laravel, "kind": "site", "label": "site-a",
             "size": os.path.getsize(self.laravel)},
            {"path": self.php, "kind": "php", "label": "php84",
             "size": os.path.getsize(self.php)},
        ]

    def test_counts_and_grouping(self):
        data = aggregate.report(log_sources=self.sources)
        self.assertTrue(data["ok"])
        self.assertEqual(data["files"], 2)
        self.assertEqual(data["entries"], 6)  # 2 error + 1 fatal + 2 warning + 1 info
        self.assertEqual(data["counts"]["error"], 2)
        self.assertEqual(data["counts"]["fatal"], 1)
        self.assertEqual(data["counts"]["warning"], 2)
        self.assertEqual(data["counts"]["notice"], 1)
        # 两条 Undefined variable 路径/行号不同，归一化后应合并
        top = [g for g in data["top"] if "Undefined variable" in g["message"]]
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]["count"], 2)
        self.assertEqual(top[0]["source"], "site-a")

    def test_level_filter(self):
        data = aggregate.report(log_sources=self.sources, level="fatal")
        self.assertEqual(data["entries"], 6)
        self.assertEqual([g["level"] for g in data["top"]], ["fatal"])
        self.assertTrue(all(r["level"] == "fatal" for r in data["recent"]))

    def test_path_filter_can_leave_nothing(self):
        data = aggregate.report(log_sources=self.sources, only="no-such-path")
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "no_source")

    def test_empty_sources(self):
        data = aggregate.report(log_sources=[])
        self.assertEqual(data["error"], "no_source")
        self.assertEqual(data["total"] if "total" in data else 0, 0)

    def test_missing_source_file_is_not_fatal(self):
        data = aggregate.report(log_sources=[
            {"path": os.path.join(self._tmp.name, "gone.log"), "kind": "site",
             "label": "x", "size": 0}])
        self.assertEqual(data["error"], "no_match")


class CliTest(unittest.TestCase):
    class Stub:
        DEFAULT_TOP = 10
        DEFAULT_LIMIT = 200
        ERROR_MESSAGES = dict(aggregate.ERROR_MESSAGES)
        LEVEL_MESSAGES = dict(aggregate.LEVEL_MESSAGES)

        @staticmethod
        def report(*_a, **_k):
            return {"ok": True, "error": "", "files": 1, "entries": 2,
                    "lines": 9, "counts": {"error": 2, "warning": 0},
                    "top": [{"level": "error", "count": 2, "last": "t",
                             "source": "s", "message": "m", "path": "/x.log"}],
                    "recent": [], "sources": []}

    def test_parser_accepts_options(self):
        ns = cli.build_parser().parse_args(
            ["errors", "report", "--level", "error", "--top", "3"])
        self.assertEqual(ns.level, "error")
        self.assertEqual(ns.top, 3)

    def test_report_renders_and_sets_data(self):
        ns = cli.build_parser().parse_args(["errors", "report"])
        result = cli.Result()
        with mock.patch.object(cli, "_php_errors", lambda: self.Stub):
            ns.func(ns, result)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["entries"], 2)

    def test_manifest_declared(self):
        self.assertEqual(MANIFEST.key, "php_errors")
        self.assertEqual(MANIFEST.nav_group, "diag")
        self.assertTrue(MANIFEST.platform_supported(sys.platform))


if __name__ == "__main__":
    unittest.main()
