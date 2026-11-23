# -*- coding: utf-8 -*-
"""数据库 Driver 共用模型与标识符工具。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


ENGINES = ("mysql", "sqlite")
SYSTEM_SCHEMAS_MYSQL = frozenset({
    "information_schema", "mysql", "performance_schema", "sys",
})
DEFAULT_LIMIT = 200
HARD_LIMIT = 1000


@dataclass
class ColumnInfo:
    name: str
    type: str = ""
    nullable: bool = True
    primary_key: bool = False
    default: Optional[str] = None
    extra: str = ""


@dataclass
class TableInfo:
    name: str
    kind: str = "table"  # table / view
    engine: str = ""
    rows_approx: Optional[int] = None


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    truncated: bool = False
    elapsed_ms: float = 0.0
    sql: str = ""
    row_count: int = 0


@dataclass
class Filter:
    """简单列筛选：column op value；op 为 eq/ne/like/isnull/notnull。"""

    column: str
    op: str = "eq"
    value: str = ""


class DriverError(RuntimeError):
    """Driver 执行失败（缺客户端 / 认证 / SQL 错误）。"""

    def __init__(self, message: str, *, kind: str = "failed"):
        super().__init__(message)
        self.kind = kind  # failed / no_tool / auth / timeout


class Driver(Protocol):
    def test(self) -> dict: ...

    def list_databases(self) -> list[str]: ...

    def list_tables(self, database: str = "") -> list[TableInfo]: ...

    def list_columns(self, table: str, database: str = "") -> list[ColumnInfo]: ...

    def query(self, sql: str, *, limit: int = DEFAULT_LIMIT) -> QueryResult: ...


def quote_ident(name: str, *, engine: str = "mysql") -> str:
    """安全引用标识符（禁止空串与含 NUL）。"""
    s = str(name or "")
    if not s or "\x00" in s:
        raise ValueError("invalid identifier")
    if engine == "sqlite":
        return '"' + s.replace('"', '""') + '"'
    return "`" + s.replace("`", "``") + "`"


def clamp_limit(limit: int | None, default: int = DEFAULT_LIMIT) -> int:
    try:
        n = int(limit) if limit is not None else default
    except (TypeError, ValueError):
        n = default
    if n < 1:
        n = 1
    return min(n, HARD_LIMIT)


def build_where(filters: list[Filter], *, engine: str = "mysql") -> tuple[str, list[str]]:
    """把 Filter 列表编成 WHERE 子句；返回 (sql_fragment, notes)。

    值一律当字符串字面量内嵌（经转义），避免依赖服务端预处理协议；
    仅允许白名单运算符。
    """
    notes: list[str] = []
    parts: list[str] = []
    for f in filters or []:
        col = (f.column or "").strip()
        if not col:
            continue
        op = (f.op or "eq").lower()
        ident = quote_ident(col, engine=engine)
        if op == "isnull":
            parts.append(f"{ident} IS NULL")
        elif op == "notnull":
            parts.append(f"{ident} IS NOT NULL")
        elif op in ("eq", "ne", "like"):
            lit = _sql_string(f.value or "", engine=engine)
            if op == "eq":
                parts.append(f"{ident} = {lit}")
            elif op == "ne":
                parts.append(f"{ident} <> {lit}")
            else:
                parts.append(f"{ident} LIKE {lit}")
        else:
            notes.append(f"skip op={op}")
    if not parts:
        return "", notes
    return " WHERE " + " AND ".join(parts), notes


def _sql_string(value: str, *, engine: str) -> str:
    s = str(value).replace("\x00", "")
    if engine == "sqlite":
        return "'" + s.replace("'", "''") + "'"
    # MySQL：转义反斜杠与引号
    s = s.replace("\\", "\\\\").replace("'", "\\'")
    return "'" + s + "'"
