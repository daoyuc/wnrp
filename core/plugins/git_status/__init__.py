# -*- coding: utf-8 -*-
"""Git 工作区看板插件：本地项目 → 分支 / 未提交 / 领先落后 / 最近提交。

安全边界（见 ``docs/DECISIONS.md`` ADR 21）：

- 只调用**只读** git 子命令：``status --porcelain=v2 --branch`` 与 ``log -1``；
  不调用 ``fetch`` / ``pull`` / ``add`` / ``commit`` 等任何会改仓库或联网的动作；
- 命令以 argv 列表交给 ``subprocess.run``（不经 shell），并关闭可选锁
  （``--no-optional-locks``）与凭据交互（``GIT_TERMINAL_PROMPT=0``），
  因此不会写 index、不会等人输入密码；
- 仓库来源复用项目启动器的项目发现（``www`` 一层子目录 + vhost root），
  不递归整个磁盘、不采信项目里的 ``.phpvm.json``；
- 无后台进程、无定时执行：面板与 CLI 都只在显式刷新时跑一次。
"""
import sys

from ..base import PluginSpec
from . import repos

MANIFEST = PluginSpec(
    key="git_status",
    name="Git 工作区",
    panel=("ui.plugins.git_status_panel", "GitStatusPanel"),
    default_settings={"timeout": 6.0},
    platforms=("win32", "darwin", "linux"),
    description="汇总各项目的分支、未提交改动与待推送 / 待拉取数量",
)


def supported(platform: str = "") -> bool:
    """三平台都可用（git 可执行文件缺失时由 ``repos`` 返回错误码，不崩）。"""
    return MANIFEST.platform_supported(platform or sys.platform)


__all__ = ["MANIFEST", "supported", "repos"]
