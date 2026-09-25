#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phpvm 命令行入口（无 GUI，供脚本 / AI 工具非交互调用）。

设计约定（面向 AI 调用）
------------------------
1. **无界面依赖**：只 import ``core/*``，绝不 import ``ui/*``（含 tkinter），
   因此可在 SSH / CI / 无桌面环境的机器上运行。
2. **完全非交互**：任何命令都不弹窗、不等待输入。需要提权的操作（写 hosts）
   只有显式 ``--hosts`` 才会触发，且会弹出系统授权框（macOS osascript / Win UAC）。
3. **机器可读**：加 ``--json`` 时 stdout **只**输出一个 JSON 文档：
   ``{"ok": bool, "command": "<group>.<action>", "data": {...}, "warnings": [...]}``
   字段名与枚举值一律英文（稳定契约），说明性文本按当前语言输出。
4. **退出码**：0 成功 / 1 业务失败（``ok=false``）/ 2 参数用法错误 / 130 被中断。
5. **安全默认**：写操作支持 ``--dry-run``（只报告将做什么）；删除类需 ``--yes``，
   Redis 高危命令需 ``--force``；所有改动前自动备份 ``.bak``。
6. **自描述**：``python3 cli.py schema`` 输出全部命令 / 参数 / 示例的机器可读契约，
   AI 可先读 schema 再决定调用（推荐 ``schema --json``）。

常用示例
--------
    python3 cli.py env --json                  # 环境与全部服务快照
    python3 cli.py php list --json             # PHP 版本与运行状态
    python3 cli.py php start php82
    python3 cli.py nginx test                  # nginx -t
    python3 cli.py site create --domain app.test --root ~/wnrp/www/app \\
        --template laravel --php php82 --hosts
    python3 cli.py schema --json               # 命令自描述契约
"""
import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field

# 保证无论从哪个目录启动都能正确导入 core 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import (  # noqa: E402
    adminer,
    app_paths,
    backup_bundle,
    cert_manager,
    diag,
    file_backup,
    hosts_manager,
    log_sources,
    modules,
    overview,
    project_config,
    site_service,
    site_templates,
    updater,
    version,
    xdebug,
)
from core import config as config_mod  # noqa: E402
from core.config import Config, WNRP_ROOT  # noqa: E402
from core.i18n import t  # noqa: E402  (统一文案入口；此前缺失导致 t() 调用崩溃)
from core.health_monitor import HealthMonitor  # noqa: E402
from core.mysql_manager import MysqlManager  # noqa: E402
from core.nginx_manager import NginxManager  # noqa: E402
from core.php_extension import (  # noqa: E402
    apply_extensions,
    read_enabled_exts,
    scan_ext_dir,
)
from core.php_manager import PhpManager  # noqa: E402
from core.redis_manager import RedisManager  # noqa: E402
from core.service_group import PHP_SCOPES, ServiceGroup, scope_label  # noqa: E402
from core.sqlite_manager import SqliteManager  # noqa: E402
from core.vhost_manager import (  # noqa: E402
    VhostManager,
    nginx_path,
    safe_conf_base,
    valid_domain,
)

# --------------------------------------------------------------------------- #
# 结果封装
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    """命令结果：统一的 JSON / 文本输出与退出码来源。"""

    ok: bool = True
    data: dict = field(default_factory=dict)
    lines: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    code: int = 0

    def say(self, text="") -> "Result":
        self.lines.append(str(text))
        return self

    def note(self, text: str) -> "Result":
        self.notes.append(str(text))
        return self

    def fail(self, message: str, **data) -> "Result":
        self.ok = False
        if data:
            self.data.update(data)
        self.data["error"] = message
        self.say("失败：" + message)
        return self


def _flag(args, name: str, default=False):
    """读取全局开关（父解析器用 SUPPRESS 默认值，故需 getattr）。"""
    return getattr(args, name, default)


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def _php_manager(cfg, refresh: bool = True, fast: bool = True) -> PhpManager:
    pm = PhpManager(cfg)
    pm.scan_versions()
    if refresh:
        pm.resolve(refresh_status=True, fast=fast)
    return pm


def _php_dict(v) -> dict:
    return {
        "name": v.name,
        "display": v.display,
        "port": v.port,
        "running": bool(v.running),
        "pid": v.pid,
        "dir": v.dir,
        "cgi": v.cgi,
        "ini": v.ini,
    }


def _pick_php(pm: PhpManager, name: str, r: Result):
    """按版本名（php82 / 8.2 / 82）挑选版本；不唯一或未找到时写入失败信息并返回 None。"""
    key = (name or "").strip().lower()
    if not key:
        r.fail("请指定 PHP 版本名", available=[v.name for v in pm.versions])
        return None
    exact = [v for v in pm.versions if v.name.lower() == key]
    if len(exact) == 1:
        return exact[0]
    short = key.lstrip("php").replace(".", "")
    loose = [v for v in pm.versions
             if key in v.name.lower()
             or v.display.lower().startswith(key.lstrip("php"))
             or (short and v.name.lower().lstrip("php") == short)]
    if len(loose) == 1:
        return loose[0]
    if not loose:
        r.fail("未找到 PHP 版本：" + name, available=[v.name for v in pm.versions])
        return None
    r.fail("PHP 版本名不唯一：" + name, matched=[v.name for v in loose])
    return None


def _nginx() -> NginxManager:
    return NginxManager()


def _nginx_dict(ng: NginxManager, with_version: bool = True) -> dict:
    running, pids = ng.get_status()
    return {
        "running": running,
        "pids": pids,
        "exe": ng.exe,
        "exists": os.path.exists(ng.exe),
        "prefix": ng.prefix,
        "mode": ng.mode,
        "conf_dir": ng.conf_dir,
        "main_conf": ng.main_conf,
        "vhost_dir": ng.vhost_dir,
        "logs_dir": ng.logs_dir,
        "version": ng.get_version() if (with_version and os.path.exists(ng.exe)) else "",
    }


def _redis_dict(inst) -> dict:
    mgr = RedisManager()
    running, pids = mgr.get_status(inst)
    return {
        "name": inst.name,
        "dir": inst.dir,
        "port": inst.port,
        "running": running,
        "pids": pids,
        "conf": inst.conf,
        "cli": inst.cli,
        "version": mgr.get_version(inst),
    }


def _mysql_dict(inst) -> dict:
    mgr = MysqlManager()
    running, pids = mgr.get_status(inst)
    return {
        "name": inst.name,
        "dir": inst.dir,
        "port": inst.port,
        "running": running,
        "pids": pids,
        "conf": inst.conf,
        "datadir": inst.datadir,
        "version": mgr.detect_version(inst),
        "mode": inst.mode,
        "service": inst.service,
        "service_state": inst.service_state,
        "start_type": inst.start_type,
    }


def _pick(instances, name: str, r: Result, kind: str):
    if not instances:
        r.fail(f"未发现任何 {kind} 实例")
        return None
    if not name:
        return instances[0]
    key = name.strip().lower()
    exact = [i for i in instances if i.name.lower() == key]
    if len(exact) == 1:
        return exact[0]
    loose = [i for i in instances if key in i.name.lower()]
    if len(loose) == 1:
        return loose[0]
    if not loose:
        r.fail(f"未找到 {kind} 实例：" + name,
               available=[i.name for i in instances])
        return None
    r.fail(f"{kind} 实例名不唯一：" + name, matched=[i.name for i in loose])
    return None


def _site_dict(e) -> dict:
    return {
        "server_name": e.server_name,
        "file": e.file,
        "file_rel": e.file_rel,
        "root": e.root,
        "port": e.port,
        "php_version": e.php_version,
        "disabled": bool(e.disabled),
        "note": e.note,
        "url": VhostManager.site_url(e),
    }


def _pick_site(vm: VhostManager, target: str, r: Result):
    """按配置文件名 / 路径 / 域名定位站点。"""
    entries = vm.scan(include_disabled=True)
    key = (target or "").strip()
    if not key:
        r.fail("请指定站点（配置文件名 / 路径 / 域名）")
        return None
    if os.path.isabs(key) or key.endswith(".conf") or key.endswith(".disabled"):
        for e in entries:
            if os.path.normcase(os.path.abspath(e.file)) == os.path.normcase(os.path.abspath(key)):
                return e
        for e in entries:
            if os.path.basename(e.file) == os.path.basename(key):
                return e
        r.fail("未找到该配置文件：" + key, vhost_dir=vm.vhost_dir)
        return None
    hits = [e for e in entries if key.lower() in e.server_name.lower().split()]
    if not hits:
        hits = [e for e in entries if key.lower() in e.server_name.lower()]
    if not hits:
        hits = [e for e in entries
                if os.path.splitext(os.path.basename(e.file))[0].lower() == key.lower()]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        r.fail("未找到匹配站点：" + key)
        return None
    r.fail("匹配到多个站点，请用配置文件名精确指定", matched=[e.file for e in hits])
    return None


def _tail(path: str, lines: int) -> str:
    """读取文件尾部 N 行（最多回溯 256KB，避免大日志卡死）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            chunk = min(size, 256 * 1024)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", "replace")
    except OSError as e:
        return f"（读取失败：{e}）"
    rows = data.splitlines()
    return "\n".join(rows[-lines:]) if lines > 0 else "\n".join(rows)


def _test_output_ok(output: str) -> bool:
    low = (output or "").lower()
    return "successful" in low and "failed" not in low


def _service_group(cfg: Config) -> ServiceGroup:
    """按模块开关实例化服务编排（与 main.py 的开机自启逻辑一致）。"""
    redis_mgr = RedisManager() if modules.is_enabled("redis", cfg) else None
    mysql_mgr = MysqlManager() if modules.is_enabled("mysql", cfg) else None
    return ServiceGroup(PhpManager(cfg), NginxManager(), redis_mgr, mysql_mgr)


# --------------------------------------------------------------------------- #
# version / env
# --------------------------------------------------------------------------- #
def cmd_version(a, r: Result) -> Result:
    data = {
        "app_name": version.APP_NAME,
        "app_version": version.version(),
        "app_id": version.APP_ID,
        "platform": updater.platform_key(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "env_root": WNRP_ROOT,
        "package_dir": app_paths.package_dir(),
        "data_dir": app_paths.data_dir(),
        "config_path": config_mod.default_config_path(),
        "installed_bundle": app_paths.is_installed_bundle(),
        "auto_update_supported": updater.supports_auto_update(),
        "updates_dir": updater.updates_dir(),
        "repo": version.GITHUB_REPO,
        "release_page": version.RELEASES_PAGE.format(repo=version.GITHUB_REPO),
    }
    r.data = data
    r.say(f"phpvm {data['app_version']}（{data['platform']}）")
    r.say(f"环境根：{data['env_root']}")
    r.say(f"数据目录：{data['data_dir']}")
    r.say(f"配置：{data['config_path']}")
    return r


def cmd_env(a, r: Result) -> Result:
    cfg = Config()
    ng = _nginx()
    pm = _php_manager(cfg, refresh=True, fast=True)
    data = {
        "app_version": version.version(),
        "platform": updater.platform_key(),
        "env_root": WNRP_ROOT,
        "data_dir": app_paths.data_dir(),
        "config_path": config_mod.default_config_path(),
        "modules": {m["key"]: modules.is_enabled(m["key"], cfg) for m in modules.MODULES},
        "nginx": _nginx_dict(ng),
        "php": [_php_dict(v) for v in pm.versions],
    }
    if modules.is_enabled("redis", cfg):
        data["redis"] = [_redis_dict(i) for i in RedisManager().refresh_instances()]
    if modules.is_enabled("mysql", cfg):
        data["mysql"] = [_mysql_dict(i) for i in MysqlManager().refresh_instances()]
    r.data = data
    r.say(f"phpvm {data['app_version']} · nginx {'运行中' if data['nginx']['running'] else '已停止'}"
          f"（{data['nginx']['version'] or '未知版本'}）")
    for v in data["php"]:
        r.say(f"  {'●' if v['running'] else '○'} {v['name']} · PHP {v['display'] or '?'} "
              f"· 端口 {v['port']}" + (f" · PID {v['pid']}" if v["pid"] else ""))
    return r


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def cmd_config_list(a, r: Result) -> Result:
    cfg = Config()
    data = {
        "config_path": cfg.config_path,
        "ports": dict(cfg.ports),
        "settings": dict(cfg.settings),
        "defaults": {"ports": dict(config_mod.DEFAULT_PORTS),
                     "settings": dict(config_mod.DEFAULT_SETTINGS)},
    }
    r.data = data
    r.say("配置文件：" + cfg.config_path)
    r.say("端口映射：")
    for k in sorted(data["ports"]):
        r.say(f"  {k} = {data['ports'][k]}")
    r.say("设置：")
    for k in sorted(data["settings"]):
        r.say(f"  {k} = {data['settings'][k]!r}")
    return r


def _set_path(obj: dict, path: str, value):
    cur = obj
    parts = path.split(".")
    for p in parts[:-1]:
        if p not in cur or not isinstance(cur[p], dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def _get_path(obj: dict, path: str, missing=KeyError):
    cur = obj
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            if missing is KeyError:
                raise KeyError(path)
            return missing
        cur = cur[p]
    return cur


def _coerce(raw: str):
    text = raw.strip()
    low = text.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "none", "~"):
        return None
    if text.startswith(("[", "{")):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    try:
        return int(text)
    except ValueError:
        return text


def cmd_config_get(a, r: Result) -> Result:
    cfg = Config()
    if a.key in ("ports", "settings", ""):
        value = cfg.ports if a.key == "ports" else (
            cfg.settings if a.key == "settings" else {"ports": cfg.ports, "settings": cfg.settings})
        r.data = {"config_path": cfg.config_path, "key": a.key or "*", "value": value}
        r.say(json.dumps(value, ensure_ascii=False, indent=2))
        return r
    try:
        value = _get_path({"ports": cfg.ports, "settings": cfg.settings}, a.key)
    except KeyError:
        return r.fail("未知配置键：" + a.key,
                      available_ports=sorted(cfg.ports),
                      available_settings=sorted(cfg.settings))
    r.data = {"config_path": cfg.config_path, "key": a.key, "value": value}
    r.say(f"{a.key} = {value!r}")
    return r


def cmd_config_set(a, r: Result) -> Result:
    cfg = Config()
    value = _coerce(a.value)
    parts = a.key.split(".")
    if parts[0] not in ("ports", "settings") or len(parts) < 2:
        return r.fail("只允许设置 ports.<版本名> 或 settings.<键名>：" + a.key)
    if parts[0] == "ports":
        try:
            port = int(value)
        except (TypeError, ValueError):
            return r.fail("端口必须是整数：" + a.value)
        err = Config.validate_port(port)
        if err:
            return r.fail(err)
        name = parts[-1]
        other = next((k for k, v in cfg.ports.items() if v == port and k != name), None)
        if other:
            return r.fail(f"端口 {port} 已被 {other} 占用", conflict=other)
        if a.dry_run:
            r.data = {"dry_run": True, "key": a.key, "value": port,
                      "old": cfg.get_port(name)}
            r.say(f"[dry-run] 将设置 ports.{name} = {port}（原 {cfg.get_port(name)}）")
            return r
        old = cfg.get_port(name)
        cfg.set_port(name, port)
        r.data = {"key": a.key, "value": port, "old": old}
        r.say(f"已设置 ports.{name} = {port}（原 {old}）")
        vm = VhostManager(cfg)
        hits = vm.entries_with_port(old) if old != port else []
        if hits:
            r.note(f"仍有 {len(hits)} 个站点引用旧端口 {old}，"
                   f"请执行：site sync-port --old {old} --new {port} --reload")
            r.data["vhosts_using_old_port"] = [e.file for e in hits]
        return r
    key = parts[-1]
    if key not in config_mod.DEFAULT_SETTINGS:
        return r.fail("未知设置键：" + key, available=sorted(config_mod.DEFAULT_SETTINGS))
    if a.dry_run:
        r.data = {"dry_run": True, "key": a.key, "value": value,
                  "old": cfg.get_setting(key)}
        r.say(f"[dry-run] 将设置 settings.{key} = {value!r}")
        return r
    old = cfg.get_setting(key)
    cfg.set_setting(key, value)
    r.data = {"key": a.key, "value": value, "old": old}
    r.say(f"已设置 settings.{key} = {value!r}（原 {old!r}）")
    if key in ("lang", "disabled_modules"):
        r.note("该设置需重启 phpvm（GUI）后生效")
    return r


# --------------------------------------------------------------------------- #
# php
# --------------------------------------------------------------------------- #
def cmd_php_list(a, r: Result) -> Result:
    cfg = Config()
    pm = _php_manager(cfg, refresh=not a.no_status, fast=not a.precise)
    r.data = {"count": len(pm.versions), "versions": [_php_dict(v) for v in pm.versions]}
    if not pm.versions:
        r.note(f"未扫描到 PHP 版本（环境根：{WNRP_ROOT}）")
    for v in pm.versions:
        r.say(f"{'●' if v.running else '○'} {v.name} · PHP {v.display or '?'} · "
              f"端口 {v.port}" + (f" · PID {v.pid}" if v.pid else ""))
        r.say(f"    目录 {v.dir}")
        r.say(f"    ini  {v.ini or '（未使用独立配置）'}")
    return r


def _php_target(a, r: Result):
    cfg = Config()
    pm = _php_manager(cfg, refresh=True, fast=True)
    v = _pick_php(pm, a.name, r)
    return pm, v


def cmd_php_status(a, r: Result) -> Result:
    pm, v = _php_target(a, r)
    if v is None:
        return r
    r.data = _php_dict(v)
    r.say(f"{'●' if v.running else '○'} {v.name} · PHP {v.display or '?'} · 端口 {v.port}"
          + (f" · PID {v.pid}" if v.pid else ""))
    return r


def _php_batch(a, r: Result, action: str) -> Result:
    cfg = Config()
    pm = _php_manager(cfg, refresh=True, fast=True)
    if (a.name or "").lower() == "all":
        targets = list(pm.versions)
    else:
        v = _pick_php(pm, a.name, r)
        if v is None:
            return r
        targets = [v]
    if not targets:
        return r.fail("未扫描到任何 PHP 版本，无法操作")
    results = []
    ok_all = True
    for v in targets:
        try:
            msg = getattr(pm, action)(v)
            results.append({"name": v.name, "ok": True, "message": msg})
        except Exception as e:  # noqa: BLE001 —— 单版本失败不影响其它版本
            ok_all = False
            results.append({"name": v.name, "ok": False, "message": str(e)})
    r.ok = ok_all
    r.data = {"action": action, "results": results}
    for item in results:
        r.say(f"{'✓' if item['ok'] else '✗'} {item['name']}：{item['message']}")
    return r


def cmd_php_start(a, r: Result) -> Result:
    return _php_batch(a, r, "start")


def cmd_php_stop(a, r: Result) -> Result:
    return _php_batch(a, r, "stop")


def cmd_php_restart(a, r: Result) -> Result:
    return _php_batch(a, r, "restart")


def cmd_php_port(a, r: Result) -> Result:
    cfg = Config()
    pm = _php_manager(cfg, refresh=False)
    v = _pick_php(pm, a.name, r)
    if v is None:
        return r
    old = cfg.get_port(v.name)
    if a.set is None:
        r.data = {"name": v.name, "port": old, "running": bool(v.running)}
        r.say(f"{v.name} 端口 = {old}")
        return r
    try:
        new = int(a.set)
    except (TypeError, ValueError):
        return r.fail("新端口必须是整数：" + str(a.set))
    err = Config.validate_port(new)
    if err:
        return r.fail(err)
    conflict = next((k for k, p in cfg.ports.items() if p == new and k != v.name), None)
    if conflict:
        return r.fail(f"端口 {new} 已被 {conflict} 占用", conflict=conflict)
    vm = VhostManager(cfg)
    hits = vm.entries_with_port(old) if old != new else []
    if a.dry_run:
        r.data = {"dry_run": True, "name": v.name, "old_port": old, "new_port": new,
                  "vhosts_to_sync": [e.file for e in hits]}
        r.say(f"[dry-run] 将把 {v.name} 端口 {old} → {new}"
              + (f"，需同步 {len(hits)} 个站点配置" if hits else ""))
        return r
    cfg.set_port(v.name, new)
    v.port = new
    r.data = {"name": v.name, "old_port": old, "new_port": new,
              "vhosts_using_old_port": [e.file for e in hits]}
    r.say(f"已把 {v.name} 端口 {old} → {new}")
    if hits:
        r.note(f"仍有 {len(hits)} 个站点引用旧端口 {old}；"
               f"执行 site sync-port --old {old} --new {new} --reload 一键同步")
    if v.running:
        r.note("端口已改但进程仍在旧端口运行，需重启该版本：php restart " + v.name)
    return r


def cmd_php_ini(a, r: Result) -> Result:
    cfg = Config()
    pm = _php_manager(cfg, refresh=False)
    v = _pick_php(pm, a.name, r)
    if v is None:
        return r
    if not v.ini:
        return r.fail(f"{v.name} 未使用独立配置文件（将读取 PHP 编译默认配置）")
    keys = pm.read_key_ini(v)
    if a.key:
        if a.key not in keys:
            return r.fail(f"配置中未找到键：{a.key}", available=sorted(keys))
        r.data = {"name": v.name, "ini": v.ini, "key": a.key, "value": keys[a.key]}
        r.say(f"{a.key} = {keys[a.key]}")
        return r
    data = {"name": v.name, "ini": v.ini, "keys": keys}
    try:
        data["extensions"] = sorted(read_enabled_exts(v.ini))
    except OSError:
        data["extensions"] = []
    if a.all:
        data["raw"] = pm.read_ini(v)
    r.data = data
    r.say(f"{v.name} 配置：{v.ini}")
    for k in sorted(keys):
        r.say(f"  {k} = {keys[k]}")
    if data["extensions"]:
        r.say("已启用扩展：" + "、".join(data["extensions"]))
    return r


def cmd_php_check(a, r: Result) -> Result:
    cfg = Config()
    pm = _php_manager(cfg, refresh=False)
    v = _pick_php(pm, a.name, r)
    if v is None:
        return r
    res = HealthMonitor().self_check(v)
    r.data = {"name": v.name, "ok": res["ok"], "ini": res["ini"],
              "version": res["version"], "checks": res["checks"]}
    r.ok = bool(res["ok"])
    if not res["ok"]:
        r.data["error"] = "自检未通过"
    r.say(f"{v.name}（PHP {res['version']}）自检：{'通过' if res['ok'] else '存在问题'}")
    for c in res["checks"]:
        r.say(f"  {'✓' if c['ok'] else '✗'} {c['name']}：{c['detail']}")
    return r


def cmd_php_ext_list(a, r: Result) -> Result:
    cfg = Config()
    pm = _php_manager(cfg, refresh=False)
    v = _pick_php(pm, a.name, r)
    if v is None:
        return r
    infos = scan_ext_dir(v.dir)
    data = {"name": v.name, "ext_dir": os.path.join(v.dir, "ext"),
            "count": len(infos),
            "extensions": [{"key": i.key, "file": i.dll, "enabled": bool(i.enabled),
                            "desc": i.desc} for i in infos]}
    r.data = data
    if not infos:
        r.note(f"未在 {data['ext_dir']} 发现扩展文件"
               "（Homebrew / Linux 安装的 PHP 扩展通常编译进主程序或由包管理器提供）")
    for i in infos:
        r.say(f"  {'●' if i.enabled else '○'} {i.key}" + (f"  {i.desc}" if i.desc else ""))
    return r


def cmd_php_ext_set(a, r: Result) -> Result:
    cfg = Config()
    pm = _php_manager(cfg, refresh=False)
    v = _pick_php(pm, a.name, r)
    if v is None:
        return r
    if not v.ini:
        return r.fail(f"{v.name} 未使用独立配置文件，无法修改扩展")
    enable = {s.strip() for s in (a.enable or "").split(",") if s.strip()}
    disable = {s.strip() for s in (a.disable or "").split(",") if s.strip()}
    if not enable and not disable:
        return r.fail("请用 --enable / --disable 指定扩展（逗号分隔）")
    if enable & disable:
        return r.fail("同一扩展不能同时启用与禁用：" + "、".join(sorted(enable & disable)))
    if a.dry_run:
        r.data = {"dry_run": True, "ini": v.ini, "enable": sorted(enable),
                  "disable": sorted(disable)}
        r.say(f"[dry-run] 将在 {v.ini} 启用 {sorted(enable)} / 禁用 {sorted(disable)}")
        return r
    try:
        count, msg = apply_extensions(v.ini, enable, disable)
    except OSError as e:
        return r.fail(f"写入 ini 失败：{e}", ini=v.ini)
    r.data = {"ini": v.ini, "changed": count, "message": msg,
              "enable": sorted(enable), "disable": sorted(disable)}
    r.say(f"已修改 {v.ini}（{count} 处）：{msg}")
    r.note("需重启该 PHP 版本后生效：php restart " + v.name)
    return r


# --------------------------------------------------------------------------- #
# Xdebug 调试开关
# --------------------------------------------------------------------------- #
def cmd_php_xdebug_status(a, r: Result) -> Result:
    cfg = Config()
    pm = _php_manager(cfg, refresh=False)
    v = _pick_php(pm, a.name, r)
    if v is None:
        return r
    info = xdebug.status(v)
    r.data = {"name": v.name, **info}
    r.say(f"{v.name} Xdebug：{'已开启' if info['enabled'] else '未开启'}"
          f"（扩展{'已安装' if info['installed'] else '未安装'}）")
    r.say(f"  配置文件：{info['ini'] or '（无）'}")
    if info["enabled"]:
        r.say(f"  端口 {info['port']} · mode={info['mode'] or '-'} · "
              f"host={info['client_host'] or '-'} · idekey={info['idekey'] or '-'}")
    return r


def cmd_php_xdebug_enable(a, r: Result) -> Result:
    cfg = Config()
    pm = _php_manager(cfg, refresh=False)
    v = _pick_php(pm, a.name, r)
    if v is None:
        return r
    port = int(a.port or xdebug.DEFAULT_PORT)
    if not 1 <= port <= 65535:
        return r.fail("调试端口必须在 1-65535 之间", port=port)
    changes = {
        "zend_extension": xdebug._ext_value(v),
        "xdebug.mode": "debug",
        "xdebug.start_with_request": "yes",
        "xdebug.client_port": str(port),
        "xdebug.client_host": xdebug.DEFAULT_HOST,
        "xdebug.idekey": xdebug.DEFAULT_IDEKEY,
    }
    if a.dry_run:
        r.data = {"dry_run": True, "name": v.name, "ini": v.ini, "port": port,
                  "changes": changes}
        r.say(f"[dry-run] 将在 {v.ini} 写入 Xdebug 加载行与调试指令：")
        for k, val in changes.items():
            r.say(f"  {k} = {val}")
        return r
    ok, msg, backup = xdebug.enable(v, port=port)
    if not ok:
        return r.fail(msg, name=v.name, ini=v.ini)
    r.data = {"name": v.name, "ini": v.ini, "port": port, "backup": backup,
              "message": msg}
    r.say(msg)
    r.note("需重启该 PHP 版本后生效：php restart " + v.name)
    return r


def cmd_php_xdebug_disable(a, r: Result) -> Result:
    cfg = Config()
    pm = _php_manager(cfg, refresh=False)
    v = _pick_php(pm, a.name, r)
    if v is None:
        return r
    if a.dry_run:
        r.data = {"dry_run": True, "name": v.name, "ini": v.ini,
                  "action": "注释 Xdebug 加载行"}
        r.say(f"[dry-run] 将注释 {v.ini} 中的 Xdebug 加载行")
        return r
    ok, msg, backup = xdebug.disable(v)
    if not ok:
        return r.fail(msg, name=v.name, ini=v.ini)
    r.data = {"name": v.name, "ini": v.ini, "backup": backup, "message": msg}
    r.say(msg)
    r.note("需重启该 PHP 版本后生效：php restart " + v.name)
    return r


# --------------------------------------------------------------------------- #
# nginx
# --------------------------------------------------------------------------- #
def cmd_nginx_status(a, r: Result) -> Result:
    ng = _nginx()
    data = _nginx_dict(ng)
    r.data = data
    r.say(f"nginx {'运行中' if data['running'] else '已停止'}"
          f"（{data['version'] or '未知版本'}）")
    r.say(f"  可执行文件：{data['exe']}" + ("" if data["exists"] else "（不存在）"))
    r.say(f"  前缀：{data['prefix']}（模式 {data['mode']}）")
    r.say(f"  主配置：{data['main_conf']}")
    r.say(f"  站点目录：{data['vhost_dir']}")
    r.say(f"  日志目录：{data['logs_dir']}")
    if data["pids"]:
        r.say("  PID：" + "、".join(map(str, data["pids"])))
    return r


def _nginx_action(a, r: Result, action: str) -> Result:
    ng = _nginx()
    if not os.path.exists(ng.exe):
        return r.fail("未找到 nginx 可执行文件：" + ng.exe, exe=ng.exe)
    try:
        msg = getattr(ng, action)()
    except Exception as e:  # noqa: BLE001
        return r.fail(f"nginx {action} 失败：{e}")
    running, pids = ng.get_status()
    r.data = {"action": action, "message": msg, "running": running, "pids": pids}
    r.say(msg or f"nginx {action} 完成")
    r.say(f"当前状态：{'运行中' if running else '已停止'}"
          + (f"（PID {pids}）" if pids else ""))
    return r


def cmd_nginx_start(a, r: Result) -> Result:
    return _nginx_action(a, r, "start")


def cmd_nginx_stop(a, r: Result) -> Result:
    return _nginx_action(a, r, "stop")


def cmd_nginx_reload(a, r: Result) -> Result:
    return _nginx_action(a, r, "reload")


def cmd_nginx_test(a, r: Result) -> Result:
    ng = _nginx()
    if not os.path.exists(ng.exe):
        return r.fail("未找到 nginx 可执行文件：" + ng.exe, exe=ng.exe)
    out = ng.test_config()
    ok = _test_output_ok(out)
    r.ok = ok
    r.data = {"ok": ok, "output": out, "main_conf": ng.main_conf}
    if not ok:
        r.data["error"] = "nginx -t 未通过"
    r.say(out.strip() or "（无输出）")
    return r


def cmd_nginx_logs(a, r: Result) -> Result:
    ng = _nginx()
    logs_dir = ng.logs_dir
    if not os.path.isdir(logs_dir):
        return r.fail("日志目录不存在：" + logs_dir)
    files = sorted(f for f in os.listdir(logs_dir) if f.endswith(".log")
                   or ".log." in f)
    if a.list:
        r.data = {"logs_dir": logs_dir, "files": files}
        r.say("日志目录：" + logs_dir)
        for f in files:
            r.say("  " + f)
        return r
    name = a.file
    if not name:
        for cand in ("error.log", "access.log"):
            if cand in files:
                name = cand
                break
        name = name or (files[0] if files else "")
    if not name:
        return r.fail("日志目录下没有 .log 文件：" + logs_dir)
    path = name if os.path.isabs(name) else os.path.join(logs_dir, name)
    if not os.path.isfile(path):
        return r.fail("日志文件不存在：" + path)
    text = _tail(path, a.lines)
    r.data = {"logs_dir": logs_dir, "file": path, "lines": a.lines, "content": text}
    r.say(text)
    return r


# --------------------------------------------------------------------------- #
# logs（站点与应用日志聚合，只读）
# --------------------------------------------------------------------------- #
def cmd_logs_sources(a, r: Result) -> Result:
    cfg = Config()
    groups = log_sources.collect(cfg)
    r.data = {
        "groups": {
            g: [{"label": s.label, "path": s.path, "kind": s.kind} for s in srcs]
            for g, srcs in groups.items()
        }
    }
    if not groups:
        r.say(t("未找到任何日志源"))
        return r
    r.say(t("可查看的日志源："))
    for g, srcs in groups.items():
        r.say(f"[{g}]")
        for s in srcs:
            r.say(f"  {s.label}  ({s.kind})  ->  {s.path}")
    return r


def cmd_logs_tail(a, r: Result) -> Result:
    path = a.path
    if not os.path.isfile(path):
        return r.fail(t("日志文件不存在：{path}", path=path))
    text = _tail(path, a.lines)
    r.data = {"file": path, "lines": a.lines, "content": text}
    r.say(text)
    return r


# --------------------------------------------------------------------------- #
# overview（首页总览仪表盘，只读快照）
# --------------------------------------------------------------------------- #
def cmd_overview_summary(a, r: Result) -> Result:
    cfg = Config()
    nginx = NginxManager()
    php = PhpManager(cfg)
    redis = RedisManager() if modules.is_enabled("redis", cfg) else None
    mysql = MysqlManager() if modules.is_enabled("mysql", cfg) else None
    vhost = VhostManager(cfg)
    ov = overview.build_overview(cfg, php, nginx, redis, mysql, vhost)
    r.data = ov.to_dict()
    r.say(t("总览摘要："))
    r.say("  Nginx: " + (t("运行") if ov.nginx.running else t("停止")))
    for s in ov.php:
        r.say("  PHP " + s.name + ": " + (t("运行") if s.running else t("停止")) +
              (f" ({s.detail})" if s.detail else ""))
    for s in ov.redis:
        r.say("  Redis " + s.name + ": " + (t("运行") if s.running else t("停止")))
    for s in ov.mysql:
        r.say("  MySQL " + s.name + ": " + (t("运行") if s.running else t("停止")))
    r.say(t("共 {n} 个站点", n=ov.site_count))
    r.say(t("告警 {n} 项", n=len(ov.alerts)))
    for al in ov.alerts:
        r.say("  - " + al)
    r.say(t("健康：{h}", h=ov.health))
    return r


# --------------------------------------------------------------------------- #
# backup（环境备份与迁移，F6）
# --------------------------------------------------------------------------- #
def cmd_backup_export(a, r: Result) -> Result:
    cfg = Config()
    res = backup_bundle.export_bundle(a.zip, cfg)
    r.ok = bool(res.get("ok"))
    r.data = res
    if not res.get("ok"):
        r.data["error"] = res.get("message", "")
    r.say(res.get("message", ""))
    if res.get("ok"):
        r.say(t("清单：{counts}", counts=json.dumps(res.get("counts", {}), ensure_ascii=False)))
    return r


def cmd_backup_restore(a, r: Result) -> Result:
    if not a.yes and not a.dry_run:
        r.fail(t("恢复会覆盖当前配置，需加 --yes 确认（可先 --dry-run 预览）"))
        return r
    cfg = Config()
    res = backup_bundle.restore_bundle(a.zip, cfg, dry_run=a.dry_run)
    r.ok = bool(res.get("ok"))
    r.data = res
    if not res.get("ok"):
        r.data["error"] = res.get("message", "")
    r.say(res.get("message", ""))
    for it in res.get("items", []) or []:
        r.say(f"  [{it['kind']}] {it['target']}")
    return r


# --------------------------------------------------------------------------- #
# project（项目级配置 .phpvm.json，F8）
# --------------------------------------------------------------------------- #
def _project_path(a, r: Result):
    """定位项目配置：--path（文件或目录）优先，否则从当前目录向上查找。"""
    given = getattr(a, "path", None)
    if given:
        p = os.path.abspath(os.path.expanduser(given))
        if os.path.isdir(p):
            p = os.path.join(p, project_config.PROJECT_FILE)
        if not os.path.isfile(p):
            r.fail(t("未找到项目配置：{path}", path=p))
            return None
        return p
    p = project_config.find_project_config(os.getcwd())
    if not p:
        r.fail(t("当前目录及上层未找到 {name}", name=project_config.PROJECT_FILE))
        return None
    return p


def cmd_project_show(a, r: Result) -> Result:
    path = _project_path(a, r)
    if not path:
        return r
    try:
        project = project_config.load_project(path)
    except project_config.ProjectConfigError as e:
        return r.fail(str(e))
    r.data = {"path": path, **project}
    r.say(t("项目配置：{path}", path=path))
    r.say(f"  php      = {project['php']}")
    r.say(f"  domains  = {'、'.join(project['domains']) or '—'}")
    r.say(f"  services = {'、'.join(project['services']) or '—'}")
    r.say(f"  hosts    = {project['hosts']}    start = {project['start']}")
    if project["env"]:
        r.say("  env      = " + json.dumps(project["env"], ensure_ascii=False))
    return r


def cmd_project_apply(a, r: Result) -> Result:
    path = _project_path(a, r)
    if not path:
        return r
    try:
        project = project_config.load_project(path)
    except project_config.ProjectConfigError as e:
        return r.fail(str(e))
    cfg = Config()
    res = project_config.apply_project(
        project, cfg, VhostManager(cfg), PhpManager(cfg),
        nginx_mgr=NginxManager(),
        redis_mgr=RedisManager() if modules.is_enabled("redis", cfg) else None,
        mysql_mgr=MysqlManager() if modules.is_enabled("mysql", cfg) else None,
        start=a.start, hosts=a.hosts, dry_run=a.dry_run)
    r.ok = bool(res.get("ok"))
    r.data = {"path": path, **res}
    if not res.get("ok"):
        r.data["error"] = res.get("error") or res.get("message", "")
    r.say(res.get("message", ""))
    for s in res.get("steps", []):
        mark = "·" if s.get("skip") else ("✓" if s.get("ok") else "✗")
        r.say(f"  {mark} [{s.get('tag')}] {s.get('message', '')}")
    for w in res.get("warnings", []):
        r.note(w)
    return r


# --------------------------------------------------------------------------- #
# adminer（数据库 GUI，F9）
# --------------------------------------------------------------------------- #
def cmd_adminer_status(a, r: Result) -> Result:
    st = adminer.status(Config())
    r.data = st
    r.say(t("Adminer 文件：{path}（{state}）", path=st["file"],
            state=t("已安装") if st["installed"] else t("未安装")))
    r.say(t("托管站点：{site}", site=st["server_name"] or t("（未托管）")))
    r.say(t("访问地址：{url}", url=st["url"]))
    return r


def cmd_adminer_install(a, r: Result) -> Result:
    cfg = Config()
    res = adminer.install(cfg, domain=a.domain, php=getattr(a, "php", None),
                          hosts=a.hosts, dry_run=a.dry_run)
    r.ok = bool(res.get("ok"))
    r.data = res
    if not res.get("ok"):
        r.data["error"] = res.get("message", "")
    r.say(res.get("message", ""))
    for s in res.get("steps", []):
        mark = "·" if s.get("skip") else ("✓" if s.get("ok") else "✗")
        r.say(f"  {mark} [{s.get('tag')}] {s.get('message', '')}")
    r.note(t("Adminer 具备写库能力，仅供本地开发使用。"))
    return r


def cmd_adminer_open(a, r: Result) -> Result:
    import webbrowser
    st = adminer.status(Config())
    r.data = st
    if not st["installed"] or not st["hosted"]:
        r.note(t("Adminer 尚未安装或未托管站点，可先执行 adminer install。"))
    url = st["url"]
    try:
        opened = webbrowser.open(url)
    except Exception:  # noqa: BLE001
        opened = False
    r.say(t("访问地址：{url}", url=url))
    r.data["opened"] = bool(opened)
    return r


# --------------------------------------------------------------------------- #
# site
# --------------------------------------------------------------------------- #
def cmd_site_list(a, r: Result) -> Result:
    cfg = Config()
    vm = VhostManager(cfg)
    entries = vm.scan(include_disabled=True)
    items = [_site_dict(e) for e in entries]
    if a.domain:
        key = a.domain.lower()
        items = [i for i in items if key in i["server_name"].lower()]
    r.data = {"vhost_dir": vm.vhost_dir, "main_conf": vm.main_conf,
              "include": vm.include_status(), "count": len(items), "sites": items}
    for i in items:
        flag = "禁用" if i["disabled"] else "启用"
        r.say(f"[{flag}] {i['server_name']}  →  {i['file_rel']}")
        r.say(f"        root={i['root'] or '-'}  port={i['port'] or '-'}  "
              f"php={i['php_version'] or '-'}"
              + (f"  ⚠ {i['note']}" if i["note"] else ""))
    if not vm.include_status()["covered"]:
        r.note("生效 nginx.conf 尚未 include 站点目录，新站点不会被加载；"
               "可执行 site create（自动补 include）或手动添加 include。")
    return r


def cmd_site_show(a, r: Result) -> Result:
    cfg = Config()
    vm = VhostManager(cfg)
    e = _pick_site(vm, a.target, r)
    if e is None:
        return r
    data = _site_dict(e)
    try:
        with open(e.file, "r", encoding="utf-8", errors="replace") as f:
            data["content"] = f.read()
    except OSError as err:
        data["content"] = f"（读取失败：{err}）"
    real = [d for d in e.server_name.split() if not d.startswith("*.")]
    data["hosts"] = hosts_manager.mapping_for_domains(real)
    r.data = data
    r.say(f"{data['server_name']}（{data['file']}）")
    for k in ("root", "port", "php_version", "url", "disabled", "note"):
        r.say(f"  {k} = {data[k]}")
    for d, ip in data["hosts"].items():
        r.say(f"  hosts {d} → {ip or '未映射'}")
    r.say("---- 配置内容 ----")
    r.say(data["content"])
    return r


# --------------------------------------------------------------------------- #
# diag（一键体检 / 502 诊断，只读）
# --------------------------------------------------------------------------- #
_LEVEL_ORDER = {"ok": 0, "skip": 0, "warn": 1, "err": 2}


def _emit_diag(r: Result, entry, items) -> None:
    """把单站点诊断结果写入 r（文本 + JSON data）。"""
    r.data = {
        "server_name": entry.server_name,
        "file": entry.file,
        "summary": diag.summarize(items),
        "items": [it.to_dict() for it in items],
    }
    r.say(f"=== {entry.server_name} ({entry.file_rel}) ===")
    for it in items:
        line = f"[{diag.level_label(it.level)}] {it.name}"
        if it.detail:
            line += f"：{it.detail}"
        r.say(line)
        if it.fix:
            r.say(f"       ↳ {t('修复')}：{it.fix}")


def _ready_deps(cfg):
    """构造诊断所需的就绪数据（站点 / PHP 版本 / nginx 状态），避免每个站点重复扫描。"""
    vm = VhostManager(cfg)
    pm = PhpManager(cfg)
    pm.scan_versions()
    pm.resolve(refresh_status=True, fast=False)
    nginx = NginxManager()
    return vm, pm.versions, nginx.get_status()[0], nginx.logs_dir


def cmd_diag_site(a, r: Result) -> Result:
    cfg = Config()
    vm = VhostManager(cfg)
    e = _pick_site(vm, a.target, r)
    if e is None:
        return r
    _vm, php_versions, nginx_running, logs_dir = _ready_deps(cfg)
    # 用同一 vm 实例保持一致；diagnose_site 内部缺省也会重建，这里显式传入
    items = diag.diagnose_site(
        e, config=cfg, vhost_mgr=vm,
        php_versions=php_versions,
        nginx_running=nginx_running,
        logs_dir=logs_dir,
    )
    _emit_diag(r, e, items)
    # 只读诊断只报告，不把「发现错误」当作业务失败（退出码仍按严重度给出提示）
    return r


def cmd_diag_all(a, r: Result) -> Result:
    cfg = Config()
    vm = VhostManager(cfg)
    entries = vm.scan(include_disabled=True)
    if not entries:
        r.say(t("未发现任何站点配置"))
        r.data = {"count": 0, "sites": []}
        return r
    _vm, php_versions, nginx_running, logs_dir = _ready_deps(cfg)
    sites = []
    worst = "ok"
    for e in entries:
        items = diag.diagnose_site(
            e, config=cfg, vhost_mgr=vm,
            php_versions=php_versions,
            nginx_running=nginx_running,
            logs_dir=logs_dir,
        )
        summary = diag.summarize(items)
        sites.append({
            "server_name": e.server_name,
            "file": e.file,
            "summary": summary,
            "items": [it.to_dict() for it in items],
        })
        if _LEVEL_ORDER[summary] > _LEVEL_ORDER[worst]:
            worst = summary
    r.data = {"count": len(sites), "summary": worst, "sites": sites}
    for s in sites:
        r.say(f"=== {s['server_name']} → {s['summary']} ===")
    return r


def _plan_site(a, r: Result):
    """解析并校验建站参数，返回 (doms, tpl, docroot, port, php_name, conf_name)。"""
    doms = [d.strip().lower() for d in (a.domain or "").split() if d.strip()]
    if not doms:
        r.fail("请提供 --domain（可多个，空格分隔）")
        return None
    bad = [d for d in doms if not valid_domain(d)]
    if bad:
        r.fail("域名不合法：" + "、".join(bad))
        return None
    tpl = site_templates.TEMPLATE_MAP.get((a.template or "").strip())
    if tpl is None:
        r.fail("未知模板：" + str(a.template),
               available=list(site_templates.TEMPLATE_MAP))
        return None
    root = os.path.abspath(os.path.expanduser(a.root)) if a.root else ""
    if not root:
        r.fail("请提供 --root（项目目录）")
        return None
    if not os.path.isdir(root):
        r.fail("项目目录不存在或不是文件夹：" + root)
        return None
    docroot = nginx_path(root.rstrip("\\/")) + tpl.get("root_suffix", "")
    if tpl.get("root_suffix") and not os.path.isdir(docroot):
        r.note(f"文档根 {docroot} 不存在；若项目入口就在项目根目录，"
               "请改用 --template php")
    port, php_name = None, None
    if a.no_php:
        port = None
    elif a.port:
        try:
            port = int(a.port)
        except (TypeError, ValueError):
            r.fail("--port 必须是整数：" + str(a.port))
            return None
    elif a.php:
        pm = _php_manager(Config(), refresh=False)
        v = _pick_php(pm, a.php, r)
        if v is None:
            return None
        port, php_name = v.port, v.name
    elif tpl.get("needs_php", True):
        r.fail("该模板需要 PHP：请用 --php <版本名> 指定，"
               "或加 --no-php 明确创建静态站点")
        return None
    conf = (a.conf or safe_conf_base(doms[0]) + ".conf").strip()
    if not conf.endswith(".conf"):
        conf += ".conf"
    return doms, tpl, docroot, port, php_name, conf


def _to_site_plan(a, doms, tpl, docroot, port, conf) -> site_service.SitePlan:
    """CLI 参数 → core 建站计划。

    策略保持 CLI 的严格口径：生效主配置缺失时 include 记为失败
    （``allow_missing_main_conf=False``），以便尽早暴露环境问题。
    注意 ``site render`` 只有 ``site create`` 的部分参数（无 --hosts / --no-reload），
    因此这两个开关用 getattr 取默认值。
    """
    return site_service.SitePlan(
        domains=doms, template_key=tpl["key"], docroot=docroot, port=port,
        conf_name=conf, https=bool(getattr(a, "https", False)),
        hosts=bool(getattr(a, "hosts", False)),
        reload=not getattr(a, "no_reload", False),
        allow_missing_main_conf=False)


def cmd_site_render(a, r: Result) -> Result:
    """只渲染配置文本，不做任何写入（AI 预览 / 评审用）。"""
    plan = _plan_site(a, r)
    if plan is None:
        return r
    doms, tpl, docroot, port, php_name, conf = plan
    content, cert_res = site_service.render_config_for(
        _to_site_plan(a, doms, tpl, docroot, port, conf))
    if content is None:
        return r.fail((cert_res or {}).get("message", "配置渲染失败"),
                      cert=cert_res or {})
    r.data = {"template": tpl["key"], "domains": doms, "docroot": docroot,
              "port": port, "php": php_name, "conf": conf, "https": bool(a.https),
              "content": content}
    r.say(content)
    return r


def cmd_site_create(a, r: Result) -> Result:
    plan = _plan_site(a, r)
    if plan is None:
        return r
    doms, tpl, docroot, port, php_name, conf = plan
    vm = VhostManager(Config())
    site_plan = _to_site_plan(a, doms, tpl, docroot, port, conf)

    if a.dry_run:
        content, cert_res = site_service.render_config_for(site_plan)
        if content is None:
            return r.fail((cert_res or {}).get("message", "配置渲染失败"),
                          cert=cert_res or {})
        r.data = {"dry_run": True, "template": tpl["key"], "domains": doms,
                  "docroot": docroot, "port": port, "php": php_name,
                  "conf": conf, "path": os.path.join(vm.vhost_dir, conf),
                  "hosts": [d for d in doms if not d.startswith("*.")],
                  "content": content}
        r.say(f"[dry-run] 将写入 {os.path.join(vm.vhost_dir, conf)}")
        r.say(content)
        return r

    if a.hosts and not [d for d in doms if not d.startswith("*.")]:
        r.note("域名均为泛解析，跳过 hosts 写入")
        site_plan.hosts = False

    # 编排与回滚都下沉在 core/site_service，与 GUI 向导共用同一实现
    res = site_service.create_site(site_plan)
    steps = res.steps
    rolled_back = False
    if res.fatal:
        rolled = site_service.rollback_site(res.rollback)
        for item in rolled:
            if item.get("message"):
                r.note(item["message"])
        rolled_back = any(item.get("ok") for item in rolled)
        r.note("配置检查未通过，已回滚本次对 vhost / nginx.conf 的修改")

    fatal = [s for tag, s in steps
             if tag in site_service.HARD_STEPS and not s.get("ok") and not s.get("skip")]
    r.ok = not res.fatal
    r.data = {
        "template": tpl["key"], "domains": doms, "docroot": docroot,
        "port": port, "php": php_name, "conf": conf,
        "path": res.path, "https": bool(a.https),
        "rolled_back": rolled_back,
        "steps": [{"step": tag, **s} for tag, s in steps],
    }
    if fatal:
        r.data["error"] = "建站未通过校验：" + fatal[0].get("message", "")
    for tag, s in steps:
        mark = "✓" if s.get("ok") else ("-" if s.get("skip") else "✗")
        r.say(f"{mark} {tag}：{s.get('message', '')}")
    r.say("站点配置：" + str(res.path))
    return r


def cmd_site_remove(a, r: Result) -> Result:
    cfg = Config()
    vm = VhostManager(cfg)
    e = _pick_site(vm, a.target, r)
    if e is None:
        return r
    if not a.yes:
        return r.fail("删除站点需显式确认：加 --yes（会先备份为 .bak）",
                      file=e.file)
    backup = file_backup.backup_path(e.file)
    if a.dry_run:
        r.data = {"dry_run": True, "file": e.file, "backup": backup}
        r.say(f"[dry-run] 将删除 {e.file}（备份为 {backup}）")
        return r
    try:
        backup = file_backup.backup(e.file) or backup
        os.remove(e.file)
    except OSError as err:
        return r.fail(f"删除失败：{err}", file=e.file)
    ng = _nginx()
    test_ok, out, rolled_back = True, "", False
    if os.path.exists(ng.exe):
        out = ng.test_config()
        test_ok = _test_output_ok(out)
        if not test_ok:
            file_backup.restore(backup, e.file, remove_backup=False)
            rolled_back = True
            r.note("nginx -t 未通过，已还原站点配置")
    reloaded = False
    if test_ok and not a.no_reload and ng.get_status()[0]:
        ng.reload()
        reloaded = True
    r.ok = test_ok
    r.data = {"file": e.file, "backup": backup, "test_output": out,
              "rolled_back": rolled_back, "reloaded": reloaded}
    if not test_ok:
        r.data["error"] = "删除后 nginx -t 未通过（已还原）"
    r.say(f"已删除 {e.file}（备份 {backup}）")
    return r


def cmd_site_enable(a, r: Result) -> Result:
    cfg = Config()
    vm = VhostManager(cfg)
    e = _pick_site(vm, a.target, r)
    if e is None:
        return r
    res = vm.set_site_enabled(e.file, True)
    r.ok = bool(res.get("ok"))
    r.data = res
    if not res.get("ok"):
        r.data["error"] = res.get("message", "")
    r.say(res.get("message", ""))
    return r


def cmd_site_disable(a, r: Result) -> Result:
    cfg = Config()
    vm = VhostManager(cfg)
    e = _pick_site(vm, a.target, r)
    if e is None:
        return r
    res = vm.set_site_enabled(e.file, False)
    r.ok = bool(res.get("ok"))
    r.data = res
    if not res.get("ok"):
        r.data["error"] = res.get("message", "")
    r.say(res.get("message", ""))
    return r


def cmd_site_php(a, r: Result) -> Result:
    cfg = Config()
    vm = VhostManager(cfg)
    e = _pick_site(vm, a.target, r)
    if e is None:
        return r
    pm = _php_manager(cfg, refresh=False)
    v = _pick_php(pm, a.php, r)
    if v is None:
        return r
    res = vm.set_site_php(e.file, e.server_name, v.port)
    r.ok = bool(res.get("ok"))
    r.data = {"site": e.file, "server_name": e.server_name,
              "php": v.name, "port": v.port, **res}
    if not res.get("ok"):
        r.data["error"] = res.get("message", "")
    r.say(res.get("message", ""))
    return r


def cmd_site_secure(a, r: Result) -> Result:
    cfg = Config()
    vm = VhostManager(cfg)
    e = _pick_site(vm, a.target, r)
    if e is None:
        return r
    real = [d for d in e.server_name.split() if d and not d.startswith("*.")]
    if a.dry_run:
        st = cert_manager.status()
        r.data = {"dry_run": True, "site": e.file, "server_name": e.server_name,
                  "domains": real, "cert": st, "action": "append-443"}
        r.say(t("将文件：{path}", path=e.file))
        r.say(t("将域名：{doms}", doms="、".join(real) or "—"))
        r.say(t("证书能力：{msg}", msg=st["message"]))
        return r
    res = site_service.secure_site(e, cfg)
    r.ok = bool(res.get("ok"))
    r.data = {"site": e.file, "server_name": e.server_name, **res}
    if not res.get("ok"):
        r.data["error"] = res.get("message", "")
    r.say(res.get("message", ""))
    if res.get("output"):
        r.say(res["output"])
    return r


def cmd_site_unsecure(a, r: Result) -> Result:
    cfg = Config()
    vm = VhostManager(cfg)
    e = _pick_site(vm, a.target, r)
    if e is None:
        return r
    if a.dry_run:
        try:
            with open(e.file, "r", encoding="utf-8", errors="replace") as f:
                has = "listen 443" in f.read()
        except OSError as err:
            return r.fail(t("读取配置失败：{err}", err=err))
        r.data = {"dry_run": True, "site": e.file, "server_name": e.server_name,
                  "has_https": has, "action": "remove-443"}
        r.say(t("将文件：{path}", path=e.file))
        r.say(t("当前 HTTPS：{state}", state=t("已启用") if has else t("未启用")))
        return r
    res = site_service.unsecure_site(e, cfg)
    r.ok = bool(res.get("ok"))
    r.data = {"site": e.file, "server_name": e.server_name, **res}
    if not res.get("ok"):
        r.data["error"] = res.get("message", "")
    r.say(res.get("message", ""))
    if res.get("output"):
        r.say(res["output"])
    return r


def cmd_site_sync_port(a, r: Result) -> Result:
    cfg = Config()
    vm = VhostManager(cfg)
    try:
        old, new = int(a.old), int(a.new)
    except (TypeError, ValueError):
        return r.fail("--old / --new 必须是整数")
    if a.dry_run:
        files = vm._find_files_with_port(old)  # noqa: SLF001 —— 只读探测
        r.data = {"dry_run": True, "old": old, "new": new, "files": files}
        r.say(f"[dry-run] 将把 {len(files)} 个文件中的 fastcgi_pass {old} → {new}")
        for f in files:
            r.say("  " + f)
        return r
    results = vm.sync_port(old, new)
    r.data = {"old": old, "new": new, "results": results}
    if not results:
        r.note(f"没有配置文件引用端口 {old}")
    ok = all(x.get("ok") for x in results)
    reloaded = False
    if ok and results and a.reload:
        ng = _nginx()
        if ng.get_status()[0]:
            r.data["reload"] = ng.reload()
            reloaded = True
    r.ok = ok
    if not ok:
        r.data["error"] = "部分文件同步失败（已自动还原）"
    for x in results:
        r.say(f"{'✓' if x.get('ok') else '✗'} {x['file']}：{x.get('message', '')}")
    if reloaded:
        r.say("已平滑重载 nginx")
    return r


# --------------------------------------------------------------------------- #
# hosts
# --------------------------------------------------------------------------- #
def cmd_hosts_status(a, r: Result) -> Result:
    doms = [d.strip().lower() for d in a.domains if d.strip()]
    if not doms:
        return r.fail("请提供至少一个域名")
    mapping = hosts_manager.mapping_for_domains(doms)
    r.data = {"hosts_path": hosts_manager.hosts_path(), "mapping": mapping}
    for d in doms:
        r.say(f"{d} → {mapping.get(d) or '未映射'}")
    return r


def cmd_hosts_add(a, r: Result) -> Result:
    doms = [d.strip().lower() for d in a.domains if d.strip()]
    if not doms:
        return r.fail("请提供至少一个域名")
    mapping = hosts_manager.mapping_for_domains(doms)
    if a.dry_run:
        r.data = {"dry_run": True,
                  "to_add": [d for d in doms if not mapping.get(d)],
                  "already": [d for d in doms if mapping.get(d) == "127.0.0.1"],
                  "conflict": [d for d in doms
                               if mapping.get(d) not in (None, "127.0.0.1")]}
        r.say("[dry-run] " + json.dumps(r.data, ensure_ascii=False))
        return r
    res = hosts_manager.ensure_entries(doms, ip=a.ip)
    r.ok = bool(res.get("ok"))
    r.data = {"hosts_path": hosts_manager.hosts_path(), **res}
    if not res.get("ok"):
        r.data["error"] = res.get("message", "")
    r.say(res.get("message", ""))
    if res.get("conflict"):
        r.note("以下域名已指向其它 IP，未改动：" + "、".join(res["conflict"]))
    r.note("无写权限时会触发系统授权弹窗（macOS osascript / Windows UAC）")
    return r


def cmd_hosts_restore(a, r: Result) -> Result:
    """用写入前的备份整文件还原 hosts（phpvm 每次写入 / 删除前都会备份）。"""
    backup = hosts_manager.backup_path()
    if not hosts_manager.has_backup():
        return r.fail("未找到 hosts 备份：" + backup, backup=backup)
    if a.dry_run:
        r.data = {"dry_run": True, "backup": backup, "hosts_path": hosts_manager.hosts_path()}
        r.say(f"[dry-run] 将用备份覆盖 hosts：{backup}")
        return r
    if not a.yes:
        return r.fail("还原 hosts 需显式确认：加 --yes（会用备份覆盖当前 hosts）",
                      backup=backup)
    ok, msg = hosts_manager.restore_backup()
    r.ok = bool(ok)
    r.data = {"hosts_path": hosts_manager.hosts_path(), "backup": backup,
              "message": msg}
    if not ok:
        r.data["error"] = msg
    r.say(msg)
    return r


def cmd_hosts_remove(a, r: Result) -> Result:
    doms = [d.strip().lower() for d in a.domains if d.strip()]
    if not doms:
        return r.fail("请提供至少一个域名")
    if a.dry_run:
        r.data = {"dry_run": True, "to_remove": doms}
        r.say("[dry-run] 将从 phpvm 托管块移除：" + "、".join(doms))
        return r
    res = hosts_manager.remove_entries(doms)
    r.ok = bool(res.get("ok"))
    r.data = {"hosts_path": hosts_manager.hosts_path(), **res}
    if not res.get("ok"):
        r.data["error"] = res.get("message", "")
    r.say(res.get("message", "") or ("已移除：" + "、".join(res.get("removed", []))))
    return r


# --------------------------------------------------------------------------- #
# redis / mysql
# --------------------------------------------------------------------------- #
_REDIS_DANGEROUS = {"FLUSHALL", "FLUSHDB", "SHUTDOWN", "SLAVEOF",
                    "REPLICAOF", "DEBUG"}


def cmd_redis_list(a, r: Result) -> Result:
    cfg = Config()
    if not modules.is_enabled("redis", cfg):
        return r.fail("Redis 模块已停用（settings.disabled_modules），"
                      "如需使用请在 GUI 关于页启用或 config set settings.disabled_modules []")
    insts = RedisManager().refresh_instances()
    r.data = {"count": len(insts), "instances": [_redis_dict(i) for i in insts]}
    for i in insts:
        d = _redis_dict(i)
        r.say(f"{'●' if d['running'] else '○'} {d['name']} · 端口 {d['port']} · {d['version']}")
    return r


def _redis_target(a, r: Result):
    cfg = Config()
    if not modules.is_enabled("redis", cfg):
        return None, r.fail("Redis 模块已停用（settings.disabled_modules）")
    mgr = RedisManager()
    inst = _pick(mgr.refresh_instances(), a.name, r, "Redis")
    return mgr, inst


def cmd_redis_status(a, r: Result) -> Result:
    mgr, inst = _redis_target(a, r)
    if inst is None:
        return r
    r.data = _redis_dict(inst)
    r.say(f"{'●' if r.data['running'] else '○'} {inst.name} · 端口 {inst.port} · "
          f"{r.data['version']}")
    return r


def _redis_action(a, r: Result, action: str) -> Result:
    mgr, inst = _redis_target(a, r)
    if inst is None:
        return r
    try:
        msg = getattr(mgr, action)(inst)
    except Exception as e:  # noqa: BLE001
        return r.fail(f"Redis {action} 失败：{e}")
    running, pids = mgr.get_status(inst)
    r.data = {"name": inst.name, "action": action, "message": msg,
              "running": running, "pids": pids}
    r.say(msg or f"Redis {action} 完成")
    r.say(f"当前状态：{'运行中' if running else '已停止'}")
    return r


def cmd_redis_start(a, r: Result) -> Result:
    return _redis_action(a, r, "start")


def cmd_redis_stop(a, r: Result) -> Result:
    return _redis_action(a, r, "stop")


def cmd_redis_restart(a, r: Result) -> Result:
    return _redis_action(a, r, "restart")


def cmd_redis_ping(a, r: Result) -> Result:
    mgr, inst = _redis_target(a, r)
    if inst is None:
        return r
    out = mgr.ping(inst)
    r.ok = "PONG" in (out or "").upper()
    r.data = {"name": inst.name, "output": out, "ok": r.ok}
    if not r.ok:
        r.data["error"] = "PING 未返回 PONG"
    r.say(out.strip())
    return r


def cmd_redis_cmd(a, r: Result) -> Result:
    mgr, inst = _redis_target(a, r)
    if inst is None:
        return r
    cmd = (a.command or "").strip()
    if not cmd:
        return r.fail("请提供 Redis 命令（--command，或用 -- <命令>）")
    head = cmd.split()[0].upper()
    if head in _REDIS_DANGEROUS and not a.force:
        return r.fail(f"命令 {head} 属高危操作，需显式加 --force 才会执行",
                      command=cmd)
    if a.dry_run:
        r.data = {"dry_run": True, "name": inst.name, "db": a.db, "command": cmd}
        r.say(f"[dry-run] redis-cli -p {inst.port} -n {a.db} <<< {cmd}")
        return r
    try:
        out = mgr.run_command(inst, db=a.db, command=cmd)
    except Exception as e:  # noqa: BLE001
        return r.fail(f"执行失败：{e}")
    r.data = {"name": inst.name, "port": inst.port, "db": a.db,
              "command": cmd, "output": out}
    r.say(out.strip() or "（无输出）")
    return r


def cmd_mysql_list(a, r: Result) -> Result:
    cfg = Config()
    if not modules.is_enabled("mysql", cfg):
        return r.fail("MySQL 模块已停用（settings.disabled_modules）")
    insts = MysqlManager().refresh_instances()
    r.data = {"count": len(insts), "instances": [_mysql_dict(i) for i in insts]}
    for i in insts:
        d = _mysql_dict(i)
        r.say(f"{'●' if d['running'] else '○'} {d['name']} · 端口 {d['port']} · "
              f"{d['mode']} · {d['version']}")
    return r


def _mysql_target(a, r: Result):
    cfg = Config()
    if not modules.is_enabled("mysql", cfg):
        return None, r.fail("MySQL 模块已停用（settings.disabled_modules）")
    mgr = MysqlManager()
    inst = _pick(mgr.refresh_instances(), a.name, r, "MySQL")
    return mgr, inst


def _mysql_action(a, r: Result, action: str) -> Result:
    mgr, inst = _mysql_target(a, r)
    if inst is None:
        return r
    try:
        msg = getattr(mgr, action)(inst)
    except Exception as e:  # noqa: BLE001
        return r.fail(f"MySQL {action} 失败：{e}")
    running, pids = mgr.get_status(inst)
    r.data = {"name": inst.name, "action": action, "message": msg,
              "running": running, "pids": pids, "mode": inst.mode}
    r.say(msg or f"MySQL {action} 完成")
    r.say(f"当前状态：{'运行中' if running else '已停止'}")
    return r


def cmd_mysql_start(a, r: Result) -> Result:
    return _mysql_action(a, r, "start")


def cmd_mysql_stop(a, r: Result) -> Result:
    return _mysql_action(a, r, "stop")


def cmd_mysql_restart(a, r: Result) -> Result:
    return _mysql_action(a, r, "restart")


def cmd_mysql_status(a, r: Result) -> Result:
    mgr, inst = _mysql_target(a, r)
    if inst is None:
        return r
    r.data = _mysql_dict(inst)
    r.say(f"{'●' if r.data['running'] else '○'} {inst.name} · 端口 {inst.port} · "
          f"{inst.mode}")
    return r


def cmd_mysql_log(a, r: Result) -> Result:
    mgr, inst = _mysql_target(a, r)
    if inst is None:
        return r
    path = mgr.error_log_path(inst)
    if not path or not os.path.isfile(path):
        return r.fail("未找到错误日志：" + str(path))
    text = mgr.tail_error_log(inst, max_lines=a.lines)
    r.data = {"name": inst.name, "file": path, "lines": a.lines, "content": text}
    r.say(text)
    return r


# --------------------------------------------------------------------------- #
# sqlite（只读）
# --------------------------------------------------------------------------- #
def _sqlite_open(path: str):
    mgr = SqliteManager()  # 不传 config：避免写入 settings.sqlite_last_db
    mgr.open(path)
    return mgr


def cmd_sqlite_tables(a, r: Result) -> Result:
    try:
        mgr = _sqlite_open(a.path)
    except (OSError, ValueError) as e:
        return r.fail(str(e))
    import sqlite3
    try:
        tabs = mgr.tables()
        items = []
        for t in tabs:
            items.append({"name": t.name, "kind": t.kind,
                          "rows": mgr.count_rows(t.name) if t.kind == "table" else None})
        r.data = {"path": mgr.path, "count": len(items), "tables": items}
        for i in items:
            r.say(f"  {i['kind']:5s} {i['name']}" +
                  (f"  （{i['rows']} 行）" if i["rows"] is not None else ""))
    except sqlite3.Error as e:
        return r.fail(f"读取失败：{e}")
    finally:
        mgr.close()
    return r


def cmd_sqlite_columns(a, r: Result) -> Result:
    try:
        mgr = _sqlite_open(a.path)
    except (OSError, ValueError) as e:
        return r.fail(str(e))
    import sqlite3
    try:
        cols = mgr.columns(a.table)
        r.data = {"path": mgr.path, "table": a.table,
                  "columns": [{"name": c.name, "type": c.type, "notnull": c.notnull,
                               "pk": c.pk, "default": c.default} for c in cols]}
        for c in cols:
            r.say(f"  {c.name} {c.type}"
                  + (" NOT NULL" if c.notnull else "")
                  + (" PK" if c.pk else "")
                  + (f" DEFAULT {c.default}" if c.default else ""))
    except sqlite3.Error as e:
        return r.fail(f"读取失败：{e}")
    finally:
        mgr.close()
    return r


def cmd_sqlite_query(a, r: Result) -> Result:
    try:
        mgr = _sqlite_open(a.path)
    except (OSError, ValueError) as e:
        return r.fail(str(e))
    import sqlite3
    from core.sqlite_manager import format_value
    try:
        res = mgr.query(a.sql, limit=a.limit, offset=a.offset)
    except ValueError as e:
        return r.fail(str(e))
    except sqlite3.Error as e:
        return r.fail(f"查询失败：{e}")
    finally:
        mgr.close()
    rows = [[format_value(v) for v in row] for row in res.rows]
    r.data = {"path": a.path, "columns": res.columns, "rows": rows,
              "truncated": res.truncated, "elapsed_ms": round(res.elapsed, 1),
              "row_count": len(rows)}
    if not _flag(a, "json", False):
        r.say(" | ".join(res.columns))
        for row in rows:
            r.say(" | ".join(row))
        r.say(f"（{len(rows)} 行" + ("，结果已截断" if res.truncated else "") + "）")
    if res.truncated:
        r.note(f"结果超过 limit={a.limit}，已截断；可用 --limit / --offset 翻页")
    return r


# --------------------------------------------------------------------------- #
# services / update / module / schema
# --------------------------------------------------------------------------- #
def _php_scope_arg(raw: str | None) -> tuple[str, list[str] | None]:
    """解析 services start-all 的 --php：策略名或逗号分隔的版本名。

    返回 (php_scope, php_names)：命中策略名时 names 为 None；给出的是版本名
    （如 php82,php85）时 scope 回落 "all"、由 names 精确指定（未知版本名只告警）。
    """
    text = (raw or "").strip()
    if not text:
        return "all", None
    low = text.lower()
    if low in PHP_SCOPES:
        return low, None
    names = [p.strip() for p in text.split(",") if p.strip()]
    return ("all", names) if names else ("all", None)


def cmd_services_start(a, r: Result) -> Result:
    cfg = Config()
    scope, names = _php_scope_arg(getattr(a, "php", None))
    group = _service_group(cfg)
    if a.dry_run:
        targets, skipped = group.php_targets(scope, names)
        r.data = {"dry_run": True, "action": "start_all", "php_scope": scope,
                  "php_names": [v.name for v in targets],
                  "php_skipped": [v.name for v in skipped]}
        r.say("[dry-run] 将按顺序启动 PHP → Redis → MySQL → Nginx")
        r.say(f"PHP（{scope_label(scope)}）："
              + ("、".join(v.name for v in targets) or "（无）"))
        if skipped:
            r.say("跳过： " + "、".join(v.name for v in skipped))
        return r
    msg = group.start_all(php_scope=scope, php_names=names)
    r.data = {"action": "start_all", "php_scope": scope, "message": msg}
    r.say(msg)
    return r


def cmd_services_stop(a, r: Result) -> Result:
    cfg = Config()
    if a.dry_run:
        r.data = {"dry_run": True, "action": "stop_all"}
        r.say("[dry-run] 将停止 Nginx → MySQL → Redis → 各 PHP")
        return r
    msg = _service_group(cfg).stop_all()
    r.data = {"action": "stop_all", "message": msg}
    r.say(msg)
    return r


def cmd_update_check(a, r: Result) -> Result:
    try:
        rel = updater.check_update()
    except Exception as e:  # noqa: BLE001 —— 网络类异常统一收敛
        return r.fail(f"检查更新失败：{e}")
    cur = version.version()
    newer = updater.is_newer_version(rel.version)
    r.data = {
        "current": cur,
        "latest": rel.version,
        "is_newer": newer,
        "tag": rel.tag,
        "page_url": rel.page_url,
        "published_at": rel.published_at,
        "notes": rel.notes,
        "platform": updater.platform_key(),
        "assets": {k: {"name": v.name, "size": v.size, "sha256": v.sha256,
                       "url": v.url} for k, v in rel.assets.items()},
    }
    r.say(f"当前 {cur} · 最新 {rel.version}（{'有更新' if newer else '已是最新'}）")
    r.say(rel.page_url)
    return r


def cmd_update_download(a, r: Result) -> Result:
    try:
        rel = updater.check_update()
    except Exception as e:  # noqa: BLE001
        return r.fail(f"检查更新失败：{e}")
    if not updater.is_newer_version(rel.version):
        r.data = {"up_to_date": True, "current": version.version(),
                  "latest": rel.version}
        r.say(f"当前已是最新版本（{version.version()}）")
        return r
    if a.dry_run:
        asset = rel.assets.get(updater.platform_key())
        r.data = {"dry_run": True, "latest": rel.version,
                  "asset": ({"name": asset.name, "size": asset.size} if asset else None),
                  "dest_dir": updater.updates_dir()}
        r.say(f"[dry-run] 将下载 {asset.name if asset else '（无本平台资产）'} "
              f"到 {updater.updates_dir()}")
        return r
    progress = None
    if not _flag(a, "json", False):
        def progress(done, total):  # 进度写 stderr，保持 stdout 纯净
            pct = f"{done * 100 // total}%" if total else ""
            sys.stderr.write(f"\r下载中 {done}/{total} {pct}   ")
            sys.stderr.flush()
    try:
        path = updater.download(rel, on_progress=progress)
    except Exception as e:  # noqa: BLE001
        return r.fail(f"下载失败：{e}")
    if progress:
        sys.stderr.write("\n")
    r.data = {"path": path, "version": rel.version, "tag": rel.tag,
              "page_url": rel.page_url, "updates_dir": updater.updates_dir(),
              "auto_apply_supported": updater.supports_auto_update()}
    r.say("已下载：" + path)
    r.note("自动替换需以安装包形态运行；否则请手动安装该包")
    return r


# --------------------------------------------------------------------------- #
# tune（开发环境配置推荐）
# --------------------------------------------------------------------------- #
def _tune_target(a, r: Result):
    """解析 tune 命令的目标文件与建议项；失败返回 None（r 已带错误信息）。"""
    from core import tuning

    profile = tuning.detect_machine()
    r.data["machine"] = {"cpu": profile.cpu, "mem_gb": profile.mem_gb,
                         "platform": profile.platform, "tier": profile.tier}
    if a.target == "php":
        cfg = Config()
        pm = _php_manager(cfg, refresh=False)
        name = getattr(a, "name", "") or ""
        v = pm.versions[0] if (not name and len(pm.versions) == 1) else _pick_php(pm, name, r)
        if v is None:
            return None
        if not v.ini:
            r.fail(f"{v.name} 未使用独立配置文件，无法给出建议")
            return None
        items = tuning.php_suggestions(v.ini, profile, v.display)
        r.data.update(target="php", name=v.name, file=v.ini, notes=tuning.php_notes())
        return v.ini, items
    ng = _nginx()
    if not os.path.exists(ng.main_conf):
        r.fail("未找到 nginx 主配置：" + ng.main_conf, main_conf=ng.main_conf)
        return None
    with open(ng.main_conf, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    items = tuning.nginx_suggestions(text, profile)
    r.data.update(target="nginx", file=ng.main_conf, notes=tuning.nginx_notes(profile))
    return ng.main_conf, items


def cmd_tune_suggest(a, r: Result) -> Result:
    got = _tune_target(a, r)
    if got is None:
        return r
    path, items = got
    only = {s.strip() for s in (getattr(a, "only", "") or "").split(",") if s.strip()}
    if only:
        items = [i for i in items if i.key in only]
    r.data["items"] = [i.to_dict() for i in items]
    r.say(f"{path}：{len(items)} 项建议"
          + (f"（按 --only 过滤：{', '.join(sorted(only))}）" if only else ""))
    for i in items:
        r.say(f"  {i.key}: {i.current or '（未设置）'} → {i.value}")
    if not items:
        r.note("当前配置已符合开发环境推荐值")
    return r


def cmd_tune_apply(a, r: Result) -> Result:
    from core import tuning

    got = _tune_target(a, r)
    if got is None:
        return r
    path, items = got
    wanted = {s.strip() for s in (getattr(a, "items", "") or "").split(",") if s.strip()}
    if wanted:
        unknown = sorted(wanted - {i.key for i in items})
        if unknown:
            return r.fail("以下配置项不在建议列表中：" + "、".join(unknown),
                          available=[i.key for i in items])
        chosen = [i for i in items if i.key in wanted]
    else:
        chosen = list(items)
    if not chosen:
        r.data["applied"] = []
        r.say("没有需要应用的建议项（当前配置已符合推荐值）")
        return r
    if a.dry_run:
        r.data.update(dry_run=True, file=path,
                      items=[{"key": i.key, "from": i.current, "to": i.value} for i in chosen])
        r.say(f"[dry-run] 将在 {path} 写入 {len(chosen)} 项：")
        for i in chosen:
            r.say(f"  {i.key}: {i.current or '（未设置）'} → {i.value}")
        return r
    if a.target == "php":
        res = tuning.apply_php(path, chosen)
    else:
        res = tuning.apply_nginx(path, chosen, verify=True, reload=a.reload)
    r.data.update({k: v for k, v in res.items() if k != "items"})
    r.data["applied"] = res.get("items", [])
    if res.get("rolled_back") or res.get("ok") is False:
        return r.fail(res.get("error") or "写入失败", file=path)
    r.say(f"已写入 {res.get('changed', 0)} 项：{path}")
    r.say("备份：" + str(res.get("backup", "")))
    if a.target == "php":
        r.note("需重启该 PHP 版本后生效：php restart " + str(r.data.get("name", "")))
    elif not a.reload:
        r.note("配置已写入，执行 nginx reload 生效")
    return r


def cmd_module_list(a, r: Result) -> Result:
    cfg = Config()
    disabled = modules.disabled_modules(cfg)
    items = [{"key": m["key"], "name": m["name"], "required": m["required"],
              "enabled": m["key"] not in disabled} for m in modules.MODULES]
    r.data = {"disabled": disabled, "modules": items}
    for i in items:
        state = "启用" if i["enabled"] else "停用"
        r.say(f"  {'✓' if i['enabled'] else '✗'} {i['key']:8s} {i['name']}"
              + ("（刚需）" if i["required"] else f"  [{state}]"))
    return r


def cmd_module_set(a, r: Result, enable: bool) -> Result:
    cfg = Config()
    keys = [k.strip() for k in a.keys if k.strip()]
    if not keys:
        return r.fail("请提供至少一个模块 key")
    known = {m["key"] for m in modules.MODULES}
    unknown = [k for k in keys if k not in known]
    if unknown:
        return r.fail("未知模块 key：" + "、".join(unknown), available=sorted(known))
    required = [k for k in keys if k in modules.REQUIRED_KEYS]
    if required:
        return r.fail("刚需模块不可停用：" + "、".join(required))
    cur = set(modules.disabled_modules(cfg))
    new = (cur - set(keys)) if enable else (cur | set(keys))
    if a.dry_run:
        r.data = {"dry_run": True, "disabled": sorted(new)}
        r.say("[dry-run] 禁用列表将变为：" + json.dumps(sorted(new), ensure_ascii=False))
        return r
    modules.set_disabled(cfg, sorted(new))
    r.data = {"disabled": modules.disabled_modules(cfg)}
    r.say("已更新禁用列表：" + json.dumps(r.data["disabled"], ensure_ascii=False))
    r.note("模块开关需重启 phpvm（GUI）后生效")
    return r


def cmd_module_enable(a, r: Result) -> Result:
    return cmd_module_set(a, r, True)


def cmd_module_disable(a, r: Result) -> Result:
    return cmd_module_set(a, r, False)


def cmd_schema(a, r: Result) -> Result:
    parser = _ROOT_PARSER
    r.data = {
        "cli": "phpvm",
        "entry": "python3 cli.py <group> <action> [options]",
        "app_version": version.version(),
        "python": sys.version.split()[0],
        "conventions": {
            "json": "加 --json：stdout 只输出一个 {\"ok\",\"command\",\"data\",\"warnings\"} 文档",
            "exit_codes": {"0": "成功", "1": "业务失败（ok=false）",
                           "2": "参数用法错误", "130": "被中断"},
            "non_interactive": "永不弹窗/等待输入；写 hosts 可能触发系统授权框（需 --hosts）",
            "safety": "写操作支持 --dry-run；删除需 --yes；Redis 高危命令需 --force",
            "recommended_flow": ["schema --json", "env --json",
                                 "site create ... --dry-run", "site create ..."],
        },
        "commands": _describe_parser(parser) if parser else {},
    }
    r.say(json.dumps(r.data, ensure_ascii=False, indent=2))
    return r


def _describe_parser(parser: argparse.ArgumentParser) -> dict:
    """把 argparse 结构转成自描述字典（供 AI 读取命令契约）。"""
    desc = {"help": (parser.description or "").strip(), "options": [], "subcommands": {}}
    for act in parser._actions:  # noqa: SLF001 —— 仅读取结构
        if act.dest == "help":
            continue
        if isinstance(act, argparse._SubParsersAction):  # noqa: SLF001
            for name, sub in act.choices.items():
                desc["subcommands"][name] = _describe_parser(sub)
            continue
        item = {
            "dest": act.dest,
            "positional": not act.option_strings,
            "flags": list(act.option_strings),
            "required": bool(act.required),
            "help": (act.help or "").strip(),
        }
        if act.choices:
            item["choices"] = sorted(str(c) for c in act.choices)
        if act.default not in (None, argparse.SUPPRESS, True, False, ""):
            item["default"] = act.default
        desc["options"].append(item)
    return desc


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #
COMMON = argparse.ArgumentParser(add_help=False)
COMMON.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                    help="以 JSON 输出（stdout 只有一个 JSON 文档，供程序解析）")
COMMON.add_argument("--lang", metavar="CODE", default=argparse.SUPPRESS,
                    help="输出语言（en / zh_CN / zh_TW / ja / ko），默认取配置")
COMMON.add_argument("--quiet", "-q", action="store_true", default=argparse.SUPPRESS,
                    help="安静模式（仅输出错误）")
COMMON.add_argument("--debug", action="store_true", default=argparse.SUPPRESS,
                    help="出错时打印完整堆栈")

_ROOT_PARSER: argparse.ArgumentParser | None = None


def _leaf(parent, name, func, command, help_text, **kwargs):
    p = parent.add_parser(name, help=help_text, parents=[COMMON], **kwargs)
    p.set_defaults(func=func, command=command)
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phpvm",
        description="phpvm 命令行入口（无 GUI，供脚本 / AI 工具非交互调用）",
        epilog="提示：先执行 `phpvm schema --json` 读取全部命令的自描述契约。",
        parents=[COMMON],
    )
    parser.add_argument("--version", action="store_true", dest="app_version",
                        help="输出版本信息后退出")
    sub = parser.add_subparsers(dest="group", metavar="<group>")

    # version / env / schema
    _leaf(sub, "version", cmd_version, "version", "输出版本与环境路径信息")
    _leaf(sub, "env", cmd_env, "env", "输出全部服务快照（nginx / PHP / Redis / MySQL）")
    _leaf(sub, "schema", cmd_schema, "schema", "输出全部命令的自描述契约（供 AI 读取）")

    # config
    g = sub.add_parser("config", help="配置读写（config.json）", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "list", cmd_config_list, "config.list", "列出全部端口映射与设置")
    p = _leaf(gs, "get", cmd_config_get, "config.get", "读取配置键（ports.<name> / settings.<key>）")
    p.add_argument("key", help="配置键，如 ports.php82 / settings.lang；ports / settings 取整体")
    p = _leaf(gs, "set", cmd_config_set, "config.set", "写入配置键")
    p.add_argument("key", help="配置键，如 ports.php82 / settings.check_update_on_start")
    p.add_argument("value", help="新值（true/false/null/整数/JSON 数组/字符串）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")

    # php
    g = sub.add_parser("php", help="PHP 版本管理", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(gs, "list", cmd_php_list, "php.list", "列出所有 PHP 版本与运行状态")
    p.add_argument("--no-status", action="store_true", help="不探测运行状态（更快）")
    p.add_argument("--precise", action="store_true", help="精确状态判定（校验进程命令行，较慢）")
    p = _leaf(gs, "status", cmd_php_status, "php.status", "查看单个版本状态")
    p.add_argument("name", help="版本名，如 php82 / 8.2 / 82")
    for act, fn, hlp in (("start", cmd_php_start, "启动版本（name 可为 all）"),
                         ("stop", cmd_php_stop, "停止版本（name 可为 all）"),
                         ("restart", cmd_php_restart, "重启版本（name 可为 all）")):
        p = _leaf(gs, act, fn, f"php.{act}", hlp)
        p.add_argument("name", help="版本名，或 all 表示全部版本")
    p = _leaf(gs, "port", cmd_php_port, "php.port", "查看或修改 FastCGI 端口")
    p.add_argument("name", help="版本名")
    p.add_argument("--set", metavar="PORT", help="设置为新端口（1-65535，不可与其它版本重复）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "ini", cmd_php_ini, "php.ini", "读取关键配置 / 已启用扩展")
    p.add_argument("name", help="版本名")
    p.add_argument("--key", help="只读取指定配置键")
    p.add_argument("--all", action="store_true", help="附带输出 ini 全文")
    p = _leaf(gs, "check", cmd_php_check, "php.check", "版本自检（版本/扩展/配置加载）")
    p.add_argument("name", help="版本名")
    p = _leaf(gs, "ext-list", cmd_php_ext_list, "php.ext-list", "列出本地扩展与启用状态")
    p.add_argument("name", help="版本名")
    p = _leaf(gs, "ext-set", cmd_php_ext_set, "php.ext-set", "启用 / 禁用扩展（写 ini，自动备份）")
    p.add_argument("name", help="版本名")
    p.add_argument("--enable", metavar="A,B", help="要启用的扩展（逗号分隔）")
    p.add_argument("--disable", metavar="A,B", help="要禁用的扩展（逗号分隔）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "xdebug-status", cmd_php_xdebug_status, "php.xdebug-status",
              "查看 Xdebug 调试状态（是否已装 / 已开启 / 端口）")
    p.add_argument("name", help="版本名")
    p = _leaf(gs, "xdebug-enable", cmd_php_xdebug_enable, "php.xdebug-enable",
              "开启 Xdebug 调试（写 ini：加载行 + 调试指令，自动备份并自检）")
    p.add_argument("name", help="版本名")
    p.add_argument("--port", type=int, default=xdebug.DEFAULT_PORT,
                   help=f"调试端口（默认 {xdebug.DEFAULT_PORT}）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "xdebug-disable", cmd_php_xdebug_disable, "php.xdebug-disable",
              "关闭 Xdebug 调试（注释加载行，调试指令保留）")
    p.add_argument("name", help="版本名")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")

    # nginx
    g = sub.add_parser("nginx", help="Nginx 管理", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "status", cmd_nginx_status, "nginx.status", "运行状态与路径信息")
    _leaf(gs, "start", cmd_nginx_start, "nginx.start", "启动 nginx")
    _leaf(gs, "stop", cmd_nginx_stop, "nginx.stop", "停止 nginx")
    _leaf(gs, "reload", cmd_nginx_reload, "nginx.reload", "平滑重载配置")
    _leaf(gs, "test", cmd_nginx_test, "nginx.test", "配置检查（nginx -t）")
    p = _leaf(gs, "logs", cmd_nginx_logs, "nginx.logs", "查看日志尾部")
    p.add_argument("--file", help="日志文件名（默认优先 error.log）")
    p.add_argument("--lines", type=int, default=100, help="尾部行数（默认 100）")
    p.add_argument("--list", action="store_true", help="只列出日志文件")

    # logs（站点与应用日志聚合，只读）
    g = sub.add_parser("logs", help="站点与应用日志聚合（只读）", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "sources", cmd_logs_sources, "logs.sources",
          "列出可查看的日志文件（Nginx / PHP / 站点应用）")
    _p = _leaf(gs, "tail", cmd_logs_tail, "logs.tail", "查看任意日志文件尾部（只读）")
    _p.add_argument("path", help="日志文件绝对路径")
    _p.add_argument("--lines", type=int, default=200, help="尾部行数（默认 200）")

    # overview（首页总览仪表盘，只读快照）
    g = sub.add_parser("overview", help="首页总览仪表盘（只读快照）", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "summary", cmd_overview_summary, "overview.summary",
          "输出环境总览快照（服务状态 / 站点告警 / 健康级别）")

    # backup（环境备份与迁移，仅配置）
    g = sub.add_parser("backup", help="环境备份与迁移（仅配置，不含数据库数据）",
                       parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(gs, "export", cmd_backup_export, "backup.export",
              "导出环境配置（config.json / vhost / nginx.conf / php.ini）为 zip")
    p.add_argument("zip", help="导出目标 zip 路径")
    p = _leaf(gs, "restore", cmd_backup_restore, "backup.restore",
              "从 zip 恢复环境配置（改前备份，nginx -t 失败整体回滚）")
    p.add_argument("zip", help="备份 zip 路径")
    p.add_argument("--dry-run", action="store_true", help="只报告将恢复哪些文件")
    p.add_argument("--yes", action="store_true", help="确认恢复（覆盖当前配置）")

    # project（项目级配置 .phpvm.json，F8）
    g = sub.add_parser("project", help="项目级配置（.phpvm.json）", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(gs, "show", cmd_project_show, "project.show",
              "显示项目配置（--path 或从当前目录向上查找 .phpvm.json）")
    p.add_argument("--path", help="项目配置路径或所在目录")
    p = _leaf(gs, "apply", cmd_project_apply, "project.apply",
              "按项目配置对齐：切站点 PHP / 写 hosts / 启服务")
    p.add_argument("--path", help="项目配置路径或所在目录")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p.add_argument("--hosts", action="store_true", help="写入 hosts（可能需要系统授权）")
    p.add_argument("--start", action="store_true", help="启动配置中列出的服务")

    # adminer（数据库 GUI，F9）
    g = sub.add_parser("adminer", help="Adminer 数据库 GUI（单文件托管）", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "status", cmd_adminer_status, "adminer.status", "查看 Adminer 安装 / 托管状态")
    p = _leaf(gs, "install", cmd_adminer_install, "adminer.install",
              "下载 Adminer 并托管为站点（默认 adminer.test）")
    p.add_argument("--domain", default=adminer.DEFAULT_DOMAIN, help="站点域名（默认 adminer.test）")
    p.add_argument("--php", help="用于托管站点的 PHP 版本名（默认最新 / 在跑的）")
    p.add_argument("--hosts", action="store_true", help="同时写入 hosts（可能需要系统授权）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    _leaf(gs, "open", cmd_adminer_open, "adminer.open", "在浏览器打开 Adminer")

    # site
    g = sub.add_parser("site", help="站点（vhost）管理", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(gs, "list", cmd_site_list, "site.list", "列出全部站点与 hosts/include 状态")
    p.add_argument("--domain", help="按域名过滤")
    p = _leaf(gs, "show", cmd_site_show, "site.show", "查看单个站点详情与配置内容")
    p.add_argument("target", help="域名 / 配置文件名 / 配置文件路径")
    p = _leaf(gs, "render", cmd_site_render, "site.render",
              "只渲染站点配置文本，不写入（预览用）")
    _site_create_args(p)
    p = _leaf(gs, "create", cmd_site_create, "site.create",
              "创建站点：写 vhost → 补 include → nginx -t → hosts → 重载")
    _site_create_args(p)
    p.add_argument("--hosts", action="store_true",
                   help="同时写入 hosts（可能需要系统授权弹窗）")
    p.add_argument("--no-reload", action="store_true", help="创建成功后不重载 nginx")
    p = _leaf(gs, "remove", cmd_site_remove, "site.remove", "删除站点配置（需 --yes，自动备份）")
    p.add_argument("target", help="域名 / 配置文件名 / 配置文件路径")
    p.add_argument("--yes", action="store_true", help="确认删除")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p.add_argument("--no-reload", action="store_true", help="删除成功后不重载 nginx")
    for act, fn, hlp in (("enable", cmd_site_enable, "启用站点"),
                         ("disable", cmd_site_disable, "禁用站点（改名 .conf.disabled）")):
        p = _leaf(gs, act, fn, f"site.{act}", hlp)
        p.add_argument("target", help="域名 / 配置文件名 / 配置文件路径")
    for act, fn, hlp in (
            ("secure", cmd_site_secure, "为既有站点启用 HTTPS（生成证书 + 追加 443 server 块）"),
            ("unsecure", cmd_site_unsecure, "关闭既有站点的 HTTPS（移除 443 server 块）")):
        p = _leaf(gs, act, fn, f"site.{act}", hlp)
        p.add_argument("target", help="域名 / 配置文件名 / 配置文件路径")
        p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "php", cmd_site_php, "site.php", "切换站点使用的 PHP 版本（改 fastcgi_pass）")
    p.add_argument("target", help="域名 / 配置文件名 / 配置文件路径")
    p.add_argument("--php", required=True, help="目标 PHP 版本名")
    p = _leaf(gs, "sync-port", cmd_site_sync_port, "site.sync-port",
              "一键把引用旧端口的 fastcgi_pass 替换为新端口")
    p.add_argument("--old", required=True, help="旧端口")
    p.add_argument("--new", required=True, help="新端口")
    p.add_argument("--reload", action="store_true", help="同步成功后平滑重载 nginx")
    p.add_argument("--dry-run", action="store_true", help="只列出将被修改的文件")

    # diag（一键体检 / 502 诊断，只读）
    g = sub.add_parser("diag", help="一键体检 / 502 诊断（只读）", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(gs, "site", cmd_diag_site, "diag.site", "诊断单个站点的 502 原因")
    p.add_argument("target", help="域名 / 配置文件名 / 配置文件路径")
    _leaf(gs, "all", cmd_diag_all, "diag.all", "对全部站点做只读体检")

    # hosts
    g = sub.add_parser("hosts", help="hosts 映射管理", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(gs, "status", cmd_hosts_status, "hosts.status", "查询域名映射")
    p.add_argument("domains", nargs="+", help="域名列表")
    p = _leaf(gs, "add", cmd_hosts_add, "hosts.add", "写入映射（只动 phpvm 托管块）")
    p.add_argument("domains", nargs="+", help="域名列表")
    p.add_argument("--ip", default="127.0.0.1", help="目标 IP（默认 127.0.0.1）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "remove", cmd_hosts_remove, "hosts.remove", "移除托管块内的映射")
    p.add_argument("domains", nargs="+", help="域名列表")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "restore", cmd_hosts_restore, "hosts.restore",
              "用写入前的备份还原 hosts（整文件覆盖）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p.add_argument("--yes", action="store_true", help="确认覆盖当前 hosts")

    # redis
    g = sub.add_parser("redis", help="Redis 管理", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(gs, "list", cmd_redis_list, "redis.list", "列出实例")
    p = _leaf(gs, "status", cmd_redis_status, "redis.status", "实例状态")
    p.add_argument("name", nargs="?", default="", help="实例名（默认第一个）")
    for act, fn, hlp in (("start", cmd_redis_start, "启动实例"),
                         ("stop", cmd_redis_stop, "停止实例"),
                         ("restart", cmd_redis_restart, "重启实例")):
        p = _leaf(gs, act, fn, f"redis.{act}", hlp)
        p.add_argument("name", nargs="?", default="", help="实例名（默认第一个）")
    p = _leaf(gs, "ping", cmd_redis_ping, "redis.ping", "PING 测试")
    p.add_argument("name", nargs="?", default="", help="实例名（默认第一个）")
    p = _leaf(gs, "cmd", cmd_redis_cmd, "redis.cmd", "执行单条命令")
    p.add_argument("name", nargs="?", default="", help="实例名（默认第一个）")
    p.add_argument("--command", help="Redis 命令，如 'GET foo'")
    p.add_argument("--db", type=int, default=0, help="逻辑库 0-15（默认 0）")
    p.add_argument("--force", action="store_true",
                   help="允许高危命令（FLUSHALL/FLUSHDB/SHUTDOWN 等）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")

    # mysql
    g = sub.add_parser("mysql", help="MySQL 管理", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "list", cmd_mysql_list, "mysql.list", "列出实例")
    p = _leaf(gs, "status", cmd_mysql_status, "mysql.status", "实例状态")
    p.add_argument("name", nargs="?", default="", help="实例名（默认第一个）")
    for act, fn, hlp in (("start", cmd_mysql_start, "启动实例"),
                         ("stop", cmd_mysql_stop, "停止实例"),
                         ("restart", cmd_mysql_restart, "重启实例")):
        p = _leaf(gs, act, fn, f"mysql.{act}", hlp)
        p.add_argument("name", nargs="?", default="", help="实例名（默认第一个）")
    p = _leaf(gs, "log", cmd_mysql_log, "mysql.log", "查看错误日志尾部")
    p.add_argument("name", nargs="?", default="", help="实例名（默认第一个）")
    p.add_argument("--lines", type=int, default=100, help="尾部行数（默认 100）")

    # sqlite
    g = sub.add_parser("sqlite", help="SQLite 只读查询", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(gs, "tables", cmd_sqlite_tables, "sqlite.tables", "列出表 / 视图")
    p.add_argument("path", help="数据库文件路径")
    p = _leaf(gs, "columns", cmd_sqlite_columns, "sqlite.columns", "查看表结构")
    p.add_argument("path", help="数据库文件路径")
    p.add_argument("table", help="表名")
    p = _leaf(gs, "query", cmd_sqlite_query, "sqlite.query",
              "执行只读查询（SELECT / WITH / PRAGMA / EXPLAIN / VALUES）")
    p.add_argument("path", help="数据库文件路径")
    p.add_argument("sql", help="SQL 语句（建议加引号）")
    p.add_argument("--limit", type=int, default=200, help="返回行数上限（默认 200）")
    p.add_argument("--offset", type=int, default=0, help="跳过前 N 行")

    # services
    g = sub.add_parser("services", help="整套服务编排", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(gs, "start-all", cmd_services_start, "services.start-all",
              "按序启动 PHP → Redis → MySQL → Nginx")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p.add_argument("--php", metavar="SCOPE|NAMES", default=None,
                   help="PHP 启动范围：newest（仅最新）/ used（站点引用+最新）"
                        "/ active（跟随 cmd 生效版本）/ all（全部，默认），"
                        "或逗号分隔的版本名（如 php82,php85）")
    p = _leaf(gs, "stop-all", cmd_services_stop, "services.stop-all", "停止全部服务")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")

    # update
    g = sub.add_parser("update", help="软件更新", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "check", cmd_update_check, "update.check", "检查 GitHub Releases 最新版本")
    p = _leaf(gs, "download", cmd_update_download, "update.download",
              "下载本平台安装包（SHA-256 校验）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")

    # tune
    g = sub.add_parser("tune", help="开发环境配置推荐（PHP / Nginx）", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(gs, "suggest", cmd_tune_suggest, "tune.suggest",
              "按本机硬件列出开发环境推荐值（只读，不写文件）")
    p.add_argument("--target", choices=["php", "nginx"], required=True,
                   help="目标：php / nginx")
    p.add_argument("--name", help="PHP 版本名（--target php；省略时若只有一个版本则用它）")
    p.add_argument("--only", help="只看指定项（逗号分隔键名）")
    p = _leaf(gs, "apply", cmd_tune_apply, "tune.apply",
              "写入建议项（自动备份 .bak；nginx 写入后跑 nginx -t，失败自动还原）")
    p.add_argument("--target", choices=["php", "nginx"], required=True,
                   help="目标：php / nginx")
    p.add_argument("--name", help="PHP 版本名（--target php）")
    p.add_argument("--items", help="要写入的键名（逗号分隔）；省略表示全部建议")
    p.add_argument("--reload", action="store_true",
                   help="（--target nginx）写入成功后平滑重载")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")

    # module
    g = sub.add_parser("module", help="功能模块开关", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "list", cmd_module_list, "module.list", "列出模块与启用状态")
    p = _leaf(gs, "enable", cmd_module_enable, "module.enable", "启用模块")
    p.add_argument("keys", nargs="+", help="模块 key：redis/mysql/sqlite/log")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "disable", cmd_module_disable, "module.disable", "停用模块（重启 GUI 生效）")
    p.add_argument("keys", nargs="+", help="模块 key：redis/mysql/sqlite/log")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")

    return parser


def _site_create_args(p: argparse.ArgumentParser) -> None:
    """site render / create 共用参数。"""
    p.add_argument("--domain", required=True, help="域名，可多个（空格分隔，支持 *.dev 泛解析）")
    p.add_argument("--root", required=True, help="项目目录（Laravel/ThinkPHP 自动拼 /public）")
    p.add_argument("--template", default="laravel",
                   help="模板：laravel / wordpress / thinkphp / php / static / spa")
    p.add_argument("--php", help="PHP 版本名（决定 fastcgi_pass 端口）")
    p.add_argument("--port", help="直接指定 FastCGI 端口（与 --php 二选一）")
    p.add_argument("--no-php", action="store_true", help="不使用 PHP（静态 / 纯前端站点）")
    p.add_argument("--conf", help="配置文件名（默认 <安全域名>.conf）")
    p.add_argument("--https", action="store_true", help="生成 443 变体（需 openssl / mkcert）")
    p.add_argument("--dry-run", action="store_true", help="只输出将要写入的配置，不落盘")


# --------------------------------------------------------------------------- #
# 输出与入口
# --------------------------------------------------------------------------- #
#: 步骤标记 → 终端编码放不下时的 ASCII 降级（Windows GBK 控制台下 print 会抛
#: UnicodeEncodeError，例如 `site create` 输出的 ✓ / ✗）
_MARK_FALLBACK = {"✓": "[OK]  ", "✗": "[FAIL]", "●": "*", "○": "o"}


def _stdout_can_encode(text: str) -> bool:
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _out(text: str) -> None:
    """打印一行；终端编码不支持其中的符号时降级为 ASCII，绝不因编码崩溃。"""
    if _stdout_can_encode(text):
        print(text)
        return
    safe = text
    for mark, fallback in _MARK_FALLBACK.items():
        safe = safe.replace(mark, fallback)
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(safe.encode(enc, "replace").decode(enc, "replace"))


def _render(res: Result, command: str, as_json: bool, quiet: bool) -> None:
    if as_json:
        payload = {"ok": res.ok, "command": command, "data": res.data}
        if res.notes:
            payload["warnings"] = res.notes
        _out(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if quiet:
        if not res.ok:
            for line in res.lines:
                _out(line)
        return
    for note in res.notes:
        _out("提示：" + note)
    for line in res.lines:
        _out(line)
    if not res.lines and res.data:
        _out(json.dumps(res.data, ensure_ascii=False, indent=2, default=str))
    if not res.lines and not res.data:
        _out("完成。")


def main(argv: list | None = None) -> int:
    global _ROOT_PARSER
    parser = build_parser()
    _ROOT_PARSER = parser
    args = parser.parse_args(argv)

    from core.i18n import set_language

    lang = _flag(args, "lang", None)
    if lang:
        set_language(lang)
    else:
        try:
            set_language(Config().get_lang())
        except Exception:  # noqa: BLE001 —— 语言探测失败不影响 CLI
            set_language("zh_CN")

    as_json = _flag(args, "json", False)
    quiet = _flag(args, "quiet", False)
    debug = _flag(args, "debug", False)

    if getattr(args, "app_version", False):
        res = Result()
        cmd_version(args, res)
        _render(res, "version", as_json, quiet)
        return 0 if res.ok else 1

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 2

    res = Result()
    command = getattr(args, "command", args.group or "")
    started = time.time()
    try:
        func(args, res)
    except KeyboardInterrupt:
        res.ok = False
        res.code = 130
        res.data["error"] = "已中断"
        res.say("已中断")
    except Exception as e:  # noqa: BLE001 —— CLI 兜底：转成 ok=false 而非抛栈
        res.ok = False
        res.data["error"] = f"{type(e).__name__}: {e}"
        res.say(f"失败：{type(e).__name__}: {e}")
        if debug:
            import traceback
            traceback.print_exc()
    res.data.setdefault("elapsed_ms", int((time.time() - started) * 1000))
    try:
        _render(res, command, as_json, quiet)
    except BrokenPipeError:
        # 输出被 `| head` 之类的下游截断：静默收尾，不把栈抛给调用方
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 0 if res.ok else 1
    return res.code or (0 if res.ok else 1)


if __name__ == "__main__":
    sys.exit(main())
