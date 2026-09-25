# -*- coding: utf-8 -*-
"""F3 日志源推导测试：纯函数 + collect 集成（注入假依赖，不触真实 nginx/PHP）。"""
import os
import tempfile
import types
import unittest
from unittest import mock

from core import i18n as _i18n
from core import log_sources as logsrc


def _entry(root, server_name="app.test", file_rel="app.conf", file="/v/app.conf"):
    return types.SimpleNamespace(root=root, server_name=server_name,
                                file_rel=file_rel, file=file)


class LogSourcesPureTest(unittest.TestCase):
    def test_app_logs_for(self):
        tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(tmp, "storage", "logs"))
        with open(os.path.join(tmp, "storage", "logs", "laravel.log"), "w") as f:
            f.write("x")
        os.makedirs(os.path.join(tmp, "runtime", "log"))
        with open(os.path.join(tmp, "runtime", "log", "error.log"), "w") as f:
            f.write("x")
        srcs = logsrc._app_logs_for(tmp)
        labels = {s.label for s in srcs}
        self.assertIn("storage/logs/laravel.log", labels)
        self.assertIn("runtime/log/error.log", labels)
        self.assertTrue(all(s.kind == "app" for s in srcs))

    def test_app_logs_for_empty(self):
        tmp = tempfile.mkdtemp()
        self.assertEqual(logsrc._app_logs_for(tmp), [])
        self.assertEqual(logsrc._app_logs_for(""), [])

    def test_php_error_log(self):
        tmp = tempfile.mkdtemp()
        ini = os.path.join(tmp, "php.ini")
        with open(ini, "w") as f:
            f.write("error_log = /abs/path/php_errors.log\n")
        self.assertEqual(logsrc._php_error_log(types.SimpleNamespace(ini=ini)),
                         "/abs/path/php_errors.log")

    def test_php_error_log_relative(self):
        tmp = tempfile.mkdtemp()
        ini = os.path.join(tmp, "php.ini")
        with open(ini, "w") as f:
            f.write('error_log = "logs/php.log"\n')
        self.assertEqual(logsrc._php_error_log(types.SimpleNamespace(ini=ini)),
                         os.path.join(tmp, "logs", "php.log"))

    def test_php_error_log_syslog_and_missing(self):
        tmp = tempfile.mkdtemp()
        ini = os.path.join(tmp, "php.ini")
        with open(ini, "w") as f:
            f.write("error_log = syslog\n")
        self.assertEqual(logsrc._php_error_log(types.SimpleNamespace(ini=ini)), "")
        self.assertEqual(logsrc._php_error_log(types.SimpleNamespace(ini="")), "")
        self.assertEqual(logsrc._php_error_log(types.SimpleNamespace(ini="/no/ini")), "")


class CollectTest(unittest.TestCase):
    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")
        self._tmp = tempfile.mkdtemp()
        # 假 Nginx：logs 目录里放一个 error.log
        ng_logs = os.path.join(self._tmp, "logs")
        os.makedirs(ng_logs)
        with open(os.path.join(ng_logs, "error.log"), "w") as f:
            f.write("2026/01/01 [error] x\n")
        self._FakeNg = types.SimpleNamespace(logs_dir=ng_logs)
        # 假站点：root 下带 Laravel 日志
        self._site_root = os.path.join(self._tmp, "www", "app")
        os.makedirs(os.path.join(self._site_root, "storage", "logs"))
        with open(os.path.join(self._site_root, "storage", "logs", "laravel.log"), "w") as f:
            f.write("log\n")
        self._vhost = types.SimpleNamespace(
            scan=lambda: [_entry(self._site_root, server_name="app.test")])

    def tearDown(self):
        _i18n.set_language(self._lang)

    def test_collect_groups(self):
        with mock.patch.object(logsrc, "NginxManager", lambda: self._FakeNg), \
             mock.patch.object(logsrc, "_php_sources", lambda cfg: []):
            groups = logsrc.collect(None, self._vhost)
        self.assertIn("Nginx", groups)
        self.assertIn("app.test", groups)
        ng_labels = {s.label for s in groups["Nginx"]}
        self.assertIn("error.log", ng_labels)
        app_labels = {s.label for s in groups["app.test"]}
        self.assertIn("storage/logs/laravel.log", app_labels)
        self.assertTrue(all(s.kind == "app" for s in groups["app.test"]))

    def test_collect_empty(self):
        fake_vhost = types.SimpleNamespace(scan=lambda: [])
        with mock.patch.object(logsrc, "NginxManager",
                               lambda: types.SimpleNamespace(logs_dir="/no/dir")), \
             mock.patch.object(logsrc, "_php_sources", lambda cfg: []):
            groups = logsrc.collect(None, fake_vhost)
        self.assertEqual(groups, {})


if __name__ == "__main__":
    unittest.main()
