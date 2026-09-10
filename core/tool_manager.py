# -*- coding: utf-8 -*-
"""外部开发工具探测：目前为 Composer。

定位：只做「探测 + 调用」，不内置下载安装器 ——
系统已装（如 C:\\ProgramData\\ComposerSetup）时直接给出路径与版本，
并支持把某个 PHP 版本目录前置到 PATH 后用该版本运行 composer；
未安装时返回安装指引。
"""
import os
import shutil

from . import process_utils as pu
from .config import IS_WIN
from .i18n import t

_COMPOSER = "composer.bat" if IS_WIN else "composer"
# 常见安装位置（ComposerSetup / 手动放置 / Homebrew）
_EXTRA_PATHS = (
    r"C:\ProgramData\ComposerSetup\bin\composer.bat",
    r"C:\ProgramData\ComposerSetup\bin\composer",
    os.path.expanduser(r"~\AppData\Roaming\Composer\composer.bat"),
    r"C:\tools\composer\composer.bat",
    "/usr/local/bin/composer",
    "/opt/homebrew/bin/composer",
    os.path.expanduser("~/.composer/composer"),
)


def detect_composer() -> str:
    """返回可用的 composer 可执行文件路径；未找到返回空串。"""
    found = shutil.which("composer") or shutil.which(_COMPOSER) or ""
    if found:
        return found
    for p in _EXTRA_PATHS:
        if p and os.path.exists(p):
            return p
    return ""


def composer_version(path: str = "") -> str:
    """执行 `composer -V` 并返回首行文本；失败返回空串。"""
    exe = path or detect_composer()
    if not exe:
        return ""
    code, out, err = pu.run_cmd([exe, "-V"], timeout=60)
    lines = [(ln or "").strip() for ln in (out or err or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    # composer 前置输出可能夹带 PHP 告警（Deprecated/Warning），优先取版本行
    for ln in reversed(lines):
        if "composer version" in ln.lower():
            return ln
    return lines[-1]


def info() -> dict:
    """返回 {ok, path, version, message}。"""
    path = detect_composer()
    if not path:
        return {"ok": False, "path": "", "version": "", "message": install_guide()}
    version = composer_version(path)
    return {
        "ok": True,
        "path": path,
        "version": version or t("（未能读取版本）"),
        "message": t("已检测到 Composer：{path}", path=path),
    }


def install_guide() -> str:
    """未安装 Composer 时的安装指引（仅提示，不内置下载）。"""
    if IS_WIN:
        return t(
            "未检测到 Composer。可任选一种方式安装：\n"
            "  · 下载 Composer-Setup.exe（https://getcomposer.org/Composer-Setup.exe），"
            "安装后重启 phpvm 即自动识别\n"
            "  · 或下载 composer.phar 放到任意目录后，在「编辑端口」同级设置里用 php 调用\n"
            "安装后 Composer 会使用系统 PATH 中的 php；如需指定版本，"
            "请用本页「打开终端」或「Composer」按钮（会前置所选版本目录）。")
    return t(
        "未检测到 Composer。可任选一种方式安装：\n"
        "  · macOS：brew install composer\n"
        "  · Linux：sudo apt install composer（或按 getcomposer.org 官方指引安装）\n"
        "安装后重启 phpvm 即自动识别。")


def open_composer_terminal(php_dir: str = "", cwd: str = "") -> bool:
    """新开终端，PATH 前置指定 PHP 版本目录后执行 `composer -V`。"""
    return pu.open_terminal(cwd=cwd or php_dir, path_prepend=php_dir,
                            command="composer -V")
