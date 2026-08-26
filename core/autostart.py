# -*- coding: utf-8 -*-
"""开机自启管理：HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run。

以 pythonw.exe（无控制台窗口）隐藏方式启动 phpvm，仅当前用户生效，
无需管理员权限。
"""
import os
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "phpvm"

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _launch_cmd() -> str:
    pyw = r"C:\Python312\pythonw.exe"
    if not os.path.exists(pyw):
        pyw = "pythonw"
    main_py = os.path.join(APP_DIR, "main.py")
    return f'"{pyw}" "{main_py}"'


def _read_value() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
            return value
    except (FileNotFoundError, OSError):
        return None


def is_enabled() -> bool:
    """当前是否已启用（且命令与 phpvm 一致）。"""
    return _read_value() == _launch_cmd()


def enable() -> bool:
    """写入自启项，返回是否成功。"""
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _launch_cmd())
        return True
    except OSError:
        return False


def disable() -> bool:
    """删除自启项，返回是否成功（原本未启用也视为成功）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_WRITE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False
