# -*- coding: utf-8 -*-
"""open_terminal 的 Linux 分支：此前忽略 command 参数，命令永远跑 php -v。"""
import unittest
from unittest import mock

from core import process_utils as pu


class OpenTerminalLinuxTest(unittest.TestCase):
    def _run(self, **kwargs):
        with mock.patch.object(pu, "IS_WIN", False), \
                mock.patch.object(pu.sys, "platform", "linux"), \
                mock.patch.object(pu.subprocess, "Popen") as popen:
            ok = pu.open_terminal(**kwargs)
        return ok, popen

    def test_command_is_used_instead_of_hardcoded_php_v(self):
        ok, popen = self._run(cwd="/tmp/app", path_prepend="/opt/php",
                              command="composer install")
        self.assertTrue(ok)
        args = popen.call_args[0][0]
        self.assertIn("bash", args)
        script = args[-1]
        self.assertIn("composer install", script)
        self.assertNotIn("php -v", script)
        self.assertIn("exec bash", script)

    def test_paths_are_quoted(self):
        ok, popen = self._run(cwd="/tmp/my dir", path_prepend="/opt/php dir",
                              command="php -v")
        self.assertTrue(ok)
        script = popen.call_args[0][0][-1]
        self.assertIn("'/tmp/my dir'", script)
        self.assertIn("'/opt/php dir'", script)

    def test_default_command_is_php_v(self):
        ok, popen = self._run(cwd="/tmp/app")
        self.assertTrue(ok)
        self.assertIn("php -v", popen.call_args[0][0][-1])


if __name__ == "__main__":
    unittest.main()
