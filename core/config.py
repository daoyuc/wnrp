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

WNRP_ROOT = r"C:\wnrp"
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
}


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

    def get_port(self, name: str) -> int:
        return self.ports.get(name, DEFAULT_PORTS.get(name, 9000))

    def set_port(self, name: str, port: int) -> None:
        self.ports[name] = int(port)
        self.save()

    @staticmethod
    def validate_port(port: int) -> str | None:
        """端口合法性校验，返回错误信息；合法返回 None。"""
        if not isinstance(port, int):
            return "端口必须是整数"
        if not (1 <= port <= 65535):
            return "端口必须在 1-65535 之间"
        return None

    def validate_unique(self, name: str, port: int) -> str | None:
        """校验端口在所有版本间唯一（排除自身）。"""
        for other, p in self.ports.items():
            if other != name and p == port:
                return f"端口 {port} 已被 [{other}] 占用"
        return None
