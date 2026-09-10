# -*- coding: utf-8 -*-
"""可选模块注册表：控制主窗口加载哪些功能页。

约定：
- 默认全部启用；用户可在「关于 → 模块」取消勾选非刚需模块；
- `required=True` 的模块为刚需（PHP 版本管理 / Nginx 管理 / 站点映射），不允许取消；
- 被禁用的模块在下次启动时**不创建面板、不实例化对应 manager**，
  因此相关功能代码不会被加载（重启后生效，因为界面在启动时构建）。

持久化：config.json → settings.disabled_modules（模块 key 列表）。
"""
from .i18n import t

# key 需与 ui/main_window.py 中的面板构建分支一致
MODULES: list[dict] = [
    {"key": "php", "name": "PHP 版本管理", "required": True},
    {"key": "nginx", "name": "Nginx 管理", "required": True},
    {"key": "vhost", "name": "站点映射", "required": True},
    {"key": "redis", "name": "Redis 管理", "required": False},
    {"key": "mysql", "name": "MySQL 管理", "required": False},
    {"key": "sqlite", "name": "SQLite 数据库", "required": False},
    {"key": "log", "name": "Nginx 日志", "required": False},
]

_KEYS = {m["key"] for m in MODULES}
REQUIRED_KEYS = {m["key"] for m in MODULES if m["required"]}


def normalize(disabled) -> list[str]:
    """清洗配置值：只保留已知且非刚需的 key，去重。"""
    if not isinstance(disabled, (list, tuple, set)):
        return []
    out: list[str] = []
    for k in disabled:
        k = str(k)
        if k in _KEYS and k not in REQUIRED_KEYS and k not in out:
            out.append(k)
    return out


def disabled_modules(config) -> list[str]:
    """返回被禁用的模块 key 列表（已清洗）。"""
    return normalize(config.get_setting("disabled_modules", []))


def is_enabled(key: str, config) -> bool:
    """模块是否启用；未知 key 视为启用（保守，不隐藏功能）。"""
    if key in REQUIRED_KEYS:
        return True
    return key not in disabled_modules(config)


def set_disabled(config, keys) -> list[str]:
    """写入禁用列表（自动剔除刚需与未知项），返回最终列表。"""
    cleaned = normalize(keys)
    config.set_setting("disabled_modules", cleaned)
    return cleaned


def label(meta: dict) -> str:
    """设置界面显示文本（刚需模块附带说明）。"""
    name = t(meta["name"])
    return f"{name}（{t('刚需，不可取消')}）" if meta["required"] else name
