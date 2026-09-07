# -*- coding: utf-8 -*-
"""终端 `php` 命令版本管理（Windows 的 cmd / macOS 的终端一致目标：新开终端 php 指向目标版本）。

- Windows：命令解析时 User PATH 优先级高于 Machine PATH（拼接后 User 在前），
  修改 **用户级** PATH（HKCU\\Environment），把目标版本目录放到最前并移除其它
  wnrp php* 条目，新打开的 cmd 中 `php` 即指向目标版本，无需管理员权限。
- macOS/Linux：在 ~/.zshrc / ~/.bash_profile 维护带 `# >>> phpvm-managed <<<`
  标记的 `export PATH="<版本目录>:$PATH"` 块，新开终端生效。

说明：已打开的终端不会感知环境变量变化，需新开窗口生效。
"""
import os
import re
import sys
import threading

from .config import IS_WIN, WNRP_ROOT

# 终端 PHP 二进制文件名
_PHP_BIN = "php.exe" if IS_WIN else "php"

# 匹配 WNRP_ROOT 下的 php / php56 / php72 ...（排除 phpvm / phpcbf 等）
def _is_wnrp_php_entry(entry: str) -> bool:
    """判断 PATH 条目是否为 WNRP_ROOT\\php* 版本目录。"""
    expanded = os.path.expandvars(entry).strip().strip('"')
    if not expanded:
        return False
    norm = os.path.normcase(os.path.normpath(expanded))
    root = os.path.normcase(os.path.normpath(WNRP_ROOT))
    if not norm.startswith(root + os.sep):
        return False
    return bool(re.match(r"^php[0-9]*$", os.path.basename(norm)))


def _split_path(path: str | None) -> list[str]:
    """按系统 PATH 分隔符拆分，去除空项与多余空白。"""
    if not path:
        return []
    return [p.strip() for p in path.split(os.pathsep) if p.strip()]


def _join_path(entries: list[str]) -> str:
    return os.pathsep.join(entries)


# --------------------------------------------------------------------------- #
# Windows：注册表用户/机器 PATH
# --------------------------------------------------------------------------- #
def _win_get_user_path() -> str:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, "Path")
            return str(value or "")
    except OSError:
        return ""


def _win_get_machine_path() -> str:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "Path")
            return str(value or "")
    except OSError:
        return ""


def _win_set_user_path(value: str) -> None:
    import ctypes
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
        ) as key:
            try:
                _type = winreg.QueryValueEx(key, "Path")[1]
            except OSError:
                _type = winreg.REG_EXPAND_SZ
            winreg.SetValueEx(key, "Path", 0, _type, value)
    except OSError as e:
        raise OSError(f"写入用户环境变量失败：{e}") from e
    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF,  # HWND_BROADCAST
            0x001A,  # WM_SETTINGCHANGE
            0,
            "Environment",
            0x0002,  # SMTO_ABORTIFHUNG
            1000,
            None,
        )
    except Exception:  # noqa: BLE001
        pass


def get_user_path() -> str:
    return _win_get_user_path() if IS_WIN else ""


def get_machine_path() -> str:
    return _win_get_machine_path() if IS_WIN else ""


def set_user_path(value: str) -> None:
    if IS_WIN:
        _win_set_user_path(value)


# --------------------------------------------------------------------------- #
# macOS/Linux：shell rc 文件中的 phpvm PATH 块
# --------------------------------------------------------------------------- #
_SHELL_RCS = (".zshrc", ".bash_profile")
_MARK_HEAD = "# >>> phpvm-managed >>>"
_MARK_TAIL = "# <<< phpvm-managed <<<"

_POSIX_EXPORT_RE = re.compile(
    r'^export\s+PATH="(?P<dir>.+?):\$PATH"\s*$'
)


def _posix_rc_files() -> list[str]:
    home = os.path.expanduser("~")
    return [os.path.join(home, name) for name in _SHELL_RCS if os.path.exists(os.path.join(home, name))]


def _posix_read_php_dir() -> str | None:
    """从任意 shell rc 的 phpvm 块解析目标版本目录（按 rc 顺序，zsh 优先）。"""
    for path in _posix_rc_files():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        in_block = False
        for line in lines:
            s = line.strip()
            if s == _MARK_HEAD:
                in_block = True
                continue
            if s == _MARK_TAIL:
                in_block = False
                continue
            if in_block:
                m = _POSIX_EXPORT_RE.match(s)
                if m:
                    return m.group("dir")
    return None


def _posix_write_php_dir(version_dir: str) -> None:
    """把目标版本目录写入各 shell rc 的 phpvm 块（先移除旧块）。"""
    block = f'{_MARK_HEAD}\nexport PATH="{version_dir}:$PATH"\n{_MARK_TAIL}\n'
    home = os.path.expanduser("~")
    for name in _SHELL_RCS:
        path = os.path.join(home, name)
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            else:
                content = ""
            content = _posix_strip_block(content)
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n" if content and not content.endswith("\n") else "")
                f.write(block)
        except OSError:
            continue


def _posix_strip_block(content: str) -> str:
    lines = content.splitlines()
    out: list[str] = []
    skip = False
    for line in lines:
        if line.strip() == _MARK_HEAD:
            skip = True
            continue
        if line.strip() == _MARK_TAIL:
            skip = False
            continue
        if not skip:
            out.append(line)
    return "\n".join(out).rstrip("\n") + ("\n" if out else "")


# --------------------------------------------------------------------------- #
# 对外统一 API
# --------------------------------------------------------------------------- #
def _get_wnrp_entries(path: str | None) -> list[str]:
    return [e for e in _split_path(path) if _is_wnrp_php_entry(e)]


def get_effective_php_dir() -> str | None:
    """返回终端 `php` 实际生效的 wnrp 版本目录，无则 None。

    Windows：User PATH 优先，Machine PATH 兜底，取第一个含 php.exe 的目录。
    macOS/Linux：读 shell rc 的 phpvm 块；其次查当前进程 PATH。
    """
    if IS_WIN:
        for entry in _get_wnrp_entries(get_user_path()) + _get_wnrp_entries(get_machine_path()):
            expanded = os.path.expandvars(entry).strip().strip('"')
            if os.path.exists(os.path.join(expanded, "php.exe")):
                return expanded
        return None

    d = _posix_read_php_dir()
    if d and os.path.exists(os.path.join(d, _PHP_BIN)):
        return d
    for entry in _get_wnrp_entries(os.environ.get("PATH", "")):
        if os.path.exists(os.path.join(entry, _PHP_BIN)):
            return entry
    return None


def set_cli_php(version_dir: str) -> str:
    """将指定 PHP 版本目录设为终端 `php` 命令来源，返回描述（调试用）。"""
    version_dir = os.path.normpath(version_dir)
    if not os.path.exists(os.path.join(version_dir, _PHP_BIN)):
        raise ValueError(f"目录中不存在 {_PHP_BIN}：{version_dir}")

    if IS_WIN:
        kept = [e for e in _split_path(get_user_path()) if not _is_wnrp_php_entry(e)]
        kept.insert(0, version_dir)
        new_path = _join_path(kept)
        set_user_path(new_path)
        return new_path

    _posix_write_php_dir(version_dir)
    return f'export PATH="{version_dir}:$PATH"'


# 终端 php 版本缓存：{版本目录: (版本号, php 二进制 mtime)}。
_cli_version_cache: dict[str, tuple[str, float]] = {}
_cli_version_lock = threading.Lock()


def _php_bin_mtime(d: str) -> float:
    try:
        return os.path.getmtime(os.path.join(d, _PHP_BIN))
    except OSError:
        return -1.0


def _run_php_v(d: str) -> str:
    try:
        import subprocess

        kw: dict = {"capture_output": True, "text": True, "timeout": 10}
        if IS_WIN:
            kw["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.run([os.path.join(d, _PHP_BIN), "-v"], **kw)
        text = proc.stdout or proc.stderr
        m = re.search(r"PHP\s+([0-9]+\.[0-9]+\.[0-9]+)", text)
        return m.group(1) if m else "未知"
    except Exception:  # noqa: BLE001
        return "未知"


def get_cli_version() -> str:
    """解析当前生效终端 php 版本号；无生效版本时返回 '未知'。"""
    d = get_effective_php_dir()
    if not d:
        return "未知"
    mtime = _php_bin_mtime(d)
    with _cli_version_lock:
        cached = _cli_version_cache.get(d)
        if cached and cached[1] == mtime:
            return cached[0]
    version = _run_php_v(d)
    with _cli_version_lock:
        _cli_version_cache[d] = (version, mtime)
    return version


def clear_cli_version_cache() -> None:
    """清空终端 php 版本缓存（切换版本后调用，确保下次探测实时）。"""
    with _cli_version_lock:
        _cli_version_cache.clear()


def get_cli_info() -> dict:
    """汇总当前终端 php 信息：{'dir', 'name', 'version'}；无则全为 None。"""
    d = get_effective_php_dir()
    if not d:
        return {"dir": None, "name": None, "version": None}
    return {"dir": d, "name": os.path.basename(d), "version": get_cli_version()}
