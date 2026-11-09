# -*- coding: utf-8 -*-
"""ui/download_dialog.py 状态判定回归：本地已有旧版本时必须显示「可更新」。"""
import unittest

from ui.download_dialog import (
    STATE_INSTALLED,
    STATE_UPDATE,
    state_for,
)


class StateForTest(unittest.TestCase):
    """守「本地 php8.5.9 / 线上 8.5.12-1 只显示已安装」的缺陷。"""

    def test_older_installed_is_update(self):
        self.assertEqual(state_for("8.5.9", "8.5.12-1"), (STATE_UPDATE, "update"))

    def test_same_version_is_installed(self):
        self.assertEqual(
            state_for("8.5.12-1", "8.5.12-1"), (STATE_INSTALLED, "installed"))

    def test_newer_installed_stays_installed(self):
        self.assertEqual(
            state_for("8.6.0", "8.5.12-1"), (STATE_INSTALLED, "installed"))

    def test_unparseable_display_is_conservative(self):
        # display 为空（php -v 解析失败）或含非数字段 → 保守判「已安装」
        self.assertEqual(state_for("", "8.5.12-1"), (STATE_INSTALLED, "installed"))
        self.assertEqual(state_for("8.x", "8.5.12-1"), (STATE_INSTALLED, "installed"))


if __name__ == "__main__":
    unittest.main()
