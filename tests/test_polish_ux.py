# -*- coding: utf-8 -*-
"""W4 UX：ini/调优一键重启、SSH 凭据门禁与片段收折、hosts 还原预览。"""
import inspect
import unittest

from core import i18n as _i18n
from ui import dialogs, php_panel, redis_panel, ssh_panel, vhost_panel


class _Lang(unittest.TestCase):
    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")

    def tearDown(self):
        _i18n.set_language(self._lang)


class PhpRestartOfferWiringTest(_Lang):
    def test_ini_edit_offers_restart(self):
        src = inspect.getsource(dialogs.IniEditDialog._poll_save)
        self.assertIn("offer_php_restart", src)

    def test_php_panel_passes_on_restart(self):
        edit = inspect.getsource(php_panel.PhpPanel._edit_ini)
        self.assertIn("on_restart", edit)
        tune = inspect.getsource(php_panel.PhpPanel._recommend_settings)
        self.assertIn("on_restart", tune)
        self.assertIn("php_running", tune)


class SshCredAndSnippetTest(_Lang):
    def test_cred_actions_disable_when_missing(self):
        src = inspect.getsource(ssh_panel.SshPanel._sync_cred_actions)
        self.assertIn("disabled", src)
        self.assertIn("先补密码或私钥", src)

    def test_builtin_snippets_collapsed_by_default(self):
        src = inspect.getsource(ssh_panel.SshPanel._render_snippets)
        self.assertIn("builtin-", src)
        self.assertIn("_show_builtin", src)
        init = inspect.getsource(ssh_panel.SshPanel._build)
        self.assertIn("显示常用探测", init)


class RedisDangerWiringTest(_Lang):
    def test_exec_uses_shared_confirm(self):
        src = inspect.getsource(redis_panel.RedisPanel._exec_cmd)
        self.assertIn("confirm_redis_danger", src)
        self.assertIn("flushall", src)


class HostsRestorePreviewTest(_Lang):
    def test_restore_uses_backup_summary(self):
        src = inspect.getsource(vhost_panel.VhostPanel._restore_hosts)
        self.assertIn("backup_summary", src)
        self.assertIn("备份时间", src)
        self.assertIn("将恢复约", src)


if __name__ == "__main__":
    unittest.main()
