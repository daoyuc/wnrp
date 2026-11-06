# -*- coding: utf-8 -*-
"""项目发现：合并 www 目录、vhost root 与 ``.phpvm.json`` 元信息，按规范化路径去重。

只读扫描，不写任何文件；磁盘 I/O 由调用方放进 worker 线程。

注意：**只读项目根目录下的** ``.phpvm.json``，不用 ``find_project_config()`` 向上查找 ——
后者可能命中 www 根目录的共享配置，把元信息错误地套到每个子项目上。
"""
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from ... import php_manager, project_config, vhost_manager
from ...config import WNRP_ROOT
from ...i18n import t
from .. import settings as plugin_settings

KEY = "project_launcher"
PROJECT_FILE = project_config.PROJECT_FILE
WWW_KEY = "www_dir"
RECENT_KEY = "recent"
RECENT_LIMIT = 20
SORT_KEYS = ("name", "php", "domains", "recent")
_URL_SCHEMES = ("http", "https")
_RE_LISTEN_443 = re.compile(r"^\s*listen\s+443\b", re.M)

# 用于判断该项目「适合哪些内置命令模板」的标志文件（相对项目根）
_MARKER_PATHS = (
    "composer.json", "artisan", "package.json", "index.php",
    "public/index.php", "vendor/bin/phpunit", "vendor/bin/phpunit.bat",
)


def norm_key(path: str) -> str:
    """路径去重键：Windows 大小写不敏感，统一成 normcase + abspath。"""
    return os.path.normcase(os.path.abspath(path or ""))


def default_www_dir() -> str:
    return os.path.join(WNRP_ROOT, "www")


def resolve_www_dir(config=None) -> str:
    """当前生效的扫描根目录：插件设置优先，其次 ``<WNRP_ROOT>/www``。"""
    configured = str(plugin_settings.get(config, KEY, WWW_KEY, "") or "").strip()
    return configured or default_www_dir()


def set_www_dir(config, path: str) -> tuple[str, str]:
    """持久化扫描根目录；返回 ``(保存后的路径, 错误说明)``。

    空串表示恢复默认（``<WNRP_ROOT>/www``），便于「选错了再清空」。
    """
    raw = str(path or "").strip()
    if not raw:
        plugin_settings.set(config, KEY, WWW_KEY, "")
        return default_www_dir(), ""
    full = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(full):
        return "", t("目录不存在：{path}", path=full)
    plugin_settings.set(config, KEY, WWW_KEY, full)
    return full, ""


def root_exists(project) -> bool:
    """项目目录还在不在（移动/删除后置灰提示用）。"""
    root = str(getattr(project, "root", "") or "")
    return bool(root) and os.path.isdir(root)


def match_query(project, query: str) -> bool:
    """搜索：覆盖名称、路径、PHP 版本、域名、站点 URL、标志文件。"""
    q = str(query or "").strip().lower()
    if not q:
        return True
    fields = [project.name, project.root, project.php, *project.domains,
              *project.urls, *project.markers]
    if project.has_project_config:
        fields.append(PROJECT_FILE)
    return any(q in str(field).lower() for field in fields)


def arrange(items, sort_key: str = "name", reverse: bool = False,
            recent=()) -> list:
    """按列排序；``sort_key="recent"`` 时按最近使用顺序（未用过的按名称）。"""
    out = list(items)
    if sort_key == "recent":
        rank = {key: i for i, key in enumerate(recent or ())}
        out.sort(key=lambda p: (rank.get(p.key, len(rank)), p.name.lower(),
                                p.root.lower()), reverse=reverse)
    elif sort_key == "php":
        out.sort(key=lambda p: ((p.php or ""), p.name.lower(), p.root.lower()),
                 reverse=reverse)
    elif sort_key == "domains":
        out.sort(key=lambda p: (" ".join(p.domains), p.name.lower(),
                                p.root.lower()), reverse=reverse)
    else:
        out.sort(key=lambda p: (p.name.lower(), p.root.lower()), reverse=reverse)
    return out


def recent_keys(config) -> list[str]:
    """最近使用过的项目键（最近的在最前，最多 ``RECENT_LIMIT`` 条）。"""
    raw = plugin_settings.get(config, KEY, RECENT_KEY, [])
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        key = str(item or "").strip()
        if key and key not in out:
            out.append(key)
    return out[:RECENT_LIMIT]


def touch_recent(config, project) -> list[str]:
    """把一个项目提到「最近使用」最前面（打开站点 / 终端 / 编辑器后调用）。"""
    key = getattr(project, "key", "") or norm_key(getattr(project, "root", ""))
    if not key:
        return recent_keys(config)
    items = [item for item in recent_keys(config) if item != key]
    items.insert(0, key)
    items = items[:RECENT_LIMIT]
    plugin_settings.set(config, KEY, RECENT_KEY, items)
    return items


def clear_recent(config) -> None:
    plugin_settings.set(config, KEY, RECENT_KEY, [])


@dataclass(frozen=True)
class Project:
    """一个本地项目（不可变，便于跨线程传给 UI 渲染）。"""

    name: str
    root: str
    php: str = ""
    domains: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()  # 只来自 vhost server_name，用于「在浏览器打开」
    markers: tuple[str, ...] = ()
    has_project_config: bool = False
    sources: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return norm_key(self.root)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "root": self.root,
            "php": self.php,
            "domains": list(self.domains),
            "urls": list(self.urls),
            "markers": list(self.markers),
            "has_project_config": self.has_project_config,
            "sources": list(self.sources),
        }


def _detect_markers(root: str) -> tuple[str, ...]:
    found = []
    for marker in _MARKER_PATHS:
        if os.path.exists(os.path.join(root, *marker.split("/"))):
            found.append(marker)
    # Windows 的 phpunit 是 .bat，统一成同一 marker 供模板匹配
    if "vendor/bin/phpunit.bat" in found and "vendor/bin/phpunit" not in found:
        found.append("vendor/bin/phpunit")
    return tuple(found)


def _www_roots(www_dir: str) -> list[str]:
    """只扫一层子目录，避免递归整个 www 树。"""
    if not www_dir or not os.path.isdir(www_dir):
        return []
    out: list[str] = []
    try:
        with os.scandir(www_dir) as it:
            for entry in it:
                if entry.is_dir() and not entry.name.startswith("."):
                    out.append(entry.path)
    except OSError:
        pass
    return out


def _vhost_root(entry) -> str:
    raw = str(getattr(entry, "root", "") or "").strip().strip('"')
    return raw if raw and os.path.isdir(raw) else ""


def valid_site_url(url: str) -> bool:
    """URL 能否交给 ``webbrowser.open``：只允许 http(s) + 合法域名、不带路径参数。"""
    if not isinstance(url, str) or not url or len(url) > 300:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if (parts.scheme not in _URL_SCHEMES or not parts.hostname
            or port is not None or parts.path not in ("", "/")
            or parts.query or parts.fragment
            or parts.username or parts.password):
        return False
    if "*" in parts.hostname:  # 泛解析域名不是可访问地址
        return False
    return vhost_manager.valid_domain(parts.hostname)


def site_urls(project) -> list[str]:
    """项目可访问的站点 URL（已过滤非法值），GUI / CLI 共用。"""
    return [u for u in (getattr(project, "urls", ()) or ()) if valid_site_url(u)]


def _entry_urls(entry) -> list[str]:
    """一个 server 块生成的 URL；域名只取自 vhost 配置，绝不来自 ``.phpvm.json``。

    HTTPS 以配置文件里是否出现 ``listen 443`` 近似判断（与站点页一致）；同文件
    多 server 块时可能把 http 块也判成 https，故多域名项目在右键菜单里可逐个选。
    已禁用站点（``.conf.disabled``）不产出 URL —— 打开必然失败。
    """
    if getattr(entry, "disabled", False):
        return []
    conf = str(getattr(entry, "file", "") or "")
    https = False
    if conf and os.path.isfile(conf):
        try:
            with open(conf, "r", encoding="utf-8", errors="replace") as f:
                https = bool(_RE_LISTEN_443.search(f.read()))
        except OSError:
            pass
    scheme = "https" if https else "http"
    out: list[str] = []
    for name in str(getattr(entry, "server_name", "") or "").split():
        if name.startswith("*") or not vhost_manager.valid_domain(name):
            continue
        url = f"{scheme}://{name}"
        if valid_site_url(url) and url not in out:
            out.append(url)
    return out


def scan(config=None, www_dir: str = "", vhost_entries=None) -> list[Project]:
    """扫描项目；``www_dir`` / ``vhost_entries`` 可注入，便于测试与离线使用。"""
    effective_www = www_dir or resolve_www_dir(config)
    if vhost_entries is None and config is not None:
        try:
            vhost_entries = vhost_manager.VhostManager(config).scan()
        except Exception:  # noqa: BLE001 - 站点扫描失败不该让整个插件空白
            vhost_entries = []
    vhost_entries = list(vhost_entries or [])

    indexed: dict[str, dict] = {}
    for root, source in ([(r, "www") for r in _www_roots(effective_www)]
                         + [(r, "vhost") for r in
                            filter(None, (_vhost_root(e) for e in vhost_entries))]):
        key = norm_key(root)
        if key not in indexed:
            indexed[key] = {"root": os.path.abspath(root), "sources": set(),
                            "php": "", "domains": [], "urls": []}
        indexed[key]["sources"].add(source)

    for entry in vhost_entries:
        root = _vhost_root(entry)
        item = indexed.get(norm_key(root)) if root else None
        if item is None:
            continue
        if not item["php"]:
            item["php"] = str(getattr(entry, "php_version", "") or "").split(",")[0].strip()
        for domain in str(getattr(entry, "server_name", "") or "").split():
            if domain and not domain.startswith("*") and domain not in item["domains"]:
                item["domains"].append(domain)
        for url in _entry_urls(entry):
            if url not in item["urls"]:
                item["urls"].append(url)

    out: list[Project] = []
    for item in indexed.values():
        root = item["root"]
        has_cfg = False
        cfg_path = os.path.join(root, PROJECT_FILE)
        if os.path.isfile(cfg_path):
            try:
                cfg = project_config.load_project(cfg_path)
                has_cfg = True
                item["php"] = cfg.get("php") or item["php"]
                for domain in cfg.get("domains") or []:
                    if domain not in item["domains"]:
                        item["domains"].append(domain)
            except project_config.ProjectConfigError:
                pass  # 坏配置只影响元信息，不该丢掉整个项目
        out.append(Project(
            name=os.path.basename(root) or root,
            root=root,
            php=item["php"],
            domains=tuple(item["domains"]),
            urls=tuple(item["urls"]),
            markers=_detect_markers(root),
            has_project_config=has_cfg,
            sources=tuple(sorted(item["sources"])),
        ))
    out.sort(key=lambda p: (p.name.lower(), p.root.lower()))
    return out


def find(project_list, name_or_root: str) -> Project | None:
    """按项目名（大小写不敏感）或路径查找；先精确、后子串。"""
    query = (name_or_root or "").strip()
    if not query:
        return None
    for project in project_list:
        if project.name.lower() == query.lower() or norm_key(query) == project.key:
            return project
    for project in project_list:
        if query.lower() in project.name.lower():
            return project
    return None


def resolve_php_dir(config, php_name: str) -> str:
    """按 AGENTS 铁律：判 PHP 状态统一走 ``resolve(refresh_status=True, fast=True)``。

    拿不到目录时返回空串（只是不前置 PATH，命令仍可在终端里跑）。
    """
    if not php_name or config is None:
        return ""
    try:
        for version in php_manager.PhpManager(config).resolve(
                refresh_status=True, fast=True):
            if getattr(version, "name", "") == php_name:
                return str(getattr(version, "dir", "") or "")
    except Exception:  # noqa: BLE001 - PHP 探测失败不影响打开终端
        pass
    return ""
