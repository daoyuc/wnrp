# -*- coding: utf-8 -*-
"""配置加载 / 保存 / 端口映射管理。

- 端口映射持久化到 phpvm/config.json
- 默认端口映射与现有脚本约定保持一致：
  php82 -> 9000（主版本，vhost 默认指向）
  php74 -> 9074（start_php74.bat 已改为 9074）
  php   -> 9001（与 fund.conf / type_test.conf 的 vhost 兼容）
  其余按版本号规则：9056 / 9072 / 9073 / 9080 / 9081
"""
import json
import os
import re
import sys

IS_WIN = sys.platform.startswith("win")

# 环境根目录：Windows 默认 C:\wnrp；macOS/Linux 默认 ~/wnrp（可用 WNRP_ROOT 覆盖）
WNRP_ROOT = os.environ.get("WNRP_ROOT") or (
    r"C:\wnrp" if IS_WIN else os.path.join(os.path.expanduser("~"), "wnrp")
)
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

DEFAULT_PORTS = {
    "php": 9001,     # PHP 5.x 老版本（start_nginx-php72.bat 中曾用 9001）
    "php56": 9056,
    "php72": 9072,
    "php73": 9073,
    "php74": 9074,   # 已按用户要求改为 9074
    "php8": 9080,
    "php81": 9081,
    "php82": 9000,   # 主版本，vhost 默认指向
    "php85": 9085,
}

# 功能开关等设置（持久化到 config.json 的 settings 字段）
DEFAULT_SETTINGS = {
    "auto_recover_crash": False,  # php-cgi 崩溃后自动重启（自愈），默认关闭
    "auto_recover_limit": 3,      # 每小时每版本自愈次数上限
    "lang": None,                 # 界面语言；None = 跟随系统 locale（en/zh_CN/zh_TW/ja/ko）
}


def brew_prefixes() -> list[str]:
    """返回存在的 Homebrew 前缀根（仅 posix；Windows 返回空）。

    用于 mac/Linux 自动发现 brew 安装的 php/nginx/redis。常见前缀：
    /opt/homebrew（Apple Silicon）、/usr/local（Intel）、/home/linuxbrew/.linuxbrew。
    """
    if IS_WIN:
        return []
    cands = [
        os.environ.get("HOMEBREW_PREFIX", ""),
        "/opt/homebrew",
        "/usr/local",
        "/home/linuxbrew/.linuxbrew",
    ]
    out: list[str] = []
    for c in cands:
        c = c.rstrip("/")
        if c and c not in out and os.path.isdir(os.path.join(c, "opt")):
            out.append(c)
    return out


def _derive_port(name: str) -> int:
    """未在默认映射表中的版本名推导端口。

    规则与目录约定一致：php{两位版本号} → 9000+版本号（php74→9074、
    php83→9083），保证多版本自动发现时默认端口互不冲突；无规则可循时回落 9000。
    """
    m = re.fullmatch(r"php(\d{2,3})", name)
    if m:
        return 9000 + int(m.group(1))
    return 9000


class Config:
    """phpvm 配置：端口映射 + 功能设置 的加载与持久化。"""

    def __init__(self, config_path: str = CONFIG_PATH):
        self.config_path = config_path
        self.ports: dict[str, int] = dict(DEFAULT_PORTS)
        self.settings: dict = dict(DEFAULT_SETTINGS)
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.config_path):
            self.save()
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved = data.get("ports", {})
            for k, v in saved.items():
                try:
                    self.ports[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
            for k in DEFAULT_SETTINGS:
                if k in data.get("settings", {}):
                    self.settings[k] = data["settings"][k]
        except (OSError, json.JSONDecodeError):
            # 配置损坏时回退默认并覆盖保存
            self.save()

    def save(self) -> None:
        data = {"ports": self.ports, "settings": self.settings}
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def get_setting(self, key: str, default=None):
        return self.settings.get(key, DEFAULT_SETTINGS.get(key, default))

    def set_setting(self, key: str, value) -> None:
        self.settings[key] = value
        self.save()

    # ---- 界面语言 ----
    def get_lang(self) -> str:
        """返回当前界面语言 code；配置为空时按系统 locale 探测。"""
        from .i18n import LANGS, detect_language

        saved = self.settings.get("lang")
        return saved if saved in LANGS else detect_language()

    def set_lang(self, lang: str) -> None:
        """持久化界面语言（None 表示恢复“跟随系统”）。"""
        from .i18n import LANGS

        if lang in LANGS or lang is None:
            self.settings["lang"] = lang
            self.save()

    def get_port(self, name: str) -> int:
        return self.ports.get(name, _derive_port(name))

    def set_port(self, name: str, port: int) -> None:
        self.ports[name] = int(port)
        self.save()

    @staticmethod
    def validate_port(port: int) -> str | None:
        """端口合法性校验，返回错误信息；合法返回 None。"""
        from .i18n import t

        if not isinstance(port, int):
            return t("端口必须是整数")
        if not (1 <= port <= 65535):
            return t("端口必须在 1-65535 之间")
        return None

    def validate_unique(self, name: str, port: int) -> str | None:
        """校验端口在所有版本间唯一（排除自身）。"""
        from .i18n import t

        for other, p in self.ports.items():
            if other != name and p == port:
                return t("端口 {port} 已被 [{other}] 占用", port=port, other=other)
        return None
