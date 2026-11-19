# -*- coding: utf-8 -*-
"""证书到期插件：把各站点证书按剩余天数排成一张表。

安全边界（见 ``docs/DECISIONS.md`` ADR 27）：

- 只读：读 vhost 配置里的 ``ssl_certificate`` 路径，再用系统 ``openssl``
  读到期时间；不联网、不续期、不改配置；
- 不内置 OpenSSL、不下载二进制；找不到 openssl 时明确报 ``no_tool``；
- 面板刷新由主窗口 tick 节流（300s，证书到期变化很慢）。
"""
import sys

from ..base import PluginSpec
from . import watch

MANIFEST = PluginSpec(
    key="cert_watch",
    name="证书到期",
    panel=("ui.plugins.cert_watch_panel", "CertWatchPanel"),
    platforms=("win32", "darwin", "linux"),
    default_settings={},
    description="按剩余天数列出各站点 SSL 证书（只读，不续期）",
    nav_group="diag",
)


def supported(platform: str = "") -> bool:
    """三平台都可用（只要 PATH 里有 openssl）。"""
    return MANIFEST.platform_supported(platform or sys.platform)


__all__ = ["MANIFEST", "supported", "watch"]
