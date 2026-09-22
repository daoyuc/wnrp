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
import traceback

# 保证无论从哪个目录启动都能正确导入包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MUTEX_NAME = "Global\\wnrp_phpvm_singleton_mutex"
ERROR_ALREADY_EXISTS = 183
SOCK_PATH = os.path.join(tempfile.gettempdir(), "phpvm_singleton.sock")

_lock_sock: socket.socket | None = None

# 重启场景：旧实例释放单例锁存在竞态，新实例以此节奏轮询补获（最长 ~5s）
_RESTART_RETRIES = 50
_RESTART_RETRY_DELAY = 0.1


def _start_all_services() -> None:
    """无界面启动整套服务（Nginx + 各 PHP + Redis + MySQL）。

    供「开机自动启动服务」写入的启动脚本调用；每项的成败由 ServiceGroup
    写入全局运行日志（core.run_log），结果同时追加到 autostart_services.log，
    便于排查登录时未起来的服务。
    """
    from core import app_paths, run_log
    from core.config import Config
    from core import modules
    from core.i18n import t
    from core.mysql_manager import MysqlManager
    from core.nginx_manager import NginxManager
    from core.php_manager import PhpManager
    from core.redis_manager import RedisManager
    from core.service_group import ServiceGroup

    run_log.info("app", t("开机自启：开始启动全部服务"))
    cfg = Config()
    # 按模块开关决定是否实例化：停用的模块不加载其 manager（重启后生效）
    redis_mgr = RedisManager() if modules.is_enabled("redis", cfg) else None
    mysql_mgr = MysqlManager() if modules.is_enabled("mysql", cfg) else None
    group = ServiceGroup(PhpManager(cfg), NginxManager(), redis_mgr, mysql_mgr)
    try:
        msg = group.start_all()
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        run_log.error("app", t("开机自启启动服务失败：{msg}", msg=msg))
    log = app_paths.data_file("autostart_services.log")
    try:
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] --start-all\n{msg}\n")
    except OSError:
        pass


def _install_excepthook() -> None:
    """未捕获异常：写运行日志后仍走默认打印（终端/日志都能看到 traceback）。"""

    def hook(exc_type, exc, tb) -> None:
        from core import run_log
        from core.i18n import t

        try:
            run_log.error("app", t("未捕获异常：{name}：{msg}",
                                   name=exc_type.__name__, msg=exc))
        except Exception:  # noqa: BLE001
            pass
        traceback.print_exception(exc_type, exc, tb)

    sys.excepthook = hook


def _is_restart() -> bool:
    """当前进程是否为「重启」拉起的新实例（由 UI 重启功能注入环境变量）。"""
    return os.environ.get("PHPVM_RESTART") == "1"


def _enable_high_dpi() -> None:
    """Windows：开启高 DPI 感知（须在创建 Tk 根窗口之前调用）。

    未开启时系统对整个窗口做位图拉伸，高分辨率屏上文字发虚；开启后 Tk 按
    真实 DPI 绘制（字体与控件同步放大）。逐级降级：
    PER_MONITOR_V2（Win10 1703+）→ 系统级感知 → 放弃（保持系统默认拉伸）。
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if ctypes.windll.shcore.SetProcessDpiAwarenessContext(-4):
            return
    except (AttributeError, OSError):
        pass
    try:
        import ctypes

        # 2 = PROCESS_PER_MONITOR_DPI_AWARE（老版本 Win10 / Win8.1）
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


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


def _log_second_instance() -> None:
    """已有实例在运行、本次启动直接退出：记一条日志，便于排查「点了没反应」。"""
    try:
        from core import run_log
        from core.i18n import t

        run_log.warn("app", t("phpvm 已在运行，本次启动已退出"))
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    _install_excepthook()
    handle = None
    if sys.platform.startswith("win"):
        import ctypes

        handle = _create_mutex_handle()
        if handle is None:
            if not _is_restart():
                from core.config import Config
                from core.i18n import set_language, t

                set_language(Config().get_lang())
                _log_second_instance()
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
                _log_second_instance()
                return
    else:
        if not _acquire_posix_lock():
            if _is_restart():
                # 重启拉起：旧实例退出释放 socket 锁有竞态，短暂重试
                if not _restart_retry(_acquire_posix_lock):
                    _log_second_instance()
                    return
            else:
                _log_second_instance()
                return

    from core import run_log, updater
    from core.config import Config

    config = Config()

    # 国际化：按配置/系统 locale 设定界面语言（须先于任何界面文本创建/模块级翻译常量）
    from core.i18n import set_language, t

    set_language(config.get_lang())

    # 开机自启场景：--start-all 只启动整套服务，不拉起界面、不占用单例锁
    if "--start-all" in sys.argv[1:]:
        _start_all_services()
        return

    from core import modules
    from core.nginx_manager import NginxManager
    from core.php_manager import PhpManager

    # 必须在创建 Tk 根窗口（MainWindow）之前开启，否则不生效
    _enable_high_dpi()
    from ui.main_window import MainWindow

    run_log.info("app", t("phpvm {ver} 启动（{platform} · 语言 {lang}）",
                          ver=updater.current_version(), platform=sys.platform,
                          lang=config.get_lang()))

    # 停用的模块不实例化、不加载其面板代码（见 core/modules.py）
    redis_mgr = None
    if modules.is_enabled("redis", config):
        from core.redis_manager import RedisManager

        redis_mgr = RedisManager()
    mysql_mgr = None
    if modules.is_enabled("mysql", config):
        from core.mysql_manager import MysqlManager

        mysql_mgr = MysqlManager()

    app = MainWindow(PhpManager(config), NginxManager(), redis_mgr, mysql_mgr, config)
    try:
        app.mainloop()
    finally:
        if handle is not None:
            import ctypes

            ctypes.windll.kernel32.ReleaseMutex(handle)
        _release_posix_lock()
        run_log.info("app", t("phpvm 已退出"))


if __name__ == "__main__":
    main()
