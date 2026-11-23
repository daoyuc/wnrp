# -*- coding: utf-8 -*-
"""core.db.base：标识符引用与 WHERE 构造。"""
import unittest

from core.db.base import Filter, build_where, clamp_limit, quote_ident


class QuoteIdentTest(unittest.TestCase):
    def test_mysql_escapes_backtick(self):
        self.assertEqual(quote_ident("a`b"), "`a``b`")

    def test_sqlite_escapes_quote(self):
        self.assertEqual(quote_ident('a"b', engine="sqlite"), '"a""b"')

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            quote_ident("")


class BuildWhereTest(unittest.TestCase):
    def test_eq_and_like(self):
        where, notes = build_where([
            Filter("id", "eq", "1"),
            Filter("name", "like", "%x%"),
        ])
        self.assertIn("`id` = '1'", where)
        self.assertIn("`name` LIKE '%x%'", where)
        self.assertEqual(notes, [])

    def test_isnull(self):
        where, _ = build_where([Filter("deleted_at", "isnull")])
        self.assertEqual(where, " WHERE `deleted_at` IS NULL")

    def test_skips_unknown_op(self):
        where, notes = build_where([Filter("x", "gt", "1")])
        self.assertEqual(where, "")
        self.assertTrue(notes)

    def test_escapes_quote_in_value(self):
        where, _ = build_where([Filter("n", "eq", "O'Brien")])
        self.assertIn(r"'O\'Brien'", where)


class ClampLimitTest(unittest.TestCase):
    def test_hard_cap(self):
        self.assertEqual(clamp_limit(9999), 1000)

    def test_default(self):
        self.assertEqual(clamp_limit(None), 200)


if __name__ == "__main__":
    unittest.main()
