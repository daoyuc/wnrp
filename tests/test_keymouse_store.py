# -*- coding: utf-8 -*-
"""键鼠统计存储层回归测试（写入累加 / 峰值取最大 / 保留期清理 / 清空）。"""
import os
import tempfile
import unittest

from core.plugins.keymouse import store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "stats.db")
        self.writer = store.Writer(self.path)
        self.addCleanup(self.writer.close)

    def _read(self):
        conn = store.open_readonly(self.path)
        self.assertIsNotNone(conn)
        self.addCleanup(conn.close)
        return conn

    def test_flush_accumulates(self):
        self.writer.flush({("2026-11-04", 32): 2}, {1000: (2, 30)})
        self.writer.flush({("2026-11-04", 32): 3}, {1000: (3, 45)})
        conn = self._read()
        self.assertEqual(store.key_total(conn, "2026-11-04", "2026-11-05"), 5)
        totals = store.typing_totals(conn, 999, 1001)
        self.assertEqual(totals["presses"], 5)
        self.assertEqual(totals["active_minutes"], 1)
        self.assertEqual(totals["peak_kpm"], 45)  # MAX，而不是累加

    def test_top_keys_ordering(self):
        self.writer.flush({
            ("2026-11-04", 65): 3,
            ("2026-11-04", 32): 9,
            ("2026-11-04", 66): 5,
        }, {})
        conn = self._read()
        tops = store.top_keys(conn, "2026-11-04", "2026-11-05", limit=2)
        self.assertEqual(tops, [(32, 9), (66, 5)])
        self.assertEqual(store.distinct_keys(conn, "2026-11-04", "2026-11-05"), 3)

    def test_day_range_is_exclusive(self):
        self.writer.flush({("2026-11-03", 65): 1, ("2026-11-04", 65): 2}, {})
        conn = self._read()
        self.assertEqual(store.key_total(conn, "2026-11-04", "2026-11-05"), 2)
        self.assertEqual(store.first_day(conn), "2026-11-03")

    def test_cleanup_removes_old_rows(self):
        self.writer.flush({("2000-01-01", 65): 1, ("2026-11-04", 65): 2},
                          {0: (1, 1)})
        removed = self.writer.cleanup(30)
        self.assertGreaterEqual(removed, 2)
        conn = self._read()
        self.assertEqual(store.key_total(conn, "2000-01-01", "2001-01-01"), 0)
        self.assertEqual(store.first_day(conn), "2026-11-04")

    def test_cleanup_zero_keeps_everything(self):
        self.writer.flush({("2000-01-01", 65): 1}, {})
        self.assertEqual(self.writer.cleanup(0), 0)
        conn = self._read()
        self.assertEqual(store.first_day(conn), "2000-01-01")

    def test_clear(self):
        self.writer.flush({("2026-11-04", 65): 1}, {1: (1, 1)})
        self.writer.clear()
        conn = self._read()
        self.assertEqual(store.first_day(conn), "")
        self.assertEqual(store.typing_totals(conn, 0, 10)["presses"], 0)

    def test_missing_db_returns_none(self):
        self.assertIsNone(store.open_readonly(os.path.join(self._tmp.name, "nope.db")))
        self.assertEqual(store.info(None)["exists"], False)


if __name__ == "__main__":
    unittest.main()
