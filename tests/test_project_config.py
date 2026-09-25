# -*- coding: utf-8 -*-
"""F8 项目级配置：core.project_config 的查找 / 校验 / 对齐（切 PHP / hosts / 启服务）。"""
import json
import os
import tempfile
import types
import unittest
from unittest import mock

from core import project_config


class FakeConfig:
    def __init__(self, ports=None):
        self.ports = dict(ports or {})


class FakeVhost:
    def __init__(self, entries):
        self._entries = entries
        self.calls = []

    def scan(self, include_disabled=False):
        return self._entries

    def set_site_php(self, path, server_name, new_port):
        self.calls.append((path, server_name, new_port))
        return {"ok": True, "message": "switched", "path": path}


class FakePhp:
    def __init__(self, versions):
        self._versions = versions
        self.started = []

    def resolve(self, refresh_status=True, fast=True):
        return self._versions

    def start(self, v):
        v.running = True
        self.started.append(v.name)
        return f"{v.name} started"


class FakeRedis:
    def __init__(self, instances):
        self.instances = instances
        self.started = []

    def refresh_instances(self):
        return self.instances

    def start(self, inst):
        inst.running = True
        self.started.append(inst.name)
        return "redis started"


def _entry(name="app.test", port=9001, path="/tmp/app.conf"):
    return types.SimpleNamespace(file=path, server_name=name, port=port)


def _ver(name="php82", port=9000, running=False):
    return types.SimpleNamespace(name=name, port=port, running=running)


class FindLoadTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def test_find_walks_up(self):
        root = os.path.join(self.dir.name, "proj")
        deep = os.path.join(root, "a", "b")
        os.makedirs(deep)
        path = os.path.join(root, project_config.PROJECT_FILE)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        self.assertEqual(project_config.find_project_config(deep), path)
        self.assertIsNone(project_config.find_project_config(self.dir.name))

    def test_load_normalizes_and_validates(self):
        p = os.path.join(self.dir.name, project_config.PROJECT_FILE)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"php": "php82", "domains": "app.test",
                       "services": "redis", "hosts": 1}, f)
        proj = project_config.load_project(p)
        self.assertEqual(proj["domains"], ["app.test"])
        self.assertEqual(proj["services"], ["redis"])
        self.assertTrue(proj["hosts"])

    def test_load_requires_php(self):
        p = os.path.join(self.dir.name, project_config.PROJECT_FILE)
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"domains": ["a.test"]}')
        with self.assertRaises(project_config.ProjectConfigError):
            project_config.load_project(p)

    def test_load_rejects_bad_json(self):
        p = os.path.join(self.dir.name, project_config.PROJECT_FILE)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(project_config.ProjectConfigError):
            project_config.load_project(p)


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeConfig({"php82": 9000, "php74": 9074})

    def _project(self, **over):
        base = {"php": "php82", "domains": ["app.test"], "services": [],
                "hosts": False, "start": False, "env": {}}
        base.update(over)
        return base

    def test_switches_matching_site(self):
        vm = FakeVhost([_entry(port=9001)])
        res = project_config.apply_project(self._project(), self.cfg, vm)
        self.assertTrue(res["ok"], res)
        self.assertEqual(vm.calls, [("/tmp/app.conf", "app.test", 9000)])
        self.assertTrue(res["changed"])

    def test_dry_run_does_not_write(self):
        vm = FakeVhost([_entry(port=9001)])
        res = project_config.apply_project(self._project(), self.cfg, vm, dry_run=True)
        self.assertTrue(res["ok"])
        self.assertEqual(vm.calls, [])
        self.assertFalse(res["changed"])

    def test_unknown_php_is_error(self):
        vm = FakeVhost([_entry()])
        res = project_config.apply_project(self._project(php="php99"), self.cfg, vm)
        self.assertFalse(res["ok"])
        self.assertIn("php99", res["error"])

    def test_no_matching_site_is_skip(self):
        vm = FakeVhost([_entry(name="other.test")])
        res = project_config.apply_project(self._project(), self.cfg, vm)
        self.assertTrue(res["ok"])
        self.assertTrue(res["steps"][0]["skip"])

    def test_hosts_written_when_requested(self):
        vm = FakeVhost([_entry()])
        with mock.patch.object(project_config.hosts_manager, "ensure_entries",
                               return_value={"ok": True, "added": ["app.test"],
                                             "message": "hosts ok"}) as m:
            res = project_config.apply_project(self._project(), self.cfg, vm, hosts=True)
        m.assert_called_once_with(["app.test"])
        self.assertTrue(any(s["tag"] == "hosts" and s["changed"] for s in res["steps"]))

    def test_start_services(self):
        v = _ver("php82", 9000, running=False)
        php = FakePhp([v])
        inst = types.SimpleNamespace(name="redis", running=False)
        redis = FakeRedis([inst])
        vm = FakeVhost([_entry(port=9000)])
        res = project_config.apply_project(
            self._project(services=["redis"], start=True), self.cfg, vm, php,
            redis_mgr=redis)
        self.assertTrue(res["ok"], res)
        self.assertEqual(php.started, ["php82"])
        self.assertEqual(redis.started, ["redis"])


if __name__ == "__main__":
    unittest.main()
