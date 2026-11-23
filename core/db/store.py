# -*- coding: utf-8 -*-
"""数据库连接持久化（凭据独立文件，不进 config.json / 备份包）。

文件：``app_paths.data_file("db_connections.json")``，写前 ``.bak``，POSIX 0600。
``PHPVM_DB_CONNECTIONS`` 可覆盖路径（测试隔离）。见 ADR 28。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid

from core import app_paths, file_backup
from core.i18n import t
from core.ssh_store import set_secret_permissions

from .base import ENGINES

CONNECTIONS_FILE = "db_connections.json"
DATA_VERSION = 1
MAX_NAME = 64
MAX_HOST = 255
MAX_USER = 64
MAX_DB = 64
_RE_NAME = re.compile(r"^[\w.\- \u4e00-\u9fff]+$", re.U)
_RE_HOST = re.compile(r"^[A-Za-z0-9._:\-\[\]]+$")
_RE_USER = re.compile(r"^[\w.\-@]+$", re.U)


def connections_path() -> str:
    env = (os.environ.get("PHPVM_DB_CONNECTIONS") or "").strip()
    return os.path.expanduser(env) if env else app_paths.data_file(CONNECTIONS_FILE)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def new_id() -> str:
    return f"conn-{uuid.uuid4().hex[:8]}"


def _atomic_write(path: str, payload: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dbconn-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        set_secret_permissions(tmp)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    set_secret_permissions(path)


def load_all() -> dict:
    path = connections_path()
    if not os.path.isfile(path):
        return {"version": DATA_VERSION, "connections": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"version": DATA_VERSION, "connections": []}
    if not isinstance(data, dict):
        return {"version": DATA_VERSION, "connections": []}
    conns = data.get("connections")
    if not isinstance(conns, list):
        conns = []
    data["version"] = DATA_VERSION
    data["connections"] = [c for c in conns if isinstance(c, dict)]
    return data


def save_all(data: dict) -> None:
    path = connections_path()
    file_backup.backup(path)
    payload = {
        "version": DATA_VERSION,
        "connections": list(data.get("connections") or []),
    }
    _atomic_write(path, payload)


def list_connections(*, include_secret: bool = False) -> list[dict]:
    rows = []
    for c in load_all().get("connections") or []:
        rows.append(public(c) if not include_secret else dict(c))
    return rows


def get(conn_id: str, *, include_secret: bool = False) -> dict | None:
    cid = (conn_id or "").strip()
    if not cid:
        return None
    for c in load_all().get("connections") or []:
        if c.get("id") == cid or c.get("name") == cid:
            return dict(c) if include_secret else public(c)
    return None


def public(conn: dict) -> dict:
    """对外视图：不含 password；标 credential_ready。"""
    out = {k: v for k, v in conn.items() if k != "password"}
    engine = (conn.get("engine") or "").lower()
    if engine == "sqlite":
        ready = bool((conn.get("path") or "").strip())
    else:
        ready = bool((conn.get("user") or "").strip() and (conn.get("host") or "").strip())
    out["credential_ready"] = ready
    out["has_password"] = bool(conn.get("password"))
    return out


def validate(fields: dict, *, partial: bool = False) -> list[str]:
    """校验字段；返回错误文案列表（空 = 通过）。"""
    errs: list[str] = []
    engine = (fields.get("engine") or "").strip().lower()
    if not partial or "engine" in fields:
        if engine not in ENGINES:
            errs.append(t("引擎必须是 mysql 或 sqlite"))
    name = (fields.get("name") or "").strip()
    if not partial or "name" in fields:
        if not name or len(name) > MAX_NAME or not _RE_NAME.match(name):
            errs.append(t("连接名称不合法"))
    if engine == "sqlite" or (partial and fields.get("path") is not None):
        if engine == "sqlite" or "path" in fields:
            path = (fields.get("path") or "").strip()
            if engine == "sqlite" and not path:
                errs.append(t("SQLite 连接需要数据库文件路径"))
    if engine == "mysql" or (not engine and not partial):
        host = (fields.get("host") or "127.0.0.1").strip()
        if not partial or "host" in fields:
            if not host or len(host) > MAX_HOST or not _RE_HOST.match(host):
                errs.append(t("主机名不合法"))
        user = (fields.get("user") or "").strip()
        if not partial or "user" in fields:
            if not user or len(user) > MAX_USER or not _RE_USER.match(user):
                errs.append(t("用户名不合法"))
        if "port" in fields or not partial:
            try:
                port = int(fields.get("port") or 3306)
            except (TypeError, ValueError):
                port = -1
            if not (1 <= port <= 65535):
                errs.append(t("端口必须在 1–65535"))
    return errs


def add(fields: dict, *, dry_run: bool = False) -> dict:
    errs = validate(fields)
    if errs:
        raise ValueError("; ".join(errs))
    data = load_all()
    name = fields["name"].strip()
    if any(c.get("name") == name for c in data["connections"]):
        raise ValueError(t("连接名称已存在：{name}", name=name))
    row = _normalize_new(fields)
    if dry_run:
        return {"dry_run": True, "connection": public(row)}
    data["connections"].append(row)
    save_all(data)
    return {"connection": public(row)}


def update(conn_id: str, fields: dict, *, dry_run: bool = False) -> dict:
    data = load_all()
    idx = _index(data["connections"], conn_id)
    if idx < 0:
        raise KeyError(t("未找到连接：{id}", id=conn_id))
    cur = dict(data["connections"][idx])
    merged = dict(cur)
    for k, v in fields.items():
        if k in ("id", "created_at"):
            continue
        if k == "password" and v is None:
            continue
        merged[k] = v
    errs = validate(merged)
    if errs:
        raise ValueError("; ".join(errs))
    # 改名冲突
    new_name = merged["name"].strip()
    for i, c in enumerate(data["connections"]):
        if i != idx and c.get("name") == new_name:
            raise ValueError(t("连接名称已存在：{name}", name=new_name))
    merged = _normalize_existing(merged)
    if dry_run:
        return {"dry_run": True, "connection": public(merged)}
    data["connections"][idx] = merged
    save_all(data)
    return {"connection": public(merged)}


def remove(conn_id: str, *, yes: bool = False, dry_run: bool = False) -> dict:
    data = load_all()
    idx = _index(data["connections"], conn_id)
    if idx < 0:
        raise KeyError(t("未找到连接：{id}", id=conn_id))
    row = public(data["connections"][idx])
    if dry_run:
        return {"dry_run": True, "connection": row}
    if not yes:
        raise ValueError(t("删除连接需要 --yes"))
    data["connections"].pop(idx)
    save_all(data)
    return {"connection": row, "removed": True}


def _index(conns: list, conn_id: str) -> int:
    cid = (conn_id or "").strip()
    for i, c in enumerate(conns):
        if c.get("id") == cid or c.get("name") == cid:
            return i
    return -1


def _normalize_new(fields: dict) -> dict:
    engine = fields["engine"].strip().lower()
    now = _now()
    row = {
        "id": new_id(),
        "name": fields["name"].strip(),
        "engine": engine,
        "host": (fields.get("host") or "127.0.0.1").strip(),
        "port": int(fields.get("port") or 3306),
        "user": (fields.get("user") or "").strip(),
        "password": fields.get("password") or "",
        "database": (fields.get("database") or "").strip(),
        "socket": (fields.get("socket") or "").strip(),
        "path": (fields.get("path") or "").strip(),
        "mysql_cli": (fields.get("mysql_cli") or "").strip(),
        "created_at": now,
        "updated_at": now,
    }
    if engine == "sqlite":
        row["host"] = ""
        row["port"] = 0
        row["user"] = ""
        row["password"] = ""
    return row


def _normalize_existing(row: dict) -> dict:
    engine = (row.get("engine") or "mysql").strip().lower()
    out = {
        "id": row.get("id") or new_id(),
        "name": (row.get("name") or "").strip(),
        "engine": engine,
        "host": (row.get("host") or "127.0.0.1").strip(),
        "port": int(row.get("port") or (3306 if engine == "mysql" else 0)),
        "user": (row.get("user") or "").strip(),
        "password": row.get("password") or "",
        "database": (row.get("database") or "").strip(),
        "socket": (row.get("socket") or "").strip(),
        "path": (row.get("path") or "").strip(),
        "mysql_cli": (row.get("mysql_cli") or "").strip(),
        "created_at": row.get("created_at") or _now(),
        "updated_at": _now(),
    }
    if engine == "sqlite":
        out["host"] = ""
        out["port"] = 0
        out["user"] = ""
        out["password"] = ""
    return out
