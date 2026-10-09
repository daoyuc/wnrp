"""core/health_monitor：Windows 崩溃事件查询（去掉 PowerShell 的 [datetime] 强制转换）。

修复背景：原查询在 PowerShell 里用 `$_.TimeCreated -ge [datetime]'…'` 做时间过滤，个别
事件会让整条管道抛错并以非零码退出，被上层误判为「查询失败」——守护每 30s 刷屏。
现改为只取近期事件、时间窗口放到 Python 侧过滤；仅数据源真正不可用（命令失败）才返回 None。
"""
import unittest
from unittest import mock

from core import health_monitor as hm_mod
from core.health_monitor import HealthMonitor


@unittest.skipUnless(hm_mod.IS_WIN, "Windows-only event query")
class WinCrashEventTest(unittest.TestCase):

    def _patch(self, out):
        return mock.patch("core.health_monitor.pu.run_cmd", return_value=(0, out, ""))

    def test_returns_list_not_none_on_success(self):
        # 即便无崩溃事件，也应返回空列表而非 None（否则守护每轮刷屏）
        with self._patch(""):
            self.assertEqual(HealthMonitor().fetch_crash_events(hours=6), [])

    def test_filters_php_cgi_and_time_window(self):
        from datetime import datetime, timedelta
        now = datetime.now()
        in_window = (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        out = "\n".join([
            in_window + "|Faulting application name php-cgi.exe, version 1",
            in_window + "|Faulting application name notepad.exe, version 1",
            "2000-01-01 00:00:00|Faulting application name php-cgi.exe, version 1",
        ])
        with self._patch(out):
            evs = HealthMonitor().fetch_crash_events(hours=6)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["app"], "php-cgi.exe")

    def test_returns_none_when_command_fails(self):
        with mock.patch("core.health_monitor.pu.run_cmd",
                        return_value=(1, "", "boom")):
            self.assertIsNone(HealthMonitor().fetch_crash_events(hours=6))


if __name__ == "__main__":
    unittest.main()
