# -*- coding: utf-8 -*-
"""`cli.py ssh` 命令组：参数解析、副作用门控（--dry-run / --yes）、凭据边界。

断言的是「命令函数产出的 Result」与「是否真的落盘 / 真的连远端」，
不涉及 `main()` 的 JSON 序列化（那部分由其它测试与 `schema` 守护）。
"""
import inspect
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import cli
from core import backup_bundle, ssh_manager, ssh_store
from tests.fakes import FakeConfig


class SshCliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.path = os.path.join(self.root, "ssh_hosts.json")
        env = mock.patch.dict(os.environ, {
            "PHPVM_SSH_HOSTS": self.path,
            "PHPVM_SSH_KNOWN_HOSTS": os.path.join(self.root, "ssh_known_hosts"),
            # 默认私钥目录指向空目录：别让本机 ~/.ssh 影响断言
            "PHPVM_SSH_KEY_DIR": os.path.join(self.root, "ssh_keys"),
        })
        env.start()
        self.addCleanup(env.stop)
        # 标记「已播种」：内置运维片段只在首次使用时写入，别让它干扰片段断言；
        # 播种行为本身由 test_snippet_list_seeds_builtin_snippets_on_first_use 守。
        self.cfg = FakeConfig(
            settings={"ssh_snippets_builtin": ssh_store.BUILTIN_SNIPPETS_VERSION})
        patches = [
            mock.patch.object(cli, "Config", return_value=self.cfg),
            mock.patch.object(ssh_store, "set_secret_permissions", return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.key = os.path.join(self.root, "id_test")
        with open(self.key, "w", encoding="utf-8") as f:
            f.write("KEY")

    # ---------------- helpers ----------------
    def run_cli(self, argv, stdin_text=""):
        ns = cli.build_parser().parse_args(argv)
        if stdin_text:
            stdin = mock.patch.object(sys, "stdin", io.StringIO(stdin_text))
            stdin.start()
            self.addCleanup(stdin.stop)
        result = cli.Result()
        ns.func(ns, result)
        return result

    def seed_host(self, **over):
        raw = {"name": "demo", "host": "10.0.0.8", "port": 22, "user": "deploy",
               "auth": "password", "password": "s3cret", "tags": "prod"}
        raw.update(over)
        host, err = ssh_store.add_host(raw)
        self.assertEqual(err, "")
        return host

    def _default_key(self, name: str) -> str:
        """在隔离的默认私钥目录里放一把钥匙，返回其路径。"""
        base = os.environ["PHPVM_SSH_KEY_DIR"]
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("KEY")
        return path

    def seed_snippet(self, host=None, command="df -h", name="磁盘"):
        snip, err = ssh_store.add_snippet(
            self.cfg, {"name": name, "command": command,
                       "host_ids": [host["id"]] if host else []})
        self.assertEqual(err, "")
        return snip

    # ---------------- 解析 ----------------
    def test_parser_fills_func_and_command(self):
        cases = {
            ("ssh", "host", "list"): "ssh.host.list",
            ("ssh", "host", "add", "--name", "n", "--host", "h",
             "--user", "u"): "ssh.host.add",
            ("ssh", "host", "test", "demo"): "ssh.host.test",
            ("ssh", "host", "connect", "demo"): "ssh.host.connect",
            ("ssh", "snippet", "list"): "ssh.snippet.list",
            ("ssh", "snippet", "run", "x", "--host", "demo"): "ssh.snippet.run",
        }
        for argv, expected in cases.items():
            with self.subTest(argv):
                ns = cli.build_parser().parse_args(list(argv))
                self.assertEqual(ns.command, expected)
                self.assertTrue(callable(ns.func))

    def test_command_is_not_clobbered_by_command_text(self):
        ns = cli.build_parser().parse_args(
            ["ssh", "snippet", "add", "--name", "a", "--command-text", "ls"])
        self.assertEqual(ns.command, "ssh.snippet.add")
        self.assertEqual(ns.command_text, "ls")

    def test_invalid_choice_exits_with_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.build_parser().parse_args(["ssh", "host", "add", "--auth", "token"])
        self.assertEqual(ctx.exception.code, 2)

    # ---------------- 主机 ----------------
    def test_host_add_dry_run_does_not_write(self):
        res = self.run_cli(
            ["ssh", "host", "add", "--name", "demo", "--host", "10.0.0.8",
             "--user", "deploy", "--password-stdin", "--dry-run"],
            stdin_text="pw\n")
        self.assertTrue(res.ok)
        self.assertTrue(res.data["dry_run"])
        self.assertTrue(res.data["host"]["has_password"])
        self.assertNotIn("pw", json.dumps(res.data, ensure_ascii=False))
        self.assertFalse(os.path.exists(self.path))

    def test_host_add_reads_password_from_stdin(self):
        res = self.run_cli(
            ["ssh", "host", "add", "--name", "demo", "--host", "10.0.0.8",
             "--user", "deploy", "--password-stdin"], stdin_text="topsecret\n")
        self.assertTrue(res.ok)
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["hosts"][0]["password"], "topsecret")
        # Result 只暴露脱敏副本；密码也不会混进 config.json / 片段
        self.assertNotIn("topsecret", json.dumps(res.data, ensure_ascii=False))
        self.assertNotIn("topsecret", json.dumps(self.cfg.settings, ensure_ascii=False))

    def test_host_add_without_password_still_saves(self):
        """连不上 / 还没拿到密码：点确定就存下来，只提示「连接前补齐」。"""
        res = self.run_cli(
            ["ssh", "host", "add", "--name", "demo", "--host", "10.0.0.8",
             "--user", "deploy"])
        self.assertTrue(res.ok)
        self.assertFalse(res.data["host"]["credential_ready"])
        self.assertTrue(any("密码或私钥" in note for note in res.notes))
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["hosts"][0]["name"], "demo")
        self.assertEqual(raw["hosts"][0]["password"], "")

    def test_host_add_with_key_path_but_not_ready_is_saved(self):
        # 默认私钥目录在 setUp 里指向空目录：找不到就照旧「先存后补」
        res = self.run_cli(
            ["ssh", "host", "add", "--name", "demo", "--host", "10.0.0.8",
             "--user", "deploy", "--auth", "key"])
        self.assertTrue(res.ok)
        self.assertEqual(res.data["host"]["key_path"], "")
        self.assertFalse(res.data["host"]["credential_ready"])
        self.assertEqual(len(ssh_store.load_hosts()), 1)

    def test_host_add_key_without_path_uses_default_key(self):
        """没选私钥时自动选用本机 ~/.ssh 的常用私钥，并说明用了哪个。"""
        key = self._default_key("id_ed25519")
        res = self.run_cli(
            ["ssh", "host", "add", "--name", "demo", "--host", "10.0.0.8",
             "--user", "deploy", "--auth", "key"])
        self.assertTrue(res.ok)
        self.assertEqual(os.path.normcase(res.data["host"]["key_path"]),
                         os.path.normcase(key))
        self.assertTrue(res.data["host"]["credential_ready"])
        self.assertTrue(any("自动选用" in note for note in res.notes))

    def test_host_add_key_autofill_dry_run_reports_path(self):
        key = self._default_key("id_rsa")
        res = self.run_cli(
            ["ssh", "host", "add", "--name", "demo", "--host", "10.0.0.8",
             "--user", "deploy", "--auth", "key", "--dry-run"])
        self.assertTrue(res.ok)
        self.assertEqual(os.path.normcase(res.data["host"]["key_path"]),
                         os.path.normcase(key))
        self.assertFalse(os.path.exists(self.path))

    def test_host_update_to_key_auth_autofills_default(self):
        self.seed_host()
        key = self._default_key("id_ed25519")
        res = self.run_cli(["ssh", "host", "update", "demo", "--auth", "key"])
        self.assertTrue(res.ok)
        self.assertEqual(os.path.normcase(res.data["host"]["key_path"]),
                         os.path.normcase(key))
        self.assertTrue(res.data["host"]["credential_ready"])

    def test_host_update_other_fields_keeps_existing_path(self):
        """只改备注不去补默认私钥，尊重用户已有的（含空）选择。"""
        self.seed_host(auth="key", password="")
        self._default_key("id_ed25519")
        res = self.run_cli(["ssh", "host", "update", "demo", "--note", "备注"])
        self.assertTrue(res.ok)
        self.assertEqual(res.data["host"]["key_path"], "")

    def test_host_update_can_clear_password_without_failing(self):
        host = self.seed_host()
        res = self.run_cli(["ssh", "host", "update", "demo", "--note", "备注"])
        self.assertTrue(res.ok)
        self.assertTrue(res.data["host"]["credential_ready"])
        self.assertFalse(any("密码或私钥" in note for note in res.notes))
        self.assertEqual(ssh_store.find_host(ssh_store.load_hosts(), host["id"])
                         ["password"], "s3cret")

    def test_host_add_rejects_bad_port(self):
        res = self.run_cli(
            ["ssh", "host", "add", "--name", "demo", "--host", "10.0.0.8",
             "--user", "deploy", "--port", "0", "--password-stdin"],
            stdin_text="pw\n")
        self.assertFalse(res.ok)
        self.assertIn("端口", res.data["error"])
        self.assertFalse(os.path.exists(self.path))

    def test_host_add_rejects_unknown_key_file(self):
        res = self.run_cli(
            ["ssh", "host", "add", "--name", "demo", "--host", "10.0.0.8",
             "--user", "deploy", "--auth", "key",
             "--key-path", os.path.join(self.root, "missing")])
        self.assertFalse(res.ok)
        self.assertIn("私钥", res.data["error"])

    def test_host_list_returns_public_view(self):
        self.seed_host()
        res = self.run_cli(["ssh", "host", "list"])
        self.assertTrue(res.ok)
        self.assertEqual(res.data["count"], 1)
        self.assertNotIn("password", res.data["hosts"][0])
        self.assertTrue(res.data["hosts"][0]["has_password"])

    def test_host_list_filter_by_tag(self):
        self.seed_host(tags="prod")
        self.seed_host(name="dev-box", host="10.0.0.9", tags="dev")
        res = self.run_cli(["ssh", "host", "list", "--tag", "dev"])
        self.assertEqual([h["name"] for h in res.data["hosts"]], ["dev-box"])

    def test_host_remove_requires_yes(self):
        self.seed_host()
        res = self.run_cli(["ssh", "host", "remove", "demo"])
        self.assertFalse(res.ok)
        self.assertIn("--yes", res.data["error"])
        self.assertEqual(len(ssh_store.load_hosts()), 1)

    def test_host_remove_dry_run_keeps_host(self):
        self.seed_host()
        res = self.run_cli(["ssh", "host", "remove", "demo", "--yes", "--dry-run"])
        self.assertTrue(res.ok)
        self.assertTrue(res.data["dry_run"])
        self.assertEqual(len(ssh_store.load_hosts()), 1)

    def test_host_remove_deletes(self):
        self.seed_host()
        res = self.run_cli(["ssh", "host", "remove", "demo", "--yes"])
        self.assertTrue(res.ok)
        self.assertEqual(ssh_store.load_hosts(), [])

    def test_host_update_reports_secret_as_changed_flag(self):
        self.seed_host()
        res = self.run_cli(
            ["ssh", "host", "update", "demo", "--note", "新备注",
             "--password-stdin", "--dry-run"], stdin_text="newpw\n")
        self.assertTrue(res.ok)
        self.assertTrue(res.data["patch"]["password_changed"])
        self.assertNotIn("newpw", json.dumps(res.data, ensure_ascii=False))

    def test_host_update_without_fields_fails(self):
        self.seed_host()
        res = self.run_cli(["ssh", "host", "update", "demo"])
        self.assertFalse(res.ok)
        self.assertIn("没有要更新", res.data["error"])

    def test_unknown_host_reports_available_names(self):
        self.seed_host()
        res = self.run_cli(["ssh", "host", "test", "nope"])
        self.assertFalse(res.ok)
        self.assertEqual(res.data["available"], ["demo"])

    def test_module_disabled_blocks_commands(self):
        self.cfg.settings["disabled_modules"] = ["ssh"]
        res = self.run_cli(["ssh", "host", "list"])
        self.assertFalse(res.ok)
        self.assertIn("停用", res.data["error"])

    # ---------------- 连接 ----------------
    def test_host_test_untrusted_key_hints_trust_host(self):
        self.seed_host()
        fake = {"ok": False, "target": "deploy@10.0.0.8:22", "latency_ms": 0,
                "banner": "", "server": "", "fingerprint": "SHA256:abc",
                "key_type": "ssh-ed25519", "trusted": False,
                "error": "主机指纹尚未信任", "error_kind": "host_key"}
        with mock.patch.object(ssh_manager, "test_connection", return_value=fake) as call:
            res = self.run_cli(["ssh", "host", "test", "demo", "--trust-host"])
        self.assertFalse(res.ok)
        self.assertEqual(res.data["error_kind"], "host_key")
        self.assertTrue(any("--trust-host" in note for note in res.notes))
        self.assertTrue(call.call_args.kwargs["trust"])

    def test_host_connect_dry_run_does_not_open_terminal(self):
        self.seed_host()
        with mock.patch.object(ssh_manager, "open_terminal") as opener:
            res = self.run_cli(["ssh", "host", "connect", "demo", "--dry-run"])
        self.assertTrue(res.ok)
        self.assertIn("ssh", res.data["command"])
        self.assertNotIn("s3cret", res.data["command"])
        opener.assert_not_called()

    def test_host_connect_password_auth_notes_terminal_input(self):
        self.seed_host()
        with mock.patch.object(ssh_manager, "open_terminal", return_value=True):
            res = self.run_cli(["ssh", "host", "connect", "demo"])
        self.assertTrue(res.ok)
        self.assertTrue(any("终端" in note for note in res.notes))

    # ---------------- 片段 ----------------
    def test_snippet_add_rejects_unknown_host(self):
        res = self.run_cli(["ssh", "snippet", "add", "--name", "a",
                            "--command-text", "ls", "--host", "ghost"])
        self.assertFalse(res.ok)
        self.assertIn("未找到主机", res.data["error"])

    def test_snippet_add_links_host_and_hides_from_other_hosts(self):
        host = self.seed_host()
        res = self.run_cli(["ssh", "snippet", "add", "--name", "磁盘",
                            "--command-text", "df -h", "--host", host["name"]])
        self.assertTrue(res.ok)
        self.assertEqual(res.data["snippet"]["host_ids"], [host["id"]])
        self.assertEqual(len(ssh_store.load_snippets(self.cfg)), 1)
        # 片段不是凭据，存配置（随备份带走），但主机文件里没有它
        with open(self.path, "r", encoding="utf-8") as f:
            self.assertNotIn("df -h", f.read())

    def test_snippet_list_filters_by_host(self):
        host = self.seed_host()
        other = self.seed_host(name="other", host="10.0.0.9")
        self.seed_snippet(host)
        self.seed_snippet(other, command="uptime", name="负载")
        res = self.run_cli(["ssh", "snippet", "list", "--host", "demo"])
        self.assertEqual([s["name"] for s in res.data["snippets"]], ["磁盘"])

    def test_snippet_list_seeds_builtin_snippets_on_first_use(self):
        """第一次看片段表就有一批常用运维命令，且只提示一次。"""
        fresh = FakeConfig()
        with mock.patch.object(cli, "Config", return_value=fresh):
            res = self.run_cli(["ssh", "snippet", "list"])
            again = self.run_cli(["ssh", "snippet", "list"])
        self.assertTrue(res.ok)
        expected = len(ssh_store.builtin_snippets())
        self.assertEqual(res.data["count"], expected)
        self.assertIn("系统负载与运行时长",
                      [s["name"] for s in res.data["snippets"]])
        self.assertEqual(fresh.get_setting(ssh_store.BUILTIN_SEED_KEY),
                         ssh_store.BUILTIN_SNIPPETS_VERSION)
        self.assertTrue(any("已预置" in n for n in res.notes))
        self.assertEqual(again.data["count"], expected)
        self.assertFalse(any("已预置" in n for n in again.notes))

    def test_snippet_run_requires_yes(self):
        host = self.seed_host()
        snip = self.seed_snippet(host)
        with mock.patch.object(ssh_manager, "exec_command") as runner:
            res = self.run_cli(["ssh", "snippet", "run", snip["id"],
                                "--host", "demo"])
        self.assertFalse(res.ok)
        self.assertIn("--yes", res.data["error"])
        self.assertEqual(res.data["target"], "deploy@10.0.0.8:22")
        runner.assert_not_called()

    def test_snippet_run_dry_run_does_not_execute(self):
        host = self.seed_host()
        snip = self.seed_snippet(host)
        with mock.patch.object(ssh_manager, "exec_command") as runner:
            res = self.run_cli(["ssh", "snippet", "run", snip["id"],
                                "--host", "demo", "--dry-run"])
        self.assertTrue(res.ok)
        self.assertEqual(res.data["command"], "df -h")
        runner.assert_not_called()

    def test_snippet_run_with_yes_executes(self):
        host = self.seed_host()
        snip = self.seed_snippet(host)
        fake = {"ok": True, "target": "deploy@10.0.0.8:22", "command": "df -h",
                "stdout": "Filesystem\n", "stderr": "", "exit_status": 0,
                "duration_ms": 12, "truncated": False, "error": "",
                "error_kind": ""}
        with mock.patch.object(ssh_manager, "exec_command",
                               return_value=fake) as runner:
            res = self.run_cli(["ssh", "snippet", "run", snip["id"],
                                "--host", "demo", "--yes", "--trust-host"])
        self.assertTrue(res.ok)
        self.assertEqual(res.data["exit_status"], 0)
        self.assertIn("Filesystem", res.lines[0])
        self.assertEqual(runner.call_args.args[1], "df -h")
        self.assertTrue(runner.call_args.kwargs["trust"])

    def test_snippet_run_failure_is_not_ok(self):
        host = self.seed_host()
        snip = self.seed_snippet(host)
        fake = {"ok": False, "target": "deploy@10.0.0.8:22", "command": "df -h",
                "stdout": "", "stderr": "boom", "exit_status": 1,
                "duration_ms": 5, "truncated": False,
                "error": "", "error_kind": ""}
        with mock.patch.object(ssh_manager, "exec_command", return_value=fake):
            res = self.run_cli(["ssh", "snippet", "run", snip["id"],
                                "--host", "demo", "--yes"])
        self.assertFalse(res.ok)
        self.assertTrue(any("退出码为 1" in line for line in res.lines))

    def test_snippet_remove_requires_yes(self):
        host = self.seed_host()
        snip = self.seed_snippet(host)
        res = self.run_cli(["ssh", "snippet", "remove", snip["id"]])
        self.assertFalse(res.ok)
        self.assertEqual(len(ssh_store.load_snippets(self.cfg)), 1)


class CredentialBoundaryTest(unittest.TestCase):
    def test_backup_bundle_never_touches_ssh_credentials(self):
        source = inspect.getsource(backup_bundle)
        self.assertNotIn("ssh_hosts", source)
        self.assertNotIn("ssh_known_hosts", source)


if __name__ == "__main__":
    unittest.main()
