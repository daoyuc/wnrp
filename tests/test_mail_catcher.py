# -*- coding: utf-8 -*-
"""F11 邮件捕获：垫片落盘、.eml 列表解析、enable/disable 与还原。

全部在临时目录中进行，不触碰真实 php.ini 与数据目录。
"""
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from core import i18n as _i18n
from core import mail_catcher
from core.config import IS_WIN


class _Lang(unittest.TestCase):
    """断言中文文案：显式切 zh_CN，避免英文 locale 下环境性失败。"""

    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")

    def tearDown(self):
        _i18n.set_language(self._lang)


class ShimTest(_Lang):
    """垫片本身：从 stdin 读邮件并写成 .eml。"""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.mail = os.path.join(self.dir.name, "mail")
        self.shim = os.path.join(self.dir.name, "phpvm-sendmail.sh")
        for attr, val in (("mail_dir", self.mail), ("shim_path", self.shim)):
            p = mock.patch.object(mail_catcher, attr, return_value=val)
            p.start()
            self.addCleanup(p.stop)

    @unittest.skipIf(IS_WIN, "sendmail_path 仅 Unix 有效")
    def test_shim_captures_stdin_as_eml(self):
        ok, path = mail_catcher.install_shim()
        self.assertTrue(ok, path)
        self.assertTrue(os.access(path, os.X_OK), "垫片必须是可执行文件")
        raw = b"From: a@test\nTo: b@test\nSubject: hi\n\nbody\n"
        subprocess.run([path], input=raw, check=True)
        files = [n for n in os.listdir(self.mail) if n.endswith(".eml")]
        self.assertEqual(len(files), 1, files)
        with open(os.path.join(self.mail, files[0]), "rb") as f:
            self.assertIn(b"Subject: hi", f.read())

    def test_install_refused_on_windows(self):
        with mock.patch.object(mail_catcher, "IS_WIN", True):
            ok, msg = mail_catcher.install_shim()
        self.assertFalse(ok)
        self.assertIn("Unix", msg)


class ListTest(_Lang):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.mail = os.path.join(self.dir.name, "mail")
        os.makedirs(self.mail)
        p = mock.patch.object(mail_catcher, "mail_dir", return_value=self.mail)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, name: str, body: bytes, mtime: float | None = None) -> str:
        p = os.path.join(self.mail, name)
        with open(p, "wb") as f:
            f.write(body)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_list_parses_headers_and_sorts_desc(self):
        self._write("a.eml", b"From: a@test\nTo: b@test\nSubject: one\n\nbody\n", mtime=1000)
        self._write("b.eml", b"From: c@test\nTo: d@test\nSubject: two\n\nbody\n", mtime=2000)
        mails = mail_catcher.list_mails()
        self.assertEqual([m["subject"] for m in mails], ["two", "one"])
        self.assertEqual(mails[0]["from"], "c@test")
        self.assertEqual(mails[0]["to"], "d@test")

    def test_list_decodes_rfc2047_subject(self):
        self._write("c.eml", "From: a@test\nSubject: =?utf-8?b?5L2g5aW9?=\n\nx\n".encode())
        self.assertEqual(mail_catcher.list_mails()[0]["subject"], "你好")

    def test_list_skips_unreadable_entry(self):
        os.makedirs(os.path.join(self.mail, "dir.eml"))  # 目录冒充邮件 → 读取失败被跳过
        self._write("ok.eml", b"Subject: s\n\nx\n")
        self.assertEqual([m["name"] for m in mail_catcher.list_mails()], ["ok.eml"])

    def test_list_respects_limit(self):
        for i in range(3):
            self._write(f"{i}.eml", b"Subject: s\n\nx\n", mtime=1000 + i)
        self.assertEqual(len(mail_catcher.list_mails(limit=2)), 2)

    def test_read_and_clear(self):
        p = self._write("a.eml", b"Subject: s\n\nbody\n")
        self.assertIn("Subject: s", mail_catcher.read_mail(p))
        self.assertEqual(mail_catcher.clear_mails(), 1)
        self.assertEqual(mail_catcher.list_mails(), [])


@unittest.skipIf(IS_WIN, "sendmail_path 仅 Unix 有效：Windows 的 mail() 走 SMTP，"
                         "enable()/disable() 会直接拒绝（见 test_install_refused_on_windows）")
class EnableDisableTest(_Lang):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ini = os.path.join(self.dir.name, "php.ini")
        self.shim = os.path.join(self.dir.name, "phpvm-sendmail.sh")
        for attr, val in (("mail_dir", os.path.join(self.dir.name, "mail")),
                          ("shim_path", self.shim)):
            p = mock.patch.object(mail_catcher, attr, return_value=val)
            p.start()
            self.addCleanup(p.stop)
        self.v = types.SimpleNamespace(name="php82", ini=self.ini)

    def _write(self, text: str) -> None:
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write(text)

    def _read(self) -> str:
        with open(self.ini, encoding="utf-8") as f:
            return f.read()

    def test_enable_writes_marker_backup_and_is_idempotent(self):
        self._write("memory_limit = 128M\n;sendmail_path =\n")
        ok, msg, backup = mail_catcher.enable(self.v)
        self.assertTrue(ok, msg)
        self.assertTrue(os.path.exists(backup), "改前必须备份")
        self.assertEqual(self._read().count("sendmail_path ="), 2)  # 原注释行 + 我们的行
        self.assertIn(mail_catcher.MARK, self._read())
        self.assertTrue(mail_catcher.status(self.v)["enabled"])
        mail_catcher.enable(self.v)  # 重复开启不应叠加
        self.assertEqual(self._read().count(mail_catcher.MARK), 1)
        self.assertEqual(self._read().count("\nsendmail_path ="), 1)

    def test_disable_restores_original_value(self):
        self._write("sendmail_path = /usr/sbin/sendmail -t -i\n")
        mail_catcher.enable(self.v)
        self.assertIn(mail_catcher.ORIG_MARK, self._read())
        ok, msg, _ = mail_catcher.disable(self.v)
        self.assertTrue(ok, msg)
        body = self._read()
        self.assertIn("sendmail_path = /usr/sbin/sendmail -t -i", body)
        self.assertNotIn(mail_catcher.MARK, body)
        self.assertFalse(mail_catcher.status(self.v)["enabled"])

    def test_disable_without_original_removes_our_lines(self):
        self._write("memory_limit = 128M\n")
        mail_catcher.enable(self.v)
        mail_catcher.disable(self.v)
        self.assertNotIn("sendmail_path", self._read())

    def test_enable_requires_ini(self):
        self.v.ini = os.path.join(self.dir.name, "missing.ini")
        ok, msg, _ = mail_catcher.enable(self.v)
        self.assertFalse(ok)
        self.assertIn("php.ini", msg)


if __name__ == "__main__":
    unittest.main()
