# -*- coding: utf-8 -*-
"""自愈操作历史：内存 + 磁盘（recover_history.json）双写。

供崩溃详情对话框的「自愈历史」页可视化展示，方便追溯每次崩溃自愈的
决策（重启 / 失败 / 防抖跳过 / 达上限）与结果。
"""
import json
import os
import threading
import time

_HISTORY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recover_history.json")
_MAX_ENTRIES = 200
_lock = threading.Lock()

# action -> 展示文案
ACTION_LABELS = {
    "start": "自愈重启",
    "fail": "自愈失败",
    "skip_interval": "防抖跳过",
    "skip_limit": "已达上限",
    "manual": "手动重启",
}


def load() -> list[dict]:
    """读取历史记录（最新在前）。文件缺失/损坏时返回空列表。"""
    try:
        with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def append(version: str, action: str, detail: str) -> list[dict]:
    """追加一条记录并持久化，返回最新列表（最新在前）。"""
    entry = {
        "ts": time.time(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "version": version,
        "action": action,
        "detail": detail,
    }
    with _lock:
        history = load()
        history.insert(0, entry)
        del history[_MAX_ENTRIES:]
        try:
            with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
        return history
