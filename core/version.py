# -*- coding: utf-8 -*-
"""phpvm 版本与发布元信息（单一事实来源）。

版本号被三处消费，务必保持同源：
1. 关于页显示与「检查更新」比对（core/updater.py）；
2. 打包脚本（packaging/build.py）写入 Info.plist / Inno Setup 版本号；
3. GitHub Releases 的 tag（构建时由 CI 去掉前缀 `v` 注入）。

构建期可用环境变量 ``PHPVM_BUILD_VERSION`` 覆盖，因此本文件里的
``APP_VERSION`` 只需与当前开发分支保持一致。
"""
import os
import re

APP_NAME = "phpvm"
APP_ID = "com.phpvm.app"
APP_TITLE = "phpvm · PHP 版本管理器"

# 语义化版本 MAJOR.MINOR.PATCH（可带 -beta.N 之类的预发布后缀）
APP_VERSION = "1.0.0"

# 自动升级发布源（GitHub Releases）
GITHUB_REPO = "daoyuc/wnrp"
RELEASES_API = "https://api.github.com/repos/{repo}/releases/latest"
RELEASES_PAGE = "https://github.com/{repo}/releases"

# 安装包命名约定：build.py 与 updater 必须一致
#   macos   -> phpvm-<version>-macos.dmg
#   windows -> phpvm-<version>-windows-setup.exe
#   linux   -> phpvm-<version>-linux.tar.gz
PLATFORM_SUFFIX = {
    "macos": ".dmg",
    "windows": "-setup.exe",
    "linux": ".tar.gz",
}

# 校验和清单资产名（内含 `<sha256>  <文件名>` 若干行）
SUMS_ASSET = "SHA256SUMS.txt"

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+]([0-9A-Za-z.\-]+))?$")


def version() -> str:
    """当前运行版本（安装包内为构建时注入的版本）。"""
    return os.environ.get("PHPVM_BUILD_VERSION") or APP_VERSION


def parse(text: str) -> tuple[int, int, int, str] | None:
    """解析版本号 → (major, minor, patch, pre)；无法解析返回 None。"""
    m = _VERSION_RE.match((text or "").strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0),
            int(m.group(3) or 0), m.group(4) or "")


def is_newer(remote: str, local: str) -> bool:
    """remote 是否比 local 新。

    数字段按数值比较；数字段相同时，正式版视为高于同号预发布版
    （1.0.0 > 1.0.0-beta.1）。任一侧无法解析时退化为「不同即视为更新」。
    """
    r, l = parse(remote), parse(local)
    if r is None or l is None:
        return bool(remote) and remote != local
    if r[:3] != l[:3]:
        return r[:3] > l[:3]
    return bool(l[3]) and not r[3]


def tag_for(ver: str) -> str:
    """版本号 → release tag（统一 `v` 前缀）。"""
    v = (ver or "").strip()
    return v if v.startswith("v") else "v" + v


def asset_name(platform_key: str, ver: str) -> str:
    """按命名约定拼出某平台的安装包文件名。"""
    suffix = PLATFORM_SUFFIX.get(platform_key, "")
    return f"{APP_NAME}-{ver}-{platform_key}{suffix}"
