# -*- coding: utf-8 -*-
"""主题偏好：模式（浅色 / 深色 / 跟随系统）与调色板。

设计要点
--------
- **不 import tkinter**：CLI、后台线程、崩溃守护进程都可安全使用；
  只有 ``ui/theme.py`` 负责把它应用到 ttk 样式与控件上。
- 模式持久化在 ``config.json`` 的 ``settings.theme``：
  ``light`` / ``dark`` / ``system``（默认 ``system`` = 跟随系统外观）。
- 调色板按「角色」命名（primary / bg / card_bg / text …），界面代码一律
  引用角色而不是具体色值，这样新增主题只需再给一份字典。

新增主题：往 :data:`PALETTES` 加一份与 ``light`` 同键的字典即可。
"""
import os
import subprocess
import sys

LIGHT = "light"
DARK = "dark"
SYSTEM = "system"
MODES = (LIGHT, DARK, SYSTEM)
DEFAULT_MODE = SYSTEM

SETTING_KEY = "theme"


def _default_font() -> str:
    if sys.platform.startswith("win"):
        return "Microsoft YaHei"
    if sys.platform == "darwin":
        return "PingFang SC"
    return "Noto Sans CJK SC"


def _mono_font() -> str:
    if sys.platform.startswith("win"):
        return "Consolas"
    if sys.platform == "darwin":
        return "Menlo"
    return "DejaVu Sans Mono"


FONT = _default_font()
MONO_FONT = _mono_font()

#: 每个主题的色板（键 = 角色名，界面代码只认角色）
PALETTES: dict[str, dict[str, str]] = {
    LIGHT: {
        "primary": "#2B579A",
        "primary_dark": "#1E3F73",
        "primary_light": "#E8EDF5",
        "bg": "#F5F5F5",
        "card_bg": "#FFFFFF",
        "text": "#333333",
        "text_dim": "#666666",
        "ok": "#107C10",
        "err": "#D13438",
        "warn": "#FF8C00",
        "gray": "#A0A0A0",
        "log_bg": "#1E1E1E",
        "log_fg": "#C8C8C8",
        "log_accent": "#7FB0FF",
        "border": "#D3DCE8",
        "row_alt": "#FAFBFC",
        "panel_alt": "#F4F6FA",
        "hl_bg": "#3B2F00",
        "select_bg": "#D6E4F7",
        "btn_bg": "#FFFFFF",
        "btn_active": "#E8EDF5",
        "btn_pressed": "#D3DEED",
        "tab_bg": "#DDE3EC",
    },
    DARK: {
        "primary": "#4C8DFF",
        "primary_dark": "#2E6BE6",
        "primary_light": "#26303F",
        "bg": "#1E1F22",
        "card_bg": "#2A2C31",
        "text": "#E8EAED",
        "text_dim": "#A2A8B3",
        "ok": "#57C777",
        "err": "#FF7B72",
        "warn": "#E3B341",
        "gray": "#6E747D",
        "log_bg": "#14161A",
        "log_fg": "#C8C8C8",
        "log_accent": "#7FB0FF",
        "border": "#3A3E45",
        "row_alt": "#31343A",
        "panel_alt": "#232529",
        "hl_bg": "#4A3B00",
        "select_bg": "#2F4B75",
        "btn_bg": "#33363C",
        "btn_active": "#3D4149",
        "btn_pressed": "#484D56",
        "tab_bg": "#24272C",
    },
}

# 与色板无关、所有主题共享的字体角色
_SHARED = {"font": FONT, "mono_font": MONO_FONT}


def normalize(mode, default: str = DEFAULT_MODE) -> str:
    """把任意输入（含别名）规整为 ``light`` / ``dark`` / ``system``。"""
    m = str(mode or "").strip().lower()
    if m in ("system", "auto", "os", "follow", ""):
        return SYSTEM
    if m in ("dark", "black", "night"):
        return DARK
    if m in ("light", "white", "day"):
        return LIGHT
    return default


def system_prefers_dark() -> bool | None:
    """系统当前是否为深色外观；无法判断返回 ``None``（此时按浅色处理）。

    - macOS：``defaults read -g AppleInterfaceStyle``（浅色时该键不存在）；
    - Windows：注册表 ``AppsUseLightTheme``（0 = 深色）；
    - Linux：``gsettings`` 的 ``color-scheme`` / ``GTK_THEME`` 环境变量。
    """
    if sys.platform == "darwin":
        try:
            cp = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=3,
            )
            if cp.returncode != 0:
                return False  # 键不存在 = 浅色
            return cp.stdout.strip().lower().startswith("dark")
        except (OSError, subprocess.SubprocessError):
            return None
    if sys.platform.startswith("win"):
        try:
            import winreg

            with winreg.OpenKey(  # type: ignore[attr-defined]
                winreg.HKEY_CURRENT_USER,  # type: ignore[attr-defined]
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")  # type: ignore[attr-defined]
                return int(value) == 0
        except Exception:  # noqa: BLE001
            return None
    env = os.environ.get("GTK_THEME", "")
    if "dark" in env.lower():
        return True
    try:
        cp = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=3,
        )
        if cp.returncode == 0 and "dark" in cp.stdout.lower():
            return True
        if cp.returncode == 0:
            return False
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def resolve(mode=None, system_dark: bool | None = None) -> str:
    """把模式解析为实际生效的主题名（``light`` / ``dark``）。"""
    m = normalize(mode or DEFAULT_MODE)
    if m != SYSTEM:
        return m
    dark = system_dark if system_dark is not None else system_prefers_dark()
    return DARK if dark else LIGHT


def palette(mode=None) -> dict[str, str]:
    """返回（已解析的）调色板副本，含 ``font`` / ``mono_font``。"""
    name = resolve(mode)
    out = dict(PALETTES.get(name, PALETTES[LIGHT]))
    out.update(_SHARED)
    out["name"] = name
    return out


def role_values(mode=None) -> set[str]:
    """返回该主题用到的全部色值（用于切换时建立 旧色 → 新色 映射）。"""
    return {v for k, v in palette(mode).items() if k not in ("name",)}


def get_mode(config=None) -> str:
    """读取当前主题模式（未保存时返回默认 ``system``）。"""
    if config is None:
        return DEFAULT_MODE
    return normalize(config.get_setting(SETTING_KEY, DEFAULT_MODE))


def set_mode(config, mode) -> str:
    """持久化主题模式，返回规整后的模式名。"""
    m = normalize(mode)
    config.set_setting(SETTING_KEY, m)
    return m


# 主题名（供 UI 下拉/菜单展示；键为英文稳定值，展示名由 i18n 翻译）
MODE_LABELS = {
    LIGHT: "浅色",
    DARK: "深色",
    SYSTEM: "跟随系统",
}
