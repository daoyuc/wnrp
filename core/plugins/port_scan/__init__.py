# -*- coding: utf-8 -*-
"""端口占用插件：一张表看清「谁占着 80」。

安全边界（见 ``docs/DECISIONS.md`` ADR 25）：

- 只读采集：``netstat``（Windows）/ ``lsof``（macOS / Linux）加一次
  ``tasklist`` 取进程名；不杀进程、不改配置、不写文件、无后台进程；
- 归属只按可执行文件名推断，不读进程路径，不声称"phpvm 确认管辖"；
- 面板刷新由主窗口 tick 节流驱动（60s），CLI 只有显式调用才采集。
"""
import sys

from ..base import PluginSpec
from . import scan

MANIFEST = PluginSpec(
    key="port_scan",
    name="端口占用",
    panel=("ui.plugins.port_scan_panel", "PortScanPanel"),
    platforms=("win32", "darwin", "linux"),
    default_settings={},
    description="列出本机 TCP 监听端口与占用进程（只读，不杀进程）",
    nav_group="diag",
)


def supported(platform: str = "") -> bool:
    """三平台都可用（netstat / lsof 均为系统自带）。"""
    return MANIFEST.platform_supported(platform or sys.platform)


__all__ = ["MANIFEST", "supported", "scan"]
