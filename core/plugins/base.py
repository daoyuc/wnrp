# -*- coding: utf-8 -*-
"""插件声明契约（纯数据，不含任何 UI / 系统调用）。

一个「内置插件」= ``core/plugins/<key>/`` 包 + 导出的 ``MANIFEST: PluginSpec``。
框架只读该声明，不做动态执行任意代码：

- 面板在 ``ui/`` 层：``panel=("ui.plugins.keymouse_panel", "KeymousePanel")``，
  由主窗口在构建页签时延迟导入（``core`` 不 import ``ui``，铁律）；
- 后台进程：``daemon_script`` 指向随仓库分发的脚本，由 ``spawn`` 以无窗口方式
  拉起（独立进程，与 GUI 生命周期解耦）；
- 默认设置：``default_settings`` 会写进 ``config.json`` 的
  ``settings.plugin_settings[key]``，插件只从这一处读写自己的配置；
- 平台：``platforms`` 前缀匹配 ``sys.platform``；不匹配时插件仍出现在设置页
  与面板里，但面板应显示「当前平台不支持」，不得崩溃。

新增插件 = 新增一个目录并导出 MANIFEST，无需改框架代码。
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PluginSpec:
    """插件元数据（不可变，便于跨线程共享与缓存）。"""

    key: str                               # 稳定标识，同时是模块开关 key
    name: str                              # 展示名 msgid（t() 原文）
    panel: tuple[str, str] | None = None   # (ui 层模块路径, 类名)；None = 无面板
    daemon_script: str | None = None       # 相对仓库根的后台脚本路径
    ensure_background: tuple[str, str] | None = None  # (模块, 函数) 惰性拉起后台
    default_settings: dict = field(default_factory=dict)
    platforms: tuple[str, ...] = ("win32",)
    required: bool = False
    description: str = ""                  # 一句话说明 msgid（设置页/CLI 用）
    # 左侧导航分组：service / site / diag / other（见 core/modules.py::GROUPS）。
    # 默认 other：插件作者不必关心分组，未声明时落在「其他」组。
    nav_group: str = "other"

    # ---- 便捷访问 -------------------------------------------------------
    @property
    def panel_module(self) -> str:
        return self.panel[0] if self.panel else ""

    @property
    def panel_class(self) -> str:
        return self.panel[1] if self.panel else ""

    def platform_supported(self, platform: str) -> bool:
        """当前平台是否受支持（前缀匹配：win32 / darwin / linux）。"""
        return any(platform.startswith(p) for p in self.platforms)


# --------------------------------------------------------------------------- #
# 面板契约（实现类必须遵循，放在 ui/plugins/*，此处仅文档）
# --------------------------------------------------------------------------- #
class PluginPanelContract:
    """面板需要实现的两个方法（主窗口与主题系统按此调用）。

    - ``auto_refresh()``：主窗口定时 tick 调用，**仅在页签可见时**调度；
      实现里应只入队 / 起 worker，绝不阻塞主线程。
    - ``refresh_theme()``：主题切换时由 ``ui.theme.apply_mode`` 递归调用，
      Canvas 自绘内容必须在这里按新调色板重绘（颜色不能固化在初始化时）。
    """

    def auto_refresh(self) -> None:  # pragma: no cover - 契约文档
        raise NotImplementedError

    def refresh_theme(self) -> None:  # pragma: no cover - 契约文档
        raise NotImplementedError
