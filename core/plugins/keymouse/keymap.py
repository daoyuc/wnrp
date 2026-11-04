# -*- coding: utf-8 -*-
"""虚拟键码 → 展示名（纯数据，无依赖、无 i18n）。

只做「按了哪个键」的可读化，不涉及输入内容。键名使用语言无关的短名
（字母 / 数字 / 符号原样，特殊键用英文缩写），避免四语言各维护一份键名表。
"""
# Windows 虚拟键码（WinUser.h）中的特殊键
_SPECIAL: dict[int, str] = {
    0x08: "Backspace",
    0x09: "Tab",
    0x0D: "Enter",
    0x13: "Pause",
    0x14: "CapsLock",
    0x1B: "Esc",
    0x20: "Space",
    0x21: "PgUp",
    0x22: "PgDn",
    0x23: "End",
    0x24: "Home",
    0x25: "Left",
    0x26: "Up",
    0x27: "Right",
    0x28: "Down",
    0x2C: "PrintScreen",
    0x2D: "Insert",
    0x2E: "Delete",
    0x5B: "Win",
    0x5C: "Win",
    0x5D: "Menu",
    0x6A: "*",
    0x6B: "+",
    0x6D: "-",
    0x6E: ".",
    0x6F: "/",
    0x90: "NumLock",
    0x91: "ScrollLock",
    0xA0: "Shift",
    0xA1: "Shift",
    0xA2: "Ctrl",
    0xA3: "Ctrl",
    0xA4: "Alt",
    0xA5: "Alt",
    # OEM 键（美式布局常见符号）
    0xBA: ";",
    0xBB: "=",
    0xBC: ",",
    0xBD: "-",
    0xBE: ".",
    0xBF: "/",
    0xC0: "`",
    0xDB: "[",
    0xDC: "\\",
    0xDD: "]",
    0xDE: "'",
}


def name_of(vk: int) -> str:
    """虚拟键码 → 展示名；未收录的键回落 ``0x1E`` 形式，保证排名榜不出现空行。"""
    try:
        vk = int(vk)
    except (TypeError, ValueError):
        return "?"
    if 0x41 <= vk <= 0x5A:        # A-Z
        return chr(vk)
    if 0x30 <= vk <= 0x39:        # 0-9
        return chr(vk)
    if 0x60 <= vk <= 0x69:        # 小键盘 0-9
        return f"Num{vk - 0x60}"
    if 0x70 <= vk <= 0x87:        # F1-F24
        return f"F{vk - 0x6F}"
    return _SPECIAL.get(vk) or f"0x{vk:02X}"


def is_modifier(vk: int) -> bool:
    """是否修饰键（Shift / Ctrl / Alt / Win）——统计里通常希望单独看。"""
    return int(vk) in (0x10, 0x11, 0x12, 0x5B, 0x5C,
                       0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5)
