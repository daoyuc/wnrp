# -*- coding: utf-8 -*-
"""SSH 传输层：认证参数、指纹策略、输出限流与依赖缺失降级（不连真实主机）。

paramiko 全程用假模块 / 假 client 替身，测试不依赖本机是否安装 paramiko。
"""
import base64
import hashlib
import os
import socket
import tempfile
import types
import unittest
from unittest import mock

from core import i18n as _i18n, ssh_manager, ssh_store


class FakeKey:
    def __init__(self, name: str = "ssh-ed25519", blob: bytes = b"server-key"):
        self._name = name
        self._blob = blob

    def get_name(self) -> str:
        return self._name

    def asbytes(self) -> bytes:
        return self._blob


class FakeTransport:
    def __init__(self, key: FakeKey | None = None,
                 version: str = "SSH-2.0-OpenSSH_9.6"):
        self.remote_version = version
        self._key = key

    def get_remote_server_key(self):
        return self._key


class FakeChannel:
    """有限缓冲：先吐完 stdout / stderr，再报退出码。"""

    def __init__(self, out: bytes = b"", err: bytes = b"", status: int = 0):
        self._out, self._err, self._status = out, err, status

    def recv_ready(self) -> bool:
        return bool(self._out)

    def recv(self, n: int) -> bytes:
        chunk, self._out = self._out[:n], self._out[n:]
        return chunk

    def recv_stderr_ready(self) -> bool:
        return bool(self._err)

    def recv_stderr(self, n: int) -> bytes:
        chunk, self._err = self._err[:n], self._err[n:]
        return chunk

    def exit_status_ready(self) -> bool:
        return True

    def recv_exit_status(self) -> int:
        return self._status

    @property
    def channel(self):
        """paramiko 的 stdout / stderr 是 ChannelFile，靠 ``.channel`` 取通道。"""
        return self


class HangingChannel(FakeChannel):
    """永不返回退出码，用于触发超时路径。"""

    def exit_status_ready(self) -> bool:
        return False


class FakeHostKeys:
    def __init__(self, known: bool = False):
        self.known = known

    def lookup(self, name):  # noqa: ARG002
        return {"ssh-ed25519": FakeKey()} if self.known else None


class FakeClient:
    def __init__(self, *, known: bool = False, key: FakeKey | None = None,
                 channel: FakeChannel | None = None,
                 connect_error: Exception | None = None):
        self.host_keys = FakeHostKeys(known)
        self.key = key or FakeKey()
        self.channel = channel or FakeChannel(out=b"ok\n")
        self.connect_error = connect_error
        self.kwargs = None
        self.policy = None
        self.loaded_paths: list[str] = []
        self.saved_path = None
        self.closed = False
        self.command = ""
        self.timeout = None

    def load_host_keys(self, path):
        self.loaded_paths.append(path)

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def get_host_keys(self):
        return self.host_keys

    def connect(self, **kwargs):
        self.kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error

    def save_host_keys(self, path):
        self.saved_path = path

    def get_transport(self):
        return FakeTransport(self.key)

    def exec_command(self, command, timeout=None):
        self.command, self.timeout = command, timeout
        return (None, self.channel, self.channel)

    def close(self):
        self.closed = True


class _Base(unittest.TestCase):
    def setUp(self):
        # 错误分类与降级提示断言中文：显式切 zh_CN，避免英文 locale 下环境性失败
        lang = _i18n.current_language()
        _i18n.set_language("zh_CN")
        self.addCleanup(_i18n.set_language, lang)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.known = os.path.join(self.root, "ssh_known_hosts")
        env = mock.patch.dict(os.environ, {"PHPVM_SSH_KNOWN_HOSTS": self.known})
        env.start()
        self.addCleanup(env.stop)
        perm = mock.patch.object(ssh_store, "set_secret_permissions", return_value=True)
        self.perm = perm.start()
        self.addCleanup(perm.stop)
        self.key_path = os.path.join(self.root, "id_test")
        with open(self.key_path, "w", encoding="utf-8") as f:
            f.write("KEY")

    def host(self, **over) -> dict:
        raw = {"name": "demo", "host": "10.0.0.8", "port": 22, "user": "deploy",
               "auth": "password", "password": "s3cret", "key_path": "",
               "passphrase": "", "tags": [], "note": ""}
        raw.update(over)
        return raw


class ConnectKwargsTest(_Base):
    def test_password_auth(self):
        kw = ssh_manager._connect_kwargs(self.host(), 5.0)
        self.assertEqual(kw["password"], "s3cret")
        self.assertNotIn("key_filename", kw)
        self.assertFalse(kw["look_for_keys"])
        self.assertFalse(kw["allow_agent"])

    def test_key_auth_with_passphrase(self):
        kw = ssh_manager._connect_kwargs(
            self.host(auth="key", key_path=self.key_path, passphrase="pp"), 5.0)
        self.assertEqual(kw["key_filename"], self.key_path)
        self.assertEqual(kw["passphrase"], "pp")
        self.assertNotIn("password", kw)

    def test_key_auth_without_passphrase_omits_it(self):
        kw = ssh_manager._connect_kwargs(
            self.host(auth="key", key_path=self.key_path), 5.0)
        self.assertNotIn("passphrase", kw)

    def test_port_falls_back_to_22(self):
        kw = ssh_manager._connect_kwargs(self.host(port=0), 3.0)
        self.assertEqual(kw["port"], 22)
        self.assertEqual(kw["timeout"], 3.0)


class PolicyTest(_Base):
    def test_reject_policy_raises_with_fingerprint(self):
        key = FakeKey()
        with self.assertRaises(ssh_manager.HostKeyNotTrusted) as ctx:
            ssh_manager._RejectWithKey().missing_host_key(None, "10.0.0.8", key)
        exc = ctx.exception
        self.assertEqual(exc.key_type, "ssh-ed25519")
        self.assertTrue(exc.fingerprint.startswith("SHA256:"))
        self.assertEqual(exc.fingerprint, ssh_manager.fingerprint(key))

    def test_fingerprint_is_sha256_without_padding(self):
        key = FakeKey(blob=b"abc")
        digest = base64.b64encode(hashlib.sha256(b"abc").digest()).decode()
        self.assertEqual(ssh_manager.fingerprint(key), "SHA256:" + digest.rstrip("="))

    def test_host_key_name_for_non_default_port(self):
        self.assertEqual(ssh_manager._host_key_name(self.host()), "10.0.0.8")
        self.assertEqual(ssh_manager._host_key_name(self.host(port=2222)),
                         "[10.0.0.8]:2222")

    def test_new_client_policy_follows_trust(self):
        fake_paramiko = types.SimpleNamespace(
            SSHClient=FakeClient, AutoAddPolicy=lambda: "AUTO")
        with open(self.known, "w", encoding="utf-8") as f:
            f.write("")
        with mock.patch.object(ssh_manager, "_paramiko", return_value=fake_paramiko):
            client = ssh_manager._new_client(False)
            self.assertIsInstance(client.policy, ssh_manager._RejectWithKey)
            self.assertEqual(client.loaded_paths, [self.known])
            trusting = ssh_manager._new_client(True)
            self.assertEqual(trusting.policy, "AUTO")

    def test_target_label(self):
        self.assertEqual(ssh_manager.target_label(self.host()), "deploy@10.0.0.8:22")


class TestConnectionTest(_Base):
    def test_success_reports_banner_and_fingerprint(self):
        key = FakeKey()
        client = FakeClient(key=key)
        with mock.patch.object(ssh_manager, "_new_client", return_value=client):
            res = ssh_manager.test_connection(self.host())
        self.assertTrue(res["ok"])
        self.assertTrue(res["trusted"])
        self.assertEqual(res["server"], "SSH-2.0-OpenSSH_9.6")
        self.assertEqual(res["fingerprint"], ssh_manager.fingerprint(key))
        self.assertEqual(res["key_type"], "ssh-ed25519")
        self.assertGreaterEqual(res["latency_ms"], 0)
        self.assertTrue(client.closed)

    def test_untrusted_host_key_is_reported(self):
        exc = ssh_manager.HostKeyNotTrusted("10.0.0.8", "ssh-ed25519", "SHA256:xyz")
        client = FakeClient(connect_error=exc)
        with mock.patch.object(ssh_manager, "_new_client", return_value=client):
            res = ssh_manager.test_connection(self.host())
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_kind"], "host_key")
        self.assertEqual(res["fingerprint"], "SHA256:xyz")
        self.assertIn("指纹", res["error"])
        self.assertTrue(client.closed)

    def test_missing_dependency_degrades(self):
        with mock.patch.object(ssh_manager, "_new_client",
                               side_effect=ssh_manager.ParamikoMissing()):
            res = ssh_manager.test_connection(self.host())
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_kind"], "missing_dependency")
        self.assertIn(ssh_manager.install_hint(), res["error"])

    def test_network_error_is_classified(self):
        client = FakeClient(connect_error=socket.timeout("timed out"))
        with mock.patch.object(ssh_manager, "_new_client", return_value=client):
            res = ssh_manager.test_connection(self.host(), timeout=3.0)
        self.assertEqual(res["error_kind"], "timeout")
        self.assertIn("3", res["error"])

    def test_missing_credentials_reported_before_dialing(self):
        """没有凭据时不去建链，直接给「还缺什么」的可读错误。"""
        cases = {
            "无密码": (self.host(password=""), "密码"),
            "无私钥": (self.host(auth="key", key_path=""), "私钥"),
        }
        for label, (host, hint) in cases.items():
            with self.subTest(label):
                with mock.patch.object(ssh_manager, "_new_client") as new_client:
                    res = ssh_manager.test_connection(host)
                new_client.assert_not_called()
                self.assertFalse(res["ok"])
                self.assertEqual(res["error_kind"], "credential")
                self.assertIn(hint, res["error"])

    def test_missing_credentials_blocks_exec(self):
        with mock.patch.object(ssh_manager, "_new_client") as new_client:
            res = ssh_manager.exec_command(self.host(password=""), "uptime")
        new_client.assert_not_called()
        self.assertEqual(res["error_kind"], "credential")
        self.assertEqual(res["exit_status"], -1)

    def test_available_and_hint(self):
        self.assertIn("pip install", ssh_manager.install_hint())
        with mock.patch.object(ssh_manager, "_paramiko",
                               side_effect=ssh_manager.ParamikoMissing()):
            self.assertFalse(ssh_manager.available())

    def test_classify_error_kinds(self):
        class AuthError(Exception):
            pass

        fake = types.SimpleNamespace(AuthenticationException=AuthError,
                                     BadHostKeyException=ValueError)
        with mock.patch.object(ssh_manager, "_paramiko", return_value=fake):
            self.assertEqual(ssh_manager._classify(AuthError("no")), "auth")
            self.assertEqual(ssh_manager._classify(ValueError("hmm")), "host_key")
        self.assertEqual(ssh_manager._classify(OSError("down")), "network")


class KnownHostsWriteTest(_Base):
    def test_trust_saves_new_fingerprint(self):
        client = FakeClient()
        with mock.patch.object(ssh_manager, "_new_client", return_value=client):
            ssh_manager._connect(self.host(), trust=True)
        self.assertEqual(client.saved_path, self.known)
        self.assertTrue(self.perm.called)

    def test_trust_does_not_rewrite_known_host(self):
        client = FakeClient(known=True)
        with mock.patch.object(ssh_manager, "_new_client", return_value=client):
            ssh_manager._connect(self.host(), trust=True)
        self.assertIsNone(client.saved_path)

    def test_no_trust_never_saves(self):
        client = FakeClient()
        with mock.patch.object(ssh_manager, "_new_client", return_value=client):
            ssh_manager._connect(self.host(), trust=False)
        self.assertIsNone(client.saved_path)

    def test_connect_failure_closes_client(self):
        client = FakeClient(connect_error=OSError("refused"))
        with mock.patch.object(ssh_manager, "_new_client", return_value=client):
            with self.assertRaises(OSError):
                ssh_manager._connect(self.host())
        self.assertTrue(client.closed)


class ExecCommandTest(_Base):
    def test_success_captures_streams(self):
        client = FakeClient(channel=FakeChannel(out=b"hello\n", err=b"warn\n"))
        with mock.patch.object(ssh_manager, "_new_client", return_value=client):
            res = ssh_manager.exec_command(self.host(), "uptime")
        self.assertTrue(res["ok"])
        self.assertEqual(res["stdout"], "hello\n")
        self.assertEqual(res["stderr"], "warn\n")
        self.assertEqual(res["exit_status"], 0)
        self.assertFalse(res["truncated"])
        self.assertEqual(client.command, "uptime")
        self.assertTrue(client.closed)

    def test_output_is_truncated_at_limit(self):
        client = FakeClient(channel=FakeChannel(out=b"x" * 500))
        with mock.patch.object(ssh_manager, "_new_client", return_value=client):
            res = ssh_manager.exec_command(self.host(), "cat big.log", max_bytes=100)
        self.assertTrue(res["truncated"])
        self.assertEqual(len(res["stdout"]), 100)

    def test_non_zero_exit_is_failure(self):
        client = FakeClient(channel=FakeChannel(out=b"", err=b"boom", status=2))
        with mock.patch.object(ssh_manager, "_new_client", return_value=client):
            res = ssh_manager.exec_command(self.host(), "false")
        self.assertFalse(res["ok"])
        self.assertEqual(res["exit_status"], 2)

    def test_non_zero_exit_without_stderr_sets_error(self):
        client = FakeClient(channel=FakeChannel(status=7))
        with mock.patch.object(ssh_manager, "_new_client", return_value=client):
            res = ssh_manager.exec_command(self.host(), "exit 7")
        self.assertEqual(res["error_kind"], "remote")
        self.assertIn("7", res["error"])

    def test_timeout_marks_truncated(self):
        class Clock:
            def __init__(self):
                self.now = 0.0

            def time(self):
                self.now += 5.0
                return self.now

            def sleep(self, _seconds):
                pass

        client = FakeClient(channel=HangingChannel())
        with mock.patch.object(ssh_manager, "_new_client", return_value=client), \
                mock.patch.object(ssh_manager, "time", Clock()):
            res = ssh_manager.exec_command(self.host(), "sleep 999", timeout=1.0)
        self.assertTrue(res["truncated"])
        self.assertEqual(res["exit_status"], -1)
        self.assertFalse(res["ok"])


class TerminalCommandTest(_Base):
    def test_argv_never_contains_password(self):
        host = self.host(port=2222)
        argv = ssh_manager.build_ssh_argv(host)
        self.assertEqual(argv[0], "ssh")
        self.assertIn("-p", argv)
        self.assertIn("2222", argv)
        self.assertIn("deploy@10.0.0.8", argv)
        self.assertNotIn("s3cret", " ".join(argv))
        self.assertTrue(any(a.startswith("UserKnownHostsFile=") for a in argv))

    def test_key_auth_adds_identity_file(self):
        argv = ssh_manager.build_ssh_argv(
            self.host(auth="key", key_path=self.key_path))
        self.assertIn("-i", argv)
        self.assertIn(self.key_path, argv)

    def test_terminal_command_hides_password(self):
        cmd = ssh_manager.terminal_command(self.host())
        self.assertIn("ssh", cmd)
        self.assertNotIn("s3cret", cmd)

    def test_open_terminal_passes_command_through(self):
        host = self.host()
        with mock.patch("core.process_utils.open_terminal",
                        return_value=True) as opener:
            self.assertTrue(ssh_manager.open_terminal(host))
        kwargs = opener.call_args.kwargs
        self.assertIn("ssh", kwargs["command"])
        self.assertNotIn("s3cret", kwargs["command"])


if __name__ == "__main__":
    unittest.main()
