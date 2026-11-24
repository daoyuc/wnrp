# -*- coding: utf-8 -*-
"""跨面板共用的确认文案与对话框。

破坏性动作（全部停止等）必须走这里，避免总览 / 设置 / 托盘各写一份而漂移。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from core.i18n import t

# 与 messagebox 标题一致，便于测试与文案扫描
STOP_ALL_TITLE = "全部停止"
STOP_ALL_BODY = (
    "将停止 Nginx、全部 PHP 版本、Redis 与 MySQL，"
    "本机站点会全部不可访问。\n确定继续？"
)


def confirm_stop_all(parent: tk.Misc | None = None) -> bool:
    """全部停止前的统一确认；用户取消返回 False。"""
    return bool(messagebox.askyesno(
        t(STOP_ALL_TITLE),
        t(STOP_ALL_BODY),
        parent=parent,
    ))


def offer_restart_now(parent: tk.Misc | None, *, title: str, detail: str) -> bool:
    """改动需重启才生效时：说明原因并询问是否立即重启。

    返回 True 表示用户选择立即重启（调用方负责执行重启）。
    """
    return bool(messagebox.askyesno(
        title,
        detail + "\n\n" + t("立即重启 phpvm 以使改动生效？"),
        parent=parent,
    ))
