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
7. **第三方依赖可选**：只有 ``ssh`` 命令组需要 ``paramiko``（``requirements.txt``），
   且在 ``core/ssh_manager.py`` 内延迟导入 —— 未安装时其余命令照常运行，``ssh`` 返回
   ``data.error`` 并给出安装方式。密码 / 私钥口令只从 stdin 读（``--password-stdin`` /
   ``--passphrase-stdin``），绝不进命令行。

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
    mail_catcher,
    modules,
    overview,
    plugins,
    project_config,
    resource_monitor,
    site_service,
    site_templates,
    updater,
    version,
    xdebug,
)
from core import config as config_mod  # noqa: E402
from core.plugins.access_insights import aggregate as access_insights  # noqa: E402
from core.plugins.git_status import repos as git_repos  # noqa: E402
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
from core.service_group import (  # noqa: E402
    PHP_SCOPES,
    ServiceGroup,
    scope_label,
    sync_crash_watch,
)
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
        "error": v.error,
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


def _mysql_dict(inst, mgr: MysqlManager | None = None) -> dict:
    """实例 → JSON 契约；复用已构造的 manager 可避免重复探测实例列表。"""
    mgr = mgr or MysqlManager()
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
        mysql_mgr = MysqlManager()
        data["mysql"] = [_mysql_dict(i, mysql_mgr) for i in mysql_mgr.instances]
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
        mark = "●" if v.running else ("!" if v.error else "○")
        r.say(f"{mark} {v.name} · PHP {v.display or '?'} · "
              f"端口 {v.port}" + (f" · PID {v.pid}" if v.pid else ""))
        if v.error:
            r.say(f"    ⚠ {t('运行依赖异常：{err}', err=v.error)}")
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
    mark = "●" if v.running else ("!" if v.error else "○")
    r.say(f"{mark} {v.name} · PHP {v.display or '?'} · 端口 {v.port}"
          + (f" · PID {v.pid}" if v.pid else ""))
    if v.error:
        r.say(t("运行依赖异常：{err}", err=v.error))
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
    done: list[str] = []
    for v in targets:
        try:
            msg = getattr(pm, action)(v)
        except Exception as e:  # noqa: BLE001 —— 单版本失败不影响其它版本
            ok_all = False
            results.append({"name": v.name, "ok": False, "message": str(e)})
        else:
            done.append(v.name)
            results.append({"name": v.name, "ok": True, "message": msg})
    # 与 GUI 一致：停止后解除崩溃自愈看护，否则守护进程会把它们重新拉起
    sync_crash_watch(done, action != "stop")
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
# insights（nginx access.log 聚合，只读）
# --------------------------------------------------------------------------- #
def _fmt_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def cmd_insights_files(a, r: Result) -> Result:
    files = access_insights.access_logs()
    r.data = {"dir": access_insights.logs_dir(), "files": files}
    if not files:
        r.say(t("未找到 nginx access 日志"))
        return r
    r.say(t("可分析的 access 日志："))
    for it in files:
        r.say(f"  {it['name']}  ({_fmt_size(it['size'])})  ->  {it['path']}")
    return r


def cmd_insights_report(a, r: Result) -> Result:
    rep = access_insights.report(a.file or "", lines=a.lines, top=a.top)
    if not rep.get("ok"):
        if rep.get("error") == "not_found":
            return r.fail(t("未找到该 access 日志：{name}", name=a.file),
                          available=rep.get("available") or [])
        return r.fail(t("未找到 nginx access 日志（目录：{dir}）",
                        dir=rep.get("dir") or ""))
    r.data = {k: v for k, v in rep.items() if k != "ok"}

    errors = r.data.get("errors") or []
    if a.errors:
        r.say(t("错误请求（最近 {n} 条）：", n=len(errors)))
        for e in errors:
            r.say(f"  {e['time']}  {e['status']}  {e['method']} {e['path']}")
        return r

    r.say(t("报告文件：{file}", file=r.data["file"]))
    r.say(t("读取 {read} 行，解析 {parsed} 行（跳过 {skipped}）",
            read=r.data["read"], parsed=r.data["parsed"],
            skipped=r.data["skipped"]))
    r.say(t("请求 {n} · 独立 IP {ips} · 5xx 占比 {rate}",
            n=r.data["requests"], ips=r.data["unique_ips"],
            rate=f'{r.data["error_rate"]:.1f}%'))
    if r.data["status_codes"]:
        r.say(t("状态码：") + "  " + "  ".join(
            f'{it["code"]}×{it["count"]} ({it["pct"]:.1f}%)'
            for it in r.data["status_codes"]))
    if r.data["methods"]:
        r.say(t("方法：") + "  " + "  ".join(
            f'{it["method"]}×{it["count"]}' for it in r.data["methods"]))
    if r.data["top_paths"]:
        r.say(t("热门路径："))
        for i, it in enumerate(r.data["top_paths"], 1):
            r.say(f"  {i:>2}. {it['path']}  ×{it['count']}  ({it['pct']:.1f}%)")
    if r.data["top_ips"]:
        r.say(t("Top IP："))
        for i, it in enumerate(r.data["top_ips"], 1):
            r.say(f"  {i:>2}. {it['ip']}  ×{it['count']}  ({it['pct']:.1f}%)")
    r.say(t("按小时分布：") + "  " + " ".join(
        str(it["count"]) for it in r.data["hourly"]))
    if errors:
        r.say(t("错误请求（最近 {n} 条）：", n=len(errors)))
        for e in errors:
            r.say(f"  {e['time']}  {e['status']}  {e['method']} {e['path']}")
    return r


# --------------------------------------------------------------------------- #
# health（站点巡检：只读 HTTP(S) 探测 nginx vhost 域名）
# --------------------------------------------------------------------------- #
def _site_health():
    from core.plugins.site_health import probe

    return probe


def cmd_health_sites(a, r: Result) -> Result:
    probe = _site_health()
    items = probe.sites(Config())
    r.data = {"total": len(items), "sites": items}
    if not items:
        r.say(t("没有可探测的站点（nginx vhost 里没有 server_name）"))
        return r
    r.say(t("可巡检站点 {n} 个：", n=len(items)))
    for it in items:
        php = f"  [{it['php_version']}]" if it.get("php_version") else ""
        r.say(f"  {it['domain']}{php}")
    return r


def cmd_health_probe(a, r: Result) -> Result:
    probe = _site_health()
    data = probe.report(Config(), scheme=a.scheme, timeout=a.timeout,
                        only=getattr(a, "domain", "") or "",
                        limit=getattr(a, "limit", 0) or 0)
    r.data = data
    if not data["total"]:
        r.say(t("没有匹配的站点（检查 --domain 过滤）"))
        return r
    r.say(t("巡检 {n} 个站点：正常 {up} · 异常 {warn} · 不可用 {down}",
            n=data["total"], up=data["up"], warn=data["warn"],
            down=data["down"]))
    avg = data.get("avg_ms")
    r.say(t("平均耗时：{ms}", ms="—" if avg is None else f"{avg} ms"))
    for it in data["sites"]:
        status = "—" if it.get("status") is None else str(it["status"])
        if it.get("error"):
            note = t(probe.ERROR_MESSAGES.get(it["error"], "网络错误"))
        else:
            note = str(it.get("location") or "")
        size = int(it.get("bytes") or 0)
        r.say(f"  {it['domain']:<28}{status:>5}  {it['ms']:>6} ms  "
              f"{size:>7} B  {note}")
    if data["down"]:
        r.note(t("有 {n} 个站点不可用，先确认 nginx 与对应 PHP 版本已启动",
                 n=data["down"]))
    return r


# --------------------------------------------------------------------------- #
# repos（Git 工作区：只读 status / log，不 fetch / pull / 提交）
# --------------------------------------------------------------------------- #
def _git_repos():
    return git_repos


def cmd_repos_list(a, r: Result) -> Result:
    repos = _git_repos()
    data = repos.report(Config(), only=getattr(a, "only", "") or "",
                        limit=getattr(a, "limit", 0) or 0,
                        timeout=getattr(a, "timeout", repos.DEFAULT_TIMEOUT))
    r.data = data
    if not data["git"]:
        r.note(t(repos.ERROR_MESSAGES["no_git"]))
    if not data["total"]:
        r.say(t("没有匹配的项目（检查 --only 过滤）"))
        return r
    r.say(t("项目 {n} 个：仓库 {repos} · 未提交 {dirty} · 待推送 {ahead} · "
            "待拉取 {behind}",
            n=data["total"], repos=data["repos"], dirty=data["dirty"],
            ahead=data["ahead"], behind=data["behind"]))
    for it in data["items"]:
        name = it.get("name") or ""
        if it.get("error"):
            r.say(f"  {name}  —  {t(repos.ERROR_MESSAGES.get(it['error'], '查询失败'))}")
            continue
        branch = it.get("branch") or "—"
        if not it.get("upstream"):
            branch = t("{branch}（无上游）", branch=branch)
        r.say(f"  {name}  [{branch}]  "
              f"{t(repos.STATE_MESSAGES.get(it.get('status'), '查询失败'))}  "
              f"↑{int(it.get('ahead') or 0)} ↓{int(it.get('behind') or 0)}  "
              f"{it.get('last_time') or '—'}  {it.get('last_subject') or ''}")
    if data["ahead"]:
        r.note(t("有 {n} 个项目存在未推送提交", n=data["ahead"]))
    return r


def cmd_repos_show(a, r: Result) -> Result:
    repos = _git_repos()
    data = repos.report(Config())
    row = repos.find(data.get("items") or [], a.name)
    if row is None:
        return r.fail(t("未找到项目：{name}", name=a.name))
    r.data = row
    r.say(t("项目：{name}", name=row.get("name") or ""))
    r.say(t("路径：{path}", path=row.get("root") or ""))
    if row.get("error"):
        r.say(t("状态：{state}", state=t(
            repos.ERROR_MESSAGES.get(row["error"], "查询失败"))))
        return r
    upstream = row.get("upstream") or ""
    branch = row.get("branch") or "—"
    r.say(t("分支：{branch}", branch=branch + (
        t("（上游 {up}）", up=upstream) if upstream else t("（无上游）"))))
    r.say(t("状态：{state}", state=t(
        repos.STATE_MESSAGES.get(row.get("status"), "查询失败"))))
    r.say(t("改动：暂存 {staged} · 修改 {modified} · 未跟踪 {untracked} · 冲突 {conflict}",
            staged=row.get("staged") or 0, modified=row.get("modified") or 0,
            untracked=row.get("untracked") or 0, conflict=row.get("conflict") or 0))
    r.say(t("领先 / 落后：↑{ahead} ↓{behind}",
            ahead=row.get("ahead") or 0, behind=row.get("behind") or 0))
    if row.get("last_hash"):
        r.say(t("最后提交：{time}  {hash}  {subject}",
                time=row.get("last_time") or "—", hash=row["last_hash"],
                subject=row.get("last_subject") or ""))
    changes = list(row.get("changes") or [])
    if changes:
        limit = max(0, int(getattr(a, "files", 20) or 0))
        shown = changes[:limit] if limit else []
        r.say(t("变更文件（{n}）：", n=len(changes)))
        for path in shown:
            r.say("  " + path)
        if limit and len(changes) > limit:
            r.say(t("  …另有 {n} 个未显示", n=len(changes) - limit))
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
# monitor（轻量资源监控，F12；只读快照 phpvm 管理进程的 CPU/内存）
# --------------------------------------------------------------------------- #
def cmd_monitor_snapshot(a, r: Result) -> Result:
    cfg = Config()
    nginx = NginxManager()
    php = PhpManager(cfg)
    redis = RedisManager() if modules.is_enabled("redis", cfg) else None
    mysql = MysqlManager() if modules.is_enabled("mysql", cfg) else None
    data = resource_monitor.collect_service_metrics(
        cfg, php, nginx, redis, mysql)
    r.data = data
    r.say(t("资源占用快照（仅统计 phpvm 管理的进程）："))
    for svc in data["services"]:
        if not svc["running"]:
            r.say(f"  {svc['name']}: {t('停止')}")
            continue
        rss = resource_monitor.format_rss(svc["rss_bytes"])
        cpu = f"{svc['cpu_percent']:.0f}%" if svc["cpu_percent"] >= 0 else t("未知")
        r.say(f"  {svc['name']}: {t('内存 {mem} · CPU {cpu}', mem=rss, cpu=cpu)}")
    tot = data["totals"]
    tot_rss = resource_monitor.format_rss(tot["rss_bytes"])
    tot_cpu = f"{tot['cpu_percent']:.0f}%" if tot["cpu_percent"] >= 0 else t("未知")
    r.say(t("合计：{n} 个运行中的服务 · 内存 {mem} · CPU {cpu}",
            n=tot["service_count"], mem=tot_rss, cpu=tot_cpu))
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
    for w in res.get("warnings") or []:
        r.note(w)
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
# mail（邮件捕获，F11；sendmail_path 仅 Unix）
# --------------------------------------------------------------------------- #
def _human_size(n: int) -> str:
    """文件体积的可读形式（列表展示用）。"""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def cmd_mail_list(a, r: Result) -> Result:
    mails = mail_catcher.list_mails(limit=a.limit)
    r.data = {"count": len(mails), "mail_dir": mail_catcher.mail_dir(), "mails": mails}
    if not mails:
        r.note(t("暂无捕获到的邮件（邮件目录：{dir}）", dir=mail_catcher.mail_dir()))
        return r
    for m in mails:
        r.say(f"{_human_size(m['size'])}  {m['subject'] or t('（无主题）')}")
        r.say(f"    from {m['from'] or '—'}  →  {m['to'] or '—'}")
        r.say(f"    {m['name']}")
    return r


def cmd_mail_show(a, r: Result) -> Result:
    mails = mail_catcher.list_mails(limit=100000)
    if not mails:
        return r.fail(t("暂无捕获到的邮件"))
    target = (a.target or "latest").strip()
    if target.lower() in ("latest", ""):
        m = mails[0]
    else:
        hits = [x for x in mails if x["name"] == target or x["file"] == target]
        if not hits:
            return r.fail(t("未找到邮件：{name}", name=target),
                          available=[x["name"] for x in mails[:10]])
        m = hits[0]
    r.data = {**m, "content": mail_catcher.read_mail(m["file"])}
    r.say(m["file"])
    r.say(r.data["content"])
    return r


def cmd_mail_clear(a, r: Result) -> Result:
    if not a.yes:
        return r.fail(t("清空捕获到的邮件需加 --yes 确认"))
    n = mail_catcher.clear_mails()
    r.data = {"removed": n, "mail_dir": mail_catcher.mail_dir()}
    r.say(t("已清空 {n} 封邮件", n=n))
    return r


def cmd_mail_status(a, r: Result) -> Result:
    _, v = _php_target(a, r)
    if v is None:
        return r
    st = mail_catcher.status(v)
    r.data = {**st, "php": v.name}
    r.say(t("[{name}] 邮件捕获：{state}", name=v.name,
            state=t("已开启") if st["enabled"] else t("未开启")))
    if st["mode"] == "smtp":
        r.say(t("捕获方式：SMTP 127.0.0.1:{port}（服务{state}；随 phpvm 运行）",
                port=st["sink_port"],
                state=t("运行中") if st["sink_running"] else t("未运行")))
    else:
        r.say(t("捕获方式：sendmail_path 垫片脚本"))
    r.say(t("邮件目录：{dir}（已捕获 {n} 封）", dir=st["mail_dir"], n=st["count"]))
    if st["current"]:
        r.say(st["current"] if st["mode"] == "smtp" else f"sendmail_path = {st['current']}")
    return r


def _mail_toggle(a, r: Result, on: bool) -> Result:
    _, v = _php_target(a, r)
    if v is None:
        return r
    ok, msg, backup = (mail_catcher.enable(v) if on else mail_catcher.disable(v))
    r.ok = bool(ok)
    r.data = {"php": v.name, "enabled": on, "backup": backup,
              "mail_dir": mail_catcher.mail_dir()}
    if not ok:
        r.data["error"] = msg
    r.say(msg)
    return r


def cmd_mail_enable(a, r: Result) -> Result:
    return _mail_toggle(a, r, True)


def cmd_mail_disable(a, r: Result) -> Result:
    return _mail_toggle(a, r, False)


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
    mgr = MysqlManager()
    insts = mgr.refresh_instances()
    r.data = {"count": len(insts), "instances": [_mysql_dict(i, mgr) for i in insts]}
    for i in insts:
        d = _mysql_dict(i, mgr)
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
# plugin / keymouse（内置插件）
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# launcher（项目启动器插件）：命令只来自内置白名单 / 用户自定义，绝不自动执行
# --------------------------------------------------------------------------- #
def _launcher_modules():
    """延迟导入：CLI 启动时不加载插件实现（停用后也不加载其代码）。"""
    from core.plugins.project_launcher import commands, projects
    return projects, commands


def _launcher_editor():
    from core.plugins.project_launcher import editor
    return editor


def _launcher_projects(a):
    projects_mod, _ = _launcher_modules()
    return projects_mod.scan(Config(), www_dir=getattr(a, "www", "") or "")


def _launcher_pick(a, r: Result):
    projects_mod, _ = _launcher_modules()
    found = _launcher_projects(a)
    project = projects_mod.find(found, a.project)
    if project is None:
        r.fail(t("未找到项目：{name}", name=a.project),
               available=[p.name for p in found])
        return None
    return project


def cmd_launcher_list(a, r: Result) -> Result:
    projects_mod, _ = _launcher_modules()
    found = _launcher_projects(a)
    recent = projects_mod.recent_keys(Config())
    found = projects_mod.arrange(found, getattr(a, "sort", "name") or "name",
                                 False, recent)
    if getattr(a, "limit", 0):
        found = found[:a.limit]
    r.data = {"projects": [p.as_dict() for p in found], "count": len(found),
              "www_dir": projects_mod.resolve_www_dir(Config()),
              "recent": recent}
    r.say(t("共 {n} 个项目", n=len(found)))
    for p in found:
        r.say(f"  {p.name}  {p.root}" + (f"  [{p.php}]" if p.php else "")
              + ("  " + " ".join(p.urls) if p.urls else ""))
    return r


def cmd_launcher_set_www(a, r: Result) -> Result:
    projects_mod, _ = _launcher_modules()
    target = "" if getattr(a, "reset", False) else str(a.dir or "")
    path = target or projects_mod.default_www_dir()
    if a.dry_run:
        r.data = {"dry_run": True, "www_dir": path}
        r.say("[dry-run] " + t("将把扫描目录设为：{path}", path=path))
        return r
    saved, err = projects_mod.set_www_dir(Config(), target)
    if err:
        return r.fail(err)
    r.data = {"www_dir": saved}
    r.say(t("已将扫描目录设为：{path}", path=saved))
    return r


def cmd_launcher_set_editor(a, r: Result) -> Result:
    editor_mod = _launcher_editor()
    text = "" if getattr(a, "reset", False) else str(a.command_text or "")
    if a.dry_run:
        r.data = {"dry_run": True, "editor": text}
        r.say("[dry-run] " + t("将把编辑器命令设为：{cmd}", cmd=text or "—"))
        return r
    saved, err = editor_mod.set_template(Config(), text)
    if err:
        return r.fail(err)
    r.data = {"editor": saved}
    r.say(t("已将编辑器命令设为：{cmd}", cmd=saved or "—"))
    return r


def cmd_launcher_editor(a, r: Result) -> Result:
    editor_mod = _launcher_editor()
    project = _launcher_pick(a, r)
    if project is None:
        return r
    result = editor_mod.open_project(project, Config(),
                                     getattr(a, "editor", "") or "",
                                     dry_run=bool(a.dry_run))
    r.data = result
    if not result.get("ok"):
        return r.fail(result.get("error", ""))
    if a.dry_run:
        r.say("[dry-run] " + t("将用编辑器打开：{name}", name=project.name))
    else:
        r.say(t("已用编辑器打开：{name}", name=project.name))
    return r


def cmd_launcher_commands(a, r: Result) -> Result:
    _, launcher_cmds = _launcher_modules()
    project = _launcher_pick(a, r)
    if project is None:
        return r
    items = launcher_cmds.for_project(Config(), project)
    r.data = {"project": project.as_dict(),
              "commands": [{**c, "label": t(c["label"])} for c in items]}
    r.say(t("项目 {name} 可用命令：", name=project.name))
    for c in items:
        r.say(f"  {c['id']:20s} {c['command']}")
    return r


def cmd_launcher_add(a, r: Result) -> Result:
    _, launcher_cmds = _launcher_modules()
    root = ""
    if a.project:
        project = _launcher_pick(a, r)
        if project is None:
            return r
        root = project.root
    if a.dry_run:
        r.data = {"dry_run": True, "label": a.label,
                  "command_text": a.command_text, "project": root}
        r.say("[dry-run] " + t("将添加自定义命令"))
        return r
    entry, err = launcher_cmds.add_custom(Config(), a.label, a.command_text, root)
    if err:
        return r.fail(err)
    r.data = entry
    r.say(t("已添加命令：{name}", name=entry["label"]))
    return r


def cmd_launcher_remove(a, r: Result) -> Result:
    _, launcher_cmds = _launcher_modules()
    if a.dry_run:
        r.data = {"dry_run": True, "id": a.id}
        r.say("[dry-run] " + t("将删除自定义命令"))
        return r
    if not a.yes:
        return r.fail(t("删除需要 --yes"))
    if not launcher_cmds.remove_custom(Config(), a.id):
        return r.fail(t("未找到自定义命令：{id}", id=a.id))
    r.data = {"id": a.id, "removed": True}
    r.say(t("已删除命令"))
    return r


def cmd_launcher_open(a, r: Result) -> Result:
    _, launcher_cmds = _launcher_modules()
    project = _launcher_pick(a, r)
    if project is None:
        return r
    result = launcher_cmds.launch(Config(), project, a.id,
                                  dry_run=bool(a.dry_run))
    r.data = result
    if not result.get("ok"):
        return r.fail(result.get("error", ""))
    if a.dry_run:
        r.say("[dry-run] " + t("将打开终端并运行：{cmd}",
                               cmd=result["command"]))
    else:
        r.say(t("已打开终端：{cmd}", cmd=result["command"]))
    return r


def cmd_launcher_browse(a, r: Result) -> Result:
    _, launcher_cmds = _launcher_modules()
    project = _launcher_pick(a, r)
    if project is None:
        return r
    result = launcher_cmds.open_site(project, getattr(a, "url", "") or "",
                                     dry_run=bool(a.dry_run))
    r.data = result
    if not result.get("ok"):
        return r.fail(result.get("error", ""),
                      available=result.get("available", []))
    if a.dry_run:
        r.say("[dry-run] " + t("将打开浏览器：{url}", url=result["url"]))
    else:
        r.say(t("已打开站点：{url}", url=result["url"]))
    return r


_KM_RANGES = ("today", "week", "month", "year", "all")


def _plugin_dict(spec) -> dict:
    return {
        "key": spec.key,
        "name": spec.name,
        "required": bool(spec.required),
        "description": spec.description,
        "platforms": list(spec.platforms),
        "has_panel": bool(spec.panel),
        "has_daemon": bool(spec.daemon_script),
        "default_settings": dict(spec.default_settings or {}),
    }


def _plugin_spec(key: str, r: Result):
    spec = plugins.get(key)
    if spec is None:
        r.fail("未找到插件：" + str(key),
               available=[s.key for s in plugins.discover()])
        return None
    return spec


def cmd_plugin_list(a, r: Result) -> Result:
    cfg = Config()
    items = []
    for spec in plugins.discover():
        item = _plugin_dict(spec)
        item["enabled"] = modules.is_enabled(spec.key, cfg)
        item["supported"] = spec.platform_supported(sys.platform)
        items.append(item)
    r.data = {"count": len(items), "plugins": items}
    for item in items:
        mark = "●" if item["enabled"] else "○"
        extra = "" if item["supported"] else t("（当前平台不支持）")
        r.say(f"{mark} {item['key']} · {t(item['name'])}{extra}")
    return r


def cmd_plugin_status(a, r: Result) -> Result:
    cfg = Config()
    spec = _plugin_spec(a.key, r)
    if spec is None:
        return r
    data = _plugin_dict(spec)
    data["enabled"] = modules.is_enabled(spec.key, cfg)
    data["supported"] = spec.platform_supported(sys.platform)
    if spec.key == "keymouse":
        from core.plugins.keymouse import status

        data["status"] = status(cfg)
    r.data = data
    r.say(f"{spec.key} · {t(spec.name)} · "
          + (t("已启用") if data["enabled"] else t("已停用")))
    if "status" in data:
        st = data["status"]
        r.say(t("后台：{state}", state=t("运行中") if st.get("running")
                else t("未运行"))
              + (f" · PID {st.get('pid')}" if st.get("pid") else ""))
    return r


def _plugin_toggle(a, r: Result, enable: bool) -> Result:
    cfg = Config()
    spec = _plugin_spec(a.key, r)
    if spec is None:
        return r
    if spec.required:
        return r.fail(t("{name} 是刚需模块，不能停用", name=t(spec.name)))
    disabled = modules.disabled_modules(cfg)
    if enable:
        disabled = [k for k in disabled if k != spec.key]
    elif spec.key not in disabled:
        disabled.append(spec.key)
    action = t("启用") if enable else t("停用")
    if a.dry_run:
        r.data = {"dry_run": True, "key": spec.key, "enabled": enable}
        r.say(t("[dry-run] 将{action}插件 {key}", action=action, key=spec.key))
        return r
    modules.set_disabled(cfg, disabled)
    r.data = {"key": spec.key, "enabled": enable, "restart_required": True}
    r.say(t("插件 {key} 已{action}，重启 phpvm 后生效", key=spec.key, action=action))
    return r


def cmd_plugin_enable(a, r: Result) -> Result:
    return _plugin_toggle(a, r, True)


def cmd_plugin_disable(a, r: Result) -> Result:
    return _plugin_toggle(a, r, False)


def _keymouse():
    from core.plugins.keymouse import stats

    return stats


def cmd_keymouse_status(a, r: Result) -> Result:
    from core.plugins.keymouse import status

    data = status(Config())
    r.data = data
    r.say(t("平台支持：{v}", v=t("是") if data.get("supported") else t("否")))
    if data.get("supported"):
        r.say(t("后台：{state}", state=t("运行中") if data.get("running")
                else t("未运行"))
              + (f" · PID {data.get('pid')}" if data.get("pid") else "")
              + (t(" · 已暂停") if data.get("paused") else ""))
        perm = data.get("permission") or {}
        if perm.get("required"):
            r.say(t("输入监控授权：{v}",
                    v=t("已授权") if perm.get("granted") else t("未授权")))
    r.say(t("开机自启：{v}", v=t("是") if data.get("autostart") else t("否")))
    db = data.get("db") or {}
    r.say(t("统计库：{path}（{size} 字节）", path=db.get("path", ""),
            size=db.get("size", 0)))
    return r


def cmd_keymouse_summary(a, r: Result) -> Result:
    stats = _keymouse()
    data = stats.summary(a.range, top=a.top)
    r.data = data
    r.say(stats.report_text(data))
    return r


def cmd_keymouse_export(a, r: Result) -> Result:
    stats = _keymouse()
    if a.dry_run:
        data = stats.summary(a.range, top=a.top)
        r.data = {"dry_run": True, "out": a.out, "range": data["range"],
                  "total_presses": data["total_presses"]}
        r.say(t("[dry-run] 将把 {range} 的统计导出到 {out}",
                range=data["label"], out=a.out))
        return r
    res = stats.export_csv(a.range, a.out, top=a.top)
    if not res.get("ok"):
        return r.fail(str(res.get("message") or "导出失败"))
    r.data = {"path": res["path"], "rows": res["rows"]}
    r.say(t("已导出 {rows} 行到 {path}", rows=res["rows"], path=res["path"]))
    return r


def _keymouse_pause(a, r: Result, paused: bool) -> Result:
    from core.plugins.keymouse import set_paused, supported

    if not supported():
        return r.fail(t("当前平台不支持键盘采集（仅 Windows / macOS）"))
    if a.dry_run:
        r.data = {"dry_run": True, "paused": paused}
        r.say(t("[dry-run] 将{action}键盘采集",
                action=t("暂停") if paused else t("恢复")))
        return r
    state = set_paused(paused)
    r.data = {"paused": state}
    r.say(t("已暂停键盘采集") if state else t("已恢复键盘采集"))
    return r


def cmd_keymouse_pause(a, r: Result) -> Result:
    return _keymouse_pause(a, r, True)


def cmd_keymouse_resume(a, r: Result) -> Result:
    return _keymouse_pause(a, r, False)


def cmd_keymouse_clear(a, r: Result) -> Result:
    from core.plugins.keymouse import store

    if not a.yes:
        return r.fail("清空统计需要 --yes 明确确认")
    if a.dry_run:
        r.data = {"dry_run": True, "action": "clear"}
        r.say("[dry-run] 将删除全部键鼠统计记录")
        return r
    writer = store.Writer()
    try:
        writer.clear()
    finally:
        writer.close()
    r.data = {"cleared": True}
    r.say(t("已清空键鼠统计记录"))
    return r


def cmd_keymouse_daemon(a, r: Result) -> Result:
    from core.plugins.keymouse import daemon, supported

    action = a.daemon_action
    if action == "status":
        r.data = {"running": daemon.is_running(), "pid": daemon.lock_pid(),
                  "supported": supported(),
                  "log": daemon.log_path()}
        r.say(t("后台：{state}", state=t("运行中") if r.data["running"]
                else t("未运行")))
        return r
    if not supported():
        return r.fail(t("当前平台不支持键盘采集（仅 Windows / macOS）"))
    if action == "start":
        if a.dry_run:
            r.data = {"dry_run": True, "action": "start"}
            r.say("[dry-run] 将启动键鼠记录后台进程")
            return r
        ok, msg = daemon.spawn()
    else:
        if a.dry_run:
            r.data = {"dry_run": True, "action": "stop"}
            r.say("[dry-run] 将停止键鼠记录后台进程")
            return r
        ok, msg = daemon.stop()
    r.data = {"running": daemon.is_running(), "pid": daemon.lock_pid(),
              "message": msg}
    if not ok:
        return r.fail(msg)
    r.say(msg)
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

    返回 (php_scope, php_names)：命中策略名时 names 为 None（custom 即
    「按 PHP 页勾选」，名单取自 settings.autostart_php_versions）；给出的是
    版本名（如 php82,php85）时 scope 回落 "all"、由 names 精确指定（未知版本名只告警）。
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
# --------------------------------------------------------------------------- #
# ssh（SSH 主机与命令片段）
# --------------------------------------------------------------------------- #
def _ssh_libs():
    """延迟导入 SSH 模块：未装 paramiko 时其余命令仍可正常使用。"""
    from core import ssh_manager, ssh_store

    return ssh_store, ssh_manager


def _ssh_guard(r: Result) -> bool:
    if not modules.is_enabled("ssh", Config()):
        r.fail(t("SSH 模块已停用（settings.disabled_modules）"))
        return False
    return True


def _ssh_stdin_secret(enabled: bool) -> str:
    """密文只从 stdin 读：避免进命令行历史与进程列表（顺序：先密码、后口令）。"""
    if enabled:
        return sys.stdin.readline().rstrip("\r\n")
    return ""


def _ssh_password(a) -> str:
    return _ssh_stdin_secret(getattr(a, "password_stdin", False))


def _ssh_autofill_key(data: dict, store) -> str:
    """私钥认证没填路径时，用本机 ``~/.ssh`` 里的常用私钥兜底；返回补上的路径。

    只是给表单填默认值：路径会写进主机配置、CLI 与面板都看得见、可随时改掉，
    连接时仍显式传 ``key_filename``（不启 ssh-agent、不隐式试其它密钥）。
    """
    if str(data.get("auth", "password")) != "key":
        return ""
    if str(data.get("key_path") or "").strip():
        return ""
    path = store.default_key_path()
    if path:
        data["key_path"] = path
    return path


def _ssh_host_data(a) -> dict:
    data = {"name": a.name, "host": a.host, "port": a.port, "user": a.user,
            "auth": a.auth or "password", "key_path": a.key_path or "",
            "passphrase": _ssh_stdin_secret(getattr(a, "passphrase_stdin", False)),
            "tags": a.tags, "note": a.note or ""}
    password = _ssh_password(a)
    if password:
        data["password"] = password
    return data


def _ssh_host_args(p, require: bool = False) -> None:
    """host add / update 共用字段（update 不传即不改）。"""
    p.add_argument("--name", default=None, required=require, help="主机名称")
    p.add_argument("--host", metavar="ADDR", default=None, required=require,
                   help="地址（IP 或域名）")
    p.add_argument("--port", type=int, default=22 if require else None,
                   help="端口（默认 22）")
    p.add_argument("--user", default=None, required=require, help="登录用户名")
    p.add_argument("--auth", choices=["password", "key"],
                   default="password" if require else None, help="认证方式")
    p.add_argument("--key-path", default=None, help="私钥文件路径（--auth key）")
    p.add_argument("--passphrase-stdin", action="store_true",
                   help="从 stdin 读取一行作为私钥口令（--auth key，可选；不进命令行）")
    p.add_argument("--tags", default=None, help="标签，逗号分隔")
    p.add_argument("--note", default=None, help="备注")
    p.add_argument("--password-stdin", action="store_true",
                   help="从 stdin 读取一行作为密码（避免进命令行历史）")


def cmd_ssh_host_list(a, r: Result) -> Result:
    store, _mgr = _ssh_libs()
    if not _ssh_guard(r):
        return r
    hosts = store.search_hosts(store.load_hosts(), a.query or "", a.tag or "")
    r.data = {"count": len(hosts), "hosts": [store.public(h) for h in hosts]}
    for h in hosts:
        r.say(f"{h['name']} · {h['user']}@{h['host']}:{h['port']} · {h['auth']}")
    if not hosts:
        r.say(t("没有匹配的主机"))
    return r


def cmd_ssh_host_add(a, r: Result) -> Result:
    store, _mgr = _ssh_libs()
    if not _ssh_guard(r):
        return r
    data = _ssh_host_data(a)
    picked = _ssh_autofill_key(data, store)
    err = store.validate_host(data)
    if err:
        return r.fail(err)
    if a.dry_run:
        r.data = {"dry_run": True, "host": store.public(data)}
        r.say(t("[dry-run] 将添加主机：{name}（{user}@{host}:{port}）",
                name=data["name"], user=data["user"],
                host=data["host"], port=data["port"]))
        if picked:
            r.say(t("未指定私钥，将自动选用默认私钥：{path}", path=picked))
        return r
    host, err = store.add_host(data)
    if err:
        return r.fail(err)
    r.data = {"host": store.public(host)}
    r.say(t("已添加主机：{name}", name=host["name"]))
    if picked:
        r.note(t("未指定私钥，已自动选用默认私钥：{path}", path=picked))
    if store.missing_secret(host):
        # 保存与连通性解耦：先登记、后补凭据，不算失败
        r.note(t("已保存，但还没有密码或私钥，连接前补齐即可"))
    return r


def cmd_ssh_host_update(a, r: Result) -> Result:
    store, _mgr = _ssh_libs()
    if not _ssh_guard(r):
        return r
    patch = {}
    for key in ("name", "host", "port", "user", "auth", "key_path",
                "note", "tags"):
        val = getattr(a, key, None)
        if val not in (None, ""):
            patch[key] = val
    # 密码 / 口令只走 stdin，且永不回显
    password = _ssh_password(a)
    if password:
        patch["password"] = password
    passphrase = _ssh_stdin_secret(getattr(a, "passphrase_stdin", False))
    if passphrase:
        patch["passphrase"] = passphrase
    if not patch:
        return r.fail(t("没有要更新的字段"))
    # 只改认证方式、没给私钥路径时同样兜底（只改备注等字段则不碰已有路径）
    picked = _ssh_autofill_key(patch, store) if "auth" in patch else ""
    safe = {k: v for k, v in patch.items() if k not in ("password", "passphrase")}
    if "password" in patch:
        safe["password_changed"] = True
    if "passphrase" in patch:
        safe["passphrase_changed"] = True
    if a.dry_run:
        r.data = {"dry_run": True, "target": a.target, "patch": safe}
        r.say(t("[dry-run] 将更新主机：{target}", target=a.target))
        if picked:
            r.say(t("未指定私钥，将自动选用默认私钥：{path}", path=picked))
        return r
    host, err = store.update_host(a.target, patch)
    if err:
        return r.fail(err)
    r.data = {"host": store.public(host), "patch": safe}
    r.say(t("已更新主机：{name}", name=host["name"]))
    if picked:
        r.note(t("未指定私钥，已自动选用默认私钥：{path}", path=picked))
    if store.missing_secret(host):
        r.note(t("已保存，但还没有密码或私钥，连接前补齐即可"))
    return r


def cmd_ssh_host_remove(a, r: Result) -> Result:
    store, _mgr = _ssh_libs()
    if not _ssh_guard(r):
        return r
    if not a.yes:
        return r.fail(t("删除主机需要 --yes 确认"))
    if a.dry_run:
        r.data = {"dry_run": True, "target": a.target}
        r.say(t("[dry-run] 将删除主机：{target}", target=a.target))
        return r
    if not store.remove_host(a.target):
        return r.fail(t("未找到主机：{name}", name=a.target))
    r.data = {"removed": a.target}
    r.say(t("已删除主机：{target}", target=a.target))
    return r


def _ssh_pick_host(store, ref: str, r: Result):
    host = store.find_host(store.load_hosts(), ref)
    if host is None:
        r.fail(t("未找到主机：{name}", name=ref),
               available=[h["name"] for h in store.load_hosts()])
    return host


def cmd_ssh_host_test(a, r: Result) -> Result:
    store, mgr = _ssh_libs()
    if not _ssh_guard(r):
        return r
    host = _ssh_pick_host(store, a.target, r)
    if host is None:
        return r
    res = mgr.test_connection(host, timeout=a.timeout, trust=a.trust_host)
    r.data = res
    r.ok = bool(res["ok"])
    if r.ok:
        r.say(t("连接成功：{target} · {ms} ms · {server}",
                target=res["target"], ms=res["latency_ms"],
                server=res["server"] or "—"))
        if res["fingerprint"]:
            r.say(t("服务端指纹：{fp}", fp=res["fingerprint"]))
    else:
        r.say(t("失败：{err}", err=res.get("error") or t("未知错误")))
        if res.get("error_kind") == "host_key" and res.get("fingerprint"):
            r.note(t("确认指纹无误后加 --trust-host 再试一次"))
    return r


def cmd_ssh_host_connect(a, r: Result) -> Result:
    store, mgr = _ssh_libs()
    if not _ssh_guard(r):
        return r
    host = _ssh_pick_host(store, a.target, r)
    if host is None:
        return r
    command = mgr.terminal_command(host)
    if a.dry_run:
        r.data = {"dry_run": True, "command": command}
        r.say("[dry-run] " + command)
        return r
    ok = mgr.open_terminal(host)
    r.data = {"opened": ok, "command": command, "auth": host["auth"]}
    if not ok:
        return r.fail(t("未能打开终端（请确认系统自带 ssh 客户端）"))
    r.say(t("已打开终端：{cmd}", cmd=command))
    if host["auth"] == "password":
        r.note(t("密码认证需在终端窗口内输入"))
    return r

def _ssh_snippet_host_ids(store, refs) -> tuple:
    """把主机名 / id 解析成 host_ids；任一未知即失败（避免静默丢关联）。"""
    hosts = store.load_hosts()
    ids = []
    for ref in refs or []:
        host = store.find_host(hosts, ref)
        if host is None:
            return [], t("未找到主机：{name}", name=ref)
        if host["id"] not in ids:
            ids.append(host["id"])
    return ids, ""


def cmd_ssh_snippet_list(a, r: Result) -> Result:
    store, _mgr = _ssh_libs()
    if not _ssh_guard(r):
        return r
    cfg = Config()
    host = None
    if a.host:
        host = _ssh_pick_host(store, a.host, r)
        if host is None:
            return r
    items = store.snippets_for(cfg, host=host, query=a.query or "", tag=a.tag or "")
    r.data = {"count": len(items), "snippets": items}
    for s in items:
        scope = (t("通用") if not s["host_ids"]
                 else t("{n} 台主机", n=len(s["host_ids"])))
        head = s["command"].strip().splitlines()[0][:60] if s["command"].strip() else ""
        r.say(f"{s['name']} · {scope} · {head}")
    if not items:
        r.say(t("没有匹配的命令片段"))
    return r


def cmd_ssh_snippet_add(a, r: Result) -> Result:
    store, _mgr = _ssh_libs()
    if not _ssh_guard(r):
        return r
    ids, err = _ssh_snippet_host_ids(store, a.host)
    if err:
        return r.fail(err)
    data = {"name": a.name, "command": a.command_text, "host_ids": ids,
            "tags": a.tags, "note": a.note or ""}
    err = store.validate_snippet(data)
    if err:
        return r.fail(err)
    if a.dry_run:
        r.data = {"dry_run": True, "snippet": data}
        r.say(t("[dry-run] 将添加命令片段：{name}", name=a.name))
        return r
    snip, err = store.add_snippet(Config(), data)
    if err:
        return r.fail(err)
    r.data = {"snippet": snip}
    r.say(t("已添加命令片段：{name}", name=snip["name"]))
    return r


def cmd_ssh_snippet_update(a, r: Result) -> Result:
    store, _mgr = _ssh_libs()
    if not _ssh_guard(r):
        return r
    patch = {}
    for key in ("name", "note"):
        val = getattr(a, key, None)
        if val:
            patch[key] = val
    if a.command_text:
        patch["command"] = a.command_text
    if a.tags:
        patch["tags"] = a.tags
    if a.host:
        ids, err = _ssh_snippet_host_ids(store, a.host)
        if err:
            return r.fail(err)
        patch["host_ids"] = ids
    elif a.clear_hosts:
        patch["host_ids"] = []
    if not patch:
        return r.fail(t("没有要更新的字段"))
    if a.dry_run:
        r.data = {"dry_run": True, "target": a.target, "patch": patch}
        r.say(t("[dry-run] 将更新命令片段：{target}", target=a.target))
        return r
    snip, err = store.update_snippet(Config(), a.target, patch)
    if err:
        return r.fail(err)
    r.data = {"snippet": snip}
    r.say(t("已更新命令片段：{name}", name=snip["name"]))
    return r


def cmd_ssh_snippet_remove(a, r: Result) -> Result:
    store, _mgr = _ssh_libs()
    if not _ssh_guard(r):
        return r
    if not a.yes:
        return r.fail(t("删除命令片段需要 --yes 确认"))
    if a.dry_run:
        r.data = {"dry_run": True, "target": a.target}
        r.say(t("[dry-run] 将删除命令片段：{target}", target=a.target))
        return r
    if not store.remove_snippet(Config(), a.target):
        return r.fail(t("未找到命令片段：{name}", name=a.target))
    r.data = {"removed": a.target}
    r.say(t("已删除命令片段：{target}", target=a.target))
    return r


def cmd_ssh_snippet_run(a, r: Result) -> Result:
    store, mgr = _ssh_libs()
    if not _ssh_guard(r):
        return r
    cfg = Config()
    snip = store.find_snippet(store.load_snippets(cfg), a.target)
    if snip is None:
        return r.fail(t("未找到命令片段：{name}", name=a.target))
    host = _ssh_pick_host(store, a.host, r)
    if host is None:
        return r
    label = mgr.target_label(host)
    if a.dry_run:
        r.data = {"dry_run": True, "snippet": snip["name"], "host": host["name"],
                  "target": label, "command": snip["command"]}
        r.say(t("[dry-run] 将在 {target} 执行：{cmd}",
                target=label, cmd=snip["command"]))
        return r
    if not a.yes:
        return r.fail(t("在远端执行命令属有副作用操作，需要 --yes 确认"
                        "（可先用 --dry-run 预览）"), target=label)
    res = mgr.exec_command(host, snip["command"], timeout=a.timeout,
                           trust=a.trust_host)
    r.data = res
    r.ok = bool(res["ok"])
    if res["stdout"].strip():
        r.say(res["stdout"].rstrip())
    if res["stderr"].strip():
        r.say("[stderr] " + res["stderr"].rstrip())
    if res["truncated"]:
        r.note(t("输出超过上限已截断"))
    if not r.ok:
        err = res.get("error") or t("远端命令退出码为 {code}", code=res["exit_status"])
        r.say(t("失败：{err}", err=err))
    return r


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

    # insights（nginx access.log 聚合，只读）
    g = sub.add_parser("insights", help="访问日志聚合（只读解析 nginx access.log）",
                       parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "files", cmd_insights_files, "insights.files",
          "列出可分析的 nginx access 日志")
    _p = _leaf(gs, "report", cmd_insights_report, "insights.report",
               "聚合统计 access.log（状态码 / 热门路径 / 错误请求）")
    _p.add_argument("--file", default="", help="日志文件名（默认 access.log）")
    _p.add_argument("--lines", type=int, default=20000,
                    help="统计尾部行数（默认 20000）")
    _p.add_argument("--top", type=int, default=10, help="Top N（默认 10）")
    _p.add_argument("--errors", action="store_true",
                    help="只输出错误请求（4xx / 5xx）")

    # health（站点巡检：只读 HTTP(S) 探测 vhost 域名，不跟随重定向）
    g = sub.add_parser("health", help="站点巡检（只读 HTTP(S) 探测 vhost 域名）",
                       parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "sites", cmd_health_sites, "health.sites",
          "列出可巡检的站点域名（不发起请求）")
    _p = _leaf(gs, "probe", cmd_health_probe, "health.probe",
               "探测站点可用性（状态码 / 耗时 / 证书剩余天数）")
    _p.add_argument("--domain", default="", help="按域名子串过滤")
    _p.add_argument("--scheme", choices=("http", "https"), default="http",
                    help="探测协议（默认 http）")
    _p.add_argument("--timeout", type=float, default=5.0,
                    help="单站点超时秒数（默认 5）")
    _p.add_argument("--limit", type=int, default=0,
                    help="最多探测多少个站点（默认全部）")

    # repos（Git 工作区：只读 status / log）
    g = sub.add_parser("repos", help="Git 工作区（只读 status / log）",
                       parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _p = _leaf(gs, "list", cmd_repos_list, "repos.list",
               "汇总各项目的分支 / 未提交 / 领先落后")
    _p.add_argument("--only", default="", help="按项目名或路径子串过滤")
    _p.add_argument("--limit", type=int, default=0,
                    help="最多查询多少个项目（默认全部）")
    _p.add_argument("--timeout", type=float, default=git_repos.DEFAULT_TIMEOUT,
                    help=f"单仓库超时秒数（默认 {git_repos.DEFAULT_TIMEOUT:g}）")
    _p = _leaf(gs, "show", cmd_repos_show, "repos.show",
               "查看单个项目的详情与变更文件")
    _p.add_argument("name", help="项目名（或路径）")
    _p.add_argument("--files", type=int, default=20,
                    help="最多列出多少个变更文件（默认 20，0 = 不列）")

    # overview（首页总览仪表盘，只读快照）
    g = sub.add_parser("overview", help="首页总览仪表盘（只读快照）", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "summary", cmd_overview_summary, "overview.summary",
          "输出环境总览快照（服务状态 / 站点告警 / 健康级别）")

    # monitor（轻量资源监控，F12；只读快照，不引 psutil）
    g = sub.add_parser("monitor", help="轻量资源监控（CPU / 内存，仅 phpvm 管理进程）",
                       parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "snapshot", cmd_monitor_snapshot, "monitor.snapshot",
          "输出各服务（Nginx / PHP / Redis / MySQL）的 CPU 与内存占用快照")

    # ssh（主机与命令片段；凭据独立存 ssh_hosts.json，不进备份包）
    g = sub.add_parser("ssh", help="SSH 主机与命令片段管理", parents=[COMMON])
    objs = g.add_subparsers(dest="object", metavar="<object>")

    hg = objs.add_parser("host", help="主机配置 / 测试连接 / 打开终端")
    hs = hg.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(hs, "list", cmd_ssh_host_list, "ssh.host.list", "列出主机")
    p.add_argument("--query", default="", help="按名称 / 地址 / 用户 / 标签过滤")
    p.add_argument("--tag", default="", help="按标签过滤")
    p = _leaf(hs, "add", cmd_ssh_host_add, "ssh.host.add",
              "新增主机（密码从 stdin 读，不进命令行历史）")
    _ssh_host_args(p, require=True)
    p.add_argument("--dry-run", action="store_true", help="只校验不写入")
    p = _leaf(hs, "update", cmd_ssh_host_update, "ssh.host.update", "更新主机字段")
    p.add_argument("target", help="主机名或 id")
    _ssh_host_args(p, require=False)
    p.add_argument("--dry-run", action="store_true", help="只报告将改什么")
    p = _leaf(hs, "remove", cmd_ssh_host_remove, "ssh.host.remove",
              "删除主机（需 --yes）")
    p.add_argument("target", help="主机名或 id")
    p.add_argument("--yes", action="store_true", help="确认删除")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(hs, "test", cmd_ssh_host_test, "ssh.host.test", "测试连接（真实握手）")
    p.add_argument("target", help="主机名或 id")
    p.add_argument("--timeout", type=float, default=10.0, help="超时秒数（默认 10）")
    p.add_argument("--trust-host", action="store_true",
                   help="首次连接时信任并记录该主机指纹")
    p = _leaf(hs, "connect", cmd_ssh_host_connect, "ssh.host.connect",
              "用系统终端打开 SSH 会话（密码在终端内输入）")
    p.add_argument("target", help="主机名或 id")
    p.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")

    sg = objs.add_parser("snippet", help="命令片段管理 / 远端执行")
    ss = sg.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(ss, "list", cmd_ssh_snippet_list, "ssh.snippet.list", "列出命令片段")
    p.add_argument("--host", default=None, help="只看该主机可用的片段（含通用片段）")
    p.add_argument("--query", default="", help="按名称 / 命令 / 标签过滤")
    p.add_argument("--tag", default="", help="按标签过滤")
    p = _leaf(ss, "add", cmd_ssh_snippet_add, "ssh.snippet.add", "新增命令片段")
    p.add_argument("--name", required=True, help="片段名称")
    p.add_argument("--command-text", dest="command_text", required=True,
                   help="要执行的命令（用引号包住；不要叫 --command，会覆盖命令标识）")
    p.add_argument("--host", action="append", default=None,
                   help="关联主机名或 id（可重复；留空为通用片段）")
    p.add_argument("--tags", default=None, help="标签，逗号分隔")
    p.add_argument("--note", default=None, help="备注")
    p.add_argument("--dry-run", action="store_true", help="只校验不写入")
    p = _leaf(ss, "update", cmd_ssh_snippet_update, "ssh.snippet.update", "更新命令片段")
    p.add_argument("target", help="片段名或 id")
    p.add_argument("--name", default=None, help="新名称")
    p.add_argument("--command-text", dest="command_text", default=None, help="新命令")
    p.add_argument("--host", action="append", default=None,
                   help="替换关联主机（可重复）")
    p.add_argument("--clear-hosts", action="store_true",
                   help="清空关联（改为通用片段）")
    p.add_argument("--tags", default=None, help="标签，逗号分隔")
    p.add_argument("--note", default=None, help="备注")
    p.add_argument("--dry-run", action="store_true", help="只报告将改什么")
    p = _leaf(ss, "remove", cmd_ssh_snippet_remove, "ssh.snippet.remove",
              "删除命令片段（需 --yes）")
    p.add_argument("target", help="片段名或 id")
    p.add_argument("--yes", action="store_true", help="确认删除")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(ss, "run", cmd_ssh_snippet_run, "ssh.snippet.run",
              "在远端执行片段（需 --yes，可先 --dry-run 预览）")
    p.add_argument("target", help="片段名或 id")
    p.add_argument("--host", required=True, help="目标主机名或 id")
    p.add_argument("--timeout", type=float, default=30.0, help="执行超时秒数（默认 30）")
    p.add_argument("--trust-host", action="store_true", help="首次连接时信任该主机指纹")
    p.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    p.add_argument("--yes", action="store_true", help="确认执行")

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

    # mail（邮件捕获 .eml，F11：Unix 用 sendmail_path 垫片，Windows 用内置 SMTP sink）
    g = sub.add_parser("mail", help="邮件捕获（.eml 落盘：Unix 走 sendmail_path，Windows 走 SMTP sink）",
                       parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(gs, "list", cmd_mail_list, "mail.list", "列出捕获到的邮件")
    p.add_argument("--limit", type=int, default=50, help="最多列出多少封（默认 50）")
    p = _leaf(gs, "show", cmd_mail_show, "mail.show", "查看邮件原文（默认最新一封）")
    p.add_argument("target", nargs="?", default="latest", help="邮件文件名 / 路径，或 latest")
    p = _leaf(gs, "clear", cmd_mail_clear, "mail.clear", "清空捕获到的邮件（需 --yes）")
    p.add_argument("--yes", action="store_true", help="确认清空")
    p = _leaf(gs, "status", cmd_mail_status, "mail.status", "查看某版本的邮件捕获状态")
    p.add_argument("name", help="版本名，如 php82 / 8.2 / 82")
    for act, fn, hlp in (("enable", cmd_mail_enable,
                          "为该版本开启邮件捕获（Unix 写 sendmail_path / Windows 写 SMTP）"),
                         ("disable", cmd_mail_disable, "关闭邮件捕获并还原原有设置")):
        p = _leaf(gs, act, fn, f"mail.{act}", hlp)
        p.add_argument("name", help="版本名")

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

    # plugin（内置插件）
    g = sub.add_parser("plugin", help="内置插件管理", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "list", cmd_plugin_list, "plugin.list", "列出插件与启用状态")
    p = _leaf(gs, "status", cmd_plugin_status, "plugin.status", "插件详情")
    p.add_argument("key", help="插件 key（如 keymouse）")
    for act, fn, hlp in (("enable", cmd_plugin_enable, "启用插件"),
                         ("disable", cmd_plugin_disable, "停用插件")):
        p = _leaf(gs, act, fn, f"plugin.{act}", hlp)
        p.add_argument("key", help="插件 key")
        p.add_argument("--dry-run", action="store_true", help="只报告将做什么")

    # keymouse（键鼠统计插件）
    g = sub.add_parser("keymouse", help="键鼠统计插件", parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    _leaf(gs, "status", cmd_keymouse_status, "keymouse.status",
          "采集状态与统计库概况")
    p = _leaf(gs, "summary", cmd_keymouse_summary, "keymouse.summary",
              "按范围汇总（今日 / 本周 / 本月 / 本年 / 全部）")
    p.add_argument("--range", choices=list(_KM_RANGES), default="today",
                   help="时间范围（默认 today）")
    p.add_argument("--top", type=int, default=10, help="键位排名条数（默认 10）")
    p = _leaf(gs, "export", cmd_keymouse_export, "keymouse.export",
              "导出 CSV 报告")
    p.add_argument("--range", choices=list(_KM_RANGES), default="today",
                   help="时间范围（默认 today）")
    p.add_argument("--out", required=True, help="导出的 CSV 路径")
    p.add_argument("--top", type=int, default=10, help="键位排名条数（默认 10）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    for act, fn, hlp in (("pause", cmd_keymouse_pause, "暂停键盘采集"),
                         ("resume", cmd_keymouse_resume, "恢复键盘采集")):
        p = _leaf(gs, act, fn, f"keymouse.{act}", hlp)
        p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "clear", cmd_keymouse_clear, "keymouse.clear",
              "清空全部统计记录（需 --yes）")
    p.add_argument("--yes", action="store_true", help="确认删除（必填）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = gs.add_parser("daemon", help="后台记录进程", parents=[COMMON])
    p.set_defaults(func=cmd_keymouse_daemon, command="keymouse.daemon")
    ds = p.add_subparsers(dest="daemon_action", metavar="<action>")
    for act, hlp in (("start", "启动后台记录"), ("stop", "停止后台记录"),
                     ("status", "查看后台状态")):
        q = ds.add_parser(act, help=hlp, parents=[COMMON])
        q.set_defaults(func=cmd_keymouse_daemon,
                       command=f"keymouse.daemon.{act}", daemon_action=act)
        q.add_argument("--dry-run", action="store_true", help="只报告将做什么")

    # launcher（项目启动器插件）
    g = sub.add_parser("launcher",
                       help="项目启动器（列出项目 / 打开站点 / 一键终端）",
                       parents=[COMMON])
    gs = g.add_subparsers(dest="action", metavar="<action>")
    p = _leaf(gs, "list", cmd_launcher_list, "launcher.list",
              "列出本地项目（www 目录 + vhost root 去重）")
    p.add_argument("--www", help="覆盖 www 根目录")
    p.add_argument("--sort", choices=["name", "php", "domains", "recent"],
                   default="name", help="排序方式（recent = 最近使用优先）")
    p.add_argument("--limit", type=int, default=0, help="只输出前 N 个")
    p = _leaf(gs, "set-www", cmd_launcher_set_www, "launcher.set-www",
              "把扫描根目录持久化到插件设置")
    p.add_argument("--dir", help="项目扫描目录（绝对路径）")
    p.add_argument("--reset", action="store_true",
                   help="恢复默认（<phpvm>/www）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "set-editor", cmd_launcher_set_editor, "launcher.set-editor",
              "设置打开项目用的编辑器命令（留空恢复自动探测）")
    # dest 必须避开 command：_leaf 用 set_defaults(command=...) 记录命令 id
    p.add_argument("--command", dest="command_text", help="如 code {path}")
    p.add_argument("--reset", action="store_true", help="恢复自动探测")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "editor", cmd_launcher_editor, "launcher.editor",
              "用编辑器 / IDE 打开项目目录")
    p.add_argument("--project", required=True, help="项目名或路径")
    p.add_argument("--editor", help="临时指定编辑器命令（不落盘）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "commands", cmd_launcher_commands, "launcher.commands",
              "列出某项目可用命令")
    p.add_argument("--project", required=True, help="项目名或路径")
    p = _leaf(gs, "add", cmd_launcher_add, "launcher.add",
              "添加自定义命令（不会自动执行）")
    p.add_argument("--label", required=True, help="显示名称")
    # dest 必须避开 command：_leaf 用 set_defaults(command="launcher.add") 记录命令 id
    p.add_argument("--command", dest="command_text", required=True,
                   help="命令文本")
    p.add_argument("--project", help="仅对某项目可见（项目名或路径）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "remove", cmd_launcher_remove, "launcher.remove",
              "删除自定义命令（需 --yes）")
    p.add_argument("--id", required=True, help="命令 id")
    p.add_argument("--yes", action="store_true", help="确认删除（必填）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "browse", cmd_launcher_browse, "launcher.browse",
              "在浏览器打开项目站点（URL 只来自 vhost 配置）")
    p.add_argument("--project", required=True, help="项目名或路径")
    p.add_argument("--url", help="指定站点 URL（见 launcher list 的 urls）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")
    p = _leaf(gs, "open", cmd_launcher_open, "launcher.open",
              "在终端打开项目并运行指定命令")
    p.add_argument("--project", required=True, help="项目名或路径")
    p.add_argument("--id", required=True, help="命令 id（见 launcher commands）")
    p.add_argument("--dry-run", action="store_true", help="只报告将做什么")

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
                        "/ active（跟随 cmd 生效版本）/ custom（按 PHP 页勾选）"
                        "/ all（全部，默认），"
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
