# -*- coding: utf-8 -*-
"""访问日志聚合插件：只读解析 nginx access.log → 状态码 / 热门路径 / 异常请求。

安全边界（见 ``docs/DECISIONS.md`` ADR 17）：

- 只读 nginx 自身日志目录里的 access 日志，界面与 CLI 都只能按**文件名**选择，
  不接受任意路径；
- 不落库、不上传、不执行任何命令；无后台进程、无定时任务，刷新由主窗口 tick
  或用户点击驱动；
- 解析失败的行计入 ``skipped``，不会让面板报错或卡住。
"""
import sys

from ..base import PluginSpec
from . import aggregate

MANIFEST = PluginSpec(
    key="access_insights",
    name="访问日志聚合",
    panel=("ui.plugins.access_insights_panel", "AccessInsightsPanel"),
    default_settings={"lines": 20000, "top_n": 10},
    platforms=("win32", "darwin", "linux"),
    description="只读解析 nginx access.log，汇总状态码、热门路径与异常请求",
    nav_group="diag",  # 与 nginx 日志同属「诊断」组：都是排障时看的只读视图
)


def supported(platform: str = "") -> bool:
    """三平台都可用（解析只用标准库）。"""
    return MANIFEST.platform_supported(platform or sys.platform)


__all__ = ["MANIFEST", "supported", "aggregate"]
