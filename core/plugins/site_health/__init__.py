# -*- coding: utf-8 -*-
"""站点巡检插件：对 nginx 站点做一次只读 HTTP(S) 可用性探测。

安全边界（见 ``docs/DECISIONS.md`` ADR 20）：

- 待探测地址只由 nginx vhost 的 ``server_name`` 生成，不接受任意 URL 输入；
  泛解析（``*.x``）、非法域名、已禁用站点一律跳过；
- 只发 ``GET /``，不跟随重定向、不带 Cookie、不提交任何数据；响应体最多读
  64 KB 用于计数，读完立即关闭连接；
- 不使用系统代理（``http.client`` 直连），因此不会把请求发给外部代理；
- 不落库、不写文件、无后台进程、无定时任务：面板刷新由主窗口 tick 节流驱动，
  CLI 只有显式调用才探测。
"""
import sys

from ..base import PluginSpec
from . import probe

MANIFEST = PluginSpec(
    key="site_health",
    name="站点巡检",
    panel=("ui.plugins.site_health_panel", "SiteHealthPanel"),
    default_settings={"timeout": 5, "scheme": "http"},
    platforms=("win32", "darwin", "linux"),
    description="探测 nginx 站点的可用性（状态码 / 耗时 / 证书剩余天数）",
)


def supported(platform: str = "") -> bool:
    """三平台都可用（只用标准库 socket / http.client）。"""
    return MANIFEST.platform_supported(platform or sys.platform)


__all__ = ["MANIFEST", "supported", "probe"]
