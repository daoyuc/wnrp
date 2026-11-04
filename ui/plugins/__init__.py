# -*- coding: utf-8 -*-
"""插件面板（UI 层）。

``core/plugins`` 只以「模块路径 + 类名」声明面板，主窗口在建页签时用
``importlib`` 延迟导入这里的模块 —— 保证 ``core`` 永远不 import ``ui``。
"""
