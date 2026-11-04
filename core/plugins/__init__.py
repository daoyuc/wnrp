# -*- coding: utf-8 -*-
"""内置插件注册表：发现、模块开关合并、后台进程拉起。

设计要点
--------
- **只认声明，不执行任意代码**：遍历 ``core/plugins/*`` 子包并 ``importlib``
  读取其 ``MANIFEST``（:class:`~core.plugins.base.PluginSpec`）。单个插件导入
  失败只记一条 warn 日志，绝不阻断 phpvm 启动。
- **与模块开关统一收口**：``core/modules.py`` 把本模块的发现结果并进
  ``MODULES``，于是设置页勾选框、启动快照、CLI ``plugin enable/disable``
  自动覆盖插件，不需要各处再写一遍。
- **后台进程与 GUI 解耦**：``ensure_backgrounds(config)`` 幂等地拉起守护进程
  （如键鼠采集），主程序退出不影响它；插件被停用后由守护进程自行退出。
- 本模块属于 ``core`` 层：**不 import tkinter / ui**。面板只以
  "模块路径 + 类名" 的形式声明，由主窗口延迟导入。
"""
import importlib
import os
import pkgutil
import sys

from .. import run_log
from ..i18n import t
from .base import PluginPanelContract, PluginSpec
from . import settings as plugin_settings

__all__ = [
    "PluginSpec",
    "PluginPanelContract",
    "plugin_settings",
    "discover",
    "reload",
    "get",
    "enabled_specs",
    "ensure_backgrounds",
]

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_SCOPE = "plugin"

_cache: list[PluginSpec] | None = None


# --------------------------------------------------------------------------- #
# 发现
# --------------------------------------------------------------------------- #
def discover(refresh: bool = False) -> list[PluginSpec]:
    """返回全部内置插件声明（按 key 排序，结果缓存）。

    单个插件导入异常 → 记 warn 并跳过；同名 key 只保留第一个。
    """
    global _cache
    if _cache is not None and not refresh:
        return list(_cache)

    found: list[PluginSpec] = []
    seen: set[str] = set()
    for info in sorted(pkgutil.iter_modules([_PLUGIN_DIR]), key=lambda i: i.name):
        if not info.ispkg or info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as e:  # noqa: BLE001 - 单个插件坏了不该拖垮启动
            run_log.warn(_SCOPE, t("插件 {name} 加载失败：{err}",
                                   name=info.name, err=f"{type(e).__name__}: {e}"))
            continue
        spec = getattr(mod, "MANIFEST", None)
        if not isinstance(spec, PluginSpec) or not spec.key:
            run_log.warn(_SCOPE, t("插件 {name} 缺少有效的 MANIFEST，已跳过",
                                   name=info.name))
            continue
        if spec.key in seen:
            run_log.warn(_SCOPE, t("插件 key 重复：{key}（{name} 已跳过）",
                                   key=spec.key, name=info.name))
            continue
        seen.add(spec.key)
        found.append(spec)

    found.sort(key=lambda s: s.key)
    _cache = found
    return list(found)


def reload() -> list[PluginSpec]:
    """丢弃缓存重新发现（测试 / 插件开关变化后强制刷新用）。"""
    global _cache
    _cache = None
    return discover()


def get(key: str) -> PluginSpec | None:
    """按 key 取插件声明；未知 key 返回 None。"""
    for spec in discover():
        if spec.key == key:
            return spec
    return None


def enabled_specs(config) -> list[PluginSpec]:
    """已启用的插件（受 ``settings.disabled_modules`` 控制）。

    平台不受支持的插件仍会返回 —— 面板需要显示「当前平台不支持采集」，
    而不是让页签凭空消失。
    """
    from .. import modules  # 延迟导入：modules 导入期会反过来调用 discover()

    return [s for s in discover() if modules.is_enabled(s.key, config)]


# --------------------------------------------------------------------------- #
# 后台进程
# --------------------------------------------------------------------------- #
def ensure_backgrounds(config) -> list[str]:
    """拉起已启用插件的后台进程（幂等，返回面向日志的结果文本）。"""
    results: list[str] = []
    platform = sys.platform
    for spec in enabled_specs(config):
        target = spec.ensure_background
        if not target or not spec.platform_supported(platform):
            continue
        try:
            mod = importlib.import_module(target[0])
            fn = getattr(mod, target[1])
            ok, msg = fn(config)
        except Exception as e:  # noqa: BLE001
            msg = t("{name} 后台启动失败：{err}", name=t(spec.name),
                    err=f"{type(e).__name__}: {e}")
            ok = False
        level = run_log.info if ok else run_log.error
        level(_SCOPE, msg)
        results.append(msg)
    return results
