# -*- coding: utf-8 -*-
"""PHP 错误日志插件：把散落在各版本与各站点里的错误日志聚成一张表。

安全边界（见 ``docs/DECISIONS.md`` ADR 26）：

- 只读：只读各 PHP 版本 ``php.ini`` 指向的 ``error_log`` 与站点
  ``storage/logs`` / ``runtime/logs`` / ``logs`` 下的 ``*.log``；
- 每个文件只读尾部 256 KB，全局最多 40 个文件（按 mtime 倒序），
  不递归子目录、不跟随符号链接、不写文件、不起后台进程；
- 面板刷新由主窗口 tick 节流（60s），CLI 只有显式调用才扫描。
"""
import sys

from ..base import PluginSpec
from . import aggregate

MANIFEST = PluginSpec(
    key="php_errors",
    name="PHP 错误日志",
    panel=("ui.plugins.php_errors_panel", "PhpErrorsPanel"),
    platforms=("win32", "darwin", "linux"),
    default_settings={},
    description="汇总各 PHP 版本与站点日志里的错误（按级别与消息聚合）",
    nav_group="diag",
)


def supported(platform: str = "") -> bool:
    """三平台都可用（纯标准库读文件）。"""
    return MANIFEST.platform_supported(platform or sys.platform)


__all__ = ["MANIFEST", "supported", "aggregate"]
