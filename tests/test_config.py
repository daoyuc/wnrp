# -*- coding: utf-8 -*-
"""core/config.py 持久化可靠性回归测试。

守「config.json 直接覆盖写，写一半崩溃 → 损坏后回退默认并覆盖保存，
端口映射与设置全部丢失」：
- save() 走 tmp + os.replace 原子替换，不留 .tmp 残留；
- 加载损坏配置时先把坏文件留档为 <config>.broken，再回退默认重建。
"""
import os
import tempfile
import unittest

from core.config import Config


class ConfigAtomicTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "config.json")

    def test_save_roundtrip(self):
        cfg = Config(self.path)
        cfg.ports["php85"] = 9999  # 非默认值，落盘后必须原样回来
        cfg.set_setting("auto_recover_crash", True)
        cfg.save()
        cfg2 = Config(self.path)
        self.assertEqual(cfg2.ports["php85"], 9999)
        self.assertTrue(cfg2.get_setting("auto_recover_crash"))
        # 原子写不残留 .tmp
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_corrupt_config_backed_up_not_silent(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"ports": {"php85": 9999, "php74": 9074}, "setti')  # 截断损坏
        cfg = Config(self.path)
        # 回退默认值（用户改过的 9999 丢失，但坏文件已留档可找回）
        self.assertNotEqual(cfg.ports.get("php85"), 9999)
        # 坏文件已留档，原始内容可人工找回
        broken = self.path + ".broken"
        self.assertTrue(os.path.isfile(broken))
        with open(broken, encoding="utf-8") as f:
            self.assertIn('"php85": 9999', f.read())

    def test_valid_config_not_renamed(self):
        cfg = Config(self.path)
        cfg.ports["php85"] = 9999
        cfg.save()
        cfg2 = Config(self.path)
        self.assertEqual(cfg2.ports["php85"], 9999)
        self.assertFalse(os.path.exists(self.path + ".broken"))


if __name__ == "__main__":
    unittest.main()
