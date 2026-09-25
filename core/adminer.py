# -*- coding: utf-8 -*-
"""Adminer 数据库 GUI（F9）：下载单文件 Adminer 并托管为本地站点。

设计边界：
- Adminer 是**单 PHP 文件**，不含外部依赖，也不需要本工具解析 SQL；
- 落盘位置：``<数据目录>/adminer/index.php``（与 config.json 同级的运行时目录）；
  命名为 ``index.php`` 以便站点根可直接访问；
- 下载失败（离线 / 被墙）时给出「用户自备」指引：手动下载 ``latest.php`` 存为
  ``<dir>/index.php`` 即可，本模块检测到文件即复用；
- Adminer 具备写库能力，**仅供本地开发**。
"""
import os

from . import app_paths, site_service
from .config import Config
from .i18n import t
from .php_downloader import download_with_progress
from .php_manager import PhpManager, version_key
from .vhost_manager import VhostManager

#: Adminer 官方「最新版单文件」（PHP 直接执行，无需解压）
ADMINER_URL = "https://www.adminer.org/latest.php"
DEFAULT_DOMAIN = "adminer.test"
FILE_NAME = "index.php"
_MIN_BYTES = 1024  # 合法 Adminer 单文件远大于此


def adminer_dir() -> str:
    """Adminer 落盘目录（运行时数据目录下的 adminer/）。"""
    return app_paths.data_file("adminer")


def adminer_file(directory: str | None = None) -> str:
    return os.path.join(directory or adminer_dir(), FILE_NAME)


def is_installed(path: str | None = None) -> bool:
    """是否已有可用的 Adminer 单文件（体积 + PHP 头校验）。"""
    return _looks_like_php(path or adminer_file())


def _looks_like_php(path: str) -> bool:
    try:
        if os.path.getsize(path) <= _MIN_BYTES:
            return False
        with open(path, "rb") as f:
            head = f.read(64)
        return b"<?php" in head
    except OSError:
        return False


def download(dest: str | None = None, progress_cb=None) -> dict:
    """下载 Adminer 单文件到 dest（先 .part 后原子改名）。返回 {ok, path, message}。"""
    target = dest or adminer_file()
    directory = os.path.dirname(target)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as e:
        return {"ok": False, "path": target, "message": t("无法创建目录：{err}", err=e)}
    try:
        download_with_progress(ADMINER_URL, target, progress_cb=progress_cb)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "path": target,
                "message": t("下载 Adminer 失败：{err}\n"
                             "可手动下载 {url} 并保存为 {path}", err=e,
                             url=ADMINER_URL, path=target)}
    if not _looks_like_php(target):
        return {"ok": False, "path": target,
                "message": t("下载内容不是有效的 PHP 文件：{path}\n"
                             "可手动下载 {url} 替换该文件。", path=target, url=ADMINER_URL)}
    return {"ok": True, "path": target, "message": t("已下载 Adminer：{path}", path=target)}


def _port_of(config, version) -> int | None:
    return (getattr(config, "ports", {}) or {}).get(version.name) or getattr(version, "port", None)


def pick_php(config, php_name: str | None = None):
    """选择托管 Adminer 的 PHP 版本：指定名优先，否则「在跑的优先、版本号最新」。

    返回 ``(name, port)``；无可用版本时返回 ``(None, None)``。
    """
    versions = PhpManager(config).scan_versions() or []
    if php_name:
        for v in versions:
            if v.name == php_name:
                return v.name, _port_of(config, v)
        port = (getattr(config, "ports", {}) or {}).get(php_name)
        return (php_name, int(port)) if port else (None, None)
    running = [v for v in versions if getattr(v, "running", False)]
    pool = running or versions
    if not pool:
        return None, None
    best = max(pool, key=version_key)
    return best.name, _port_of(config, best)


def find_site(config, domain: str = DEFAULT_DOMAIN):
    """查找已托管 Adminer 的站点：优先按 docroot 匹配，其次按域名。"""
    vm = VhostManager(config)
    target = os.path.normcase(os.path.abspath(adminer_dir()))
    entries = vm.scan()
    for e in entries:
        if e.root and os.path.normcase(os.path.abspath(e.root)) == target:
            return e
    for e in entries:
        if domain in (e.server_name or "").split():
            return e
    return None


def status(config=None) -> dict:
    """Adminer 安装 / 托管状态快照。"""
    config = config or Config()
    e = find_site(config)
    installed = is_installed()
    return {
        "installed": installed,
        "file": adminer_file(),
        "dir": adminer_dir(),
        "domain": DEFAULT_DOMAIN,
        "url": f"http://{DEFAULT_DOMAIN}",
        "site": e.file if e else "",
        "server_name": e.server_name if e else "",
        "hosted": bool(e),
    }


def install(config=None, *, domain: str = DEFAULT_DOMAIN, php: str | None = None,
            hosts: bool = False, reload: bool = True, dry_run: bool = False,
            progress_cb=None) -> dict:
    """下载（如需要）并创建托管站点。返回报告 dict（含 create_site 步骤）。"""
    config = config or Config()
    name, port = pick_php(config, php)
    if dry_run:
        return {"ok": True, "dry_run": True, "domain": domain, "php": name,
                "port": port, "file": adminer_file(), "dir": adminer_dir(),
                "message": t("将下载 Adminer 并创建站点 http://{domain}（PHP {php} :{port}）",
                             domain=domain, php=name or "?", port=port or "?")}
    if not name or not port:
        return {"ok": False,
                "message": t("未找到可用的 PHP 版本，请先在「PHP 版本管理」页启动一个版本。")}
    if not is_installed():
        res = download(progress_cb=progress_cb)
        if not res.get("ok"):
            return res

    plan = site_service.SitePlan(domains=[domain], template_key="php",
                                 docroot=adminer_dir(), port=int(port),
                                 hosts=hosts, reload=reload)
    result = site_service.create_site(plan, config)
    steps = [{"tag": tag, **res} for tag, res in result.steps]
    if result.fatal:
        failed = [tag for tag in site_service.HARD_STEPS if result.failed(tag)]
        return {"ok": False, "domain": domain, "php": name, "port": port,
                "file": adminer_file(), "site": result.path, "steps": steps,
                "message": t("创建 Adminer 站点失败（{steps}）",
                             steps="、".join(failed) or "?")}
    return {"ok": True, "domain": domain, "php": name, "port": port,
            "file": adminer_file(), "site": result.path, "steps": steps,
            "url": f"http://{domain}",
            "message": t("已安装 Adminer 并托管为 http://{domain}（PHP {php} :{port}）",
                         domain=domain, php=name, port=port)}
