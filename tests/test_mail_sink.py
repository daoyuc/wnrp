# -*- coding: utf-8 -*-
"""邮件捕获的 Windows 路径：内置 SMTP sink + ini 写入（SMTP/smtp_port）。

sink 本体与平台无关（纯标准库 socketserver），因此这些用例在各平台都跑；
ini 分支用 patch IS_WIN 的方式在任意平台上覆盖 Windows 逻辑。
"""
import email
import os
import smtplib
import tempfile
import threading
import types
import unittest
from email.message import EmailMessage
from unittest import mock

from core import i18n as _i18n
from core import mail_catcher, mail_sink


def _read(path: str) -> str:
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


class MailSinkTest(unittest.TestCase):
    """sink 本体：收信 → 落盘 .eml。"""

    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patches = [mock.patch.object(mail_catcher, "mail_dir", lambda: self.dir.name)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        ok, msg = mail_sink.start(0)  # 0 = 由系统分配空闲端口
        self.assertTrue(ok, msg)
        self.addCleanup(mail_sink.stop)

    def tearDown(self):
        _i18n.set_language(self._lang)

    def _send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP("127.0.0.1", mail_sink.bound_port(), timeout=10) as s:
            s.send_message(msg)

    def test_running_and_port(self):
        self.assertTrue(mail_sink.running())
        self.assertGreater(mail_sink.bound_port(), 0)

    def test_message_is_captured_as_eml(self):
        msg = EmailMessage()
        msg["From"] = "app@test"
        msg["To"] = "user@test"
        msg["Subject"] = "欢迎邮件"
        msg.set_content("正文第一行\n正文第二行\n")
        self._send(msg)

        files = [f for f in os.listdir(self.dir.name) if f.endswith(".eml")]
        self.assertEqual(len(files), 1, files)
        body = _read(os.path.join(self.dir.name, files[0]))
        # 非 ASCII 主题会被 smtplib 做 RFC2047 编码，故用解析后的值断言
        parsed = email.message_from_string(body, policy=email.policy.default)
        self.assertEqual(str(parsed["Subject"]), "欢迎邮件")
        self.assertIn("正文第二行", body)
        # 信封头便于回溯「谁发给谁」
        self.assertIn("X-phpvm-Envelope-From: <app@test>", body)
        self.assertIn("X-phpvm-Envelope-To: <user@test>", body)
        self.assertIn("Received: from phpvm-mail-sink", body)

    def test_dot_stuffing_round_trip(self):
        """正文里以点开头的行必须原样保留（smtplib 会 dot-stuff，sink 负责还原）。"""
        raw = b"Subject: dots\r\n\r\n.hidden line\r\n..double\r\n"
        with smtplib.SMTP("127.0.0.1", mail_sink.bound_port(), timeout=10) as s:
            s.sendmail("a@test", ["b@test"], raw)

        name = [f for f in os.listdir(self.dir.name) if f.endswith(".eml")][0]
        body = _read(os.path.join(self.dir.name, name))
        self.assertIn(".hidden line", body)
        self.assertIn("..double", body)

    def test_capture_is_readable_by_mail_catcher(self):
        msg = EmailMessage()
        msg["From"] = "app@test"
        msg["To"] = "user@test"
        msg["Subject"] = "listable"
        msg.set_content("hi")
        self._send(msg)
        mails = mail_catcher.list_mails()
        self.assertEqual(len(mails), 1, mails)
        self.assertEqual(mails[0]["subject"], "listable")
        self.assertEqual(mails[0]["from"], "app@test")

    def test_probe_recognizes_own_sink(self):
        self.assertTrue(mail_sink.probe(mail_sink.bound_port()))

    def test_probe_rejects_foreign_listener(self):
        """普通 TCP 服务（无我们的横幅）不能被误认为 sink。"""
        import socketserver as ss

        class _Plain(ss.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"220 not us\r\n")

        with ss.TCPServer(("127.0.0.1", 0), _Plain) as srv:
            port = srv.server_address[1]
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                self.assertFalse(mail_sink.probe(port))
            finally:
                srv.shutdown()

    def test_second_process_detects_existing_sink(self):
        """另一个 phpvm 进程已占用端口时，start() 应视为「已在运行」而非失败。"""
        port = mail_sink.bound_port()
        with mock.patch.object(mail_sink, "_server", None), \
                mock.patch.object(mail_sink, "_external_port", None):
            ok, msg = mail_sink.start(port)
            self.assertTrue(ok, msg)
            self.assertTrue(mail_sink.running())
            self.assertEqual(mail_sink.bound_port(), port)
            mail_sink.stop()

    def test_stop_releases_port(self):
        port = mail_sink.bound_port()
        ok, _ = mail_sink.stop()
        self.assertTrue(ok)
        self.assertFalse(mail_sink.running())
        self.assertEqual(mail_sink.bound_port(), 0)
        ok, msg = mail_sink.start(port)  # 端口已释放，可再次绑定
        self.assertTrue(ok, msg)


class SmtpIniTest(unittest.TestCase):
    """Windows 分支：把 ini 的 SMTP/smtp_port 指向 sink，并可完整还原。"""

    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ini = os.path.join(self.dir.name, "php.ini")
        self.v = types.SimpleNamespace(name="php85", ini=self.ini)
        for attr, val in (("IS_WIN", True), ("mail_dir", lambda: self.dir.name)):
            p = mock.patch.object(mail_catcher, attr, val)
            p.start()
            self.addCleanup(p.stop)
        # sink 打桩：本组用例只关心 ini 写入，不真的监听端口
        for attr, val in (("start", lambda *a, **k: (True, "ok")),
                          ("running", lambda *a, **k: True)):
            p = mock.patch.object(mail_sink, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        _i18n.set_language(self._lang)

    def _write(self, text: str) -> None:
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write(text)

    def _body(self) -> str:
        with open(self.ini, encoding="utf-8") as f:
            return f.read()

    def test_mode_is_smtp_on_windows(self):
        self.assertEqual(mail_catcher.mode(), "smtp")
        self.assertTrue(mail_catcher.supported())

    def test_enable_writes_smtp_block_and_backup(self):
        self._write("SMTP = mail.example.com\nsmtp_port = 25\nmemory_limit = 128M\n")
        ok, msg, backup = mail_catcher.enable(self.v)
        self.assertTrue(ok, msg)
        self.assertTrue(backup and os.path.exists(backup), "改前必须备份")
        body = self._body()
        self.assertIn(mail_catcher.SMTP_MARK, body)
        self.assertEqual(mail_catcher.active_smtp(self.ini),
                         ("127.0.0.1", str(mail_catcher.smtp_port())),
                         "生效中的 SMTP 必须指向本机 sink")
        self.assertTrue(mail_catcher.status(self.v)["enabled"])
        # 幂等：重复开启不应叠加标记块
        mail_catcher.enable(self.v)
        self.assertEqual(self._body().count(mail_catcher.SMTP_MARK), 1)

    def test_enable_writes_default_sender(self):
        """Windows 的 mail() 没有默认发件人会报 "Bad Message Return Path"。"""
        self._write("memory_limit = 128M\n")
        ok, msg, _ = mail_catcher.enable(self.v)
        self.assertTrue(ok, msg)
        self.assertIn("sendmail_from = dev@phpvm.local", self._body())

    def test_disable_restores_original_sender(self):
        self._write("sendmail_from = ops@corp.test\n")
        mail_catcher.enable(self.v)
        mail_catcher.disable(self.v)
        self.assertIn("sendmail_from = ops@corp.test", self._body())
        self.assertNotIn("dev@phpvm.local", self._body())

    def test_disable_restores_original_smtp(self):
        self._write("SMTP = mail.example.com\nsmtp_port = 2525\n")
        mail_catcher.enable(self.v)
        self.assertIn(mail_catcher.ORIG_MARK, self._body())
        ok, msg, _ = mail_catcher.disable(self.v)
        self.assertTrue(ok, msg)
        body = self._body()
        self.assertIn("SMTP = mail.example.com", body)
        self.assertIn("smtp_port = 2525", body)
        self.assertNotIn(mail_catcher.SMTP_MARK, body)
        self.assertFalse(mail_catcher.status(self.v)["enabled"])

    def test_disable_without_original_removes_our_lines(self):
        self._write("memory_limit = 128M\n")
        mail_catcher.enable(self.v)
        mail_catcher.disable(self.v)
        body = self._body()
        self.assertNotIn("SMTP_MARK", body)
        self.assertNotIn("smtp_port", body)
        self.assertIn("memory_limit = 128M", body)

    def test_install_shim_refused_on_windows(self):
        ok, msg = mail_catcher.install_shim()
        self.assertFalse(ok)
        self.assertIn("Unix", msg)

    def test_ensure_sink_noop_without_enabled_version(self):
        self._write("memory_limit = 128M\n")
        with mock.patch.object(mail_sink, "running", lambda: False), \
                mock.patch.object(mail_sink, "start") as start:
            self.assertIsNone(mail_catcher.ensure_sink([self.v]))
        start.assert_not_called()

    def test_ensure_sink_starts_when_a_version_is_enabled(self):
        self._write("memory_limit = 128M\n")
        mail_catcher.enable(self.v)
        with mock.patch.object(mail_sink, "running", lambda: False), \
                mock.patch.object(mail_sink, "start", lambda port: (True, "started")):
            self.assertEqual(mail_catcher.ensure_sink([self.v]), (True, "started"))


if __name__ == "__main__":
    unittest.main()
