# -*- coding: utf-8 -*-
"""自动升级：检查更新 / 下载 / 校验 / 替换安装。

发布源为 GitHub Releases（见 core/version.py 的 GITHUB_REPO）。
私有仓库或 API 限流时，可通过 ``PHPVM_GH_TOKEN`` / ``GITHUB_TOKEN``
环境变量提供访问令牌。

安装包命名与校验和清单约定见 core/version.py，由 packaging/build.py 产出：

- ``phpvm-<版本>-macos.dmg``          macOS 安装镜像（内含 phpvm.app）
- ``phpvm-<版本>-windows-setup.exe``  Inno Setup 安装程序
- ``phpvm-<版本>-linux.tar.gz``       Linux 源码包
- ``SHA256SUMS.txt``                  ``<sha256>  <文件名>`` 校验和清单

线程模型（遵循项目纪律）：本模块全部函数为**阻塞式**，须在后台线程调用；
进度经回调上报，UI 侧用 ``queue`` + ``after`` 轮询回主线程刷新。
"""
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import app_paths
from .i18n import t
from .version import (APP_NAME, GITHUB_REPO, PLATFORM_SUFFIX, RELEASES_API,
                      SUMS_ASSET)
from .version import version as current_version

TIMEOUT = 12          # 元数据请求超时（秒）
CHUNK = 256 * 1024    # 下载分块大小


class UpdateError(Exception):
    """升级流程中的可展示错误（消息已本地化）。"""


@dataclass
class Asset:
    """某个平台安装包资产。"""
    name: str
    url: str
    size: int = 0
    sha256: str = ""


@dataclass
class Release:
    """一次发布（只保留升级需要的字段）。"""
    version: str
    tag: str = ""
    notes: str = ""
    page_url: str = ""
    published_at: str = ""
    assets: dict = field(default_factory=dict)   # platform_key -> Asset
    sums: dict = field(default_factory=dict)     # 文件名 -> sha256


# --------------------------------------------------------------------------- #
# 平台与环境
# --------------------------------------------------------------------------- #
def platform_key() -> str:
    """当前平台标识（与 version.PLATFORM_SUFFIX 的键一致）。"""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def mac_app_bundle() -> str | None:
    """当前运行的 .app 路径；非安装态（源码运行）返回 None。"""
    if sys.platform != "darwin":
        return None
    path = os.path.abspath(__file__)
    marker = ".app" + os.sep
    idx = path.find(marker)
    return path[:idx + 4] if idx >= 0 else None


def supports_auto_update() -> bool:
    """当前运行形态是否支持一键自动替换。"""
    if sys.platform.startswith("win"):
        return True
    if sys.platform == "darwin":
        return mac_app_bundle() is not None
    return False


def updates_dir() -> str:
    """下载缓存目录（可写数据目录下）。"""
    d = os.path.join(app_paths.data_dir(), "updates")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        d = tempfile.gettempdir()
    return d


def _token() -> str:
    for name in ("PHPVM_GH_TOKEN", "GITHUB_TOKEN"):
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return ""


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _open(url: str, timeout: float = TIMEOUT):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", f"{APP_NAME}/{current_version()}")
    req.add_header("Accept", "application/vnd.github+json")
    token = _token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=timeout)


def _http_text(url: str, timeout: float = TIMEOUT) -> str:
    try:
        with _open(url, timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError(
                t("发布源没有可用的正式版（404）：请确认仓库已开启 Releases。")) from e
        if e.code in (401, 403):
            raise UpdateError(
                t("访问发布源被拒绝（{code}）：私有仓库需设置 PHPVM_GH_TOKEN。",
                  code=e.code)) from e
        raise UpdateError(t("网络请求失败（HTTP {code}）", code=e.code)) from e
    except urllib.error.URLError as e:
        raise UpdateError(t("无法连接发布源：{reason}", reason=e.reason)) from e
    except (TimeoutError, OSError) as e:
        raise UpdateError(t("连接发布源失败：{err}", err=e)) from e


def _parse_sums(text: str) -> dict:
    """解析 SHA256SUMS 文本 → {文件名: 摘要}。"""
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        digest = parts[0].strip().lower()
        name = os.path.basename(parts[1].strip().lstrip("*").strip())
        if len(digest) == 64 and name:
            out[name] = digest
    return out


# --------------------------------------------------------------------------- #
# 检查更新
# --------------------------------------------------------------------------- #
def check_update(timeout: float = TIMEOUT) -> Release:
    """查询最新正式版发布；网络/解析失败时抛 UpdateError。

    返回的 Release 不一定比当前版本新，是否提示由调用方用
    ``is_newer(release.version, current_version())`` 判定。
    """
    url = RELEASES_API.format(repo=GITHUB_REPO)
    raw = _http_text(url, timeout)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UpdateError(t("发布信息解析失败（返回内容不是 JSON）")) from e
    if not isinstance(data, dict):
        raise UpdateError(t("发布信息格式不正确"))

    tag = str(data.get("tag_name") or "").strip()
    ver = tag[1:] if tag.startswith("v") else tag
    if not ver:
        raise UpdateError(t("发布信息缺少版本号"))

    rel = Release(
        version=ver,
        tag=tag or ("v" + ver),
        notes=str(data.get("body") or "").strip(),
        page_url=str(data.get("html_url") or ""),
        published_at=str(data.get("published_at") or ""),
    )

    sums_url = ""
    for item in data.get("assets") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        url_ = str(item.get("browser_download_url") or "")
        if not name or not url_:
            continue
        if name.lower() == SUMS_ASSET.lower():
            sums_url = url_
            continue
        lower = name.lower()
        for key, suffix in PLATFORM_SUFFIX.items():
            if lower.endswith(suffix.lower()):
                rel.assets.setdefault(
                    key, Asset(name, url_, int(item.get("size") or 0)))

    if sums_url:
        try:
            rel.sums = _parse_sums(_http_text(sums_url, timeout))
        except UpdateError:
            rel.sums = {}
        for asset in rel.assets.values():
            asset.sha256 = rel.sums.get(asset.name, "")
    return rel


def latest_release_for(platform: str) -> Asset | None:
    """便利方法：返回最新版中指定平台的资产。"""
    return check_update().assets.get(platform)


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def download(release: Release, dest_dir: str = "", on_progress=None,
             cancel=None, timeout: float = TIMEOUT) -> str:
    """下载当前平台安装包并校验 SHA-256，返回本地路径。

    - ``on_progress(done, total)``：进度回调（total 未知时为 0），约 6 次/秒；
    - ``cancel()``：返回 True 时中断下载并抛 UpdateError。
    """
    key = platform_key()
    asset = release.assets.get(key)
    if asset is None:
        raise UpdateError(t("该版本未发布 {platform} 平台安装包", platform=key))

    dest_dir = dest_dir or updates_dir()
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        raise UpdateError(t("无法创建下载目录：{err}", err=e)) from e
    dest = os.path.join(dest_dir, asset.name)
    tmp = dest + ".part"

    digest = hashlib.sha256()
    written = 0
    total = asset.size
    last_report = 0.0
    try:
        with _open(asset.url, timeout) as resp:
            total = int(resp.headers.get("Content-Length") or total or 0)
            if on_progress:
                on_progress(0, total)
            with open(tmp, "wb") as f:
                while True:
                    if cancel is not None and cancel():
                        raise UpdateError(t("已取消下载"))
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if on_progress:
                        now = time.time()
                        if now - last_report >= 0.15:
                            last_report = now
                            on_progress(written, total)
    except UpdateError:
        _silent_remove(tmp)
        raise
    except (urllib.error.URLError, OSError) as e:
        _silent_remove(tmp)
        raise UpdateError(t("下载失败：{err}", err=e)) from e

    if on_progress:
        on_progress(written, total or written)

    if asset.sha256 and digest.hexdigest() != asset.sha256:
        _silent_remove(tmp)
        raise UpdateError(t("安装包校验失败（SHA-256 不匹配），已删除下载内容。"))
    try:
        os.replace(tmp, dest)
    except OSError as e:
        _silent_remove(tmp)
        raise UpdateError(t("保存安装包失败：{err}", err=e)) from e
    return dest


def download_latest(on_progress=None, cancel=None) -> tuple[str, Release]:
    """检查 + 下载一步到位，返回 (安装包路径, 发布信息)。"""
    release = check_update()
    if not is_newer_version(release.version):
        raise UpdateError(t("当前已是最新版本（{ver}）", ver=current_version()))
    path = download(release, on_progress=on_progress, cancel=cancel)
    return path, release


def is_newer_version(remote: str) -> bool:
    """远端版本是否比当前运行版本新。"""
    from .version import is_newer

    return is_newer(remote, current_version())


# --------------------------------------------------------------------------- #
# 应用更新
# --------------------------------------------------------------------------- #
def apply_and_restart(pkg_path: str) -> tuple[bool, str]:
    """执行替换安装并重启 phpvm。

    返回 ``(是否已接管, 提示文案)``；成功接管后调用方应尽快调用
    ``exit_app()`` 退出进程，把文件锁让给升级程序。
    """
    if sys.platform.startswith("win"):
        return _apply_windows(pkg_path)
    if sys.platform == "darwin":
        return _apply_macos(pkg_path)
    return False, t("当前平台暂不支持自动替换，请手动安装已下载的安装包。")


def _apply_windows(pkg_path: str) -> tuple[bool, str]:
    """Windows：延迟批处理静默安装（Inno Setup）并重新拉起 phpvm。"""
    launcher = os.path.join(app_paths.package_dir(), "phpvm.bat")
    bat = os.path.join(tempfile.gettempdir(), "phpvm_update.bat")
    lines = [
        "@echo off",
        "timeout /t 2 /nobreak >nul",
        f'start "" /wait "{pkg_path}" /SILENT /NORESTART /CLOSEAPPLICATIONS',
    ]
    if os.path.exists(launcher):
        lines.append(f'start "" "{launcher}"')
    lines.append('del "%~f0"')
    try:
        with open(bat, "w", encoding="gbk", errors="replace") as f:
            f.write("\r\n".join(lines) + "\r\n")
    except OSError as e:
        return False, t("无法创建升级脚本：{err}", err=e)

    flags = 0x08000000  # CREATE_NO_WINDOW：不弹出控制台窗口
    try:
        subprocess.Popen(["cmd", "/c", bat], creationflags=flags, close_fds=True)
    except OSError as e:
        return False, t("无法启动安装程序：{err}", err=e)
    return True, t("安装程序已启动，phpvm 将自动关闭并完成升级。")


def _apply_macos(pkg_path: str) -> tuple[bool, str]:
    """macOS：延迟脚本挂载 dmg → 替换 .app → 重新打开。"""
    target = mac_app_bundle()
    if not target:
        return False, t("当前不是从 .app 运行，无法自动替换；请手动挂载安装镜像。")

    script = os.path.join(tempfile.gettempdir(), "phpvm_update.sh")
    body = f"""#!/bin/sh
# phpvm 自动升级：等待主进程退出 → 挂载 dmg → 替换 .app → 重新打开
sleep 2
MNT="$(mktemp -d)"
if ! hdiutil attach -nobrowse -readonly -mountpoint "$MNT" {shlex.quote(pkg_path)} >/dev/null 2>&1; then
  osascript -e 'display alert "phpvm" message "无法挂载升级镜像，请手动安装。"' >/dev/null 2>&1
  rmdir "$MNT" 2>/dev/null
  exit 1
fi
rm -rf {shlex.quote(target)}
ditto "$MNT/phpvm.app" {shlex.quote(target)}
hdiutil detach "$MNT" -quiet >/dev/null 2>&1
rmdir "$MNT" 2>/dev/null
xattr -dr com.apple.quarantine {shlex.quote(target)} >/dev/null 2>&1
open {shlex.quote(target)}
rm -f "$0"
"""
    try:
        with open(script, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(script, 0o755)
    except OSError as e:
        return False, t("无法创建升级脚本：{err}", err=e)

    try:
        subprocess.Popen(["/bin/sh", script], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        return False, t("无法启动升级脚本：{err}", err=e)
    return True, t("升级程序已就绪，phpvm 将自动关闭并完成升级。")


def exit_app() -> None:
    """立即退出进程（跳过 Tk finally 与 atexit），让升级程序接管文件。"""
    os._exit(0)


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
