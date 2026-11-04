# -*- coding: utf-8 -*-
"""键鼠统计的 SQLite 存储：日桶 + 分钟桶，写入批量、读取只读。

为什么两层聚合
--------------
- ``key_day(day, vk, count)``：日 × 键维度，支撑日/周/月/年/全部与键位排名。
  每天约 20~40 行（实际按下的键数），一年万级，查询与体积都划算。
- ``typing_minute(ts_minute, presses, peak_kpm)``：分钟维度，支撑按小时分布、
  打字速度峰值与活跃分钟均值。每天最多 1440 行，一年十万级。
- 不做「每键每分钟」：那样每天 2~3 万行、一年千万级，查询与体积都不划算。

写入由守护进程聚合后每 2 秒批量提交一次（WAL + 单事务），读取（面板 / CLI）
用 ``mode=ro`` 打开，互不阻塞。数据只在本机用户数据目录，不上传。
"""
import os
import sqlite3
from urllib.parse import quote

from ... import app_paths

SCHEMA_VERSION = 1
SUB_DIR = os.path.join("plugins", "keymouse")
DB_NAME = "stats.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS key_day (
    day   TEXT    NOT NULL,           -- 本地日期 YYYY-MM-DD
    vk    INTEGER NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (day, vk)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS typing_minute (
    ts_minute INTEGER PRIMARY KEY,    -- epoch 分钟
    presses   INTEGER NOT NULL,
    peak_kpm  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


# --------------------------------------------------------------------------- #
# 路径 / 连接
# --------------------------------------------------------------------------- #
def data_dir() -> str:
    """插件私有数据目录（``<数据目录>/plugins/keymouse``，按需创建）。"""
    path = os.path.join(app_paths.data_dir(), SUB_DIR)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def db_path() -> str:
    return os.path.join(data_dir(), DB_NAME)


def exists(path: str | None = None) -> bool:
    return os.path.isfile(path or db_path())


def _uri(path: str) -> str:
    """Windows 反斜杠 / 空格 / 中文都安全的 SQLite URI。"""
    return "file:" + quote(os.path.abspath(path).replace("\\", "/"), safe="/:")


def open_readonly(path: str | None = None) -> sqlite3.Connection | None:
    """只读连接；数据库不存在或打不开时返回 None（调用方按「无数据」处理）。"""
    target = path or db_path()
    if not os.path.isfile(target):
        return None
    try:
        conn = sqlite3.connect(_uri(target) + "?mode=ro", uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        # 只读连接上也允许设置查询超时，避免脏 WAL 把 UI 线程挂住
        conn.execute("PRAGMA query_only=ON")
        return conn
    except sqlite3.Error:
        return None


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


class Writer:
    """写入端：调用方在内存聚合，``flush()`` 一次事务落库。"""

    def __init__(self, path: str | None = None):
        self.path = path or db_path()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=10.0)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        _ensure_schema(self.conn)

    def flush(self, keys: dict, minutes: dict) -> int:
        """批量 upsert。keys={(day, vk): n}，minutes={ts_minute: (presses, peak_kpm)}。

        返回写入的行数（便于守护进程日志观察采集是否真的在落库）。
        """
        if not keys and not minutes:
            return 0
        rows_key = [(day, int(vk), int(n)) for (day, vk), n in keys.items()
                    if int(n) > 0]
        rows_min = [(int(ts), int(p), int(k)) for ts, (p, k) in minutes.items()
                    if int(p) > 0]
        with self.conn:  # 单事务提交，避免每键一次 fsync
            if rows_key:
                self.conn.executemany(
                    "INSERT INTO key_day(day, vk, count) VALUES(?,?,?) "
                    "ON CONFLICT(day, vk) DO UPDATE SET count = count + excluded.count",
                    rows_key,
                )
            if rows_min:
                self.conn.executemany(
                    "INSERT INTO typing_minute(ts_minute, presses, peak_kpm) "
                    "VALUES(?,?,?) ON CONFLICT(ts_minute) DO UPDATE SET "
                    "presses = presses + excluded.presses, "
                    "peak_kpm = MAX(peak_kpm, excluded.peak_kpm)",
                    rows_min,
                )
        return len(rows_key) + len(rows_min)

    def cleanup(self, retention_days: int) -> int:
        """删除早于保留期的数据，返回删除行数（retention_days<=0 表示永久保留）。"""
        if not retention_days or retention_days <= 0:
            return 0
        cutoff_ts = _now_minute() - int(retention_days) * 1440
        cutoff_day = _day_str_from_minute(cutoff_ts)
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM typing_minute WHERE ts_minute < ?", (cutoff_ts,))
            removed = cur.rowcount or 0
            cur = self.conn.execute(
                "DELETE FROM key_day WHERE day < ?", (cutoff_day,))
            removed += cur.rowcount or 0
        return removed

    def clear(self) -> None:
        """清空全部统计（二次确认由调用方负责）。"""
        with self.conn:
            self.conn.execute("DELETE FROM key_day")
            self.conn.execute("DELETE FROM typing_minute")

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass


# --------------------------------------------------------------------------- #
# 时间小工具（避免 store 依赖 stats，防止循环导入）
# --------------------------------------------------------------------------- #
def _now_minute() -> int:
    import time
    return int(time.time()) // 60


def _day_str_from_minute(ts_minute: int) -> str:
    import time
    return time.strftime("%Y-%m-%d", time.localtime(int(ts_minute) * 60))


# --------------------------------------------------------------------------- #
# 读取（全部走主键前缀，配合索引即毫秒级）
# --------------------------------------------------------------------------- #
def key_total(conn: sqlite3.Connection, start_day: str, end_day: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(count), 0) FROM key_day WHERE day >= ? AND day < ?",
        (start_day, end_day)).fetchone()
    return int(row[0] or 0)


def top_keys(conn: sqlite3.Connection, start_day: str, end_day: str,
             limit: int = 10) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT vk, SUM(count) AS c FROM key_day "
        "WHERE day >= ? AND day < ? GROUP BY vk ORDER BY c DESC, vk ASC LIMIT ?",
        (start_day, end_day, int(limit))).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def distinct_keys(conn: sqlite3.Connection, start_day: str, end_day: str) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT vk) FROM key_day WHERE day >= ? AND day < ?",
        (start_day, end_day)).fetchone()
    return int(row[0] or 0)


def typing_totals(conn: sqlite3.Connection, start_ts: int, end_ts: int) -> dict:
    """区间内的分钟级汇总（聚合在 SQL 里做，不把几十万行拉进 Python）。"""
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(presses), 0), COALESCE(MAX(peak_kpm), 0) "
        "FROM typing_minute WHERE ts_minute >= ? AND ts_minute < ?",
        (int(start_ts), int(end_ts))).fetchone()
    active, presses, peak = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
    return {
        "active_minutes": active,
        "presses": presses,
        "peak_kpm": peak,
        "avg_kpm": round(presses / active, 1) if active else 0.0,
    }


def typing_minutes(conn: sqlite3.Connection, start_ts: int, end_ts: int) -> list[tuple[int, int]]:
    """分钟桶 (ts_minute, presses)，仅用于「按小时分布」这种小范围统计。"""
    rows = conn.execute(
        "SELECT ts_minute, presses FROM typing_minute "
        "WHERE ts_minute >= ? AND ts_minute < ? ORDER BY ts_minute",
        (int(start_ts), int(end_ts))).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def last_minute_presses(conn: sqlite3.Connection, start_ts: int, end_ts: int) -> int:
    row = conn.execute(
        "SELECT presses FROM typing_minute "
        "WHERE ts_minute >= ? AND ts_minute < ? ORDER BY ts_minute DESC LIMIT 1",
        (int(start_ts), int(end_ts))).fetchone()
    return int(row[0]) if row else 0


def first_day(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MIN(day) FROM key_day").fetchone()
    return str(row[0]) if row and row[0] else ""


def info(conn: sqlite3.Connection | None, path: str | None = None) -> dict:
    """数据库概况（面板 / CLI 状态展示用）。"""
    target = path or db_path()
    size = 0
    try:
        size = os.path.getsize(target)
    except OSError:
        pass
    out = {"path": target, "exists": os.path.isfile(target), "size": size,
           "first_day": "", "last_day": "", "keys": 0, "minutes": 0}
    if conn is None:
        return out
    try:
        row = conn.execute(
            "SELECT MIN(day), MAX(day), COUNT(*) FROM key_day").fetchone()
        if row:
            out["first_day"] = str(row[0] or "")
            out["last_day"] = str(row[1] or "")
            out["keys"] = int(row[2] or 0)
        row = conn.execute("SELECT COUNT(*) FROM typing_minute").fetchone()
        out["minutes"] = int(row[0] or 0) if row else 0
    except sqlite3.Error:
        pass
    return out
