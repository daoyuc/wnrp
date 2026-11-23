# -*- coding: utf-8 -*-
"""core.db.store：连接持久化与凭据边界。"""
import json
import os
import tempfile
import unittest
from unittest import mock

from core import i18n as _i18n
from core.db import store


class DbStoreTest(unittest.TestCase):
    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "db_connections.json")
        env = mock.patch.dict(os.environ, {"PHPVM_DB_CONNECTIONS": self.path})
        env.start()
        self.addCleanup(env.stop)
        mock.patch.object(store, "set_secret_permissions", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def test_add_mysql_hides_password_in_public(self):
        out = store.add({
            "name": "local", "engine": "mysql", "user": "root",
            "password": "s3cret", "host": "127.0.0.1", "port": 3306,
        })
        conn = out["connection"]
        self.assertNotIn("password", conn)
        self.assertTrue(conn["has_password"])
        self.assertTrue(conn["credential_ready"])
        raw = store.get(conn["id"], include_secret=True)
        self.assertEqual(raw["password"], "s3cret")

    def test_add_sqlite_requires_path(self):
        with self.assertRaises(ValueError):
            store.add({"name": "app", "engine": "sqlite"})

    def test_add_sqlite_ok(self):
        out = store.add({
            "name": "app", "engine": "sqlite",
            "path": "/tmp/demo.sqlite",
        })
        self.assertEqual(out["connection"]["engine"], "sqlite")
        self.assertTrue(out["connection"]["credential_ready"])

    def test_name_unique(self):
        store.add({"name": "local", "engine": "mysql", "user": "root"})
        with self.assertRaises(ValueError):
            store.add({"name": "local", "engine": "mysql", "user": "root"})

    def test_update_keeps_password_when_omitted(self):
        out = store.add({
            "name": "local", "engine": "mysql", "user": "root",
            "password": "keep-me",
        })
        store.update(out["connection"]["id"], {"database": "app"})
        raw = store.get("local", include_secret=True)
        self.assertEqual(raw["password"], "keep-me")
        self.assertEqual(raw["database"], "app")

    def test_remove_requires_yes(self):
        store.add({"name": "local", "engine": "mysql", "user": "root"})
        with self.assertRaises(ValueError):
            store.remove("local")
        store.remove("local", yes=True)
        self.assertIsNone(store.get("local"))

    def test_dry_run_add_does_not_write(self):
        out = store.add(
            {"name": "local", "engine": "mysql", "user": "root"},
            dry_run=True,
        )
        self.assertTrue(out["dry_run"])
        self.assertFalse(os.path.isfile(self.path))

    def test_file_mode_is_private_when_possible(self):
        store.add({"name": "local", "engine": "mysql", "user": "root",
                   "password": "x"})
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["connections"][0]["password"], "x")


if __name__ == "__main__":
    unittest.main()
