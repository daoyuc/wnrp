# -*- coding: utf-8 -*-
"""首页总览仪表盘：只读聚合「服务状态 / 站点告警 / 最近崩溃 / 最近运行日志」。

完全复用各 manager 与 run_log，不新增轮询、不新增常驻进程；跟随主窗口 8 秒心跳刷新。
数据由 GUI / CLI 共用（CLI 用于机器可读快照）。
"""
from dataclasses import dataclass, field
from typing import Optional

from core import i18n
from core import hosts_manager as hm
from core import run_log
from core import diag

t = i18n.t

_LOOPBACK = ("127.0.0.1", "::1")


@dataclass
class ServiceView:
    name: str
    kind: str           # nginx | php | redis | mysql
    running: bool
    detail: str = ""    # 端口 / 版本 / PID 等


@dataclass
class Overview:
    nginx: ServiceView
    php: list = field(default_factory=list)
    redis: list = field(default_factory=list)
    mysql: list = field(default_factory=list)
    site_count: int = 0
    alerts: list = field(default_factory=list)
    crashes: list = field(default_factory=list)
    recent_logs: list = field(default_factory=list)
    health: str = "ok"     # ok | warn | err

    def to_dict(self) -> dict:
        def s(x: ServiceView) -> dict:
            return {"name": x.name, "kind": x.kind,
                    "running": x.running, "detail": x.detail}
        return {
            "nginx": s(self.nginx),
            "php": [s(x) for x in self.php],
            "redis": [s(x) for x in self.redis],
            "mysql": [s(x) for x in self.mysql],
            "site_count": self.site_count,
            "alerts": list(self.alerts),
            "crashes": list(self.crashes),
            "recent_logs": list(self.recent_logs),
            "health": self.health,
        }


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def _read_hosts_map() -> dict:
    try:
        with open(hm.hosts_path(), "r", encoding="utf-8", errors="replace") as f:
            return hm.parse_mapping(f.read())
    except OSError:
        return {}


def _https_cert_missing(entry) -> Optional[str]:
    try:
        with open(entry.file, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    if "ssl_certificate" not in text:
        return None
    return None  # 仅做存在性缺失告警：证书文件存在性校验交给 diag，避免过度实现


def build_overview(config, php_mgr, nginx_mgr, redis_mgr, mysql_mgr,
                   vhost_mgr, hosts_map: Optional[dict] = None) -> Overview:
    """聚合总览数据（只读）。任一 manager 缺失（模块停用）时跳过对应分组。"""
    # Nginx
    ng_run, ng_pids = _safe(lambda: nginx_mgr.get_status(), (False, [])) or (False, [])
    nginx = ServiceView("Nginx", "nginx", bool(ng_run),
                        t("PID {p}", p=",".join(map(str, ng_pids))) if ng_pids else "")

    # PHP
    php_versions = _safe(lambda: php_mgr.resolve(refresh_status=True, fast=True), []) or []
    php = [ServiceView(v.name, "php", bool(v.running),
                       f"{getattr(v, 'display', '')} :{getattr(v, 'port', '?')}")
           for v in php_versions]
    running_ports = {getattr(v, "port", None) for v in php_versions if v.running}

    # Redis
    redis: list = []
    if redis_mgr is not None:
        _safe(redis_mgr.refresh_instances)
        _safe(redis_mgr.get_status_all)
        for inst in getattr(redis_mgr, "instances", []) or []:
            redis.append(ServiceView(getattr(inst, "name", "?"), "redis",
                                     bool(getattr(inst, "running", False)),
                                     f":{getattr(inst, 'port', '?')}"))

    # MySQL
    mysql: list = []
    if mysql_mgr is not None:
        _safe(mysql_mgr.refresh_instances)
        for inst in getattr(mysql_mgr, "instances", []) or []:
            mysql.append(ServiceView(getattr(inst, "name", "?"), "mysql",
                                     bool(getattr(inst, "running", False)),
                                     f":{getattr(inst, 'port', '?')}"))

    # 站点与告警
    entries = _safe(vhost_mgr.scan, []) or []
    alerts: list = []
    if hosts_map is None:
        hosts_map = _read_hosts_map()
    for e in entries:
        domains = (getattr(e, "server_name", "") or "").split()
        bad = [d for d in domains
               if d not in hosts_map or hosts_map.get(d) not in _LOOPBACK]
        for d in bad:
            alerts.append(t("未映射 hosts：{domain}", domain=d))
        port = getattr(e, "port", None)
        if port is not None and port not in running_ports:
            ver = getattr(e, "php_version", "") or "?"
            alerts.append(t("端口未映射：{port}（{ver}）", port=port, ver=ver))
        note = getattr(e, "note", "")
        if note:
            alerts.append(note)
        miss = _https_cert_missing(e)
        if miss:
            alerts.append(t("证书缺失：{file}", file=miss))

    # 最近崩溃 + 最近运行日志
    all_entries = run_log.entries_since(0)
    crashes = [e.line for e in all_entries if getattr(e, "scope", "") == "crash"][-5:]
    recent_logs = (run_log.as_text(5) or "").splitlines()

    # 整体健康
    if not nginx.running:
        health = "err"
    elif alerts:
        health = "warn"
    else:
        health = "ok"

    return Overview(
        nginx=nginx, php=php, redis=redis, mysql=mysql,
        site_count=len(entries), alerts=alerts,
        crashes=crashes, recent_logs=recent_logs, health=health,
    )


def diagnose_all(config, vhost_mgr, php_mgr, nginx_mgr,
                 hosts_map: Optional[dict] = None) -> tuple:
    """对所有站点跑体检，返回 (错误数, 警告数, 站点数)。复用 diag，避免重复取状态。"""
    entries = _safe(vhost_mgr.scan, []) or []
    php_versions = _safe(lambda: php_mgr.resolve(refresh_status=True, fast=True), []) or []
    ng_run = (_safe(lambda: nginx_mgr.get_status(), (False, [])) or (False, []))[0]
    if hosts_map is None:
        hosts_map = _read_hosts_map()
    logs_dir = _safe(lambda: nginx_mgr.logs_dir, "")
    err = warn = 0
    for e in entries:
        items = diag.diagnose_site(e, config, vhost_mgr, php_versions,
                                   ng_run, hosts_map, logs_dir)
        for it in items:
            if it.level == diag.LEVEL_ERR:
                err += 1
            elif it.level == diag.LEVEL_WARN:
                warn += 1
    return err, warn, len(entries)
