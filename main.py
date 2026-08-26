# -*- coding: utf-8 -*-
"""phpvm 入口：单实例保护 + 初始化配置 + 启动 GUI。"""
import ctypes
import os
import sys

# 保证无论从哪个目录启动都能正确导入包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MUTEX_NAME = "Global\\wnrp_phpvm_singleton_mutex"
ERROR_ALREADY_EXISTS = 183


def main() -> None:
    if sys.platform.startswith("win"):
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            ctypes.windll.user32.MessageBoxW(
                None,
                "phpvm 已经在运行中，请查看任务栏或系统托盘。",
                "phpvm",
                0x40,  # MB_ICONINFORMATION
            )
            return
    else:
        handle = None

    from core.config import Config
    from core.nginx_manager import NginxManager
    from core.php_manager import PhpManager
    from ui.main_window import MainWindow

    config = Config()
    app = MainWindow(PhpManager(config), NginxManager(), config)
    try:
        app.mainloop()
    finally:
        if handle:
            ctypes.windll.kernel32.ReleaseMutex(handle)


if __name__ == "__main__":
    main()
