# -*- coding: utf-8 -*-
"""macOS 采集端的计数规则（不建真实 tap，直接喂事件字段）。

与 Windows 端同一套守护：长按自动重复不重复计数、注入事件过滤、
暂停期间仍维护按键状态。键码换算成 Windows 虚拟键码后，聚合与键名两端一致。
"""
import unittest

from core.plugins.keymouse import collector_mac as mac


class VkMapTest(unittest.TestCase):
    """macOS 虚拟键码 → Windows 虚拟键码（换算错了统计排名就全乱）。"""

    def test_letters_and_digits(self):
        self.assertEqual(mac.vk_of(0), 0x41)     # A
        self.assertEqual(mac.vk_of(18), 0x31)    # 1
        self.assertEqual(mac.vk_of(49), 0x20)    # Space
        self.assertEqual(mac.vk_of(36), 0x0D)    # Return

    def test_modifiers(self):
        self.assertEqual(mac.vk_of(55), 0x5B)    # 左 Command
        self.assertEqual(mac.vk_of(56), 0xA0)    # 左 Shift
        self.assertEqual(mac.vk_of(59), 0xA2)    # 左 Control

    def test_editing_keys(self):
        self.assertEqual(mac.vk_of(51), 0x08)    # Delete（退格）
        self.assertEqual(mac.vk_of(117), 0x2E)   # Forward Delete
        self.assertEqual(mac.vk_of(123), 0x25)   # ←
        self.assertEqual(mac.vk_of(122), 0x70)   # F1

    def test_unknown_keycode_is_kept(self):
        self.assertEqual(mac.vk_of(200), 200)

    def test_invalid_input(self):
        self.assertEqual(mac.vk_of(None), -1)
        self.assertEqual(mac.vk_of("A"), -1)


class HandleTest(unittest.TestCase):
    """handle/press 是纯逻辑，不依赖系统框架，任何平台都能跑。"""

    def setUp(self):
        self.c = mac.KeyboardCollector()

    def down(self, keycode: int, repeat: bool = False,
             state: int = mac.CG_SOURCE_HID, pid: int = 0):
        self.c.handle(mac.CG_EVENT_KEY_DOWN, keycode, repeat=repeat,
                      source_state=state, source_pid=pid)

    def up(self, keycode: int):
        self.c.handle(mac.CG_EVENT_KEY_UP, keycode)

    def flags(self, keycode: int):
        self.c.handle(mac.CG_EVENT_FLAGS_CHANGED, keycode)

    def vks(self):
        return [vk for _ts, vk in self.c.take()]

    def test_auto_repeat_is_not_counted_twice(self):
        self.down(0)
        self.down(0)               # 长按：系统反复上报
        self.down(0, repeat=True)  # 显式 autorepeat 字段
        self.assertEqual(self.vks(), [0x41])

    def test_release_allows_new_count(self):
        self.down(0)
        self.up(0)
        self.down(0)
        self.assertEqual(self.vks(), [0x41, 0x41])

    def test_key_up_of_untouched_key_is_harmless(self):
        self.up(1)                 # 采集启动前就按下的键，抬起时不该报错
        self.assertEqual(self.vks(), [])

    def test_modifier_toggles_on_flags_changed(self):
        self.flags(56)             # 按下 Shift：计数
        self.flags(56)             # 抬起 Shift：只清状态
        self.flags(56)             # 再按下：重新计数
        self.assertEqual(self.vks(), [0xA0, 0xA0])

    def test_injected_is_ignored(self):
        self.down(1, state=-1)     # 其它进程用私有事件源合成
        self.assertEqual(self.vks(), [])
        self.up(1)
        self.down(1)               # 真实按键仍然计数
        self.assertEqual(self.vks(), [0x53])

    def test_injected_filter_can_be_disabled(self):
        c = mac.KeyboardCollector(filter_injected=False)
        c.handle(mac.CG_EVENT_KEY_DOWN, 1, source_state=-1)
        self.assertEqual(len(c.events), 1)

    def test_posted_event_with_source_pid_is_ignored(self):
        """CGEventPost 用默认 source 时 state ID 为 0，靠 PID 字段识别。"""
        self.down(1, pid=4242)
        self.assertEqual(self.vks(), [])
        self.up(1)
        self.down(1)               # 真实按键（PID 为 0）仍然计数
        self.assertEqual(self.vks(), [0x53])

    def test_posted_event_filter_can_be_disabled(self):
        c = mac.KeyboardCollector(filter_injected=False)
        c.handle(mac.CG_EVENT_KEY_DOWN, 1, source_state=0, source_pid=4242)
        self.assertEqual(len(c.events), 1)

    def test_paused_keeps_key_state(self):
        self.c.paused = True
        self.down(0)               # 暂停期间按下：不计数
        self.assertEqual(self.vks(), [])
        self.c.paused = False
        self.up(0)
        self.down(0)               # 恢复后重新按下才算一次
        self.assertEqual(self.vks(), [0x41])

    def test_unknown_keycode_still_counted(self):
        self.down(200)             # 未收录的键不丢，原样入库
        self.assertEqual(self.vks(), [200])

    def test_take_respects_limit(self):
        for code in range(0, 6):
            self.down(code)
        self.assertEqual(len(self.c.take(limit=2)), 2)
        self.assertEqual(len(self.c.take()), 4)

    def test_pending_counts_queue(self):
        self.down(0)
        self.assertEqual(self.c.pending(), 1)


class InstallGuardTest(unittest.TestCase):
    @unittest.skipIf(mac.IS_MAC, "macOS 上 install() 会真的请求系统授权")
    def test_refuses_on_other_platforms(self):
        c = mac.KeyboardCollector()
        self.assertFalse(c.install())
        self.assertIn("不支持", c.error)
        self.assertFalse(c.ensure_hook())     # 不该在非 macOS 上反复装
        self.assertFalse(c.trusted)

    def test_pump_is_idle_sleep(self):
        c = mac.KeyboardCollector()
        self.assertEqual(c.pump(0), 0)

    def test_permission_payload_shape(self):
        perm = mac.permission()
        self.assertEqual(set(perm), {"required", "granted", "settings_url"})
        self.assertEqual(perm["required"], mac.IS_MAC)


if __name__ == "__main__":
    unittest.main()
