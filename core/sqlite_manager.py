# -*- coding: utf-8 -*-
"""SQLite 数据库查询管理（只读）。

定位：站点自带的 SQLite 库（WordPress / Typecho / 各类小工具的 *.db、*.sqlite、
*.sqlite3）快速查看与查询。当前只提供「基础查询」能力，不做增删改与结构变更：

- discover()：在环境根与站点目录内递归发现数据库文件；
- open()：以只读方式打开（URI ``mode=ro`` + ``PRAGMA query_only``），杜绝误写；
- tables() / columns() / count_rows()：表与视图清单、列定义、行数统计；
- query()：执行查询类语句（SELECT / WITH / PRAGMA / EXPLAIN / VALUES），
  限制返回行数（默认 500）与执行时长（progress handler 超时中断）。

线程约定：连接以 ``check_same_thread=False`` 创建并用 RLock 串行化，
可安全地在工作线程中调用（面板即在后台线程里跑查询）。
"""
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote

from .config import WNRP_ROOT, Config
from .i18n import t

# 识别的数据库文件后缀
DB_EXTS = (".db", ".sqlite", ".sqlite3", ".db3")
QUERY_LIMIT = 500        # 单次查询最多返回行数（超出截断并提示）
QUERY_TIMEOUT = 10.0     # 单次查询最长执行时间（秒）
COUNT_TIMEOUT = 3.0      # 单表行数统计超时（秒），超时返回 None
DISCOVER_MAX_DEPTH = 4   # 目录扫描深度上限（相对各根目录）
DISCOVER_LIMIT = 300     # 最多发现的文件数
# 扫描时跳过的目录（依赖/缓存/版本库，避免拖慢与误报）
_SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", ".Trash", "site-packages", "dist", "build",
}
# 允许的查询类语句首关键字（只读连接兜底，双重保险）
_ALLOWED_HEADS = ("select", "with", "pragma", "explain", "values")


class SqliteNotOpenError(RuntimeError):
    """尚未打开任何数据库文件。"""


@dataclass
class TableInfo:
    """表 / 视图条目。"""

    name: str
    kind: str          # table / view


@dataclass
class ColumnInfo:
    """列定义（来自 PRAGMA table_info）。"""

    name: str
    type: str
    notnull: bool
    pk: bool
    default: str


@dataclass
class QueryResult:
    """查询结果：列名 + 行数据 + 是否截断 + 耗时（毫秒）。"""

    columns: list
    rows: list
    truncated: bool = False
    elapsed: float = 0.0


def quote_ident(name: str) -> str:
    """把标识符安全地包成双引号形式（``a"b`` → ``"a""b"``）。"""
    return '"' + str(name).replace('"', '""') + '"'


def format_value(value) -> str:
    """把单元格值转成单行显示文本：NULL / BLOB 摘要 / 换行转义 / 超长截断。"""
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<BLOB {len(bytes(value))} B>"
    text = repr(value) if isinstance(value, float) else str(value)
    text = text.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    text = text.replace("\t", "\\t")
    if len(text) > 300:
        text = text[:300] + "…"
    return text


def _readonly_uri(path: str) -> str:
    """生成 SQLite 只读 URI（兼容 Windows 盘符与含空格/中文的路径）。"""
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return "file://" + quote(p) + "?mode=ro"


def _strip_leading_comments(sql: str) -> str:
    """去掉开头的空白与 ``--`` / ``/* */`` 注释，便于识别语句类型。"""
    s = (sql or "").lstrip()
    while s:
        if s.startswith("--"):
            nl = s.find("\n")
            if nl < 0:
                return ""
            s = s[nl + 1:].lstrip()
        elif s.startswith("/*"):
            end = s.find("*/")
            if end < 0:
                return ""
            s = s[end + 2:].lstrip()
        else:
            break
    return s


def first_keyword(sql: str) -> str:
    """返回语句首个关键字（小写）；空语句返回空串。"""
    s = _strip_leading_comments(sql)
    if not s:
        return ""
    return s.split(None, 1)[0].strip("(;").lower()


class SqliteManager:
    """SQLite 只读查询管理：发现文件、打开连接、读表结构、执行查询。"""

    def __init__(self, config: Config | None = None, roots: list | None = None):
        self.config = config
        self.roots = list(roots) if roots else [WNRP_ROOT]
        self._conn: sqlite3.Connection | None = None
        self._path = ""
        self._lock = threading.RLock()

    # ---- 最近打开的数据库（跨会话记忆） ----
    def last_path(self) -> str:
        if self.config is None:
            return ""
        return self.config.get_setting("sqlite_last_db", "") or ""

    def remember(self, path: str) -> None:
        if self.config is not None:
            self.config.set_setting("sqlite_last_db", os.path.abspath(path))

    # ---- 状态 ----
    @property
    def path(self) -> str:
        """当前已打开的数据库路径；未打开时为空串。"""
        return self._path

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise SqliteNotOpenError(t("未选择数据库文件。"))
        return self._conn

    # ---- 发现数据库文件 ----
    def discover(self, roots: list | None = None, max_depth: int = DISCOVER_MAX_DEPTH,
                 limit: int = DISCOVER_LIMIT) -> list:
        """递归发现数据库文件，返回排序后的绝对路径列表。"""
        found: list = []
        seen = set()
        for root in (self.roots if roots is None else roots):
            if not root or not os.path.isdir(root):
                continue
            base = os.path.abspath(root).rstrip(os.sep)
            base_depth = base.count(os.sep)
            for cur, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs
                           if d not in _SKIP_DIRS and not d.startswith(".")]
                if cur.count(os.sep) - base_depth >= max_depth:
                    dirs[:] = []
                for name in files:
                    if not name.lower().endswith(DB_EXTS):
                        continue
                    full = os.path.join(cur, name)
                    if full in seen:
                        continue
                    seen.add(full)
                    found.append(full)
                    if len(found) >= limit:
                        return sorted(found)
        return sorted(found)

    # ---- 打开 / 关闭 ----
    def open(self, path: str) -> None:
        """以只读方式打开数据库；失败抛 sqlite3.Error / FileNotFoundError。"""
        full = os.path.abspath(path)
        if not os.path.isfile(full):
            raise FileNotFoundError(t("文件不存在：{path}", path=full))
        self.close()
        try:
            conn = sqlite3.connect(_readonly_uri(full), uri=True, timeout=5.0,
                                   check_same_thread=False)
            conn.execute("PRAGMA query_only = 1")
            conn.execute("SELECT count(*) FROM sqlite_master")
        except sqlite3.Error as e:
            msg = str(e).lower()
            if "not a database" in msg or "encrypted" in msg:
                raise sqlite3.DatabaseError(
                    t("不是有效的 SQLite 数据库文件：{path}", path=full)) from e
            raise sqlite3.DatabaseError(t("打开失败：{err}", err=e)) from e
        with self._lock:
            self._conn = conn
            self._path = full
        self.remember(full)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
            self._conn = None
            self._path = ""

    # ---- 结构 ----
    def tables(self) -> list:
        """返回表与视图清单（跳过 sqlite_ 内部表，表在前、按名称排序）。"""
        with self._lock:
            conn = self._require()
            cur = conn.execute(
                "SELECT name, type FROM sqlite_master "
                "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
                "ORDER BY type ASC, name COLLATE NOCASE"
            )
            return [TableInfo(name=r[0], kind=r[1]) for r in cur.fetchall()]

    def columns(self, table: str) -> list:
        """返回列定义（PRAGMA table_info，参数化绑定，避免标识符拼接）。"""
        with self._lock:
            conn = self._require()
            rows = conn.execute(
                'SELECT name, type, "notnull", dflt_value, pk '
                "FROM pragma_table_info(?)",
                (table,),
            ).fetchall()
        return [
            ColumnInfo(
                name=r[0],
                type=r[1] or "",
                notnull=bool(r[2]),
                pk=bool(r[4]),
                default="" if r[3] is None else str(r[3]),
            )
            for r in rows
        ]

    def count_rows(self, table: str, timeout: float = COUNT_TIMEOUT):
        """统计表行数；超时或失败返回 None（大表不阻塞界面）。"""
        try:
            res = self._run(f"SELECT COUNT(*) FROM {quote_ident(table)}", 1, timeout)
        except (sqlite3.Error, SqliteNotOpenError):
            return None
        if not res.rows:
            return 0
        try:
            return int(res.rows[0][0])
        except (TypeError, ValueError):
            return None

    # ---- 查询 ----
    def query(self, sql: str, limit: int = QUERY_LIMIT,
              timeout: float = QUERY_TIMEOUT) -> QueryResult:
        """执行查询类语句；非查询语句抛 ValueError，执行失败抛 sqlite3.Error。"""
        if first_keyword(sql) not in _ALLOWED_HEADS:
            raise ValueError(
                t("仅支持查询语句（SELECT / WITH / PRAGMA / EXPLAIN / VALUES）。"))
        return self._run(sql, limit, timeout)

    def _run(self, sql: str, limit: int, timeout: float) -> QueryResult:
        with self._lock:
            conn = self._require()
            deadline = time.monotonic() + max(0.5, float(timeout))
            conn.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline else 0, 20000)
            start = time.perf_counter()
            try:
                cur = conn.execute(sql)
                columns = [d[0] for d in (cur.description or [])]
                rows = cur.fetchmany(limit + 1) if columns else []
                truncated = len(rows) > limit
                if truncated:
                    rows = rows[:limit]
            except sqlite3.OperationalError as e:
                if "interrupt" in str(e).lower():
                    raise sqlite3.OperationalError(
                        t("查询超时（超过 {sec} 秒），已中断。请添加 LIMIT 或优化查询条件。",
                          sec=int(timeout))) from e
                raise
            finally:
                try:
                    conn.set_progress_handler(None, 0)
                except sqlite3.Error:
                    pass
            elapsed = (time.perf_counter() - start) * 1000
        return QueryResult(columns=columns, rows=rows,
                           truncated=truncated, elapsed=elapsed)
