"""core/nginx_manager：reload 前的 pid 文件修正 + 失败兜底重启。

修复背景：nginx -s reload 依赖 pid 文件定位 master；该文件在 nginx 多次启停后可能滞后
于真实 master（指向已退出的进程），导致打开过期的 Global\ngx_reload_<旧pid> 事件失败。
本测试验证 pid 路径解析、写回真实 PID、以及 reload 失败时退化为 stop+start。
"""
import os
import tempfile
import unittest
from unittest import mock

from core.nginx_manager import NginxManager


class NginxManagerPidFileTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "logs"))
        self.conf_dir = os.path.join(self.tmp, "conf")
        os.makedirs(self.conf_dir)
        self.conf = os.path.join(self.conf_dir, "nginx.conf")

    def _mgr(self, conf_text):
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(conf_text)
        m = NginxManager()
        m.prefix = self.tmp
        m.exe = "nginx"
        m.mode = "root"
        return m

    def test_pid_path_default(self):
        m = self._mgr("worker_processes 1;\n")
        self.assertEqual(m._pid_path(),
                         os.path.normpath(os.path.join(self.tmp, "logs", "nginx.pid")))

    def test_pid_path_from_conf_directive(self):
        m = self._mgr("pid     /custom/path/nginx.pid;\n")
        self.assertEqual(m._pid_path(), os.path.normpath("/custom/path/nginx.pid"))

    def test_pid_path_relative_directive(self):
        m = self._mgr("pid logs/my.pid;\n")
        self.assertEqual(m._pid_path(),
                         os.path.normpath(os.path.join(self.tmp, "logs", "my.pid")))

    def test_sync_pid_file_writes_live_master(self):
        m = self._mgr("worker_processes 1;\n")
        m._sync_pid_file(4242)
        with open(m._pid_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "4242")

    def test_reload_writes_pid_then_falls_back_to_restart(self):
        m = self._mgr("worker_processes 1;\n")
        m.get_status = mock.Mock(return_value=(True, [9999]))  # 单进程即 master

        calls = []

        def fake_run_cmd(args, timeout=10):
            calls.append(args)
            # 第一次 reload 失败（模拟 pid 文件滞后），其余当作成功
            if len(calls) == 1:
                return (1, "", "OpenEvent ngx_reload_9999 failed")
            return (0, "", "")

        with mock.patch.object(m, "stop") as stop, \
                mock.patch.object(m, "start") as start, \
                mock.patch("core.nginx_manager.pu.run_cmd",
                           side_effect=fake_run_cmd):
            msg = m.reload()
        # 单进程 → 已把真实 master PID 写回 pid 文件
        with open(m._pid_path(), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "9999")
        # reload 失败 → 兜底 stop + start，配置得以应用
        stop.assert_called_once()
        start.assert_called_once()
        self.assertIn("重启", msg)

    def test_reload_graceful_when_ok(self):
        m = self._mgr("worker_processes 1;\n")
        m.get_status = mock.Mock(return_value=(True, [9999]))
        with mock.patch.object(m, "stop") as stop, \
                mock.patch.object(m, "start") as start, \
                mock.patch("core.nginx_manager.pu.run_cmd",
                           return_value=(0, "", "")):
            msg = m.reload()
        stop.assert_not_called()
        start.assert_not_called()
        self.assertIn("平滑重载", msg)


if __name__ == "__main__":
    unittest.main()
