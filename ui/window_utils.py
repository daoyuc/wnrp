# -*- coding: utf-8 -*-
"""窗口尺寸与定位工具。

统一按「屏幕可用工作区」收敛窗口初始尺寸并居中，最高准则是：
**窗口底部的操作按钮（右下角）必须始终可见**——不被 Dock / 任务栏 / 屏幕边缘遮挡。

用法（窗口内容构建完成后调用一次）::

    from .window_utils import fit_window
    fit_window(self, master, width=780, height=540, min_width=660, min_height=440)

配套约定：底部操作按钮栏请用 ``pack(side="bottom", fill="x")``，并且**先于**
内容区 pack。Tk 按 pack 顺序分配空腔，若按钮栏最后 pack，窗口被压小时
按钮会被挤出可视区域（即「右下角按钮被遮挡」）。
"""
import ctypes
import sys
import tkinter as tk

# 非 Windows 平台的保守预留（像素）：顶部面板/菜单栏、底部任务栏/Dock
_TOP_RESERVE = 28
_BOTTOM_RESERVE_MAC = 96    # macOS 菜单栏 25~37 + Dock 约 60~80，取保守值
_BOTTOM_RESERVE_LINUX = 64
_MIN_W = 320
_MIN_H = 240


def work_area(win: tk.Misc) -> tuple[int, int, int, int]:
    """返回屏幕可用工作区 ``(x, y, width, height)``。

    Windows 用 SPI_GETWORKAREA 取真实工作区（已排除任务栏）；
    macOS / Linux 在屏幕尺寸基础上保守预留顶部菜单栏与底部 Dock / 面板。
    """
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    if sys.platform.startswith("win"):
        rect = _win_work_area()
        if rect:
            return rect
        return 0, 0, sw, sh
    if sys.platform == "darwin":
        bottom = _BOTTOM_RESERVE_MAC
    else:
        bottom = _BOTTOM_RESERVE_LINUX
    # 小屏兜底：底部预留不超过屏幕高度的 1/3，避免可用区被压得没有意义
    bottom = min(bottom, max(0, sh // 3))
    return 0, _TOP_RESERVE, sw, max(_MIN_H, sh - _TOP_RESERVE - bottom)


def _win_work_area():
    """Windows：SPI_GETWORKAREA（已排除任务栏）；失败返回 None。"""
    try:
        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        r = _RECT()
        ok = ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
        if ok and r.right > r.left and r.bottom > r.top:
            return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:  # noqa: BLE001
        pass
    return None


def fit_window(win, master=None, width=None, height=None,
               min_width=None, min_height=None, bias=3, margin=0):
    """按屏幕可用工作区收敛窗口尺寸并定位，返回最终 ``(w, h)``。

    参数：
    - ``width`` / ``height``：期望尺寸；``None`` 表示用窗口当前内容请求尺寸。
    - ``min_width`` / ``min_height``：期望最小尺寸，会被自动收敛到可用工作区内
      （否则用户无法把窗口缩小到「按钮可见」的尺寸）。
    - ``master``：定位参考窗口（在其内居中，垂直方向按 ``bias`` 略偏上）；
      ``None`` 表示相对屏幕居中。
    - ``margin``：与工作区边缘的安全留白。
    """
    win.update_idletasks()
    ax, ay, aw, ah = work_area(win)
    avail_w = max(_MIN_W, aw - 2 * margin)
    avail_h = max(_MIN_H, ah - 2 * margin)

    want_w = int(width or win.winfo_reqwidth())
    want_h = int(height or win.winfo_reqheight())
    w = min(want_w, avail_w)
    h = min(want_h, avail_h)
    if min_width:
        w = max(w, min(int(min_width), avail_w))
    if min_height:
        h = max(h, min(int(min_height), avail_h))

    # 参考窗口必须已映射（未映射时 winfo_rootx/width 不可信，会算出贴左上角的位置）
    if (master is not None and int(master.winfo_viewable()) == 1
            and int(master.winfo_width()) > 1):
        bx, by = master.winfo_rootx(), master.winfo_rooty()
        bw, bh = master.winfo_width(), master.winfo_height()
        x = bx + (bw - w) // 2
        y = by + (bh - h) // max(1, bias)
    else:
        x = ax + (aw - w) // 2
        y = ay + (ah - h) // max(1, bias)

    # 关键：把窗口整体 clamp 进工作区，保证底边不越过 Dock / 任务栏
    x = min(max(x, ax + margin), ax + margin + avail_w - w)
    y = min(max(y, ay + margin), ay + margin + avail_h - h)

    win.minsize(min(int(min_width or 0), w) or 1,
                min(int(min_height or 0), h) or 1)
    win.geometry(f"{w}x{h}+{x}+{y}")
    return w, h
