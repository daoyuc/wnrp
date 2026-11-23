# -*- coding: utf-8 -*-
"""MySQL Driver：调用本机 ``mysql`` 客户端（标准库 subprocess，零第三方驱动）。

认证用临时 ``--defaults-extra-file``（0600），密码不进 argv / 进程列表。
缺客户端时 ``DriverError(kind=no_tool)``。
"""
from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import tempfile
import time
from typing import Optional

from core.i18n import t
from core.ssh_store import set_secret_permissions

from .base import (
    SYSTEM_SCHEMAS_MYSQL,
    ColumnInfo,
    DriverError,
    Filter,
    QueryResult,
    TableInfo,
    build_where,
    clamp_limit,
    quote_ident,
)


def find_mysql_cli(explicit: str = "") -> str:
    if explicit and os.path.isfile(explicit):
        return explicit
    which = shutil.which("mysql") or shutil.which("mysql.exe")
    if which:
        return which
    # 尝试本机 MysqlManager 已发现的实例
    try:
        from core.config import Config
        from core.mysql_manager import MysqlManager
        mgr = MysqlManager(Config())
        for inst in mgr.instances:
            if inst.cli and os.path.isfile(inst.cli):
                return inst.cli
    except Exception:  # noqa: BLE001
        pass
    return ""


class MysqlCliDriver:
    def __init__(self, conn: dict):
        self.conn = dict(conn)
        self.cli = find_mysql_cli(self.conn.get("mysql_cli") or "")
        if not self.cli:
            raise DriverError(
                t("未找到 mysql 客户端；请安装 MySQL/MariaDB 客户端或配置 mysql_cli"),
                kind="no_tool",
            )

    def test(self) -> dict:
        t0 = time.perf_counter()
        out = self._exec("SELECT 1 AS ok", expect_table=True)
        ms = (time.perf_counter() - t0) * 1000
        return {
            "ok": True,
            "elapsed_ms": round(ms, 1),
            "server": self._server_version(),
            "rows": out.rows,
        }

    def list_databases(self, *, include_system: bool = False) -> list[str]:
        res = self._exec("SHOW DATABASES", expect_table=True)
        names = [str(r[0]) for r in res.rows if r]
        if include_system:
            return names
        return [n for n in names if n.lower() not in SYSTEM_SCHEMAS_MYSQL]

    def list_tables(self, database: str = "") -> list[TableInfo]:
        db = (database or self.conn.get("database") or "").strip()
        if not db:
            raise DriverError(t("请指定数据库（--database）"))
        sql = (
            "SELECT TABLE_NAME, TABLE_TYPE, ENGINE, TABLE_ROWS "
            "FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA = {_lit(db)} ORDER BY TABLE_NAME"
        )
        res = self._exec(sql, expect_table=True)
        out: list[TableInfo] = []
        for row in res.rows:
            name = str(row[0])
            kind_raw = str(row[1] or "BASE TABLE").upper()
            kind = "view" if "VIEW" in kind_raw else "table"
            engine = str(row[2] or "")
            approx = None
            try:
                if row[3] is not None and str(row[3]) != "":
                    approx = int(row[3])
            except (TypeError, ValueError):
                approx = None
            out.append(TableInfo(name=name, kind=kind, engine=engine,
                                 rows_approx=approx))
        return out

    def list_columns(self, table: str, database: str = "") -> list[ColumnInfo]:
        db = (database or self.conn.get("database") or "").strip()
        if not db:
            raise DriverError(t("请指定数据库（--database）"))
        tbl = (table or "").strip()
        if not tbl:
            raise DriverError(t("请指定表名"))
        sql = (
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, "
            "COLUMN_DEFAULT, EXTRA FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = {_lit(db)} AND TABLE_NAME = {_lit(tbl)} "
            "ORDER BY ORDINAL_POSITION"
        )
        res = self._exec(sql, expect_table=True)
        cols: list[ColumnInfo] = []
        for row in res.rows:
            cols.append(ColumnInfo(
                name=str(row[0]),
                type=str(row[1] or ""),
                nullable=str(row[2] or "").upper() == "YES",
                primary_key=str(row[3] or "").upper() == "PRI",
                default=None if row[4] is None else str(row[4]),
                extra=str(row[5] or ""),
            ))
        return cols

    def select_table(self, table: str, *, database: str = "",
                     filters: Optional[list[Filter]] = None,
                     order: str = "",
                     limit: int = 200,
                     offset: int = 0) -> QueryResult:
        db = (database or self.conn.get("database") or "").strip()
        if not db:
            raise DriverError(t("请指定数据库（--database）"))
        tbl = (table or "").strip()
        if not tbl:
            raise DriverError(t("请指定表名"))
        lim = clamp_limit(limit)
        off = max(0, int(offset or 0))
        where, _notes = build_where(filters or [], engine="mysql")
        order_sql = ""
        if order:
            # order: "col" or "col desc"
            parts = order.strip().split()
            col = parts[0]
            direction = parts[1].upper() if len(parts) > 1 else "ASC"
            if direction not in ("ASC", "DESC"):
                direction = "ASC"
            order_sql = f" ORDER BY {quote_ident(col, engine='mysql')} {direction}"
        fq = f"{quote_ident(db)}.{quote_ident(tbl)}"
        # 多取 1 行判断 truncated
        sql = (f"SELECT * FROM {fq}{where}{order_sql} "
               f"LIMIT {lim + 1} OFFSET {off}")
        res = self._exec(sql, expect_table=True)
        truncated = len(res.rows) > lim
        if truncated:
            res.rows = res.rows[:lim]
        res.truncated = truncated
        res.row_count = len(res.rows)
        res.sql = sql.replace(f"LIMIT {lim + 1}", f"LIMIT {lim}")
        return res

    def query(self, sql: str, *, limit: int = 200) -> QueryResult:
        """只读自由 SQL：仅允许单条 SELECT/SHOW/DESCRIBE/EXPLAIN/WITH。"""
        text = (sql or "").strip().rstrip(";")
        if not text or ";" in text:
            raise DriverError(t("只允许单条只读语句"))
        head = text.split(None, 1)[0].lower()
        if head not in ("select", "show", "describe", "desc", "explain", "with"):
            raise DriverError(t("自由 SQL 仅允许 SELECT / SHOW / DESCRIBE / EXPLAIN / WITH"))
        lim = clamp_limit(limit)
        # 外层包一层限制行数（SHOW 等可能不支持，失败则原样执行再截断）
        wrapped = f"SELECT * FROM ({text}) AS _phpvm_q LIMIT {lim + 1}"
        try:
            res = self._exec(wrapped, expect_table=True)
            res.sql = text
        except DriverError:
            res = self._exec(text, expect_table=True)
            res.sql = text
        if len(res.rows) > lim:
            res.rows = res.rows[:lim]
            res.truncated = True
        res.row_count = len(res.rows)
        return res

    # ------------------------------------------------------------------ #
    def _server_version(self) -> str:
        try:
            res = self._exec("SELECT VERSION()", expect_table=True)
            if res.rows and res.rows[0]:
                return str(res.rows[0][0])
        except DriverError:
            pass
        return ""

    def _exec(self, sql: str, *, expect_table: bool = False,
              timeout: float = 30.0) -> QueryResult:
        cnf = self._write_cnf()
        t0 = time.perf_counter()
        try:
            argv = [
                self.cli,
                f"--defaults-extra-file={cnf}",
                "--batch",
                "--raw",
                "--default-character-set=utf8mb4",
                "-e", sql,
            ]
            db = (self.conn.get("database") or "").strip()
            if db:
                argv.extend(["--database", db])
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    check=False,
                    env=_clean_env(),
                )
            except subprocess.TimeoutExpired as e:
                raise DriverError(t("查询超时（{sec}s）", sec=int(timeout)),
                                  kind="timeout") from e
            except FileNotFoundError as e:
                raise DriverError(t("未找到 mysql 客户端"), kind="no_tool") from e
        finally:
            try:
                os.remove(cnf)
            except OSError:
                pass

        ms = (time.perf_counter() - t0) * 1000
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            kind = "auth" if "Access denied" in err else "failed"
            raise DriverError(err or t("mysql 执行失败（退出码 {code}）",
                                       code=proc.returncode), kind=kind)

        columns, rows = _parse_batch(proc.stdout or "")
        if expect_table and not columns and not rows and (proc.stdout or "").strip():
            # 某些 SHOW 在极老客户端上无表头；尽量兜住
            pass
        return QueryResult(columns=columns, rows=rows, elapsed_ms=round(ms, 1),
                           sql=sql, row_count=len(rows))

    def _write_cnf(self) -> str:
        host = (self.conn.get("host") or "127.0.0.1").strip()
        port = int(self.conn.get("port") or 3306)
        user = (self.conn.get("user") or "").strip()
        password = self.conn.get("password") or ""
        socket = (self.conn.get("socket") or "").strip()
        lines = ["[client]", f"user={user}", f"host={host}", f"port={port}"]
        if password:
            # 值可能含特殊字符：用引号包一层并转义
            safe = password.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'password="{safe}"')
        if socket:
            lines.append(f"socket={socket}")
        fd, path = tempfile.mkstemp(prefix="phpvm-mysql-", suffix=".cnf")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            set_secret_permissions(path)
        except OSError:
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        return path


def _lit(value: str) -> str:
    s = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return "'" + s + "'"


def _parse_batch(text: str) -> tuple[list[str], list[list[str]]]:
    """解析 ``mysql --batch`` 输出（TSV，首行表头）。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return [], []
    reader = csv.reader(io.StringIO(text), delimiter="\t",
                        quoting=csv.QUOTE_MINIMAL)
    rows = list(reader)
    if not rows:
        return [], []
    header = [c for c in rows[0]]
    data = [list(r) for r in rows[1:]]
    # 对齐列数
    width = len(header)
    fixed = []
    for r in data:
        if len(r) < width:
            r = r + [""] * (width - len(r))
        elif len(r) > width:
            r = r[:width]
        fixed.append(r)
    return header, fixed


def _clean_env() -> dict:
    env = dict(os.environ)
    env.pop("MYSQL_PWD", None)  # 避免继承不安全的明文环境变量抢优先
    return env
