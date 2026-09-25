# -*- coding: utf-8 -*-
"""站点与应用日志源推导（只读）。

给定 config / vhost 管理器，推导出「可查看」的日志源清单，按来源分组返回。
只列入**已存在**的文件，避免列出一堆不存在的路径。

分组：
- ``Nginx``：nginx logs 目录下所有 *.log* 文件（access / error / 轮转）
- ``PHP``：各 PHP 版本 ini 里 error_log 指向且存在的文件
- ``<站点域名>``：站点 root 下常见应用日志目录
  （Laravel storage/logs、ThinkPHP runtime/log、Symfony var/log、CI application/logs）
"""
import os
import re
from dataclasses import dataclass

from core.i18n import t
from core.nginx_manager import NginxManager

# 站点 root 下候选的应用日志目录（相对 root）
_APP_LOG_DIRS = (
    ("storage/logs", "*.log"),       # Laravel
    ("runtime/log", "*.log"),        # ThinkPHP / Yii
    ("var/log", "*.log"),            # Symfony 系
    ("application/logs", "*.log"),   # CodeIgniter
)

_ERR_LOG_RE = re.compile(r"^\s*error_log\s*=\s*(\S+)", re.MULTILINE)


@dataclass
class LogSource:
    """单条日志源。

    ``label`` 在文件下拉里展示（相对名，如 ``error.log`` / ``storage/logs/laravel.log``）；
    ``path`` 绝对路径；``kind`` 为 ``nginx`` / ``php`` / ``app``。
    """

    label: str
    path: str
    kind: str


def collect(config, vhost_mgr=None) -> dict:
    """返回 ``{group_label: [LogSource, ...]}``，仅含已存在的文件；空分组不列入。"""
    groups: dict[str, list[LogSource]] = {}

    # 1) Nginx 日志
    nginx_sources = _nginx_sources()
    if nginx_sources:
        groups[t("Nginx")] = nginx_sources

    # 2) PHP error_log
    php_sources = _php_sources(config)
    if php_sources:
        groups[t("PHP")] = php_sources

    # 3) 站点应用日志
    if vhost_mgr is None:
        vhost_mgr = _vhost_manager(config)
    try:
        entries = vhost_mgr.scan()
    except Exception:  # noqa: BLE001
        entries = []
    for e in entries:
        srcs = _app_logs_for(e.root)
        if srcs:
            key = e.server_name or e.file_rel or e.file
            groups[key] = srcs

    return groups


# --------------------------------------------------------------------------- #
def _nginx_sources() -> list[LogSource]:
    try:
        logs_dir = NginxManager().logs_dir
        names = sorted(
            f for f in os.listdir(logs_dir)
            if f.endswith(".log") or f.endswith((".log.1", ".log.2"))
        )
    except OSError:
        return []
    out = []
    for f in names:
        p = os.path.join(logs_dir, f)
        if os.path.isfile(p):
            out.append(LogSource(f, p, "nginx"))
    return out


def _php_sources(config) -> list[LogSource]:
    try:
        from core.php_manager import PhpManager

        pm = PhpManager(config)
        pm.scan_versions()
        versions = pm.versions
    except Exception:  # noqa: BLE001
        return []
    out = []
    for v in versions:
        p = _php_error_log(v)
        if p and os.path.isfile(p):
            out.append(LogSource(f"{v.name}: error_log", p, "php"))
    return out


def _php_error_log(v) -> str:
    ini = getattr(v, "ini", "")
    if not ini or not os.path.isfile(ini):
        return ""
    try:
        with open(ini, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    m = _ERR_LOG_RE.search(text)
    if not m:
        return ""
    p = m.group(1).strip().strip("\"'")
    if not p or p.lower() == "syslog":
        return ""
    if not os.path.isabs(p):
        p = os.path.join(os.path.dirname(ini), p)
    return p


def _app_logs_for(root: str) -> list[LogSource]:
    if not root or not os.path.isdir(root):
        return []
    out = []
    for rel, _pat in _APP_LOG_DIRS:
        d = os.path.join(root, rel)
        if not os.path.isdir(d):
            continue
        try:
            files = sorted(f for f in os.listdir(d) if f.endswith(".log"))
        except OSError:
            continue
        for fn in files:
            out.append(LogSource(f"{rel}/{fn}", os.path.join(d, fn), "app"))
    return out


def _vhost_manager(config):
    from core.vhost_manager import VhostManager

    return VhostManager(config)
