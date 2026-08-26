# -*- coding: utf-8 -*-
"""统一主题：配色 / 字体 / ttk 样式。

设计基调：Windows 桌面工具，简洁高效、状态可视化。
- 主色：#2B579A / #1E3F73（蓝）
- 背景：#F5F5F5 / #FFFFFF，文本 #333333
- 功能色：绿=运行 / 红=异常 / 灰=停止 / 橙=警告
- 字体：Microsoft YaHei
"""
import tkinter as tk
from tkinter import ttk

# ---- 调色板 ----
PRIMARY = "#2B579A"
PRIMARY_DARK = "#1E3F73"
PRIMARY_LIGHT = "#E8EDF5"
BG = "#F5F5F5"
CARD_BG = "#FFFFFF"
TEXT = "#333333"
TEXT_DIM = "#666666"
OK = "#107C10"
ERR = "#D13438"
WARN = "#FF8C00"
GRAY = "#A0A0A0"
LOG_BG = "#1E1E1E"
LOG_FG = "#C8C8C8"
LOG_ACCENT = "#7FB0FF"

FONT = "Microsoft YaHei"


def setup_style(root: tk.Misc) -> ttk.Style:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    # 全局
    style.configure(".", font=(FONT, 9), background=BG, foreground=TEXT)

    # 标题
    style.configure("Title.TLabel", font=(FONT, 13, "bold"), foreground=PRIMARY_DARK, background=CARD_BG)
    style.configure("SubTitle.TLabel", font=(FONT, 8), foreground=TEXT_DIM, background=CARD_BG)
    style.configure("Section.TLabel", font=(FONT, 9, "bold"), foreground=PRIMARY_DARK, background=BG)
    style.configure("Status.TLabel", font=(FONT, 8), foreground=TEXT_DIM, background=PRIMARY_LIGHT)

    # 卡片 / 状态栏
    style.configure("Card.TFrame", background=CARD_BG)
    style.configure("Status.TFrame", background=PRIMARY_LIGHT)

    # 按钮
    style.configure("TButton", font=(FONT, 9), padding=(10, 5), background="#FFFFFF", foreground=TEXT)
    style.map("TButton", background=[("active", PRIMARY_LIGHT), ("pressed", "#D3DEED")])

    style.configure(
        "Accent.TButton", font=(FONT, 9, "bold"),
        padding=(14, 6), background=PRIMARY, foreground="#FFFFFF", borderwidth=0,
    )
    style.map(
        "Accent.TButton",
        background=[("active", PRIMARY_DARK), ("pressed", PRIMARY_DARK), ("disabled", "#8FA8CC")],
    )

    style.configure(
        "Danger.TButton", font=(FONT, 9, "bold"),
        padding=(14, 6), background=ERR, foreground="#FFFFFF", borderwidth=0,
    )
    style.map(
        "Danger.TButton",
        background=[("active", "#B02A2C"), ("pressed", "#B02A2C"), ("disabled", "#E29A9B")],
    )

    # 页签
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(8, 6, 8, 0))
    style.configure(
        "TNotebook.Tab",
        font=(FONT, 9), padding=(18, 6), background="#DDE3EC", foreground=TEXT,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", CARD_BG)],
        foreground=[("selected", PRIMARY)],
    )

    # 表格
    style.configure(
        "Treeview",
        font=(FONT, 9), rowheight=30,
        background=CARD_BG, fieldbackground=CARD_BG, foreground=TEXT, borderwidth=0,
    )
    style.configure(
        "Treeview.Heading",
        font=(FONT, 9, "bold"), background=PRIMARY_LIGHT, foreground=TEXT, padding=(6, 5),
    )
    style.map("Treeview", background=[("selected", "#D6E4F7")], foreground=[("selected", TEXT)])

    # 分帧容器
    style.configure("TLabelframe", background=BG, bordercolor="#D3DCE8")
    style.configure("TLabelframe.Label", font=(FONT, 9, "bold"), foreground=PRIMARY_DARK, background=BG)

    # Entry / Combobox
    style.configure("TEntry", fieldbackground="#FFFFFF", foreground=TEXT, padding=3)
    style.configure("TCombobox", fieldbackground="#FFFFFF", foreground=TEXT)

    return style
