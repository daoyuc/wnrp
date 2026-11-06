# -*- coding: utf-8 -*-
"""项目启动器插件：本地项目 → 站点 URL / 常用命令 → 一键打开浏览器或终端。

安全边界（见 ``docs/DECISIONS.md`` ADR 16）：

- 命令只来自**内置模板白名单**或用户在 phpvm 设置里手动添加的自定义命令；
- 绝不读取 ``.phpvm.json`` 里的字段当命令执行（项目配置可由团队共享，克隆即执行 = RCE），
  ``.phpvm.json`` 只用于展示 PHP 版本 / 域名等元信息；
- 站点 URL 同样只从 nginx vhost 的 ``server_name`` 生成，不采信项目配置里的域名；
- 无后台进程、无定时执行，只有用户点击 / 显式 CLI 调用才打开一次浏览器或终端；
- 自定义命令只落 ``settings.plugin_settings.project_launcher.custom_commands``。
"""
import sys

from ..base import PluginSpec
from . import commands, editor, projects

MANIFEST = PluginSpec(
    key="project_launcher",
    name="项目启动器",
    panel=("ui.plugins.project_launcher_panel", "ProjectLauncherPanel"),
    default_settings={"www_dir": "", "custom_commands": [], "editor": "",
                      "recent": [], "recent_first": False},
    platforms=("win32", "darwin", "linux"),
    description="列出本地项目与常用命令，一键打开站点或终端",
)


def supported(platform: str = "") -> bool:
    """三平台都可用（终端打开方式由 ``process_utils.open_terminal`` 收敛）。"""
    return MANIFEST.platform_supported(platform or sys.platform)


__all__ = ["MANIFEST", "supported", "commands", "editor", "projects"]
