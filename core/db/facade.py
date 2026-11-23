# -*- coding: utf-8 -*-
"""数据库管理对外门面（CLI / GUI 共用）。

只读浏览不写审计；未来写路径必须先 ``audit.append`` 再执行（ADR 28）。
"""
from __future__ import annotations

import os
from typing import Optional

from core.i18n import t

from . import audit, store
from .base import Filter, DriverError
from .mysql_cli import MysqlCliDriver, find_mysql_cli
from .sqlite_file import SqliteFileDriver


def list_connections() -> dict:
    return {
        "connections": store.list_connections(include_secret=False),
        "path": store.connections_path(),
        "mysql_cli": find_mysql_cli(),
    }


def suggest_local_mysql() -> dict:
    """根据本机 MysqlManager 实例给出可一键添加的连接草稿（不含密码）。"""
    items = []
    try:
        from core.config import Config
        from core.mysql_manager import MysqlManager
        mgr = MysqlManager(Config())
        for inst in mgr.instances:
            items.append({
                "name": f"local-{inst.name}",
                "engine": "mysql",
                "host": "127.0.0.1",
                "port": int(inst.port or 3306),
                "user": "root",
                "database": "",
                "mysql_cli": inst.cli or "",
                "instance": inst.name,
                "running": bool(inst.running),
            })
    except Exception as e:  # noqa: BLE001
        return {"items": [], "error": str(e)}
    return {"items": items, "mysql_cli": find_mysql_cli()}


def add_connection(fields: dict, *, password: str = "",
                   dry_run: bool = False) -> dict:
    data = dict(fields)
    if password:
        data["password"] = password
    return store.add(data, dry_run=dry_run)


def update_connection(conn_id: str, fields: dict, *,
                      password: Optional[str] = None,
                      dry_run: bool = False) -> dict:
    data = dict(fields)
    if password is not None:
        data["password"] = password
    return store.update(conn_id, data, dry_run=dry_run)


def remove_connection(conn_id: str, *, yes: bool = False,
                      dry_run: bool = False) -> dict:
    return store.remove(conn_id, yes=yes, dry_run=dry_run)


def test_connection(conn_id: str, *, password: str = "") -> dict:
    conn = _load_secret(conn_id, password_override=password)
    driver = _driver(conn)
    try:
        result = driver.test()
    except DriverError as e:
        return {
            "ok": False,
            "error": str(e),
            "error_kind": e.kind,
            "connection": store.public(conn),
        }
    # 记录一次 meta.test（只读探测，便于排查谁测过连接）
    try:
        audit.append({
            "kind": "meta.test",
            "engine": conn.get("engine"),
            "conn_id": conn.get("id"),
            "conn_name": conn.get("name"),
            "database": conn.get("database") or "",
            "result": "ok",
            "duration_ms": result.get("elapsed_ms") or 0,
            "preview": result.get("server") or result.get("path") or "",
        })
    except OSError:
        pass
    return {"ok": True, "connection": store.public(conn), **result}


def list_databases(conn_id: str, *, include_system: bool = False,
                   password: str = "") -> dict:
    conn = _load_secret(conn_id, password_override=password)
    driver = _driver(conn)
    names = driver.list_databases(include_system=include_system)
    return {
        "connection": store.public(conn),
        "databases": names,
    }


def list_tables(conn_id: str, *, database: str = "",
                password: str = "") -> dict:
    conn = _load_secret(conn_id, password_override=password)
    driver = _driver(conn)
    tables = driver.list_tables(database=database)
    return {
        "connection": store.public(conn),
        "database": database or conn.get("database") or "",
        "tables": [
            {
                "name": tbl.name,
                "kind": tbl.kind,
                "engine": tbl.engine,
                "rows_approx": tbl.rows_approx,
            }
            for tbl in tables
        ],
    }


def list_columns(conn_id: str, table: str, *, database: str = "",
                 password: str = "") -> dict:
    conn = _load_secret(conn_id, password_override=password)
    driver = _driver(conn)
    cols = driver.list_columns(table, database=database)
    return {
        "connection": store.public(conn),
        "database": database or conn.get("database") or "",
        "table": table,
        "columns": [
            {
                "name": c.name,
                "type": c.type,
                "nullable": c.nullable,
                "primary_key": c.primary_key,
                "default": c.default,
                "extra": c.extra,
            }
            for c in cols
        ],
    }


def select_rows(conn_id: str, table: str, *, database: str = "",
                filters: Optional[list] = None,
                order: str = "",
                limit: int = 200,
                offset: int = 0,
                password: str = "") -> dict:
    conn = _load_secret(conn_id, password_override=password)
    driver = _driver(conn)
    parsed = _parse_filters(filters)
    result = driver.select_table(
        table, database=database, filters=parsed,
        order=order, limit=limit, offset=offset,
    )
    return {
        "connection": store.public(conn),
        "database": database or conn.get("database") or "",
        "table": table,
        "columns": result.columns,
        "rows": result.rows,
        "truncated": result.truncated,
        "elapsed_ms": result.elapsed_ms,
        "sql": result.sql,
        "row_count": result.row_count,
        "limit": limit,
        "offset": offset,
    }


def query_sql(conn_id: str, sql: str, *, limit: int = 200,
              password: str = "") -> dict:
    conn = _load_secret(conn_id, password_override=password)
    driver = _driver(conn)
    result = driver.query(sql, limit=limit)
    return {
        "connection": store.public(conn),
        "columns": result.columns,
        "rows": result.rows,
        "truncated": result.truncated,
        "elapsed_ms": result.elapsed_ms,
        "sql": result.sql,
        "row_count": result.row_count,
    }


def list_audit(*, limit: int = 100, kind: str = "",
               conn_id: str = "") -> dict:
    rows = audit.list_entries(limit=limit, kind=kind, conn_id=conn_id)
    return {
        "entries": rows,
        "dir": audit.audit_dir(),
        "count": len(rows),
    }


# ------------------------------------------------------------------ #
def _load_secret(conn_id: str, *, password_override: str = "") -> dict:
    conn = store.get(conn_id, include_secret=True)
    if not conn:
        raise KeyError(t("未找到连接：{id}", id=conn_id))
    if password_override:
        conn = dict(conn)
        conn["password"] = password_override
    return conn


def _driver(conn: dict):
    engine = (conn.get("engine") or "").lower()
    if engine == "mysql":
        return MysqlCliDriver(conn)
    if engine == "sqlite":
        return SqliteFileDriver(conn)
    raise DriverError(t("不支持的引擎：{engine}", engine=engine))


def _parse_filters(raw) -> list[Filter]:
    """接受 list[Filter] / list[dict] / 「col=eq:value,col2=like:%x%」字符串。"""
    if not raw:
        return []
    if isinstance(raw, str):
        items = []
        for part in raw.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            col, rest = part.split("=", 1)
            if ":" in rest:
                op, val = rest.split(":", 1)
            else:
                op, val = "eq", rest
            items.append(Filter(column=col.strip(), op=op.strip(), value=val))
        return items
    out: list[Filter] = []
    for item in raw:
        if isinstance(item, Filter):
            out.append(item)
        elif isinstance(item, dict):
            out.append(Filter(
                column=str(item.get("column") or ""),
                op=str(item.get("op") or "eq"),
                value=str(item.get("value") or ""),
            ))
    return out
