# -*- coding: utf-8 -*-
"""开机自启管理。

- Windows：写入 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run，
  以 pythonw.exe（无控制台窗口）隐藏方式启动 phpvm，仅当前用户生效。
- macOS/Linux：生成 ~/Library/LaunchAgents/com.phpvm.app.plist（RunAtLoad），
  经 launchctl bootstrap 注册到当前用户 GUI 会话。

均无需管理员权限。
"""
import os
import sys

from .config import IS_WIN

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "phpvm"

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCH_AGENT_LABEL = "com.phpvm.app"
LAUNCH_AGENT_PATH = os.path.join(
    os.path.expanduser("~"), "Library", "LaunchAgents", f"{LAUNCH_AGENT_LABEL}.plist"
)


def _launch_args() -> list[str]:
    """启动 phpvm 的解释器+入口参数。Windows 优先 pythonw（无控制台）。"""
    main_py = os.path.join(APP_DIR, "main.py")
    if IS_WIN:
        pyw = r"C:\Python312\pythonw.exe"
        if not os.path.exists(pyw):
            pyw = "pythonw"
        return [pyw, main_py]
    return [sys.executable, main_py]


# --------------------------------------------------------------------------- #
# Windows：注册表 Run 项
# --------------------------------------------------------------------------- #
def _win_read_value() -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
            return value
    except (FileNotFoundError, OSError):
        return None


def _win_enable() -> bool:
    import winreg

    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_WRITE
        ) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, " ".join(f'"{a}"' for a in _launch_args()))
        return True
    except OSError:
        return False


def _win_disable() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_WRITE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# macOS/Linux：LaunchAgent plist + launchctl
# --------------------------------------------------------------------------- #
def _posix_plist_data() -> dict:
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": _launch_args(),
        "RunAtLoad": True,
    }


def _posix_read_plist() -> dict | None:
    try:
        import plistlib

        with open(LAUNCH_AGENT_PATH, "rb") as f:
            return plistlib.load(f)
    except (OSError, plistlib.InvalidFileException):
        return None


def _posix_launchctl(action: str) -> None:
    import subprocess

    uid = os.getuid()
    target = f"gui/{uid}"
    cmd = ["launchctl", action, target, LAUNCH_AGENT_PATH]
    try:
        subprocess.run(cmd, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _posix_enable() -> bool:
    import plistlib

    try:
        os.makedirs(os.path.dirname(LAUNCH_AGENT_PATH), exist_ok=True)
        with open(LAUNCH_AGENT_PATH, "wb") as f:
            plistlib.dump(_posix_plist_data(), f, sort_keys=True)
        _posix_launchctl("bootstrap")
        return True
    except OSError:
        return False


def _posix_disable() -> bool:
    _posix_launchctl("bootout")
    try:
        os.remove(LAUNCH_AGENT_PATH)
    except OSError:
        pass
    return True


def is_enabled() -> bool:
    """当前是否已启用（且命令与 phpvm 一致）。"""
    if IS_WIN:
        return _win_read_value() == " ".join(f'"{a}"' for a in _launch_args())
    data = _posix_read_plist()
    return bool(data and data.get("ProgramArguments") == _launch_args())


def enable() -> bool:
    """写入自启项，返回是否成功。"""
    return _win_enable() if IS_WIN else _posix_enable()


def disable() -> bool:
    """删除自启项，返回是否成功（原本未启用也视为成功）。"""
    return _win_disable() if IS_WIN else _posix_disable()
