# -*- coding: utf-8 -*-
"""项目启动器：扫描去重、命令白名单、自定义命令安全边界。

不碰真实 www / vhost / 系统终端，全部用临时目录与假 config。
"""
import json
import os
import tempfile
import types
import unittest

from unittest import mock

from core.i18n import t
from core.plugins.project_launcher import (MANIFEST, commands, editor, projects,
                                          supported)


class _Cfg:
    """最小 config 替身：只实现插件设置读写需要的两个方法。"""

    def __init__(self):
        self.data = {}

    def get_setting(self, key, default=None):
        return self.data.get(key, default)

    def set_setting(self, key, value):
        self.data[key] = value


def _fake_project(**kwargs):
    base = {"name": "app", "root": os.path.join(tempfile.gettempdir(), "app")}
    base.update(kwargs)
    return projects.Project(**base)


class ManifestTest(unittest.TestCase):
    def test_manifest_declares_no_background(self):
        self.assertEqual(MANIFEST.key, "project_launcher")
        self.assertIsNone(MANIFEST.daemon_script)
        self.assertIsNone(MANIFEST.ensure_background)
        self.assertEqual(MANIFEST.panel_class, "ProjectLauncherPanel")

    def test_all_three_platforms_supported(self):
        for platform in ("win32", "darwin", "linux"):
            self.assertTrue(supported(platform))


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.www = os.path.join(self.tmp.name, "www")
        os.makedirs(self.www)

    def _dir(self, name):
        path = os.path.join(self.www, name)
        os.makedirs(path)
        return path

    def _write(self, path, data):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_dedup_www_and_vhost_root(self):
        root = self._dir("app")
        entry = types.SimpleNamespace(
            root=root, php_version="php82,php83",
            server_name="app.test *.app.test other.test")
        found = projects.scan(None, www_dir=self.www, vhost_entries=[entry])
        self.assertEqual(len(found), 1)
        project = found[0]
        self.assertEqual(project.name, "app")
        self.assertEqual(project.php, "php82")
        self.assertEqual(project.domains, ("app.test", "other.test"))
        self.assertEqual(project.sources, ("vhost", "www"))

    def test_parent_config_is_not_inherited(self):
        """只读项目根自己的 .phpvm.json，不向上继承 www 根的共享配置。"""
        self._write(os.path.join(self.www, ".phpvm.json"),
                    {"php": "php56", "domains": ["parent.test"]})
        self._dir("child")
        found = projects.scan(None, www_dir=self.www, vhost_entries=[])
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].has_project_config)
        self.assertEqual(found[0].php, "")
        self.assertEqual(found[0].domains, ())

    def test_own_config_and_markers(self):
        root = self._dir("app")
        self._write(os.path.join(root, ".phpvm.json"),
                    {"php": "php83", "domains": ["app.test"]})
        self._write(os.path.join(root, "composer.json"), {})
        found = projects.scan(None, www_dir=self.www, vhost_entries=[])
        project = found[0]
        self.assertTrue(project.has_project_config)
        self.assertEqual(project.php, "php83")
        self.assertEqual(project.domains, ("app.test",))
        self.assertIn("composer.json", project.markers)

    def test_broken_config_does_not_drop_project(self):
        root = self._dir("app")
        with open(os.path.join(root, ".phpvm.json"), "w", encoding="utf-8") as f:
            f.write('{"domains": []}')  # 缺必填 php
        found = projects.scan(None, www_dir=self.www, vhost_entries=[])
        self.assertEqual(len(found), 1)
        self.assertFalse(found[0].has_project_config)

    def test_find_by_name_is_case_insensitive(self):
        self._dir("MyApp")
        found = projects.scan(None, www_dir=self.www, vhost_entries=[])
        self.assertIsNotNone(projects.find(found, "myapp"))
        self.assertIsNone(projects.find(found, "missing"))

    def test_urls_come_from_vhost_server_name(self):
        root = self._dir("app")
        entry = types.SimpleNamespace(
            root=root, php_version="php82",
            server_name="app.test *.app.test other.test")
        found = projects.scan(None, www_dir=self.www, vhost_entries=[entry])
        self.assertEqual(found[0].urls,
                         ("http://app.test", "http://other.test"))
        self.assertEqual(found[0].as_dict()["urls"],
                         ["http://app.test", "http://other.test"])

    def test_project_config_domains_do_not_become_urls(self):
        root = self._dir("app")
        self._write(os.path.join(root, ".phpvm.json"),
                    {"php": "php83", "domains": ["from-config.test"]})
        found = projects.scan(None, www_dir=self.www, vhost_entries=[])
        self.assertEqual(found[0].domains, ("from-config.test",))
        self.assertEqual(found[0].urls, ())

    def test_https_and_disabled_vhost(self):
        root = self._dir("app")
        conf = os.path.join(self.tmp.name, "app.conf")
        with open(conf, "w", encoding="utf-8") as f:
            f.write("server {\n    listen 443 ssl;\n"
                    "    server_name app.test;\n}\n")
        entry = types.SimpleNamespace(
            root=root, server_name="app.test", php_version="",
            file=conf, disabled=False)
        found = projects.scan(None, www_dir=self.www, vhost_entries=[entry])
        self.assertEqual(found[0].urls, ("https://app.test",))
        entry.disabled = True
        found = projects.scan(None, www_dir=self.www, vhost_entries=[entry])
        self.assertEqual(found[0].urls, ())


class ValidateTest(unittest.TestCase):
    def test_plain_commands_are_allowed(self):
        for text in ("php -v", "composer install --no-dev",
                     "php -S 127.0.0.1:8000 -t public"):
            ok, err = commands.validate_command(text)
            self.assertTrue(ok, text)
            self.assertEqual(err, "")

    def test_shell_metacharacters_are_rejected(self):
        for text in ("php -v && del", "a | b", "a; b", 'foo "bar"',
                     "a\nb", "echo $HOME", "a > b", "`whoami`"):
            ok, err = commands.validate_command(text)
            self.assertFalse(ok, text)
            self.assertTrue(err)

    def test_empty_and_too_long_are_rejected(self):
        self.assertFalse(commands.validate_command("")[0])
        self.assertFalse(commands.validate_command("x" * 301)[0])


class CommandListTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _Cfg()
        self.project = _fake_project()

    def test_builtin_filtered_by_markers(self):
        project = _fake_project(markers=("composer.json", "artisan"))
        ids = [c["id"] for c in commands.for_project(self.cfg, project)]
        self.assertIn("php-v", ids)
        self.assertIn("composer-install", ids)
        self.assertIn("artisan-serve", ids)
        self.assertNotIn("npm-install", ids)
        self.assertTrue(all(c["builtin"] for c in
                            commands.for_project(self.cfg, project)))

    def test_add_list_and_remove_custom(self):
        entry, err = commands.add_custom(
            self.cfg, "My server", "php -S 127.0.0.1:8000")
        self.assertEqual(err, "")
        self.assertIsNotNone(entry)
        listed = commands.for_project(self.cfg, self.project)
        self.assertIn(entry["id"], [c["id"] for c in listed])
        self.assertFalse(next(c for c in listed
                              if c["id"] == entry["id"])["builtin"])
        self.assertTrue(commands.remove_custom(self.cfg, entry["id"]))
        self.assertFalse(commands.remove_custom(self.cfg, entry["id"]))

    def test_custom_command_is_scoped_to_one_project(self):
        other = _fake_project(name="other", root="/tmp/other")
        entry, err = commands.add_custom(
            self.cfg, "Only app", "php -v", self.project.root)
        self.assertEqual(err, "")
        self.assertIn(entry["id"], [c["id"] for c in
                                    commands.for_project(self.cfg, self.project)])
        self.assertNotIn(entry["id"], [c["id"] for c in
                                       commands.for_project(self.cfg, other)])

    def test_add_rejects_dangerous_command(self):
        entry, err = commands.add_custom(self.cfg, "bad", "php -v && del")
        self.assertIsNone(entry)
        self.assertTrue(err)
        self.assertEqual(commands.custom_commands(self.cfg), [])

    def test_builtin_cannot_be_removed(self):
        self.assertFalse(commands.remove_custom(self.cfg, "php-v"))


class LaunchTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _Cfg()
        self.project = _fake_project()  # php 为空，跳过 PhpManager 探测

    def test_dry_run_never_opens_terminal(self):
        calls = []
        result = commands.launch(
            self.cfg, self.project, "php-v", dry_run=True,
            opener=lambda *a: calls.append(a) or True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(calls, [])
        self.assertEqual(result["cwd"], self.project.root)
        self.assertEqual(result["command"], "php -v")

    def test_launch_passes_cwd_and_command(self):
        calls = []
        result = commands.launch(
            self.cfg, self.project, "php-v",
            opener=lambda *a: calls.append(a) or True)
        self.assertTrue(result["opened"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], self.project.root)
        self.assertEqual(calls[0][2], "php -v")

    def test_unknown_command_fails(self):
        result = commands.launch(self.cfg, self.project, "nope")
        self.assertFalse(result["ok"])
        self.assertIn("error", result)

    def test_opener_failure_is_reported(self):
        result = commands.launch(
            self.cfg, self.project, "php-v", opener=lambda *a: False)
        self.assertFalse(result["ok"])
        self.assertFalse(result["opened"])
        self.assertIn("error", result)


class UrlValidationTest(unittest.TestCase):
    def test_plain_http_urls_are_allowed(self):
        for url in ("http://app.test", "https://app.test", "http://localhost"):
            self.assertTrue(projects.valid_site_url(url), url)

    def test_injected_urls_are_rejected(self):
        for url in ("javascript:alert(1)", "file:///etc/passwd",
                    "ftp://app.test", "http://app.test/evil",
                    "http://app.test?q=1", "http://app.test#x",
                    "http://user:pw@app.test", "http://app.test:8080",
                    "http://*.app.test", ""):
            self.assertFalse(projects.valid_site_url(url), url)


class BrowseTest(unittest.TestCase):
    def setUp(self):
        self.project = _fake_project(
            urls=("http://app.test", "http://other.test"))

    def test_dry_run_never_opens_browser(self):
        calls = []
        result = commands.open_site(
            self.project, dry_run=True,
            opener=lambda u: calls.append(u) or True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["opened"])
        self.assertEqual(calls, [])
        self.assertEqual(result["url"], "http://app.test")

    def test_second_url_is_opened(self):
        calls = []
        result = commands.open_site(
            self.project, "http://other.test",
            opener=lambda u: calls.append(u) or True)
        self.assertTrue(result["opened"])
        self.assertEqual(calls, ["http://other.test"])

    def test_explicit_url_must_be_a_candidate(self):
        result = commands.open_site(self.project, "https://evil.test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["available"],
                         ["http://app.test", "http://other.test"])

    def test_project_without_site_fails(self):
        result = commands.open_site(_fake_project())
        self.assertFalse(result["ok"])
        self.assertEqual(result["available"], [])

    def test_unsafe_url_in_project_is_filtered(self):
        project = _fake_project(
            urls=("javascript:alert(1)", "http://ok.test"))
        self.assertEqual(projects.site_urls(project), ["http://ok.test"])
        result = commands.open_site(project, opener=lambda u: True)
        self.assertEqual(result["url"], "http://ok.test")

    def test_opener_failure_is_reported(self):
        result = commands.open_site(self.project, opener=lambda u: False)
        self.assertFalse(result["ok"])
        self.assertFalse(result["opened"])
        self.assertIn("error", result)


class WwwDirTest(unittest.TestCase):
    """扫描根目录可持久化：设置值优先，空串回落默认。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _Cfg()

    def test_default_without_setting(self):
        self.assertEqual(projects.resolve_www_dir(self.cfg),
                         projects.default_www_dir())

    def test_set_then_resolve(self):
        saved, err = projects.set_www_dir(self.cfg, self.tmp.name)
        self.assertEqual(err, "")
        self.assertEqual(saved, os.path.abspath(self.tmp.name))
        self.assertEqual(projects.resolve_www_dir(self.cfg), saved)

    def test_missing_dir_is_rejected(self):
        missing = os.path.join(self.tmp.name, "nope")
        saved, err = projects.set_www_dir(self.cfg, missing)
        self.assertEqual(saved, "")
        self.assertEqual(err, t("目录不存在：{path}", path=missing))
        self.assertEqual(projects.resolve_www_dir(self.cfg),
                         projects.default_www_dir())

    def test_empty_resets_to_default(self):
        projects.set_www_dir(self.cfg, self.tmp.name)
        saved, err = projects.set_www_dir(self.cfg, "")
        self.assertEqual(err, "")
        self.assertEqual(saved, projects.default_www_dir())
        self.assertEqual(projects.resolve_www_dir(self.cfg), saved)


class QuerySortTest(unittest.TestCase):
    def test_query_covers_path_domains_and_markers(self):
        project = projects.Project(
            name="App", root=os.path.join(os.sep, "srv", "shop"),
            php="php83", domains=("shop.test",), markers=("composer.json",))
        self.assertTrue(projects.match_query(project, ""))
        self.assertTrue(projects.match_query(project, "srv"))
        self.assertTrue(projects.match_query(project, "SHOP.TEST"))
        self.assertTrue(projects.match_query(project, "composer"))
        self.assertTrue(projects.match_query(project, "php83"))
        self.assertFalse(projects.match_query(project, "zzz"))

    def test_arrange_by_columns(self):
        a = _fake_project(name="beta", root=os.path.join(os.sep, "b"), php="php82")
        b = _fake_project(name="alpha", root=os.path.join(os.sep, "a"), php="php83")
        self.assertEqual([p.name for p in projects.arrange([a, b])],
                         ["alpha", "beta"])
        self.assertEqual([p.php for p in projects.arrange([a, b], "php")],
                         ["php82", "php83"])
        self.assertEqual([p.name for p in projects.arrange([a, b], "name", True)],
                         ["beta", "alpha"])

    def test_arrange_recent_puts_used_first(self):
        a = _fake_project(name="alpha", root=os.path.join(os.sep, "a"))
        b = _fake_project(name="beta", root=os.path.join(os.sep, "b"))
        order = projects.arrange([a, b], "recent", recent=(b.key,))
        self.assertEqual([p.name for p in order], ["beta", "alpha"])


class RecentTest(unittest.TestCase):
    def setUp(self):
        self.cfg = _Cfg()

    def test_touch_moves_to_front_without_duplicates(self):
        a = _fake_project(name="a", root=os.path.join(os.sep, "a"))
        b = _fake_project(name="b", root=os.path.join(os.sep, "b"))
        projects.touch_recent(self.cfg, a)
        projects.touch_recent(self.cfg, b)
        projects.touch_recent(self.cfg, a)
        self.assertEqual(projects.recent_keys(self.cfg), [a.key, b.key])
        self.assertEqual(projects.recent_keys(self.cfg), [a.key, b.key])

    def test_limit_is_enforced_and_clear_empties(self):
        for i in range(projects.RECENT_LIMIT + 5):
            project = _fake_project(name=f"p{i}",
                                    root=os.path.join(os.sep, f"p{i}"))
            projects.touch_recent(self.cfg, project)
        keys = projects.recent_keys(self.cfg)
        self.assertEqual(len(keys), projects.RECENT_LIMIT)
        self.assertEqual(keys[0], os.path.normcase(os.path.abspath(os.sep + "p24")))
        projects.clear_recent(self.cfg)
        self.assertEqual(projects.recent_keys(self.cfg), [])

    def test_garbage_setting_is_tolerated(self):
        self.cfg.set_setting("plugin_settings",
                             {"project_launcher": {"recent": "oops"}})
        self.assertEqual(projects.recent_keys(self.cfg), [])


class EditorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _Cfg()
        self.project = projects.Project(name="app", root=self.tmp.name)

    def test_argv_substitutes_placeholder(self):
        self.assertEqual(editor.argv_of("code {path}", "/x/y"),
                         ["code", "/x/y"])
        self.assertEqual(editor.argv_of("open -a 'VS Code' {path}", "/x y"),
                         ["open", "-a", "VS Code", "/x y"])

    def test_argv_appends_path_when_placeholder_missing(self):
        self.assertEqual(editor.argv_of("code", "/x/y"), ["code", "/x/y"])

    def test_argv_rejects_broken_quotes(self):
        self.assertEqual(editor.argv_of("code 'unclosed", "/x"), [])

    def test_dry_run_does_not_spawn(self):
        calls = []
        result = editor.open_project(self.project, self.cfg, "code {path}",
                                     dry_run=True,
                                     spawn=lambda argv, cwd: calls.append(argv))
        self.assertTrue(result["ok"])
        self.assertFalse(result["opened"])
        self.assertEqual(calls, [])
        self.assertEqual(result["argv"], ["code", self.tmp.name])

    def test_spawn_receives_argv(self):
        calls = []
        result = editor.open_project(self.project, self.cfg, "code {path}",
                                     spawn=lambda argv, cwd: calls.append((argv, cwd)))
        self.assertTrue(result["opened"])
        self.assertEqual(calls, [(["code", self.tmp.name], self.tmp.name)])

    def test_missing_root_is_reported(self):
        project = projects.Project(name="gone",
                                   root=os.path.join(self.tmp.name, "gone"))
        result = editor.open_project(project, self.cfg, "code {path}")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"],
                         t("项目目录不存在：{path}", path=project.root))

    def test_no_editor_configured_fails(self):
        with mock.patch.object(editor, "detected", return_value=[]):
            result = editor.open_project(self.project, self.cfg)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"],
                         t("未找到可用的编辑器（可在设置里填写编辑器命令）"))

    def test_set_template_validates(self):
        _, err = editor.set_template(self.cfg, "x" * (editor.MAX_TEMPLATE_LEN + 1))
        self.assertNotEqual(err, "")
        _, err = editor.set_template(self.cfg, "code\nrm -rf /")
        self.assertNotEqual(err, "")
        saved, err = editor.set_template(self.cfg, "  code {path}  ")
        self.assertEqual((saved, err), ("code {path}", ""))
        self.assertEqual(editor.template(self.cfg), "code {path}")

    def test_choices_put_custom_template_first(self):
        editor.set_template(self.cfg, "myeditor {path}")
        with mock.patch.object(editor, "detected",
                               return_value=[{"id": "code", "label": "VS Code",
                                              "exe": "code"}]):
            self.assertEqual(editor.choices(self.cfg), ["myeditor {path}", "code"])


if __name__ == "__main__":
    unittest.main()
