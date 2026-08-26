# -*- coding: utf-8 -*-
"""系统 cmd `php` 命令版本管理。

原理：Windows 解析命令时 User PATH 优先级高于 Machine PATH（拼接后 User 在前）。
因此「切换 cmd php 版本」只需修改 **用户级** PATH（HKCU\\Environment），
把目标版本目录（C:\\wnrp\\phpXX）放到最前并移除其它 wnrp php* 条目，
即可让新打开的 cmd / 终端 中 `php` 指向目标版本，**无需管理员权限**。

说明：已打开的 cmd 窗口不会感知环境变量变化，需新开窗口生效。
"""
import ctypes
import os
import re
import sys
import winreg

from .config import WNRP_ROOT

# 匹配 C:\wnrp\php / php56 / php72 ...（排除 phpvm / phpcbf 等）
_PHP_DIR_RE = re.compile(r"^" + re.escape(WNRP_ROOT.lower()) + r"\\php[0-9]*$")
_WM_SETTINGCHANGE = 0x001A
_HWND_BROADCAST = 0xFFFF


def _split_path(path: str | None) -> list[str]:
    """按 ; 拆分 PATH，去除空项与多余空白。"""
    if not path:
        return []
    return [p.strip() for p in path.split(";") if p.strip()]


def _join_path(entries: list[str]) -> str:
    return ";".join(entries)


def _is_wnrp_php_entry(entry: str) -> bool:
    """判断 PATH 条目是否为 C:\\wnrp\\php* 版本目录（php / php56 ... php85）。"""
    expanded = os.path.expandvars(entry).strip().strip('"')
    if not expanded:
        return False
    norm = os.path.normcase(os.path.normpath(expanded))
    return bool(_PHP_DIR_RE.match(norm))


def get_user_path() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, "Path")
            return str(value or "")
    except OSError:
        return ""


def get_machine_path() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment") as key:
            value, _ = winreg.QueryValueEx(key, "Path")
            return str(value or "")
    except OSError:
        return ""


def _get_wnrp_entries(path: str | None) -> list[str]:
    """按顺序返回给定 PATH 中的 wnrp php* 目录条目。"""
    return [e for e in _split_path(path) if _is_wnrp_php_entry(e)]


def set_user_path(value: str) -> None:
    """写回用户 PATH（注册表 HKCU\\Environment），并广播环境变量变更。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
            try:
                _type = winreg.QueryValueEx(key, "Path")[1]
            except OSError:
                _type = winreg.REG_EXPAND_SZ
            winreg.SetValueEx(key, "Path", 0, _type, value)
    except OSError as e:
        raise OSError(f"写入用户环境变量失败：{e}") from e
    _broadcast_environment_change()


def _broadcast_environment_change() -> None:
    """通知资源管理器/终端环境变量已变更（不影响已打开窗口）。"""
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.user32.SendMessageTimeoutW(
                _HWND_BROADCAST, _WM_SETTINGCHANGE, 0, "Environment",
                0x0002,  # SMTO_ABORTIFHUNG
                1000, None,
            )
        except Exception:  # noqa: BLE001
            pass


def get_effective_php_dir() -> str | None:
    """返回当前 cmd 中 `php` 实际生效的 wnrp 版本目录（User 优先，Machine 兜底）。

    Windows 命令解析顺序为 User PATH 拼接 Machine PATH（User 在前），
    因此按顺序取第一个含 php.exe 的 wnrp php* 目录即为有效版本。
    """
    for entry in _get_wnrp_entries(get_user_path()) + _get_wnrp_entries(get_machine_path()):
        expanded = os.path.expandvars(entry).strip().strip('"')
        if os.path.exists(os.path.join(expanded, "php.exe")):
            return expanded
    return None


def set_cli_php(version_dir: str) -> str:
    """将指定 PHP 版本目录设为 cmd `php` 命令来源。

    实现：用户 PATH 中移除所有 C:\\wnrp\\php* 条目，并把目标目录插入最前。
    返回新的用户 PATH（调试用）。
    """
    version_dir = os.path.normpath(version_dir)
    if not os.path.exists(os.path.join(version_dir, "php.exe")):
        raise ValueError(f"目录中不存在 php.exe：{version_dir}")

    kept = [e for e in _split_path(get_user_path()) if not _is_wnrp_php_entry(e)]
    kept.insert(0, version_dir)
    new_path = _join_path(kept)
    set_user_path(new_path)
    return new_path


def get_cli_version() -> str:
    """执行当前生效 php -v 解析版本号；无生效版本时返回 '未知'。"""
    d = get_effective_php_dir()
    if not d:
        return "未知"
    try:
        import subprocess
        proc = subprocess.run(
            [os.path.join(d, "php.exe"), "-v"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,  # 避免每次探测弹黑窗
        )
        text = proc.stdout or proc.stderr
        m = re.search(r"PHP\s+([0-9]+\.[0-9]+\.[0-9]+)", text)
        return m.group(1) if m else "未知"
    except Exception:  # noqa: BLE001
        return "未知"


def get_cli_info() -> dict:
    """汇总当前 cmd php 信息：{'dir', 'name', 'version'}；无则全为 None。"""
    d = get_effective_php_dir()
    if not d:
        return {"dir": None, "name": None, "version": None}
    return {"dir": d, "name": os.path.basename(d), "version": get_cli_version()}
