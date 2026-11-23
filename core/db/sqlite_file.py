# -*- coding: utf-8 -*-
"""SQLite Driver：复用 ``core.sqlite_manager`` 的只读打开与查询能力。"""
from __future__ import annotations

import os
import time
from typing import Optional

from core.i18n import t
from core.sqlite_manager import SqliteManager, format_value

from .base import (
    ColumnInfo,
    DriverError,
    Filter,
    QueryResult,
    TableInfo,
    build_where,
    clamp_limit,
    quote_ident,
)


class SqliteFileDriver:
    def __init__(self, conn: dict):
        self.conn = dict(conn)
        path = os.path.expanduser((conn.get("path") or "").strip())
        if not path:
            raise DriverError(t("SQLite 连接需要数据库文件路径"))
        if not os.path.isfile(path):
            raise DriverError(t("数据库文件不存在：{path}", path=path))
        self.path = path
        self._mgr = SqliteManager()  # 不传 config，避免写 settings.sqlite_last_db
        try:
            self._mgr.open(path)
        except Exception as e:  # noqa: BLE001
            raise DriverError(str(e)) from e

    def test(self) -> dict:
        t0 = time.perf_counter()
        tables = self.list_tables()
        ms = (time.perf_counter() - t0) * 1000
        return {
            "ok": True,
            "elapsed_ms": round(ms, 1),
            "path": self.path,
            "table_count": len(tables),
        }

    def list_databases(self, *, include_system: bool = False) -> list[str]:
        return ["main"]

    def list_tables(self, database: str = "") -> list[TableInfo]:
        try:
            items = self._mgr.tables()
        except Exception as e:  # noqa: BLE001
            raise DriverError(str(e)) from e
        out: list[TableInfo] = []
        for it in items:
            approx = None
            try:
                approx = self._mgr.count_rows(it.name)
            except Exception:  # noqa: BLE001
                approx = None
            out.append(TableInfo(name=it.name, kind=it.kind,
                                 rows_approx=approx))
        return out

    def list_columns(self, table: str, database: str = "") -> list[ColumnInfo]:
        try:
            cols = self._mgr.columns(table)
        except Exception as e:  # noqa: BLE001
            raise DriverError(str(e)) from e
        return [
            ColumnInfo(
                name=c.name,
                type=c.type,
                nullable=not c.notnull,
                primary_key=c.pk,
                default=c.default if c.default != "" else None,
            )
            for c in cols
        ]

    def select_table(self, table: str, *, database: str = "",
                     filters: Optional[list[Filter]] = None,
                     order: str = "",
                     limit: int = 200,
                     offset: int = 0) -> QueryResult:
        tbl = (table or "").strip()
        if not tbl:
            raise DriverError(t("请指定表名"))
        lim = clamp_limit(limit)
        off = max(0, int(offset or 0))
        where, _ = build_where(filters or [], engine="sqlite")
        order_sql = ""
        if order:
            parts = order.strip().split()
            col = parts[0]
            direction = parts[1].upper() if len(parts) > 1 else "ASC"
            if direction not in ("ASC", "DESC"):
                direction = "ASC"
            order_sql = f" ORDER BY {quote_ident(col, engine='sqlite')} {direction}"
        sql = (f"SELECT * FROM {quote_ident(tbl, engine='sqlite')}"
               f"{where}{order_sql} LIMIT {lim + 1} OFFSET {off}")
        return self.query(sql, limit=lim)

    def query(self, sql: str, *, limit: int = 200) -> QueryResult:
        lim = clamp_limit(limit)
        t0 = time.perf_counter()
        try:
            result = self._mgr.query(sql, limit=lim + 1)
        except Exception as e:  # noqa: BLE001
            raise DriverError(str(e)) from e
        ms = (time.perf_counter() - t0) * 1000
        truncated = bool(result.truncated) or len(result.rows) > lim
        rows = result.rows[:lim]
        # 统一成可 JSON 的字符串格子
        str_rows = [[format_value(c) for c in row] for row in rows]
        return QueryResult(
            columns=list(result.columns),
            rows=str_rows,
            truncated=truncated,
            elapsed_ms=round(ms, 1),
            sql=sql,
            row_count=len(str_rows),
        )
