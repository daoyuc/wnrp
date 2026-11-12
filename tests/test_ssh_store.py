# -*- coding: utf-8 -*-
"""SSH 主机与命令片段存储：校验、原子写、权限收紧与凭据隔离。

重点守卫三件事：
1. 主机凭据只落在 ``ssh_hosts.json``（0600），``public()`` 与片段配置都不带密码；
2. 写盘先 ``.bak`` 再原子替换，不留临时文件；
3. 片段里的悬空 ``host_ids`` 被忽略，而不是让筛选失灵。
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from core import i18n as _i18n, ssh_store
from tests.fakes import FakeConfig


def _write_key(path: str) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write("-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n")
    return path


class _StoreBase(unittest.TestCase):
    """隔离数据目录：主机文件与 known_hosts 都指向临时目录。"""

    def setUp(self):
        # 校验失败文案断言中文：显式切 zh_CN，避免英文 locale 下环境性失败
        lang = _i18n.current_language()
        _i18n.set_language("zh_CN")
        self.addCleanup(_i18n.set_language, lang)
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
        # 真正收紧权限在 Windows 上要起 icacls，测试里只验证「被调用」；
        # 权限实现本身另有 PermissionsTest 逐分支验证。
        perm = mock.patch.object(ssh_store, "set_secret_permissions", return_value=True)
        self.perm = perm.start()
        self.addCleanup(perm.stop)
        self.cfg = FakeConfig()
        self.key = _write_key(os.path.join(self.root, "id_test"))

    def data(self, **over) -> dict:
        raw = {"name": "demo", "host": "10.0.0.8", "port": 22, "user": "deploy",
               "auth": "password", "password": "s3cret", "tags": "prod, web",
               "note": "n1"}
        raw.update(over)
        return raw

    def add(self, **over) -> dict:
        host, err = ssh_store.add_host(self.data(**over))
        self.assertEqual(err, "")
        self.assertIsNotNone(host)
        return host


class HostStoreTest(_StoreBase):
    def test_hosts_path_honours_env(self):
        self.assertEqual(ssh_store.hosts_path(), self.path)

    def test_add_host_persists_and_hides_secrets(self):
        host = self.add()
        self.assertTrue(host["id"].startswith("host-"))
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["version"], ssh_store.DATA_VERSION)
        self.assertEqual(raw["hosts"][0]["password"], "s3cret")
        shown = ssh_store.public(raw["hosts"][0])
        self.assertNotIn("password", shown)
        self.assertNotIn("passphrase", shown)
        self.assertTrue(shown["has_password"])
        self.assertEqual(shown["tags"], ["prod", "web"])
        self.assertTrue(self.perm.called)
        self.assertIn(self.path, [c.args[0] for c in self.perm.call_args_list])

    def test_save_creates_backup_with_same_permissions(self):
        self.add()
        self.add(name="demo2")
        bak = self.path + ".bak"
        self.assertTrue(os.path.exists(bak))
        self.assertIn(bak, [c.args[0] for c in self.perm.call_args_list])

    def test_atomic_write_leaves_no_temp_file(self):
        self.add()
        leftovers = [n for n in os.listdir(self.root) if n.startswith(".ssh-")]
        self.assertEqual(leftovers, [])

    def test_validate_host_rejects_unsafe_values(self):
        cases = {
            "空名称": self.data(name=" "),
            "过长名称": self.data(name="x" * (ssh_store.MAX_NAME + 1)),
            "空地址": self.data(host=""),
            "地址含空格": self.data(host="10.0.0.8 ; rm -rf /"),
            "地址含分号": self.data(host="example.com;id"),
            "端口非数字": self.data(port="abc"),
            "端口越界": self.data(port=70000),
            "端口为 0": self.data(port=0),
            "空用户": self.data(user=""),
            "用户含元字符": self.data(user="de ploy"),
            "认证方式未知": self.data(auth="token"),
            "私钥不存在": self.data(auth="key",
                                   key_path=os.path.join(self.root, "nope")),
        }
        for label, raw in cases.items():
            with self.subTest(label):
                self.assertTrue(ssh_store.validate_host(raw), f"{label} 应校验失败")
        self.assertIsNone(ssh_store.validate_host(self.data()))
        self.assertIsNone(ssh_store.validate_host(
            self.data(auth="key", key_path=self.key)))

    def test_host_can_be_saved_without_credentials(self):
        """连不上 / 还没拿到密码的主机也要能先登记（保存与连通性解耦）。"""
        cases = {
            "无密码": (self.data(password=""), "password"),
            "无私钥": (self.data(auth="key", key_path=""), "key"),
        }
        for label, (raw, expected) in cases.items():
            with self.subTest(label):
                # 校验与保存都不因缺凭据失败
                self.assertIsNone(ssh_store.validate_host(raw))
                host, err = ssh_store.add_host(raw)
                self.assertEqual(err, "")
                self.assertIsNotNone(host)
                self.assertEqual(ssh_store.missing_secret(host), expected)
                self.assertFalse(ssh_store.public(host)["credential_ready"])
                ssh_store.remove_host(host["name"])

    def test_update_without_password_keeps_host_editable(self):
        """先登记、后补凭据：没有密码的主机也要能继续改其它字段。"""
        host, err = ssh_store.add_host(self.data(password=""))
        self.assertEqual(err, "")
        updated, err = ssh_store.update_host(host["id"], {"note": "先占位"})
        self.assertEqual(err, "")
        self.assertEqual(updated["note"], "先占位")
        self.assertEqual(ssh_store.missing_secret(updated), "password")

    def test_switch_to_key_drops_stored_password(self):
        host = self.add()
        updated, err = ssh_store.update_host(
            host["id"], {"auth": "key", "key_path": self.key})
        self.assertEqual(err, "")
        self.assertEqual(updated["password"], "")
        self.assertEqual(ssh_store.missing_secret(updated), "")

    def test_credential_ready_when_configured(self):
        host = self.add()
        self.assertEqual(ssh_store.missing_secret(host), "")
        self.assertTrue(ssh_store.public(host)["credential_ready"])
        key_host = self.add(name="keybox", auth="key", key_path=self.key)
        self.assertEqual(ssh_store.missing_secret(key_host), "")
        self.assertTrue(ssh_store.public(key_host)["credential_ready"])

    def test_add_host_rejects_duplicate_name(self):
        self.add()
        host, err = ssh_store.add_host(self.data(password="other"))
        self.assertIsNone(host)
        self.assertIn("已存在", err)

    def test_update_host_keeps_password_when_blank(self):
        host = self.add()
        updated, err = ssh_store.update_host(host["id"], {"note": "改了备注"})
        self.assertEqual(err, "")
        self.assertEqual(updated["note"], "改了备注")
        stored = ssh_store.load_hosts()[0]
        self.assertEqual(stored["password"], "s3cret")

    def test_update_host_switches_to_key_and_drops_password(self):
        host = self.add()
        updated, err = ssh_store.update_host(
            host["id"], {"auth": "key", "key_path": self.key})
        self.assertEqual(err, "")
        stored = ssh_store.load_hosts()[0]
        self.assertEqual(stored["auth"], "key")
        self.assertEqual(stored["password"], "")
        self.assertEqual(os.path.basename(stored["key_path"]), "id_test")

    def test_update_host_missing_target(self):
        host, err = ssh_store.update_host("nope", {"note": "x"})
        self.assertIsNone(host)
        self.assertIn("未找到", err)

    def test_search_hosts_by_query_and_tag(self):
        self.add(name="alpha", tags="prod")
        self.add(name="beta", host="10.0.0.9", tags="dev")
        hosts = ssh_store.load_hosts()
        self.assertEqual([h["name"] for h in ssh_store.search_hosts(hosts, "alp")],
                         ["alpha"])
        self.assertEqual([h["name"] for h in ssh_store.search_hosts(hosts, "", "dev")],
                         ["beta"])
        self.assertEqual([h["name"] for h in ssh_store.search_hosts(hosts)],
                         ["alpha", "beta"])
        self.assertEqual(ssh_store.all_tags(hosts), ["dev", "prod"])

    def test_remove_host(self):
        host = self.add()
        self.assertTrue(ssh_store.remove_host(host["name"]))
        self.assertEqual(ssh_store.load_hosts(), [])
        self.assertFalse(ssh_store.remove_host(host["name"]))

    def test_load_hosts_corrupt_file_returns_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(ssh_store.load_hosts(), [])

    def test_load_hosts_skips_entries_without_id(self):
        ssh_store.save_hosts([])
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"hosts": [{"name": "x"}, self.data()]}, f)
        self.assertEqual(ssh_store.load_hosts(), [])

    def test_find_host_by_id_and_name(self):
        host = self.add()
        hosts = ssh_store.load_hosts()
        self.assertEqual(ssh_store.find_host(hosts, host["id"])["name"], "demo")
        self.assertEqual(ssh_store.find_host(hosts, "demo")["id"], host["id"])
        self.assertIsNone(ssh_store.find_host(hosts, ""))


class DefaultKeyTest(_StoreBase):
    """私钥留空时的默认值：只在本机真有该文件时才填，且只作为表单默认值。"""

    def test_default_key_dir_honours_env(self):
        self.assertEqual(
            os.path.normcase(ssh_store.default_key_dir()),
            os.path.normcase(os.path.join(self.root, "ssh_keys")))

    def test_no_key_returns_empty(self):
        self.assertEqual(ssh_store.default_key_path(), "")

    def test_picks_existing_key_by_priority(self):
        base = os.path.join(self.root, "ssh_keys")
        os.makedirs(base, exist_ok=True)
        self.assertEqual(ssh_store.default_key_path(), "")
        rsa = _write_key(os.path.join(base, "id_rsa"))
        self.assertEqual(ssh_store.default_key_path(), rsa)
        ed = _write_key(os.path.join(base, "id_ed25519"))
        self.assertEqual(ssh_store.default_key_path(), ed)  # ed25519 优先

    def test_directory_named_like_key_is_ignored(self):
        base = os.path.join(self.root, "ssh_keys")
        os.makedirs(os.path.join(base, "id_ed25519"), exist_ok=True)
        self.assertEqual(ssh_store.default_key_path(), "")


class SnippetStoreTest(_StoreBase):
    def test_snippets_live_in_config_not_hosts_file(self):
        host = self.add()
        snip, err = ssh_store.add_snippet(
            self.cfg, {"name": "磁盘", "command": "df -h",
                       "host_ids": [host["id"]], "tags": "ops"})
        self.assertEqual(err, "")
        self.assertTrue(snip["id"].startswith("snip-"))
        self.assertEqual(len(self.cfg.settings["ssh_snippets"]), 1)
        # 主机文件里既没有片段，也没有被片段带出的密码
        with open(self.path, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("df -h", text)
        # 片段配置里没有密码
        self.assertNotIn("s3cret", json.dumps(self.cfg.settings, ensure_ascii=False))

    def test_snippet_validation(self):
        cases = {
            "空名称": {"name": " ", "command": "ls"},
            "过长名称": {"name": "x" * (ssh_store.MAX_NAME + 1), "command": "ls"},
            "空命令": {"name": "a", "command": "   "},
            "过长命令": {"name": "a", "command": "x" * (ssh_store.MAX_COMMAND + 1)},
            "空字符": {"name": "a", "command": "ls\x00"},
        }
        for label, raw in cases.items():
            with self.subTest(label):
                self.assertTrue(ssh_store.validate_snippet(raw))
        self.assertIsNone(ssh_store.validate_snippet({"name": "a", "command": "ls"}))

    def test_snippet_crud(self):
        snip, _err = ssh_store.add_snippet(self.cfg, {"name": "磁盘", "command": "df -h"})
        _dup, err = ssh_store.add_snippet(self.cfg, {"name": "磁盘", "command": "ls"})
        self.assertIn("已存在", err)
        updated, err = ssh_store.update_snippet(
            self.cfg, snip["id"], {"command": "df -i"})
        self.assertEqual(err, "")
        self.assertEqual(ssh_store.load_snippets(self.cfg)[0]["command"], "df -i")
        self.assertTrue(ssh_store.remove_snippet(self.cfg, "磁盘"))
        self.assertEqual(ssh_store.load_snippets(self.cfg), [])

    def test_snippets_for_filters_by_host_and_tag(self):
        host = self.add()
        other = self.add(name="other", host="10.0.0.9")
        ssh_store.add_snippet(self.cfg, {"name": "通用", "command": "uptime"})
        ssh_store.add_snippet(self.cfg, {"name": "专属", "command": "df -h",
                                         "host_ids": [host["id"]], "tags": "ops"})
        ssh_store.add_snippet(self.cfg, {"name": "别家", "command": "ls",
                                         "host_ids": [other["id"]]})
        names = [s["name"] for s in ssh_store.snippets_for(self.cfg, host=host)]
        self.assertEqual(sorted(names), ["专属", "通用"])
        names = [s["name"] for s in ssh_store.snippets_for(self.cfg, host=host, tag="ops")]
        self.assertEqual(names, ["专属"])

    def test_dangling_host_ids_treated_as_generic(self):
        host = self.add()
        ssh_store.add_snippet(self.cfg, {"name": "孤儿", "command": "df -h",
                                         "host_ids": ["host-deadbeef"]})
        items = ssh_store.snippets_for(self.cfg, host=host)
        self.assertEqual([s["name"] for s in items], ["孤儿"])
        self.assertEqual([s["name"] for s in ssh_store.snippets_for(self.cfg)],
                         ["孤儿"])

    def test_load_snippets_ignores_broken_entries(self):
        self.cfg.settings["ssh_snippets"] = ["junk", {"name": "no-id"}]
        self.assertEqual(ssh_store.load_snippets(self.cfg), [])

    def test_update_snippet_clear_hosts(self):
        host = self.add()
        snip, _err = ssh_store.add_snippet(
            self.cfg, {"name": "a", "command": "ls", "host_ids": [host["id"]]})
        updated, err = ssh_store.update_snippet(self.cfg, snip["id"], {"host_ids": []})
        self.assertEqual(err, "")
        self.assertEqual(updated["host_ids"], [])


class BuiltinSnippetTest(unittest.TestCase):
    """预置片段：首次浇一次种子，删掉的不复活，与用户同名的不覆盖。"""

    def test_seeds_once_and_remembers_version(self):
        cfg = FakeConfig()
        added = ssh_store.ensure_builtin_snippets(cfg)
        self.assertEqual(len(added), len(ssh_store.builtin_snippets()))
        self.assertEqual(cfg.get_setting(ssh_store.BUILTIN_SEED_KEY),
                         ssh_store.BUILTIN_SNIPPETS_VERSION)
        items = ssh_store.load_snippets(cfg)
        self.assertEqual([s["name"] for s in items], added)
        self.assertTrue(all(s["host_ids"] == [] for s in items))
        self.assertEqual(ssh_store.ensure_builtin_snippets(cfg), [])

    def test_deleted_builtin_does_not_come_back(self):
        cfg = FakeConfig()
        ssh_store.ensure_builtin_snippets(cfg)
        self.assertTrue(ssh_store.remove_snippet(cfg, "builtin-uptime"))
        self.assertEqual(ssh_store.ensure_builtin_snippets(cfg), [])
        self.assertNotIn("builtin-uptime",
                         [s["id"] for s in ssh_store.load_snippets(cfg)])

    def test_user_snippet_with_same_name_wins(self):
        cfg = FakeConfig()
        ssh_store.add_snippet(cfg, {"name": "系统负载与运行时长",
                                    "command": "uptime -p"})
        added = ssh_store.ensure_builtin_snippets(cfg)
        self.assertNotIn("系统负载与运行时长", added)
        items = ssh_store.load_snippets(cfg)
        self.assertEqual(len(items), 1 + len(added))
        self.assertEqual(
            ssh_store.find_snippet(items, "系统负载与运行时长")["command"],
            "uptime -p")

    def test_builtin_commands_are_read_only(self):
        """预置片段是「看一眼就走」的探测命令：不提权、不跨行、不绑主机。"""
        for snip in ssh_store.builtin_snippets():
            with self.subTest(snip["id"]):
                self.assertTrue(snip["id"].startswith("builtin-"))
                self.assertEqual(snip["host_ids"], [])
                self.assertTrue(snip["name"].strip())
                self.assertTrue(snip["command"].strip())
                self.assertFalse(snip["command"].lstrip().startswith("sudo"))
                self.assertNotIn("\n", snip["command"])
                self.assertNotIn("rm ", snip["command"])
                self.assertLessEqual(len(snip["command"]), ssh_store.MAX_COMMAND)


class PermissionsTest(unittest.TestCase):
    """权限收紧的两种平台实现（不依赖真实 chmod / icacls）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "secret.json")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{}")

    def test_posix_uses_0600(self):
        with mock.patch.object(ssh_store, "IS_WIN", False), \
                mock.patch.object(ssh_store.os, "chmod") as chmod:
            self.assertTrue(ssh_store.set_secret_permissions(self.path))
        chmod.assert_called_once_with(self.path, 0o600)

    def test_windows_uses_icacls(self):
        with mock.patch.object(ssh_store, "IS_WIN", True), \
                mock.patch.dict(os.environ, {"USERNAME": "bob"}), \
                mock.patch.object(ssh_store.subprocess, "run") as run:
            self.assertTrue(ssh_store.set_secret_permissions(self.path))
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], "icacls")
        self.assertIn("bob:F", cmd)
        self.assertIn("/inheritance:r", cmd)

    def test_missing_file_or_user_is_false(self):
        self.assertFalse(ssh_store.set_secret_permissions(""))
        self.assertFalse(ssh_store.set_secret_permissions(
            os.path.join(self._tmp.name, "nope")))
        with mock.patch.object(ssh_store, "IS_WIN", True), \
                mock.patch.dict(os.environ, {"USERNAME": ""}):
            self.assertFalse(ssh_store.set_secret_permissions(self.path))

    def test_chmod_failure_is_silent(self):
        with mock.patch.object(ssh_store, "IS_WIN", False), \
                mock.patch.object(ssh_store.os, "chmod", side_effect=OSError("denied")):
            self.assertFalse(ssh_store.set_secret_permissions(self.path))


if __name__ == "__main__":
    unittest.main()
