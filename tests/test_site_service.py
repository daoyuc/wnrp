# -*- coding: utf-8 -*-
"""core/site_service：建站编排顺序、硬项失败即止、hosts/重载软项与回滚。

用假 nginx 布局（替代 VhostManager 内部构造的 NginxManager）与打桩的 hosts 写入，
不触碰真实 nginx 配置与系统 hosts。
"""
import os
import tempfile
import unittest
from unittest import mock

from core import site_service
from core.site_service import SitePlan, SiteRollback
from core.vhost_manager import VhostManager
from tests.fakes import FAILED, FakeConfig, make_layout, read

HOSTS_OK = {"ok": True, "elevated": False, "added": [], "already": [],
            "conflict": [], "message": "hosts ok"}


class SiteServiceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name

    def tearDown(self):
        self.dir.cleanup()

    def _patch(self, ng, hosts_res=None):
        """把 site_service 里的 VhostManager 换成挂了假 nginx 的同款实例。

        传入 hosts_res 时同时打桩 hosts 写入/移除（返回该 mock 供断言）。
        """
        def factory(config=None):
            vm = VhostManager(FakeConfig())
            vm.nginx = ng
            return vm

        p = mock.patch.object(site_service, "VhostManager", factory)
        p.start()
        self.addCleanup(p.stop)
        if hosts_res is None:
            return None
        ensure = mock.patch.object(site_service.hosts_manager, "ensure_entries",
                                   return_value=dict(hosts_res))
        self.addCleanup(ensure.stop)
        ensure_mock = ensure.start()
        remove = mock.patch.object(
            site_service.hosts_manager, "remove_entries",
            return_value={"ok": True, "elevated": False, "removed": [],
                          "missing": [], "message": "removed"})
        self.addCleanup(remove.stop)
        return ensure_mock, remove.start()

    @staticmethod
    def _plan(**over) -> SitePlan:
        base = dict(domains=["app.test"], template_key="php",
                    docroot="D:/www9/app", port=9085, conf_name="app.test.conf")
        base.update(over)
        return SitePlan(**base)

    def test_create_site_writes_conf_include_and_passes_test(self):
        ng = make_layout(self.root)
        self._patch(ng)
        res = site_service.create_site(self._plan(), FakeConfig())
        self.assertFalse(res.fatal, res.steps)
        # nginx 未运行 → reload 步骤记为 skip（配置已就绪，启动后自动生效）
        self.assertEqual([tag for tag, _ in res.steps],
                         ["file", "include", "test", "reload"])
        self.assertTrue(res.step("reload").get("skip"))
        self.assertTrue(res.path.endswith("app.test.conf"))
        self.assertIn("fastcgi_pass 127.0.0.1:9085;", read(res.path))
        from core.vhost_manager import nginx_path
        self.assertIn(nginx_path(ng.vhost_dir) + "/*.conf", read(ng.main_conf))

    def test_hard_step_failure_stops_before_hosts_and_reload(self):
        ng = make_layout(self.root, http_block=False)  # 无 http 块 → include 必失败
        hosts_mock, _ = self._patch(ng, hosts_res=HOSTS_OK)
        ng.running = True
        res = site_service.create_site(
            self._plan(hosts=True, domains=["app.test", "*.dev.test"]), FakeConfig())
        self.assertTrue(res.fatal)
        tags = [tag for tag, _ in res.steps]
        self.assertNotIn("hosts", tags, "硬项失败后不应再写 hosts")
        self.assertNotIn("reload", tags, "硬项失败后不应重载")
        hosts_mock.assert_not_called()
        self.assertEqual(ng.reload_count, 0)

    def test_skips_test_when_nginx_missing_but_still_writes_hosts(self):
        ng = make_layout(self.root, with_nginx_exe=False)
        hosts_mock, _ = self._patch(ng, hosts_res=dict(HOSTS_OK, added=["app.test"]))
        res = site_service.create_site(self._plan(hosts=True), FakeConfig())
        self.assertFalse(res.fatal, res.steps)
        self.assertTrue(res.step("test").get("skip"))
        hosts_mock.assert_called_once_with(["app.test"])
        self.assertEqual(res.rollback.hosts, ["app.test"], "写入的域名要记入回滚记录")

    def test_writes_hosts_without_wildcards_and_reloads(self):
        ng = make_layout(self.root)
        ng.running = True
        hosts_mock, _ = self._patch(ng, hosts_res=dict(HOSTS_OK, added=["app.test"]))
        res = site_service.create_site(
            self._plan(hosts=True, domains=["app.test", "*.dev.test"]), FakeConfig())
        hosts_mock.assert_called_once_with(["app.test"])
        self.assertEqual([tag for tag, _ in res.steps][-1], "reload")
        self.assertEqual(ng.reload_count, 1)

    def test_allow_missing_main_conf_is_skip_not_fatal(self):
        ng = make_layout(self.root)
        ng.main_conf = os.path.join(ng.site_base, "nope.conf")
        self._patch(ng)
        res = site_service.create_site(self._plan(allow_missing_main_conf=True), FakeConfig())
        self.assertFalse(res.fatal, res.steps)
        self.assertTrue(res.step("include").get("skip"))
        strict = site_service.SitePlan(**{**self._plan().__dict__})
        res2 = site_service.create_site(strict, FakeConfig())
        self.assertTrue(res2.fatal, "CLI 口径：主配置缺失应视为失败")

    def test_overwritten_conf_is_restored_on_failure(self):
        ng = make_layout(self.root, http_block=False)
        self._patch(ng)
        path = os.path.join(ng.vhost_dir, "app.test.conf")
        os.makedirs(ng.vhost_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("server { listen 80; server_name app.test; }\n")
        before = read(path)
        res = site_service.create_site(self._plan(), FakeConfig())
        self.assertTrue(res.fatal)
        self.assertEqual(len(res.rollback.vhost), 1, res.rollback)
        items = site_service.rollback_site(res.rollback, FakeConfig())
        self.assertTrue(items[0]["ok"], items)
        self.assertEqual(read(path), before, "回滚应还原被覆盖的配置")

    def test_rollback_removes_hosts_entries_and_clears_record(self):
        ng = make_layout(self.root)
        _, remove = self._patch(ng, hosts_res=HOSTS_OK)
        rb = SiteRollback(hosts=["app.test"])
        items = site_service.rollback_site(rb, FakeConfig())
        remove.assert_called_once_with(["app.test"])
        self.assertTrue(any(i["target"] == "hosts" and i["ok"] for i in items), items)
        self.assertEqual(rb.hosts, [], "回滚记录应清空，避免重复移除")

    def test_failed_test_output_marks_fatal(self):
        ng = make_layout(self.root)
        ng.test_output = FAILED
        self._patch(ng)
        res = site_service.create_site(self._plan(), FakeConfig())
        self.assertTrue(res.fatal)
        self.assertTrue(res.failed("test"))


if __name__ == "__main__":
    unittest.main()
