# -*- coding: utf-8 -*-
"""键鼠统计插件（内置，Windows Only）。

只记录**按了哪个键**与**按键时间点**：不记录字符、输入文本、窗口标题、剪贴板，
数据仅落本机用户数据目录的 SQLite，不上传。

组成：
- :mod:`.collector` —— 低层键盘钩子（回调零 I/O，只入队）；
- :mod:`.store` —— SQLite 日桶 + 分钟桶；
- :mod:`.stats` —— 今日/本周/本月/本年/全部聚合；
- :mod:`.daemon` —— 独立守护进程（单例锁 + 消息循环 + 批量落库 + 暂停）。

导入本模块**不得**装钩子、不得起线程 —— 面板与 CLI 会 import 它读 MANIFEST。
"""
import os

from ... import app_paths, autostart
from ...config import IS_WIN
from ..base import PluginSpec

MANIFEST = PluginSpec(
    key="keymouse",
    name="键鼠统计",
    description="记录按键次数与打字速度；只统计键码，不记录输入内容",
    panel=("ui.plugins.keymouse_panel", "KeymousePanel"),
    daemon_script="core/plugins/keymouse/daemon.py",
    ensure_background=("core.plugins.keymouse", "ensure_daemon"),
    default_settings={"autostart": False, "retention_days": 730},
    platforms=("win32",),
)

AUTOSTART_NAME = "phpvm-keymouse"
PAUSE_FILE_NAME = os.path.join("plugins", "keymouse", "paused")


# --------------------------------------------------------------------------- #
# 平台 / 后台进程
# --------------------------------------------------------------------------- #
def supported() -> bool:
    """当前平台是否支持采集（首版仅 Windows）。"""
    return IS_WIN


def daemon_script_path() -> str:
    return os.path.join(app_paths.package_dir(), "core", "plugins", "keymouse",
                        "daemon.py")


def ensure_daemon(config=None) -> tuple[bool, str]:
    """幂等拉起后台记录进程（框架 ``ensure_backgrounds`` 的钩子）。"""
    if not supported():
        return False, "当前平台不支持键盘采集（仅 Windows）"
    from . import daemon

    return daemon.spawn()


def stop_daemon() -> tuple[bool, str]:
    """结束后台记录进程（不删数据）。"""
    from . import daemon

    return daemon.stop()


def is_running() -> bool:
    from . import daemon

    return daemon.is_running()


def daemon_pid() -> int | None:
    from . import daemon

    return daemon.lock_pid()


# --------------------------------------------------------------------------- #
# 暂停 / 恢复（用文件通信：GUI、CLI、守护进程三方无需 IPC）
# --------------------------------------------------------------------------- #
def pause_file() -> str:
    return app_paths.data_file(PAUSE_FILE_NAME)


def is_paused() -> bool:
    return os.path.exists(pause_file())


def set_paused(paused: bool) -> bool:
    """写 / 删暂停标记；返回是否处于暂停态。"""
    path = pause_file()
    try:
        if paused:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="ascii") as f:
                f.write("paused")
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    return is_paused()


# --------------------------------------------------------------------------- #
# 开机自启（写 HKCU Run，值名独立于 phpvm 自身）
# --------------------------------------------------------------------------- #
def autostart_enabled() -> bool:
    if not IS_WIN:
        return False
    return bool(autostart.read_run_value(AUTOSTART_NAME))


def set_autostart(enabled: bool) -> bool:
    """开启：把守护进程写进 HKCU Run；关闭：删除该项。"""
    if not IS_WIN:
        return False
    if not enabled:
        return autostart.disable_run_value(AUTOSTART_NAME)
    command = autostart.quote_command(
        autostart.hidden_python_args(daemon_script_path()))
    return autostart.enable_run_value(AUTOSTART_NAME, command)


# --------------------------------------------------------------------------- #
# 状态汇总（面板 / CLI 共用）
# --------------------------------------------------------------------------- #
def status(config=None) -> dict:
    from . import store

    conn = store.open_readonly()
    try:
        db = store.info(conn)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    retention = 730
    if config is not None:
        from .. import settings as plugin_settings

        retention = int(plugin_settings.get(
            config, MANIFEST.key, "retention_days", retention) or 0)
    return {
        "key": MANIFEST.key,
        "name": MANIFEST.name,
        "supported": supported(),
        "running": is_running() if IS_WIN else False,
        "paused": is_paused(),
        "pid": daemon_pid() if IS_WIN else None,
        "autostart": autostart_enabled(),
        "retention_days": retention,
        "db": db,
    }
