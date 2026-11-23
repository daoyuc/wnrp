# -*- coding: utf-8 -*-
"""SQLite Driver + facade 只读路径（真实临时库）。"""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from core import i18n as _i18n
from core.db import facade, store
from core.db.base import DriverError


class SqliteDriverTest(unittest.TestCase):
    def setUp(self):
        self._lang = _i18n.current_language()
        _i18n.set_language("zh_CN")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "demo.sqlite")
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, status TEXT)"
        )
        conn.executemany(
            "INSERT INTO users(name, status) VALUES (?, ?)",
            [("alice", "active"), ("bob", "inactive"), ("carol", "active")],
        )
        conn.commit()
        conn.close()

        self.path = os.path.join(self._tmp.name, "db_connections.json")
        env = mock.patch.dict(os.environ, {
            "PHPVM_DB_CONNECTIONS": self.path,
            "PHPVM_DB_AUDIT_DIR": os.path.join(self._tmp.name, "db_audit"),
        })
        env.start()
        self.addCleanup(env.stop)
        mock.patch.object(store, "set_secret_permissions", return_value=True).start()
        from core.db import audit
        mock.patch.object(audit, "set_secret_permissions", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

        out = store.add({
            "name": "app", "engine": "sqlite", "path": self.db,
        })
        self.conn_id = out["connection"]["id"]

    def tearDown(self):
        _i18n.set_language(self._lang)

    def test_list_tables_and_columns(self):
        tabs = facade.list_tables(self.conn_id)
        names = [t["name"] for t in tabs["tables"]]
        self.assertIn("users", names)
        cols = facade.list_columns(self.conn_id, "users")
        self.assertEqual([c["name"] for c in cols["columns"]],
                         ["id", "name", "status"])

    def test_select_with_filter(self):
        out = facade.select_rows(
            self.conn_id, "users",
            filters="status=eq:active",
            order="id",
            limit=10,
        )
        self.assertEqual(out["row_count"], 2)
        self.assertIn("WHERE", out["sql"])

    def test_query_rejects_write(self):
        # sqlite_manager 自身拦写；这里确认 facade 路径也会失败
        with self.assertRaises(DriverError):
            facade.query_sql(self.conn_id, "DELETE FROM users")

    def test_test_connection_writes_audit(self):
        from core.db import audit
        out = facade.test_connection(self.conn_id)
        self.assertTrue(out["ok"])
        rows = audit.list_entries(kind="meta.test")
        self.assertGreaterEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
