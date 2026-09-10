# -*- coding: utf-8 -*-
"""托盘图标：确保 `phpvm.ico` 存在。

仓库不便于内置二进制资源，这里用标准库直接生成一个 32×32 的 ICO
（ICONDIR + BITMAPINFOHEADER + 32 位 BGRA + AND 掩码）：蓝底圆 + 白色 "P"。
生成失败不影响托盘（ui/tray.py 加载失败会回退系统默认图标）。
"""
import os
import struct
from typing import List

_SIZE = 32
_BG = (0x9A, 0x57, 0x2B, 0xFF)  # BGRA —— 主题蓝 #2B579A
_FG = (0xFF, 0xFF, 0xFF, 0xFF)  # 白色
_TRANSPARENT = (0, 0, 0, 0)


def _rows() -> List[bytes]:
    """自上而下生成 32 行 BGRA 像素（蓝底圆 + 白色 P）。"""
    out: List[bytes] = []
    cx = cy = (_SIZE - 1) / 2.0
    r = 15.0
    for y in range(_SIZE):
        row = bytearray()
        for x in range(_SIZE):
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy > r * r:
                row += bytes(_TRANSPARENT)
                continue
            # 白色 "P"：竖条 + 右上方块（粗略但可辨识）
            is_p = (11 <= x <= 14 and 8 <= y <= 24) or (15 <= x <= 20 and 8 <= y <= 15)
            row += bytes(_FG if is_p else _BG)
        out.append(bytes(row))
    return out


def build_ico() -> bytes:
    """返回 ICO 文件字节（单张 32×32 32bpp 图像）。"""
    rows = _rows()
    # BMP 行序为自下而上
    xor = b"".join(reversed(rows))
    # AND 掩码：每行 4 字节（32 位对齐），全 0 表示全部不透明
    mask = b"\x00" * (4 * _SIZE)
    dib = struct.pack(
        "<IiiHHIIiiII",
        40,            # biSize
        _SIZE,         # biWidth
        _SIZE * 2,     # biHeight（ICO 需含掩码高度）
        1,             # biPlanes
        32,            # biBitCount
        0,             # biCompression = BI_RGB
        len(xor) + len(mask), 0, 0, 0, 0,
    )
    image = dib + xor + mask
    icondir = struct.pack("<HHH", 0, 1, 1)  # reserved, type=icon, count
    entry = struct.pack("<BBBBHHII", _SIZE, _SIZE, 0, 0, 1, 32, len(image), 22)
    return icondir + entry + image


def ensure_icon(path: str) -> str:
    """图标不存在时生成；返回图标路径（失败时原样返回）。"""
    try:
        if os.path.exists(path):
            return path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(build_ico())
    except OSError:
        pass
    return path
