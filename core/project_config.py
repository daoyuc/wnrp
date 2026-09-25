# -*- coding: utf-8 -*-
"""项目级配置（F8）：项目根放 ``.phpvm.json``，一次对齐 PHP 版本 / hosts / 服务。

为什么用 JSON 而非 YAML：规划明确「不引入依赖」，标准库自带 JSON；文件名与
``config.json`` 风格一致，团队可直接放进项目仓库。

配置示例::

    {
      "php": "php82",                    // 必填：目标 PHP 版本名（config.json 的 ports key）
      "domains": ["myapp.test"],         // 可选：站点域名（用于切 fastcgi_pass / 写 hosts）
      "services": ["redis", "mysql"],    // 可选：需要启动的服务（php / redis / mysql / nginx）
      "hosts": true,                     // 可选：apply 时自动写 hosts
      "start": true,                     // 可选：apply 时自动启动服务
      "env": {"APP_ENV": "local"}        // 可选：仅展示（预留，不写入项目）
    }

``apply_project`` 只做「对齐」：把已存在站点的 fastcgi_pass 切到目标 PHP 端口、
（可选）写 hosts、（可选）启动服务；不修改项目业务代码。
"""
import json
import os

from . import hosts_manager
from .i18n import t

PROJECT_FILE = ".phpvm.json"


class ProjectConfigError(ValueError):
    """项目配置读取 / 校验失败（消息可直接展示）。"""


def find_project_config(start_dir: str) -> str | None:
    """从 start_dir 逐级向上查找 ``.phpvm.json``；找不到返回 None。"""
    d = os.path.abspath(start_dir or ".")
    while True:
        candidate = os.path.join(d, PROJECT_FILE)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def load_project(path: str) -> dict:
    """读取并校验项目配置，返回规范化 dict（不含 path）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise ProjectConfigError(t("读取项目配置失败：{err}", err=e))
    except ValueError as e:
        raise ProjectConfigError(t("项目配置不是合法 JSON：{err}", err=e))
    if not isinstance(data, dict):
        raise ProjectConfigError(t("项目配置必须是 JSON 对象。"))

    php = str(data.get("php") or "").strip()
    if not php:
        raise ProjectConfigError(t("项目配置缺少必填项 php（如 \"php\": \"php82\"）。"))

    domains = data.get("domains") or []
    if isinstance(domains, str):
        domains = [domains]
    domains = [str(d).strip() for d in domains if str(d).strip()]

    services = data.get("services") or []
    if isinstance(services, str):
        services = [services]
    services = [str(s).strip().lower() for s in services if str(s).strip()]

    env = data.get("env") or {}
    if not isinstance(env, dict):
        env = {}

    return {
        "php": php,
        "domains": domains,
        "services": services,
        "hosts": bool(data.get("hosts", False)),
        "start": bool(data.get("start", False)),
        "env": {str(k): str(v) for k, v in env.items()},
    }


def resolve_port(php_name: str, config, php_mgr=None):
    """版本名 → 端口：先查 ``config.ports``，再用 php_mgr 兜底。返回 (port, err)。"""
    port = (getattr(config, "ports", {}) or {}).get(php_name)
    if port:
        return int(port), None
    if php_mgr is not None:
        try:
            for v in php_mgr.resolve(refresh_status=False, fast=True):
                if v.name == php_name:
                    return int(v.port), None
        except Exception as e:  # noqa: BLE001
            return None, str(e)
    return None, None


def _step(tag, ok, message, *, skip=False, changed=False):
    return {"tag": tag, "ok": bool(ok), "skip": bool(skip),
            "changed": bool(changed), "message": message}


def apply_project(project: dict, config, vhost_mgr, php_mgr=None, *,
                  nginx_mgr=None, redis_mgr=None, mysql_mgr=None,
                  start: bool = False, hosts: bool = False,
                  dry_run: bool = False) -> dict:
    """按项目配置对齐环境（切 PHP / 写 hosts / 启服务），返回报告 dict。"""
    steps: list[dict] = []
    warnings: list[str] = []

    port, err = resolve_port(project["php"], config, php_mgr)
    if not port:
        msg = (err or t("未找到 PHP 版本 {name}（请检查 config.json 的 ports）",
                        name=project["php"]))
        return {"ok": False, "php": project["php"], "port": None, "steps": steps,
                "warnings": warnings, "changed": False,
                "error": msg, "message": msg}

    # ① 把匹配域名的站点切到目标 PHP 端口
    target = set(project["domains"])
    matched = []
    try:
        entries = vhost_mgr.scan(include_disabled=False)
    except Exception as e:  # noqa: BLE001
        entries = []
        warnings.append(str(e))
    if target:
        for e in entries:
            if set((e.server_name or "").split()) & target:
                matched.append(e)
    if not target:
        steps.append(_step("php", True,
                           t("项目配置未声明 domains，跳过 PHP 切换。"), skip=True))
    elif not matched:
        steps.append(_step("php", True,
                           t("未匹配到站点（域名：{doms}），跳过 PHP 切换",
                             doms="、".join(sorted(target))), skip=True))
    else:
        for e in matched:
            if e.port == port:
                steps.append(_step("php", True,
                                   t("{name} 已在使用 {php}（端口 {port}）",
                                     name=e.server_name, php=project["php"], port=port),
                                   skip=True))
            elif dry_run:
                steps.append(_step("php", True,
                                   t("将把 {name} 切到 {php}（端口 {port}）",
                                     name=e.server_name, php=project["php"], port=port),
                                   skip=True))
            else:
                res = vhost_mgr.set_site_php(e.file, e.server_name, port)
                steps.append(_step("php", res.get("ok"),
                                   res.get("message", ""),
                                   changed=bool(res.get("ok"))))

    # ② hosts（可选）
    real = [d for d in project["domains"] if d and not d.startswith("*.")]
    want_hosts = hosts or project.get("hosts")
    if want_hosts and real:
        if dry_run:
            steps.append(_step("hosts", True,
                               t("将写入 hosts：{doms}", doms="、".join(real)), skip=True))
        else:
            res = hosts_manager.ensure_entries(real)
            steps.append(_step("hosts", res.get("ok"), res.get("message", ""),
                               changed=bool(res.get("added"))))
    elif want_hosts:
        steps.append(_step("hosts", True, t("没有可写入 hosts 的域名。"), skip=True))

    # ③ 启动服务（可选）
    if start or project.get("start"):
        steps.extend(_start_services(project, php_mgr, nginx_mgr,
                                     redis_mgr, mysql_mgr, dry_run))

    ok = all(s.get("ok") for s in steps)
    return {"ok": ok, "php": project["php"], "port": port, "steps": steps,
            "warnings": warnings, "changed": any(s.get("changed") for s in steps),
            "message": (t("项目配置已应用") if ok
                        else t("项目配置应用存在问题，请查看步骤明细"))}


def _start_services(project, php_mgr, nginx_mgr, redis_mgr, mysql_mgr,
                    dry_run) -> list[dict]:
    steps: list[dict] = []
    want = set(project.get("services") or [])
    want.add("php")  # 目标 PHP 版本始终纳入

    if "php" in want:
        if php_mgr is None:
            steps.append(_step("start:php", True, t("未提供 PHP 管理器，跳过启动 PHP"), skip=True))
        else:
            v = None
            try:
                for ver in php_mgr.resolve(refresh_status=True, fast=True):
                    if ver.name == project["php"]:
                        v = ver
                        break
            except Exception as e:  # noqa: BLE001
                steps.append(_step("start:php", False, str(e)))
            if v is None:
                steps.append(_step("start:php", True,
                                   t("未找到 PHP 版本 {name}", name=project["php"]), skip=True))
            elif v.running:
                steps.append(_step("start:php", True,
                                   t("PHP {name} 已在运行", name=v.name), skip=True))
            elif dry_run:
                steps.append(_step("start:php", True, t("将启动 PHP {name}", name=v.name), skip=True))
            else:
                try:
                    steps.append(_step("start:php", True, php_mgr.start(v), changed=True))
                except Exception as e:  # noqa: BLE001
                    steps.append(_step("start:php", False, str(e)))

    if "nginx" in want:
        steps.extend(_start_nginx(nginx_mgr, dry_run))
    if "redis" in want:
        steps.extend(_start_instances("redis", redis_mgr, dry_run))
    if "mysql" in want:
        steps.extend(_start_instances("mysql", mysql_mgr, dry_run))
    return steps


def _start_nginx(nginx_mgr, dry_run) -> list[dict]:
    if nginx_mgr is None:
        return [_step("start:nginx", True, t("未提供 Nginx 管理器，跳过启动 Nginx"), skip=True)]
    try:
        running = nginx_mgr.get_status()[0]
    except Exception as e:  # noqa: BLE001
        return [_step("start:nginx", False, str(e))]
    if running:
        return [_step("start:nginx", True, t("Nginx 已在运行"), skip=True)]
    if dry_run:
        return [_step("start:nginx", True, t("将启动 Nginx"), skip=True)]
    try:
        return [_step("start:nginx", True, nginx_mgr.start(), changed=True)]
    except Exception as e:  # noqa: BLE001
        return [_step("start:nginx", False, str(e))]


def _start_instances(kind: str, mgr, dry_run) -> list[dict]:
    if mgr is None:
        return [_step(f"start:{kind}", True, t("未提供 {kind} 管理器，跳过", kind=kind), skip=True)]
    try:
        mgr.refresh_instances()
    except Exception as e:  # noqa: BLE001
        return [_step(f"start:{kind}", False, str(e))]
    insts = getattr(mgr, "instances", []) or []
    if not insts:
        return [_step(f"start:{kind}", True, t("未发现 {kind} 实例", kind=kind), skip=True)]
    out: list[dict] = []
    for inst in insts:
        name = getattr(inst, "name", "?")
        if getattr(inst, "running", False):
            out.append(_step(f"start:{kind}", True,
                             t("{kind} {name} 已在运行", kind=kind, name=name), skip=True))
        elif dry_run:
            out.append(_step(f"start:{kind}", True,
                             t("将启动 {kind} {name}", kind=kind, name=name), skip=True))
        else:
            try:
                out.append(_step(f"start:{kind}", True, mgr.start(inst), changed=True))
            except Exception as e:  # noqa: BLE001
                out.append(_step(f"start:{kind}", False, str(e)))
    return out
