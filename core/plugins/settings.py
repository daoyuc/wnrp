# -*- coding: utf-8 -*-
"""插件设置的统一读写入口。

所有插件配置都落在 ``config.json → settings.plugin_settings[<plugin key>]``，
避免每个插件自造顶层配置键（那样 AGENTS 的「运行期数据外置」约定会被打散，
也无法一眼看全某个插件有哪些设置）。

用法（插件代码）::

    from .. import settings as plugin_settings
    autostart = plugin_settings.get(config, "keymouse", "autostart", False)
    plugin_settings.set(config, "keymouse", "autostart", True)

``default_settings`` 来自 ``PluginSpec.default_settings``；读取时先取默认值再被
用户值覆盖，因此插件新增设置项时老配置不需要迁移。
"""
SETTINGS_KEY = "plugin_settings"


def _all(config) -> dict:
    raw = config.get_setting(SETTINGS_KEY, {}) if config is not None else {}
    return dict(raw) if isinstance(raw, dict) else {}


def all_for(config, plugin_key: str) -> dict:
    """某插件的全部设置（不含默认值，仅用户已落盘的值）。"""
    block = _all(config).get(plugin_key)
    return dict(block) if isinstance(block, dict) else {}


def get(config, plugin_key: str, name: str, default=None):
    """读一个设置项；用户值优先，其次调用方给的 default。"""
    return all_for(config, plugin_key).get(name, default)


def set(config, plugin_key: str, name: str, value) -> None:
    """写一个设置项并持久化（config.save 一次写整个 config.json）。"""
    data = _all(config)
    block = data.get(plugin_key)
    block = dict(block) if isinstance(block, dict) else {}
    block[name] = value
    data[plugin_key] = block
    config.set_setting(SETTINGS_KEY, data)


def merged(config, spec) -> dict:
    """插件设置的「默认值 + 用户值」合并结果（面板/CLI 展示用）。"""
    out = dict(spec.default_settings or {})
    out.update(all_for(config, spec.key))
    return out


def defaults(spec) -> dict:
    """插件的默认设置副本（不可变兜底，避免共享可变字典被误改）。"""
    return dict(spec.default_settings or {})
