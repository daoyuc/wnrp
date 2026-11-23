# -*- coding: utf-8 -*-
"""core.db.audit：JSONL 追加与倒序读取。"""
import os
import tempfile
import unittest
from unittest import mock

from core.db import audit


class DbAuditTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "PHPVM_DB_AUDIT_DIR": os.path.join(self._tmp.name, "db_audit"),
        })
        env.start()
        self.addCleanup(env.stop)
        mock.patch.object(audit, "set_secret_permissions", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def test_append_and_list(self):
        audit.append({
            "kind": "meta.test",
            "engine": "sqlite",
            "conn_id": "conn-aaaa",
            "conn_name": "app",
            "preview": "ok",
            "result": "ok",
            "duration_ms": 1.2,
        })
        audit.append({
            "kind": "dml.update",
            "engine": "mysql",
            "conn_id": "conn-bbbb",
            "conn_name": "local",
            "sql": "UPDATE t SET x=1",
            "result": "ok",
            "confirm": True,
        })
        rows = audit.list_entries(limit=10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["kind"], "dml.update")  # 最新在前
        only = audit.list_entries(kind="meta.test")
        self.assertEqual(len(only), 1)
        by_conn = audit.list_entries(conn_id="local")
        self.assertEqual(len(by_conn), 1)

    def test_clips_long_sql(self):
        long_sql = "SELECT " + ("x" * (audit.MAX_SQL_CHARS + 50))
        rec = audit.append({"kind": "meta.test", "sql": long_sql})
        self.assertLessEqual(len(rec["sql"]), audit.MAX_SQL_CHARS)
        self.assertTrue(rec["sql"].endswith("…"))


if __name__ == "__main__":
    unittest.main()
