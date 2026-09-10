# -*- coding: utf-8 -*-
"""phpvm 入口：单实例保护 + 初始化配置 + 启动 GUI。

- Windows：命名互斥体（Global\\wnrp_phpvm_singleton_mutex），已运行则弹提示并退出；
- macOS/Linux：Unix domain socket 锁（/tmp/phpvm_singleton.sock），
  探测到活实例即静默退出（已运行实例会在 Dock/窗口栏可见）。
"""
import os
import socket
import sys
import tempfile
import time

# 保证无论从哪个目录启动都能正确导入包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MUTEX_NAME = "Global\\wnrp_phpvm_singleton_mutex"
ERROR_ALREADY_EXISTS = 183
SOCK_PATH = os.path.join(tempfile.gettempdir(), "phpvm_singleton.sock")

_lock_sock: socket.socket | None = None

# 重启场景：旧实例释放单例锁存在竞态，新实例以此节奏轮询补获（最长 ~5s）
_RESTART_RETRIES = 50
_RESTART_RETRY_DELAY = 0.1


def _is_restart() -> bool:
    """当前进程是否为「重启」拉起的新实例（由 UI 重启功能注入环境变量）。"""
    return os.environ.get("PHPVM_RESTART") == "1"


def _restart_retry(acquire) -> bool:
    """重启实例：短暂轮询获取单例锁，避开旧实例退出竞态。"""
    for _ in range(_RESTART_RETRIES):
        if acquire():
            return True
        time.sleep(_RESTART_RETRY_DELAY)
    return False


def _create_mutex_handle():
    """Windows：创建命名互斥体并判定是否为本进程新建；被占用返回 None。"""
    import ctypes

    h = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if h and ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS:
        return h
    if h:
        ctypes.windll.kernel32.CloseHandle(h)
    return None


def _acquire_posix_lock() -> bool:
    """占用单实例 socket 锁；返回 False 表示已有实例在运行。"""
    global _lock_sock
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(SOCK_PATH)
        s.listen(1)  # 进入监听态，第二实例才能 connect 探测到活实例
        _lock_sock = s
        return True
    except OSError:
        # 已有 socket 文件：能连上说明是活实例，否则属上次异常残留，清掉重建
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.settimeout(0.5)
            c.connect(SOCK_PATH)
            c.close()
            return False
        except OSError:
            try:
                os.unlink(SOCK_PATH)
            except OSError:
                pass
            try:
                s.bind(SOCK_PATH)
                s.listen(1)
                _lock_sock = s
                return True
            except OSError:
                return False


def _release_posix_lock() -> None:
    global _lock_sock
    if _lock_sock is not None:
        try:
            _lock_sock.close()
        except OSError:
            pass
        _lock_sock = None
    try:
        os.unlink(SOCK_PATH)
    except OSError:
        pass


def main() -> None:
    handle = None
    if sys.platform.startswith("win"):
        import ctypes

        handle = _create_mutex_handle()
        if handle is None:
            if not _is_restart():
                from core.config import Config
                from core.i18n import set_language, t

                set_language(Config().get_lang())
                ctypes.windll.user32.MessageBoxW(
                    None,
                    t("phpvm 已经在运行中，请查看任务栏或系统托盘。"),
                    "phpvm",
                    0x40,  # MB_ICONINFORMATION
                )
                return
            # 重启拉起：旧实例即将退出释放互斥体，轮询等待后再试
            for _ in range(_RESTART_RETRIES):
                time.sleep(_RESTART_RETRY_DELAY)
                handle = _create_mutex_handle()
                if handle is not None:
                    break
            if handle is None:
                return
    else:
        if not _acquire_posix_lock():
            if _is_restart():
                # 重启拉起：旧实例退出释放 socket 锁有竞态，短暂重试
                if not _restart_retry(_acquire_posix_lock):
                    return
            else:
                return

    from core.config import Config

    config = Config()

    # 国际化：按配置/系统 locale 设定界面语言（须先于任何界面文本创建/模块级翻译常量）
    from core.i18n import set_language

    set_language(config.get_lang())

    from core.mysql_manager import MysqlManager
    from core.nginx_manager import NginxManager
    from core.php_manager import PhpManager
    from core.redis_manager import RedisManager
    from ui.main_window import MainWindow

    app = MainWindow(PhpManager(config), NginxManager(), RedisManager(),
                     MysqlManager(), config)
    try:
        app.mainloop()
    finally:
        if handle is not None:
            import ctypes

            ctypes.windll.kernel32.ReleaseMutex(handle)
        _release_posix_lock()


if __name__ == "__main__":
    main()
