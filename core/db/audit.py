# -*- coding: utf-8 -*-
"""数据库写操作审计（JSONL 追加）。

路径：``<数据目录>/db_audit/YYYY-MM.jsonl``，权限收紧。
写路径（未来 DML/DDL）必须先 ``append`` 成功才允许执行；本模块本身可被
只读查询的「审计浏览」使用。见 ADR 28。
"""
from __future__ import annotations

import getpass
import json
import os
import time
from typing import Any, Optional

from core import app_paths
from core.ssh_store import set_secret_permissions

AUDIT_DIR_NAME = "db_audit"
MAX_SQL_CHARS = 8000
MAX_ERROR_CHARS = 1000


def audit_dir() -> str:
    env = (os.environ.get("PHPVM_DB_AUDIT_DIR") or "").strip()
    if env:
        return os.path.expanduser(env)
    return app_paths.data_file(AUDIT_DIR_NAME)


def _month_path(ts: Optional[float] = None) -> str:
    t = time.localtime(ts if ts is not None else time.time())
    name = time.strftime("%Y-%m.jsonl", t)
    return os.path.join(audit_dir(), name)


def append(entry: dict) -> dict:
    """追加一条审计记录；返回写入后的规范化记录。

    必填：``kind``（如 ``dml.update`` / ``ddl.add_column`` / ``meta.test``）。
    失败抛 ``OSError`` —— 调用方应拒绝继续执行写操作。
    """
    now = time.time()
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "ts_epoch": now,
        "actor": _actor(),
        "kind": str(entry.get("kind") or "unknown"),
        "engine": str(entry.get("engine") or ""),
        "conn_id": str(entry.get("conn_id") or ""),
        "conn_name": str(entry.get("conn_name") or ""),
        "database": str(entry.get("database") or ""),
        "sql": _clip(str(entry.get("sql") or ""), MAX_SQL_CHARS),
        "params": entry.get("params") if isinstance(entry.get("params"), dict) else {},
        "preview": str(entry.get("preview") or "")[:500],
        "result": str(entry.get("result") or "ok"),
        "duration_ms": float(entry.get("duration_ms") or 0),
        "error": _clip(str(entry.get("error") or ""), MAX_ERROR_CHARS),
        "confirm": bool(entry.get("confirm")),
        "dry_run": bool(entry.get("dry_run")),
    }
    path = _month_path(now)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())
    set_secret_permissions(path)
    rec["_path"] = path
    return rec


def list_entries(*, limit: int = 100, kind: str = "",
                 conn_id: str = "") -> list[dict]:
    """倒序读取最近审计（跨最多 3 个月文件）。"""
    limit = max(1, min(int(limit or 100), 1000))
    files = _recent_files(3)
    rows: list[dict] = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if kind and rec.get("kind") != kind:
                continue
            if conn_id and rec.get("conn_id") != conn_id and rec.get("conn_name") != conn_id:
                continue
            rows.append(rec)
            if len(rows) >= limit:
                return rows
    return rows


def _recent_files(n: int) -> list[str]:
    base = audit_dir()
    if not os.path.isdir(base):
        return []
    names = sorted(
        (name for name in os.listdir(base) if name.endswith(".jsonl")),
        reverse=True,
    )
    return [os.path.join(base, name) for name in names[:n]]


def _actor() -> str:
    try:
        return getpass.getuser() or ""
    except Exception:  # noqa: BLE001
        return ""


def _clip(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"
