# -*- coding: utf-8 -*-
"""键鼠统计聚合回归测试：时间范围口径、日均、Top N、小时分布、打字速度。

固定「现在」为 2026-11-04（周三），避免测试随真实日期漂移。
"""
import datetime
import os
import tempfile
import unittest

from core.plugins.keymouse import stats, store

NOW_DT = datetime.datetime(2026, 11, 4, 15, 30)
NOW = NOW_DT.timestamp()


def _minute(dt: datetime.datetime) -> int:
    return int(dt.timestamp()) // 60


class StatsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "stats.db")
        writer = store.Writer(self.path)
        writer.flush(
            {
                ("2026-11-04", 32): 10,   # 空格
                ("2026-11-04", 65): 5,    # A
                ("2026-11-03", 32): 7,
                ("2026-10-20", 66): 3,
                ("2025-12-31", 67): 1,
            },
            {
                _minute(datetime.datetime(2026, 11, 4, 10, 0)): (5, 60),
                _minute(datetime.datetime(2026, 11, 4, 15, 30)): (3, 20),
                _minute(datetime.datetime(2026, 11, 3, 11, 0)): (7, 40),
            },
        )
        writer.close()

    def _summary(self, range_key, top=10):
        return stats.summary(range_key, top=top, path=self.path, now=NOW)

    # ---- 范围口径 ---------------------------------------------------- #
    def test_bounds(self):
        today = stats.bounds("today", NOW)
        self.assertEqual((today["start_day"], today["end_day"]), ("2026-11-04", "2026-11-05"))
        self.assertEqual(today["days"], 1)

        week = stats.bounds("week", NOW)
        self.assertEqual(week["start_day"], "2026-11-02")  # 本周一
        self.assertEqual(week["days"], 3)

        month = stats.bounds("month", NOW)
        self.assertEqual((month["start_day"], month["days"]), ("2026-11-01", 4))

        year = stats.bounds("year", NOW)
        self.assertEqual(year["start_day"], "2026-01-01")

        allb = stats.bounds("all", NOW, "2025-12-31")
        self.assertEqual(allb["start_day"], "2025-12-31")
        self.assertEqual(
            allb["days"],
            (datetime.date(2026, 11, 4) - datetime.date(2025, 12, 31)).days + 1)

    # ---- 聚合 -------------------------------------------------------- #
    def test_today_summary(self):
        data = self._summary("today")
        self.assertTrue(data["has_data"])
        self.assertEqual(data["total_presses"], 15)
        self.assertEqual(data["daily_avg"], 15.0)
        self.assertEqual([k["key"] for k in data["top_keys"]], ["Space", "A"])
        self.assertEqual(data["top_keys"][0]["count"], 10)
        self.assertAlmostEqual(data["top_keys"][0]["pct"], 1.0)
        self.assertEqual(data["hourly"][10], 5)
        self.assertEqual(data["hourly"][15], 3)
        self.assertEqual(data["peak_hour"], 10)
        self.assertEqual(data["active_minutes"], 2)
        self.assertEqual(data["kpm_peak"], 60)
        self.assertEqual(data["kpm_avg"], 4.0)
        self.assertEqual(data["kpm_last"], 3)  # 15:30 是当天最后一个活跃分钟

    def test_week_includes_yesterday(self):
        data = self._summary("week")
        self.assertEqual(data["total_presses"], 22)
        self.assertEqual(data["days"], 3)
        self.assertAlmostEqual(data["daily_avg"], round(22 / 3, 1))
        self.assertEqual(data["top_keys"][0]["key"], "Space")
        self.assertEqual(data["top_keys"][0]["count"], 17)
        self.assertEqual(data["hourly"][11], 7)
        self.assertTrue(data["hourly_supported"])

    def test_month_and_year_bounds(self):
        month = self._summary("month")
        self.assertEqual(month["total_presses"], 22)  # 10-20 不在本月
        year = self._summary("year")
        self.assertEqual(year["total_presses"], 25)   # 2025-12-31 不在本年
        self.assertEqual(year["top_keys"][0]["key"], "Space")

    def test_long_ranges_skip_hourly(self):
        for key in ("month", "year", "all"):
            data = self._summary(key)
            self.assertFalse(data["hourly_supported"], key)
            self.assertEqual(data["hourly"], [], key)
            self.assertEqual(data["kpm_last"], 0, key)

    def test_all_range_uses_first_day(self):
        data = self._summary("all")
        self.assertEqual(data["total_presses"], 26)
        self.assertEqual(data["first_day"], "2025-12-31")
        self.assertEqual(data["start_day"], "2025-12-31")

    def test_top_limit_clamped(self):
        data = self._summary("all", top=1)
        self.assertEqual(len(data["top_keys"]), 1)
        self.assertEqual(data["top_keys"][0]["count"], 17)

    def test_unknown_range_falls_back_to_today(self):
        self.assertEqual(self._summary("nonsense")["range"], "today")

    # ---- 空数据 / 导出 ------------------------------------------------ #
    def test_missing_db_returns_empty(self):
        data = stats.summary("today", path=os.path.join(self._tmp.name, "none.db"),
                             now=NOW)
        self.assertFalse(data["has_data"])
        self.assertEqual(data["total_presses"], 0)
        self.assertEqual(data["top_keys"], [])

    def test_export_csv(self):
        out = os.path.join(self._tmp.name, "report.csv")
        res = stats.export_csv("today", out, path=self.path, now=NOW)
        self.assertTrue(res["ok"])
        with open(out, "r", encoding="utf-8-sig") as f:
            text = f.read()
        self.assertIn("section,item,value", text)
        self.assertIn("Space,10", text)
        self.assertIn("total_presses,15", text)

    def test_report_text(self):
        text = stats.report_text(self._summary("today"))
        self.assertIn("Space", text)
        self.assertIn("15", text)


if __name__ == "__main__":
    unittest.main()
