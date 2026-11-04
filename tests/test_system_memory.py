# -*- coding: utf-8 -*-
"""整机内存采集：解析逻辑纯函数化，跨平台可单测（不依赖真实机器数值）。"""
import unittest

from core import system_memory as mod


class MeminfoTest(unittest.TestCase):
    def test_typical_linux_meminfo(self):
        text = ("MemTotal:       16384000 kB\n"
                "MemFree:         2048000 kB\n"
                "MemAvailable:    8192000 kB\n"
                "Buffers:          512000 kB\n"
                "Cached:          4096000 kB\n")
        snap = mod.from_meminfo(text)
        self.assertTrue(snap.ok)
        self.assertEqual(snap.total_bytes, 16384000 * 1024)
        self.assertEqual(snap.available_bytes, 8192000 * 1024)
        self.assertEqual(snap.percent, 50.0)
        self.assertEqual(snap.source, "proc_meminfo")

    def test_fallback_without_memavailable(self):
        text = ("MemTotal:       8000000 kB\n"
                "MemFree:        1000000 kB\n"
                "Buffers:         500000 kB\n"
                "Cached:         1500000 kB\n")
        snap = mod.from_meminfo(text)
        self.assertTrue(snap.ok)
        self.assertEqual(snap.available_bytes, 3000000 * 1024)
        self.assertEqual(snap.percent, 62.5)

    def test_missing_total_is_unavailable(self):
        self.assertFalse(mod.from_meminfo("MemFree: 100 kB\n").ok)

    def test_garbage_lines_are_ignored(self):
        text = ("MemTotal:       bad\n"
                "MemTotal:       1000 kB\n"
                "MemAvailable:   250 kB\n")
        self.assertEqual(mod.from_meminfo(text).percent, 75.0)


class VmStatTest(unittest.TestCase):
    def test_free_inactive_speculative_count_as_available(self):
        total = 16 * mod._GIB
        page = 4096
        text = ("Mach Virtual Memory Statistics: (page size of 4096 bytes)\n"
                "Pages free:                         1000000.\n"
                "Pages inactive:                     500000.\n"
                "Pages speculative:                  100000.\n"
                "Pages wired down:                   800000.\n")
        snap = mod.from_vm_stat(text, total, page)
        self.assertTrue(snap.ok)
        self.assertEqual(snap.available_bytes, 1600000 * page)
        self.assertLess(snap.percent, 100)
        self.assertEqual(snap.source, "vm_stat")

    def test_zero_total_is_unavailable(self):
        self.assertFalse(mod.from_vm_stat("Pages free: 10.\n", 0, 4096).ok)


class WinStatusTest(unittest.TestCase):
    def test_from_win_status(self):
        snap = mod.from_win_status(16 * mod._GIB, 4 * mod._GIB)
        self.assertTrue(snap.ok)
        self.assertEqual(snap.percent, 75.0)
        self.assertEqual(snap.source, "win32")

    def test_clamps_available_above_total(self):
        snap = mod.from_win_status(1000, 5000)
        self.assertEqual(snap.percent, 0.0)
        self.assertEqual(snap.available_bytes, 1000)


class FormatTest(unittest.TestCase):
    def test_format_gb(self):
        self.assertEqual(mod.format_gb(2 * mod._GIB), "2.0 GB")
        self.assertEqual(mod.format_gb(0), "—")
        self.assertEqual(mod.format_gb(-1), "—")


class SnapshotTest(unittest.TestCase):
    def test_snapshot_is_sane_on_current_platform(self):
        snap = mod.snapshot()
        self.assertIsInstance(snap.total_bytes, int)
        self.assertGreaterEqual(snap.available_bytes, 0)
        if snap.ok:
            self.assertGreaterEqual(snap.percent, 0)
            self.assertLessEqual(snap.percent, 100)
            self.assertLessEqual(snap.used_bytes, snap.total_bytes)
            self.assertLessEqual(snap.available_bytes, snap.total_bytes)


if __name__ == "__main__":
    unittest.main()
