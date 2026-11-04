# -*- coding: utf-8 -*-
"""守护进程单实例 / 心跳语义（不启动真实进程，只验证状态文件判定）。"""
import os
import tempfile
import time
import unittest
from unittest import mock

from core.plugins.keymouse import daemon


class DaemonStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patch = mock.patch.object(daemon.app_paths, "data_dir",
                                  return_value=self._tmp.name)
        patch.start()
        self.addCleanup(patch.stop)
        daemon._cleanup_state()

    def _write(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="ascii") as f:
            f.write(text)

    def test_not_running_without_lock(self):
        self.assertFalse(daemon.is_running())
        self.assertIsNone(daemon.lock_pid())

    def test_running_requires_alive_pid_and_fresh_heartbeat(self):
        self._write(daemon.lock_path(), str(os.getpid()))
        self._write(daemon.heartbeat_path(), str(int(time.time())))
        self.assertTrue(daemon.is_running())

    def test_stale_heartbeat_means_not_running(self):
        self._write(daemon.lock_path(), str(os.getpid()))
        self._write(daemon.heartbeat_path(), str(int(time.time()) - 3600))
        self.assertFalse(daemon.is_running())

    def test_touch_heartbeat_writes_timestamp(self):
        """心跳内容必须是时间戳（写成 PID 会被当成远古时间，误判为失联）。"""
        self._write(daemon.lock_path(), str(os.getpid()))
        daemon._touch_heartbeat()
        with open(daemon.heartbeat_path(), "r", encoding="ascii") as f:
            value = int(f.read().strip())
        self.assertLess(abs(value - time.time()), 5)
        self.assertTrue(daemon.is_running())

    def test_dead_pid_is_not_running(self):
        self._write(daemon.lock_path(), "999999")
        self._write(daemon.heartbeat_path(), str(int(time.time())))
        self.assertFalse(daemon.is_running())

    def test_cleanup_state_removes_files(self):
        self._write(daemon.lock_path(), "1")
        self._write(daemon.heartbeat_path(), "1")
        self._write(daemon.stop_path(), "stop")
        daemon._cleanup_state()
        for path in (daemon.lock_path(), daemon.heartbeat_path(), daemon.stop_path()):
            self.assertFalse(os.path.exists(path), path)

    def test_stop_without_process_is_noop(self):
        ok, msg = daemon.stop()
        self.assertTrue(ok)
        self.assertIn("未在运行", msg)

    def test_tail_log_missing_file(self):
        self.assertEqual(daemon.tail_log(), "")

    def test_log_rotates_when_large(self):
        path = daemon.log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("x" * (daemon.LOG_MAX_BYTES + 10))
        daemon._log("hello")
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertIn("hello", daemon.tail_log())


if __name__ == "__main__":
    unittest.main()
