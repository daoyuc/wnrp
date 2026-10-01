# -*- coding: utf-8 -*-
"""core/crash_watchdog：开机自启策略对持久化「崩溃看护列表」的裁剪。

背景（真实故障）：看护列表存在 crash_watchdog.json 里且跨会话持久化，而
``unwatch()`` 只在用户**手动停止**版本时调用 —— 正常关机不会清空它。于是上一轮
运行过的全部版本会在下次开机被逐条判定「进程失联」并拉起，绕过
``settings.autostart_php_scope``：本机实测「只勾了开机自启 phpvm」，登录后 11 个
PHP 版本全被拉活。``_prune_watch_for_boot`` 负责在守护启动首轮丢弃不属于本次
开机集合的条目。

用例全部使用替身 manager，并屏蔽 ``_log``（不写真实 crash_watchdog.log）。
"""
import time
import unittest
from unittest import mock

from core import crash_watchdog as cw
from tests.fakes import FakeConfig


class FakeVersion:
    def __init__(self, name, port=9000):
        self.name = name
        self.port = port


class FakePhp:
    """_boot_php_names / 失联探测只用得到 scan_versions 与 get_status。"""

    def __init__(self, names, running=()):
        self.versions = [FakeVersion(n) for n in names]
        self._running = set(running)

    def scan_versions(self):
        return list(self.versions)

    def refresh_all_status(self, versions=None, fast=True):
        return None

    def get_status(self, v, fast=False):
        return (v.name in self._running), []


def _stub_group(boot_names, boom=False):
    """替身 ServiceGroup：php_targets 直接回放「本次开机集合」。"""

    class Stub:
        def __init__(self, mgr, *rest):
            self.pm = mgr

        def php_targets(self, scope, names=None):
            if boom:
                raise RuntimeError("bad scope")
            return [FakeVersion(n) for n in boot_names], []

    return Stub


class PruneWatchTest(unittest.TestCase):
    def setUp(self):
        mock.patch.object(cw, "_log").start()
        self.addCleanup(mock.patch.stopall)

    def _prune(self, names, running=(), watch=None, boot=None, boom=False,
               scope="newest"):
        cfg = FakeConfig(settings={"autostart_php_scope": scope})
        pm = FakePhp(names, running)
        versions = {v.name: v for v in pm.versions}
        state = {"watch": {n: {"count": 0} for n in (watch if watch is not None else names)}}
        with mock.patch("core.service_group.ServiceGroup",
                        _stub_group(boot or [], boom)):
            cw._prune_watch_for_boot(cfg, pm, versions, state)
        return state["watch"]

    def test_drops_watch_entries_outside_boot_set(self):
        # 上一轮三个版本都在运行 → 本次只看护开机集合里的那一个
        self.assertEqual(sorted(self._prune(["php85", "php74", "php82"],
                                            boot=["php85"])), ["php85"])

    def test_keeps_watch_entries_that_are_currently_running(self):
        # 会话中途重启守护：正在运行的版本不丢看护（手动启动的照旧受保护）
        self.assertEqual(sorted(self._prune(["php85", "php74"], running=["php74"],
                                            boot=["php85"])), ["php74", "php85"])

    def test_boot_set_entries_are_kept_for_resurrection(self):
        # 开机集合里的版本即使当前没运行也要保留 → 随后由失联探测正常拉起
        watch = self._prune(["php85", "php74"], watch=["php85", "php74"],
                            boot=["php85"])
        self.assertEqual(sorted(watch), ["php85"])

    def test_drops_entries_for_removed_version_dirs(self):
        # 版本目录已删除：无需再看护（即使它不在开机集合里也一并清掉）
        self.assertEqual(sorted(self._prune(["php85"], watch=["php85", "php73"],
                                            boot=["php85"])), ["php85"])

    def test_keeps_everything_when_policy_unresolvable(self):
        # 策略解析失败时保守不动：宁可多复活，也不误删用户正在用的看护
        self.assertEqual(sorted(self._prune(["php85", "php74"], boom=True)),
                         ["php74", "php85"])

    def test_all_scope_keeps_every_watched_version(self):
        names = ["php85", "php74", "php82"]
        self.assertEqual(sorted(self._prune(names, watch=names, boot=names,
                                            scope="all")), sorted(names))

    def test_boot_names_follow_configured_scope(self):
        cfg = FakeConfig(settings={"autostart_php_scope": "custom"})
        pm = FakePhp(["php85", "php74"])
        versions = {v.name: v for v in pm.versions}
        seen: dict = {}

        class Stub:
            def __init__(self, mgr, *rest):
                seen["mgr"] = mgr

            def php_targets(self, scope, names=None):
                seen["scope"] = scope
                return [FakeVersion("php74")], []

        with mock.patch("core.service_group.ServiceGroup", Stub):
            boot = cw._boot_php_names(cfg, pm, versions)
        self.assertEqual(boot, {"php74"})
        self.assertEqual(seen["scope"], "custom")
        # 复用已扫描结果，避免守护启动时再扫一遍磁盘
        self.assertEqual([v.name for v in seen["mgr"].versions], ["php85", "php74"])


class UnwatchTest(unittest.TestCase):
    """unwatch / unwatch_many：一键停止后必须真正把版本移出看护名单。"""

    def setUp(self):
        mock.patch.object(cw, "_log").start()
        self.addCleanup(mock.patch.stopall)

    def test_unwatch_many_clears_watch_and_marks_manual_stop(self):
        state = {"watch": {"php85": {"count": 0}, "php74": {"count": 0}}}
        with mock.patch.object(cw, "_load_state", return_value=state), \
                mock.patch.object(cw, "_save_state") as save:
            cw.unwatch_many(["php85", "php74"])
        self.assertEqual(state["watch"], {})
        self.assertEqual(sorted(state.get("manual_stops", {})), ["php74", "php85"])
        save.assert_called_once()  # 批量只落一次盘

    def test_unwatch_delegates_to_unwatch_many(self):
        with mock.patch.object(cw, "unwatch_many") as many:
            cw.unwatch("php85")
        many.assert_called_once_with(["php85"])

    def test_unwatch_many_ignores_empty_input(self):
        with mock.patch.object(cw, "_load_state") as load:
            cw.unwatch_many([])
        load.assert_not_called()


class ManualGraceTest(unittest.TestCase):
    """宽限期内的版本不能被重新登记进看护名单。

    否则崩溃事件会把它塞回 watch，宽限一过失联探测立刻拉活 —— 用户点「全部停止」
    后仍会看到 PHP 自己起来。
    """

    def setUp(self):
        mock.patch.object(cw, "_log").start()
        self.addCleanup(mock.patch.stopall)

    def _try(self, grace: bool):
        pm = FakePhp(["php85"], running=())
        versions = {v.name: v for v in pm.versions}
        state = {"watch": {}, "manual_stops": {"php85": time.time()}}
        cfg = FakeConfig(settings={"auto_recover_crash": True,
                                   "auto_recover_limit": 3})
        with mock.patch.object(cw, "_manual_grace", return_value=grace), \
                mock.patch.object(pm, "start", create=True,
                                  return_value="started") as start:
            cw._try_restart(cfg, pm, versions, state, "php85", "崩溃事件 0xc0000005")
        return state, start

    def test_grace_period_skips_without_rewatching(self):
        state, start = self._try(grace=True)
        start.assert_not_called()
        self.assertNotIn("php85", state["watch"])

    def test_outside_grace_restarts_and_keeps_watching(self):
        state, start = self._try(grace=False)
        start.assert_called_once()
        self.assertIn("php85", state["watch"])


if __name__ == "__main__":
    unittest.main()
