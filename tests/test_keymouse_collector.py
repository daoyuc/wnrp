# -*- coding: utf-8 -*-
"""键盘钩子回调的计数规则（不真正安装钩子，直接喂 KBDLLHOOKSTRUCT）。

守护三件事：长按自动重复不能重复计数、注入事件必须过滤、
暂停期间仍维护按键状态（恢复后不会把「暂停时按下的键」误当成一次新按下）。
"""
import ctypes
import unittest
from unittest import mock

from core.plugins.keymouse import collector as mod

_WM_KEYDOWN = mod.WM_KEYDOWN
_WM_KEYUP = mod.WM_KEYUP


class _FakeUser32:
    def CallNextHookEx(self, *args):
        return 0


class _Base(unittest.TestCase):
    def setUp(self):
        self.c = mod.KeyboardCollector()
        self.c._user32 = _FakeUser32()
        self._refs = []   # 保持 byref 对象存活，避免 GC 后悬空指针

    def press(self, vk: int, flags: int = 0):
        kb = mod.KBDLLHOOKSTRUCT()
        kb.vkCode = vk
        kb.flags = flags
        self._refs.append(kb)
        self.c._callback(0, _WM_KEYDOWN, ctypes.pointer(kb))

    def release(self, vk: int):
        kb = mod.KBDLLHOOKSTRUCT()
        kb.vkCode = vk
        kb.flags = 0
        self._refs.append(kb)
        self.c._callback(0, _WM_KEYUP, ctypes.pointer(kb))

    def vks(self):
        return [vk for _ts, vk in self.c.take()]


@unittest.skipUnless(mod.IS_WIN, "键盘钩子结构体仅在 Windows 上可用")
class CallbackTest(_Base):
    def test_auto_repeat_is_not_counted_twice(self):
        self.press(65)
        self.press(65)   # 长按自动重复
        self.press(65)
        self.assertEqual(self.vks(), [65])

    def test_release_allows_new_count(self):
        self.press(65)
        self.release(65)
        self.press(65)
        self.assertEqual(self.vks(), [65, 65])

    def test_injected_is_ignored(self):
        self.press(66, flags=mod.LLKHF_INJECTED)
        self.assertEqual(self.vks(), [])
        self.release(66)
        self.press(66)   # 真实按键仍然计数
        self.assertEqual(self.vks(), [66])

    def test_injected_filter_can_be_disabled(self):
        c = mod.KeyboardCollector(filter_injected=False)
        c._user32 = _FakeUser32()
        kb = mod.KBDLLHOOKSTRUCT()
        kb.vkCode = 67
        kb.flags = mod.LLKHF_INJECTED
        self._refs.append(kb)
        c._callback(0, _WM_KEYDOWN, ctypes.pointer(kb))
        self.assertEqual(len(c.events), 1)

    def test_paused_keeps_key_state(self):
        self.c.paused = True
        self.press(65)            # 暂停期间按下：不计数
        self.assertEqual(self.vks(), [])
        self.c.paused = False
        self.release(65)
        self.press(65)            # 恢复后重新按下才算一次
        self.assertEqual(self.vks(), [65])

    def test_callback_never_raises(self):
        # 非法 lParam 不应把异常抛进系统回调（会被系统卸载钩子）
        self.assertEqual(self.c._callback(0, _WM_KEYDOWN, 0), 0)

    def test_take_respects_limit(self):
        for vk in range(65, 71):
            self.press(vk)
        self.assertEqual(len(self.c.take(limit=2)), 2)
        self.assertEqual(len(self.c.take()), 4)


class EnsureHookTest(unittest.TestCase):
    """钩子自动重装：替换 install/uninstall，不真正装系统钩子（跨平台可跑）。"""

    def setUp(self):
        self.c = mod.KeyboardCollector()
        self.c._hook = object()
        self.c._installed_at = 100.0
        self.c.last_event_ts = 0.0
        self.calls: list[str] = []
        self.install_ok = True
        c = self.c

        def fake_install():
            self.calls.append("install")
            if not self.install_ok:
                c.error = "SetWindowsHookEx 失败"
                return False
            c._hook = object()
            c._installed_at = 200.0
            return True

        def fake_uninstall():
            self.calls.append("uninstall")
            c._hook = None

        c.install = fake_install
        c.uninstall = fake_uninstall

    def test_no_reinstall_before_interval(self):
        self.assertTrue(self.c.ensure_hook(now=130.0))
        self.assertEqual(self.calls, [])

    def test_reinstall_after_interval(self):
        self.c._down.add(65)
        self.assertTrue(self.c.ensure_hook(now=161.0))
        self.assertEqual(self.calls, ["uninstall", "install"])
        self.assertEqual(self.c.reinstall_count, 1)
        self.assertEqual(self.c._down, set())

    def test_reinstall_deferred_while_typing(self):
        self.c.last_event_ts = 160.0
        self.assertTrue(self.c.ensure_hook(now=161.0))
        self.assertEqual(self.calls, [])

    def test_missing_hook_reinstalls_immediately(self):
        self.c._hook = None
        self.assertTrue(self.c.ensure_hook(now=101.0))
        self.assertEqual(self.calls, ["uninstall", "install"])

    def test_failed_reinstall_returns_false(self):
        self.install_ok = False
        self.assertFalse(self.c.ensure_hook(now=161.0))
        self.assertEqual(self.c.reinstall_count, 0)
        self.assertIn("失败", self.c.error)


class _PumpUser32:
    """假消息泵：PeekMessageW 按预设序列返回，记录消息等待调用。"""

    def __init__(self, peeks):
        self.peeks = list(peeks)
        self.wait_calls = 0
        self.wait_args = None

    def PeekMessageW(self, *args):
        return self.peeks.pop(0) if self.peeks else 0

    def TranslateMessage(self, *args):
        return 0

    def DispatchMessageW(self, *args):
        return 0

    def MsgWaitForMultipleObjects(self, *args):
        self.wait_calls += 1
        self.wait_args = args
        return 0

    def CallNextHookEx(self, *args):
        return 0


class PumpTest(unittest.TestCase):
    """消息泵必须「等消息」而不是 sleep —— 后者会让打字 / 输入法变粘。"""

    def setUp(self):
        self.c = mod.KeyboardCollector()
        self.c._msg = ctypes.c_int(0)

    def _pump(self, peeks, idle=0.05):
        fake = _PumpUser32(peeks)
        self.c._user32 = fake
        with mock.patch.object(mod.time, "sleep") as sleep:
            count = self.c.pump(idle_sleep=idle)
        return fake, count, sleep

    def test_idle_waits_for_message_instead_of_sleeping(self):
        fake, count, sleep = self._pump([0, 1, 0])
        self.assertEqual(count, 1)
        self.assertEqual(fake.wait_calls, 1)
        self.assertEqual(fake.wait_args[3], 50)  # 0.05s 换算成 50ms 最长等待
        self.assertEqual(fake.wait_args[4], mod.QS_ALLINPUT)
        sleep.assert_not_called()

    def test_pending_messages_are_drained_without_waiting(self):
        fake, count, sleep = self._pump([1, 1, 0])
        self.assertEqual(count, 2)
        self.assertEqual(fake.wait_calls, 0)
        sleep.assert_not_called()

    def test_timeout_still_returns_zero(self):
        fake, count, sleep = self._pump([0, 0])
        self.assertEqual(count, 0)
        self.assertEqual(fake.wait_calls, 1)
        sleep.assert_not_called()

    def test_without_hook_falls_back_to_sleep(self):
        c = mod.KeyboardCollector()
        with mock.patch.object(mod.time, "sleep") as sleep:
            self.assertEqual(c.pump(0.01), 0)
        sleep.assert_called_once_with(0.01)


if __name__ == "__main__":
    unittest.main()
