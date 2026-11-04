# -*- coding: utf-8 -*-
"""键鼠统计的时间范围换算与聚合（纯函数 + 只读查询，可在 CLI / 面板 / 测试复用）。

时间口径
--------
- 日桶用**本地日期字符串**（``YYYY-MM-DD``），事件当场按本地时区归日，
  因此跨时区 / 夏令时切换不会让历史数据错位；
- 范围查询在 Python 侧换算成 [start_day, end_day) 与 epoch 分钟区间，
  日桶走字符串比较、分钟桶走整数区间，都是主键前缀扫描；
- ``today`` 的日均 = 当日总量（与「疯键狂鼠」一致），周/月/年按已过天数摊平。
"""
import csv
import datetime
import os

from ...i18n import t
from . import keymap, store

RANGES = ("today", "week", "month", "year", "all")
RANGE_LABELS = {
    "today": "今日",
    "week": "本周",
    "month": "本月",
    "year": "本年",
    "all": "全部",
}
# 只有这些范围提供「按小时分布」（更长的区间拉分钟行不划算，也看不出规律）
HOURLY_RANGES = ("today", "week")


def _today(now: float | None = None) -> datetime.date:
    return datetime.datetime.fromtimestamp(now).date() if now else datetime.date.today()


def _start_ts(day: datetime.date) -> int:
    return int(datetime.datetime.combine(day, datetime.time.min).timestamp())


def normalize_range(value: str | None) -> str:
    key = str(value or "").strip().lower()
    return key if key in RANGES else "today"


def bounds(range_key: str, now: float | None = None,
           first_day: str = "") -> dict:
    """返回区间边界：``[start_day, end_day)``、epoch 分钟区间与统计天数。"""
    key = normalize_range(range_key)
    today = _today(now)
    first = today
    if key == "week":
        first = today - datetime.timedelta(days=today.weekday())
    elif key == "month":
        first = today.replace(day=1)
    elif key == "year":
        first = today.replace(month=1, day=1)
    elif key == "all":
        try:
            first = datetime.date.fromisoformat(first_day) if first_day else today
        except ValueError:
            first = today
        if first > today:
            first = today
    end = today + datetime.timedelta(days=1)
    return {
        "range": key,
        "start_day": first.isoformat(),
        "end_day": end.isoformat(),
        "start_ts": _start_ts(first) // 60,
        "end_ts": _start_ts(end) // 60,
        "days": max(1, (today - first).days + 1),
    }


def empty_summary(range_key: str = "today") -> dict:
    """无数据库 / 无数据时的稳定结构（面板与 CLI 都按它渲染）。"""
    key = normalize_range(range_key)
    b = bounds(key)
    return {
        "range": key,
        "label": t(RANGE_LABELS[key]),
        "start_day": b["start_day"],
        "end_day": b["end_day"],
        "days": b["days"],
        "has_data": False,
        "total_presses": 0,
        "daily_avg": 0.0,
        "key_count": 0,
        "top_keys": [],
        "hourly": [],
        "hourly_supported": key in HOURLY_RANGES,
        "peak_hour": -1,
        "active_minutes": 0,
        "kpm_avg": 0.0,
        "kpm_peak": 0,
        "kpm_last": 0,
        "first_day": "",
        "db": store.info(None),
    }


def _hourly(minutes: list[tuple[int, int]]) -> list[int]:
    """分钟桶 → 24 个小时桶（用本地时间换算，正确处理时区 / 夏令时）。"""
    hours = [0] * 24
    for ts_minute, presses in minutes:
        hour = datetime.datetime.fromtimestamp(ts_minute * 60).hour
        hours[hour] += presses
    return hours


def summary(range_key: str = "today", top: int = 10,
            path: str | None = None, now: float | None = None) -> dict:
    """聚合一个时间范围的完整结果（面板卡片 / 排名 / 小时图 / CLI 共用）。"""
    key = normalize_range(range_key)
    top = max(1, min(int(top or 10), 100))
    conn = store.open_readonly(path)
    if conn is None:
        return empty_summary(key)

    try:
        first_day = store.first_day(conn)
        b = bounds(key, now, first_day)
        total = store.key_total(conn, b["start_day"], b["end_day"])
        tops = store.top_keys(conn, b["start_day"], b["end_day"], top)
        typing = store.typing_totals(conn, b["start_ts"], b["end_ts"])
        key_count = store.distinct_keys(conn, b["start_day"], b["end_day"])

        hours: list[int] = []
        if key in HOURLY_RANGES:
            hours = _hourly(store.typing_minutes(conn, b["start_ts"], b["end_ts"]))
        peak_hour = hours.index(max(hours)) if hours and max(hours) > 0 else -1
        kpm_last = (store.last_minute_presses(conn, b["start_ts"], b["end_ts"])
                    if key == "today" else 0)
        db_info = store.info(conn)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    max_count = tops[0][1] if tops else 0
    top_keys = [{
        "vk": vk,
        "key": keymap.name_of(vk),
        "count": count,
        "pct": round(count / max_count, 3) if max_count else 0.0,
        "share": round(count / total, 3) if total else 0.0,
    } for vk, count in tops]

    return {
        "range": key,
        "label": t(RANGE_LABELS[key]),
        "start_day": b["start_day"],
        "end_day": b["end_day"],
        "days": b["days"],
        "has_data": bool(total or typing["presses"]),
        "total_presses": total,
        "daily_avg": round(total / b["days"], 1),
        "key_count": key_count,
        "top_keys": top_keys,
        "hourly": hours,
        "hourly_supported": key in HOURLY_RANGES,
        "peak_hour": peak_hour,
        "active_minutes": typing["active_minutes"],
        "kpm_avg": typing["avg_kpm"],
        "kpm_peak": typing["peak_kpm"],
        "kpm_last": kpm_last,
        "first_day": first_day,
        "db": db_info,
    }


# --------------------------------------------------------------------------- #
# 导出 / 报告
# --------------------------------------------------------------------------- #
CSV_HEADER = ("section", "item", "value")


def export_csv(range_key: str = "today", out_path: str = "", top: int = 10,
               path: str | None = None, now: float | None = None) -> dict:
    """把一次聚合结果写成 CSV（utf-8-sig，Excel 直接打开不乱码）。"""
    if not out_path:
        return {"ok": False, "message": t("未指定导出路径")}
    data = summary(range_key, top=top, path=path, now=now)
    rows: list[tuple[str, str, object]] = [
        ("summary", "range", data["range"]),
        ("summary", "start_day", data["start_day"]),
        ("summary", "end_day", data["end_day"]),
        ("summary", "days", data["days"]),
        ("summary", "total_presses", data["total_presses"]),
        ("summary", "daily_avg", data["daily_avg"]),
        ("summary", "key_count", data["key_count"]),
        ("summary", "kpm_avg", data["kpm_avg"]),
        ("summary", "kpm_peak", data["kpm_peak"]),
        ("summary", "active_minutes", data["active_minutes"]),
    ]
    rows += [("key", item["key"], item["count"]) for item in data["top_keys"]]
    rows += [("hour", f"{i:02d}", v) for i, v in enumerate(data["hourly"])]
    try:
        directory = os.path.dirname(os.path.abspath(out_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)
            writer.writerows(rows)
    except OSError as e:
        return {"ok": False, "message": t("写入失败：{err}", err=e)}
    return {"ok": True, "path": out_path, "rows": len(rows), "data": data}


def report_text(data: dict) -> str:
    """把聚合结果排成只读文本报告（面板「显示报告」/ CLI 输出）。"""
    lines = [
        t("键鼠统计 · {label}（{start} ~ {end}）",
          label=data.get("label", ""), start=data.get("start_day", ""),
          end=data.get("end_day", "")),
        "",
        t("按键总数：{total} 次 · 日均 {avg} 次 · 覆盖 {days} 天",
          total=f"{data.get('total_presses', 0):,}",
          avg=data.get("daily_avg", 0), days=data.get("days", 1)),
        t("打字速度：均值 {avg} 次/分 · 峰值 {peak} 次/分 · 活跃 {minutes} 分钟",
          avg=data.get("kpm_avg", 0), peak=data.get("kpm_peak", 0),
          minutes=data.get("active_minutes", 0)),
    ]
    tops = data.get("top_keys") or []
    if tops:
        lines += ["", t("键位排名（Top {n}）", n=len(tops))]
        for i, item in enumerate(tops, 1):
            lines.append(f"  {i:>2}. {item['key']:<12} {item['count']:>6}")
    hours = data.get("hourly") or []
    if hours and max(hours) > 0:
        lines += ["", t("按小时分布")]
        peak = max(hours)
        for hour, value in enumerate(hours):
            bar = "█" * max(0, round(value / peak * 20))
            lines.append(f"  {hour:02d}  {bar:<20} {value}")
    if not data.get("has_data"):
        lines += ["", t("暂无记录（后台记录尚未产生数据）")]
    return "\n".join(lines)
