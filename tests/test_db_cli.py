# -*- coding: utf-8 -*-
"""cli.py db 命令组：解析、dry-run / --yes、凭据边界。"""
import inspect
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

import cli
from core import backup_bundle, i18n as _i18n
from core.db import store


class _Lang(unittest.TestCase):
    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")

    def tearDown(self):
        _i18n.set_language(self._lang)


class DbCliTest(_Lang):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.path = os.path.join(self.root, "db_connections.json")
        self.audit_dir = os.path.join(self.root, "db_audit")
        env = mock.patch.dict(os.environ, {
            "PHPVM_DB_CONNECTIONS": self.path,
            "PHPVM_DB_AUDIT_DIR": self.audit_dir,
        })
        env.start()
        self.addCleanup(env.stop)
        mock.patch.object(store, "set_secret_permissions", return_value=True).start()
        from core.db import audit
        mock.patch.object(audit, "set_secret_permissions", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

        self.db = os.path.join(self.root, "demo.sqlite")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO t(name) VALUES ('a')")
        conn.commit()
        conn.close()

    def run_cli(self, argv, stdin_text=""):
        ns = cli.build_parser().parse_args(argv)
        if stdin_text:
            stdin = mock.patch.object(sys, "stdin", io.StringIO(stdin_text))
            stdin.start()
            self.addCleanup(stdin.stop)
        result = cli.Result()
        ns.func(ns, result)
        return result

    def test_parser_command_ids(self):
        cases = {
            ("db", "conn", "list"): "db.conn.list",
            ("db", "conn", "suggest"): "db.conn.suggest",
            ("db", "tables"): "db.tables",
            ("db", "query"): "db.query",
            ("db", "audit", "list"): "db.audit.list",
        }
        for argv, cmd in cases.items():
            ns = cli.build_parser().parse_args(
                list(argv) + (["--conn", "x"] if "tables" in argv or "query" in argv else [])
            )
            self.assertEqual(ns.command, cmd)

    def test_conn_add_dry_run(self):
        res = self.run_cli([
            "db", "conn", "add", "--name", "local", "--engine", "mysql",
            "--user", "root", "--dry-run",
        ])
        self.assertTrue(res.ok)
        self.assertTrue(res.data.get("dry_run"))
        self.assertFalse(os.path.isfile(self.path))

    def test_conn_add_password_from_stdin(self):
        res = self.run_cli([
            "db", "conn", "add", "--name", "local", "--engine", "mysql",
            "--user", "root", "--password-stdin",
        ], stdin_text="s3cret\n")
        self.assertTrue(res.ok)
        raw = store.get("local", include_secret=True)
        self.assertEqual(raw["password"], "s3cret")
        self.assertNotIn("password", res.data["connection"])

    def test_conn_add_sqlite(self):
        res = self.run_cli([
            "db", "conn", "add", "--name", "app", "--engine", "sqlite",
            "--path", self.db,
        ])
        self.assertTrue(res.ok)
        self.assertEqual(res.data["connection"]["engine"], "sqlite")

    def test_conn_remove_requires_yes(self):
        store.add({"name": "local", "engine": "mysql", "user": "root"})
        res = self.run_cli(["db", "conn", "remove", "local"])
        self.assertFalse(res.ok)
        self.assertIsNotNone(store.get("local"))

    def test_conn_remove_yes(self):
        store.add({"name": "local", "engine": "mysql", "user": "root"})
        res = self.run_cli(["db", "conn", "remove", "local", "--yes"])
        self.assertTrue(res.ok)
        self.assertIsNone(store.get("local"))

    def test_query_table_sqlite(self):
        store.add({"name": "app", "engine": "sqlite", "path": self.db})
        res = self.run_cli([
            "db", "query", "--conn", "app", "--table", "t", "--limit", "10",
        ])
        self.assertTrue(res.ok)
        self.assertEqual(res.data["row_count"], 1)
        self.assertEqual(res.data["columns"], ["id", "name"])

    def test_query_sql_sqlite(self):
        store.add({"name": "app", "engine": "sqlite", "path": self.db})
        res = self.run_cli([
            "db", "query", "--conn", "app",
            "--sql", "SELECT name FROM t",
        ])
        self.assertTrue(res.ok)
        self.assertEqual(res.data["row_count"], 1)

    def test_tables_sqlite(self):
        store.add({"name": "app", "engine": "sqlite", "path": self.db})
        res = self.run_cli(["db", "tables", "--conn", "app"])
        self.assertTrue(res.ok)
        names = [t["name"] for t in res.data["tables"]]
        self.assertIn("t", names)

    def test_audit_list_empty(self):
        res = self.run_cli(["db", "audit", "list"])
        self.assertTrue(res.ok)
        self.assertEqual(res.data["count"], 0)

    def test_conn_update_without_fields_fails(self):
        store.add({"name": "local", "engine": "mysql", "user": "root"})
        res = self.run_cli(["db", "conn", "update", "local"])
        self.assertFalse(res.ok)


class CredentialBoundaryTest(unittest.TestCase):
    def test_backup_bundle_never_touches_db_credentials(self):
        source = inspect.getsource(backup_bundle)
        self.assertNotIn("db_connections", source)
        self.assertNotIn("db_audit", source)


if __name__ == "__main__":
    unittest.main()
