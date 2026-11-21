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
    """证书缺失告警占位：细查交给 diag，这里不读盘（总览 8s 心跳不能扫全部 vhost）。"""
    return None


def _probe_runtime(php_mgr, nginx_mgr, redis_mgr, mysql_mgr) -> dict:
    """一次探测 Nginx / PHP / Redis / MySQL 运行态，供总览与资源画像共用。"""
    ng_run, ng_pids = _safe(lambda: nginx_mgr.get_status(), (False, [])) or (False, [])
    php_versions = []
    if php_mgr is not None:
        php_versions = _safe(
            lambda: php_mgr.resolve(refresh_status=True, fast=True), []) or []
    redis_insts: list = []
    if redis_mgr is not None:
        _safe(redis_mgr.refresh_instances)
        _safe(redis_mgr.get_status_all)
        redis_insts = list(getattr(redis_mgr, "instances", []) or [])
    mysql_insts: list = []
    if mysql_mgr is not None:
        _safe(mysql_mgr.refresh_instances)
        mysql_insts = list(getattr(mysql_mgr, "instances", []) or [])
    return {
        "nginx_status": (bool(ng_run), list(ng_pids or [])),
        "php_versions": php_versions,
        "redis_instances": redis_insts,
        "mysql_instances": mysql_insts,
    }


def _overview_from_runtime(runtime: dict, vhost_mgr,
                           hosts_map: Optional[dict] = None) -> Overview:
    """由已探测的运行时状态 + vhost/hosts/日志组装 Overview。"""
    ng_run, ng_pids = runtime["nginx_status"]
    nginx = ServiceView(
        "Nginx", "nginx", bool(ng_run),
        t("PID {p}", p=",".join(map(str, ng_pids))) if ng_pids else "")

    php_versions = runtime.get("php_versions") or []
    php = [ServiceView(v.name, "php", bool(v.running),
                       f"{getattr(v, 'display', '')} :{getattr(v, 'port', '?')}")
           for v in php_versions]
    running_ports = {getattr(v, "port", None) for v in php_versions if v.running}

    redis = [
        ServiceView(getattr(inst, "name", "?"), "redis",
                    bool(getattr(inst, "running", False)),
                    f":{getattr(inst, 'port', '?')}")
        for inst in (runtime.get("redis_instances") or [])
    ]
    mysql = [
        ServiceView(getattr(inst, "name", "?"), "mysql",
                    bool(getattr(inst, "running", False)),
                    f":{getattr(inst, 'port', '?')}")
        for inst in (runtime.get("mysql_instances") or [])
    ]

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

    all_entries = run_log.entries_since(0)
    crashes = [e.line for e in all_entries if getattr(e, "scope", "") == "crash"][-5:]
    recent_logs = (run_log.as_text(5) or "").splitlines()

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


def build_overview(config, php_mgr, nginx_mgr, redis_mgr, mysql_mgr,
                   vhost_mgr, hosts_map: Optional[dict] = None) -> Overview:
    """聚合总览数据（只读）。任一 manager 缺失（模块停用）时跳过对应分组。"""
    runtime = _probe_runtime(php_mgr, nginx_mgr, redis_mgr, mysql_mgr)
    return _overview_from_runtime(runtime, vhost_mgr, hosts_map=hosts_map)


def build_dashboard(config, php_mgr, nginx_mgr, redis_mgr, mysql_mgr,
                    vhost_mgr, hosts_map: Optional[dict] = None
                    ) -> tuple[Overview, Optional[dict]]:
    """总览 + 资源画像一次探测：状态 resolve / get_status 只跑一轮。

    返回 ``(Overview, metrics|None)``；metrics 组装失败时仍回总览，metrics 为 ``None``。
    """
    from core import resource_monitor as rm

    runtime = _probe_runtime(php_mgr, nginx_mgr, redis_mgr, mysql_mgr)
    ov = _overview_from_runtime(runtime, vhost_mgr, hosts_map=hosts_map)
    try:
        metrics = rm.metrics_from_runtime(
            nginx_status=runtime["nginx_status"],
            php_versions=runtime["php_versions"],
            redis_instances=runtime["redis_instances"],
            mysql_instances=runtime["mysql_instances"],
        )
    except Exception:  # noqa: BLE001
        metrics = None
    return ov, metrics


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
