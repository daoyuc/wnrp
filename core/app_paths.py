# -*- coding: utf-8 -*-
"""运行时可写目录判定（安装包场景的关键支撑）。

问题：phpvm 需要持久化 ``config.json`` / ``recover_history.json`` /
``crash_watchdog.*`` 等文件；安装到 ``/Applications/phpvm.app`` 或
``C:\\Program Files`` 后，代码目录只读，直接写包目录会失败。

策略（零迁移，对开发态与绿色版完全无影响）：

- **包目录可写**（源码运行、解压即用、``C:\\wnrp\\phpvm``）→ 继续用包目录，
  与历史行为一致，现有文件不会被搬动；
- **包目录只读**（.app / Program Files / 只读挂载）→ 改用用户数据目录
  ``~/.phpvm``，升级替换程序文件时用户数据不受影响。

可用环境变量覆盖：
- ``PHPVM_HOME``：强制指定数据目录（测试与便携部署用）；
- ``PHPVM_CONFIG``：单独指定 config.json 路径（见 core/config.py）。
"""
import os
import sys
import tempfile
import time

# 代码包根目录（core/ 的上级），也是开发态的数据目录
PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_data_dir: str | None = None


def package_dir() -> str:
    """代码所在目录（安装包内为只读）。"""
    return PACKAGE_DIR


def _probe_writable(directory: str) -> bool:
    """真实写入探测：目录存在且能创建/删除文件。"""
    if not os.path.isdir(directory):
        return False
    probe = os.path.join(directory, f".write_probe_{os.getpid()}_{int(time.time() * 1000)}")
    try:
        with open(probe, "w", encoding="utf-8"):
            pass
        os.remove(probe)
        return True
    except OSError:
        return False


def in_mac_bundle() -> bool:
    """是否运行在 macOS 的 .app 内。

    .app 在升级时会被整体替换，数据必须外置，因此即使包目录可写
    （例如用户把 .app 放在自己的家目录）也不使用包内目录。
    """
    if sys.platform != "darwin":
        return False
    marker = ".app" + os.sep + "Contents" + os.sep
    return marker in os.path.abspath(__file__) + os.sep


def data_dir() -> str:
    """返回运行时可写的数据目录（必要时创建）。"""
    global _data_dir
    if _data_dir:
        return _data_dir

    env = (os.environ.get("PHPVM_HOME") or "").strip()
    if env:
        target = os.path.expanduser(env)
    elif in_mac_bundle():
        target = os.path.join(os.path.expanduser("~"), ".phpvm")
    elif _probe_writable(PACKAGE_DIR):
        target = PACKAGE_DIR
    else:
        target = os.path.join(os.path.expanduser("~"), ".phpvm")

    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        target = tempfile.gettempdir()
    _data_dir = target
    return target


def data_file(name: str) -> str:
    """数据目录下的文件路径。"""
    return os.path.join(data_dir(), name)


def is_installed_bundle() -> bool:
    """是否运行在安装态（不可写的包目录，即需要走自动升级替换流程）。"""
    return not _probe_writable(PACKAGE_DIR)
