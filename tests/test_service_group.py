# -*- coding: utf-8 -*-
"""core/service_group：PHP 启动范围（newest / used / active / all / 指定版本名）。

全部使用替身 manager，不启停真实服务；涉及 vhost 的用例在临时目录 + 假 nginx 上跑，
不读写真实 nginx 配置。
"""
import os
import tempfile
import unittest
from unittest import mock

from core.php_manager import version_key
from core.service_group import (
    PHP_SCOPE_ACTIVE,
    PHP_SCOPE_ALL,
    PHP_SCOPE_NEWEST,
    PHP_SCOPE_USED,
    ServiceGroup,
    scope_label,
)
from tests.fakes import FakeConfig, make_layout

CONF_WITH_PORT = """server {{
    listen 80;
    server_name app.test;
    root D:/www9/app/public;

    location ~ \\.php$ {{
        fastcgi_pass 127.0.0.1:{port};
    }}
}}
"""


class FakePhpVersion:
    """PhpVersion 的替身：ServiceGroup 只用 name/display/port/dir/running。"""

    def __init__(self, name, display="", port=9000, running=False, directory=""):
        self.name = name
        self.display = display
        self.port = port
        self.running = running
        self.dir = directory or name
        self.pid = None


class FakePhpManager:
    """PhpManager 的替身：支持「解析版本号 → 回落目录名」两条路径。"""

    def __init__(self, versions, config=None, resolve_to=None):
        self.versions = versions
        self.config = config or FakeConfig()
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.resolved = 0
        self._resolve_to = dict(resolve_to or {})

    def scan_versions(self):
        return self.versions

    def refresh_all_status(self, versions=None, fast=True):
        return None

    def resolve(self, refresh_status=False, fast=True):
        self.resolved += 1
        for v in self.versions:
            if v.name in self._resolve_to:
                v.display = self._resolve_to[v.name]
        return self.versions

    def start(self, v):
        v.running = True
        self.started.append(v.name)
        return f"{v.name} started"

    def stop(self, v):
        v.running = False
        self.stopped.append(v.name)
        return f"{v.name} stopped"


class FakeNginx:
    def __init__(self, running=True):
        self.running = running

    def get_status(self):
        return self.running, []

    def start(self):
        self.running = True
        return "nginx started"

    def stop(self):
        self.running = False
        return "nginx stopped"


def make_group(versions, *, nginx_running=True, config=None, resolve_to=None):
    """返回 (FakePhpManager, ServiceGroup)，nginx 已运行以免干扰断言。"""
    php = FakePhpManager(versions, config=config, resolve_to=resolve_to)
    return php, ServiceGroup(php, FakeNginx(nginx_running), None, None)


class VersionKeyTest(unittest.TestCase):
    def test_display_wins_over_dir_name(self):
        old = FakePhpVersion("php80", display="8.0.30")
        new = FakePhpVersion("php9", display="9.0.1")
        self.assertGreater(version_key(new), version_key(old))

    def test_dir_name_fallback(self):
        # 字符串序里 php8 > php85，必须按数字拆成 (8,0,0) / (8,5,0)
        self.assertGreater(version_key(FakePhpVersion("php85")),
                           version_key(FakePhpVersion("php8")))
        self.assertGreater(version_key(FakePhpVersion("php8")),
                           version_key(FakePhpVersion("php74")))
        self.assertGreater(version_key(FakePhpVersion("php74")),
                           version_key(FakePhpVersion("php56")))
        self.assertGreater(version_key(FakePhpVersion("php56")),
                           version_key(FakePhpVersion("php")))

    def test_unknown_display_falls_back(self):
        v = FakePhpVersion("php85", display="未知")
        self.assertGreater(version_key(v), version_key(FakePhpVersion("php82")))


class PhpScopeTest(unittest.TestCase):
    def setUp(self):
        self.v74 = FakePhpVersion("php74", display="7.4.33", port=9074)
        self.v82 = FakePhpVersion("php82", display="8.2.4", port=9000)
        self.v85 = FakePhpVersion("php85", display="8.5.9", port=9085)

    def test_all_scope_keeps_old_behaviour(self):
        php, group = make_group([self.v74, self.v82, self.v85])
        group.start_all()
        self.assertEqual(php.started, ["php74", "php82", "php85"])

    def test_newest_scope_starts_only_latest(self):
        php, group = make_group([self.v74, self.v82, self.v85])
        msg = group.start_all(php_scope=PHP_SCOPE_NEWEST)
        self.assertEqual(php.started, ["php85"])
        self.assertIn("跳过 PHP 版本", msg)
        self.assertIn("php82", msg)
        self.assertIn("php74", msg)

    def test_display_parsed_before_picking_newest(self):
        # 目录名 order（php82 < php85）与解析后的版本号一致时也必须走解析
        v84 = FakePhpVersion("php84", port=9084)
        v85 = FakePhpVersion("php85", port=9085)
        php, group = make_group([v85, v84], resolve_to={"php85": "8.5.9", "php84": "8.4.11"})
        group.start_all(php_scope=PHP_SCOPE_NEWEST)
        self.assertEqual(php.started, ["php85"])
        self.assertEqual(php.resolved, 1)

    def test_parse_result_overrides_dir_name(self):
        # 目录名 php9 在字符串序里最小，但真实版本 9.0.1 最新
        v9 = FakePhpVersion("php9", port=9099)
        v85 = FakePhpVersion("php85", port=9085)
        php, group = make_group([v85, v9], resolve_to={"php9": "9.0.1", "php85": "8.5.9"})
        group.start_all(php_scope=PHP_SCOPE_NEWEST)
        self.assertEqual(php.started, ["php9"])

    def test_already_parsed_display_skips_resolve(self):
        php, group = make_group([self.v82, self.v85])
        group.start_all(php_scope=PHP_SCOPE_NEWEST)
        self.assertEqual(php.resolved, 0)

    def test_names_pick_exact_versions(self):
        php, group = make_group([self.v74, self.v82, self.v85])
        group.start_all(php_scope=PHP_SCOPE_ALL, php_names=["php82", "php74"])
        self.assertEqual(php.started, ["php74", "php82"])

    def test_unknown_name_is_reported_not_fatal(self):
        php, group = make_group([self.v82])
        group.start_all(php_scope=PHP_SCOPE_ALL, php_names=["php99"])
        self.assertEqual(php.started, [])
        self.assertEqual(php.versions, [self.v82])

    def test_unknown_scope_falls_back_to_newest(self):
        php, group = make_group([self.v74, self.v82, self.v85])
        group.start_all(php_scope="whatever")
        self.assertEqual(php.started, ["php85"])

    def test_running_version_not_started_twice(self):
        self.v85.running = True
        php, group = make_group([self.v74, self.v82, self.v85])
        msg = group.start_all()
        self.assertEqual(php.started, ["php74", "php82"])
        self.assertNotIn("PHP php85：", msg)

    def test_all_running_reports_nothing_to_do(self):
        for v in (self.v74, self.v82, self.v85):
            v.running = True
        php, group = make_group([self.v74, self.v82, self.v85])
        msg = group.start_all(php_scope=PHP_SCOPE_NEWEST)
        self.assertEqual(php.started, [])
        self.assertIn("跳过 PHP 版本", msg)

    def test_stop_all_ignores_scope(self):
        for v in (self.v74, self.v82, self.v85):
            v.running = True
        php, group = make_group([self.v74, self.v82, self.v85])
        group.stop_all()
        self.assertEqual(sorted(php.stopped), ["php74", "php82", "php85"])

    def test_active_scope_falls_back_to_newest_without_path(self):
        # mock os.path.abspath 无关紧要：get_effective_php_dir 返回 None 时回落最新
        with mock.patch("core.path_manager.get_effective_php_dir", return_value=None):
            php, group = make_group([self.v74, self.v82, self.v85])
            group.start_all(php_scope=PHP_SCOPE_ACTIVE)
        self.assertEqual(php.started, ["php85"])

    def test_active_scope_follows_effective_dir(self):
        with mock.patch("core.path_manager.get_effective_php_dir",
                        return_value=self.v82.dir):
            php, group = make_group([self.v74, self.v82, self.v85])
            group.start_all(php_scope=PHP_SCOPE_ACTIVE)
        self.assertEqual(php.started, ["php82"])

    def test_scope_label_falls_back(self):
        self.assertEqual(scope_label(PHP_SCOPE_NEWEST), "仅最新版本")
        self.assertEqual(scope_label("nope"), "仅最新版本")


class UsedScopeTest(unittest.TestCase):
    """used：站点 fastcgi_pass 引用到的版本 + 最新版。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ng = make_layout(self.tmp.name)
        self.cfg = FakeConfig({"php74": 9074, "php82": 9000, "php85": 9085})
        self.v74 = FakePhpVersion("php74", display="7.4.33", port=9074)
        self.v82 = FakePhpVersion("php82", display="8.2.4", port=9000)
        self.v85 = FakePhpVersion("php85", display="8.5.9", port=9085)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_vhost(self, port: int) -> None:
        path = os.path.join(self.ng.vhost_dir, "app.test.conf")
        with open(path, "w", encoding="utf-8") as f:
            f.write(CONF_WITH_PORT.format(port=port))

    def _group(self):
        php, group = make_group([self.v74, self.v82, self.v85], config=self.cfg)
        return php, group

    def test_used_scope_starts_referenced_and_newest(self):
        self._write_vhost(9074)
        php, group = self._group()
        with mock.patch("core.vhost_manager.NginxManager", lambda *a, **k: self.ng):
            group.start_all(php_scope=PHP_SCOPE_USED)
        self.assertEqual(php.started, ["php74", "php85"])

    def test_used_scope_without_vhost_is_newest_only(self):
        php, group = self._group()
        with mock.patch("core.vhost_manager.NginxManager", lambda *a, **k: self.ng):
            group.start_all(php_scope=PHP_SCOPE_USED)
        self.assertEqual(php.started, ["php85"])

    def test_used_scope_unreadable_nginx_falls_back_to_newest(self):
        php, group = self._group()
        with mock.patch("core.vhost_manager.NginxManager",
                        side_effect=OSError("no nginx")):
            group.start_all(php_scope=PHP_SCOPE_USED)
        self.assertEqual(php.started, ["php85"])


if __name__ == "__main__":
    unittest.main()
