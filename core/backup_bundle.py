# -*- coding: utf-8 -*-
"""环境备份与迁移（F6）：把「整套环境配置」导出为 zip，并支持一键恢复。

边界（重要）：
- **只打包配置，不打包数据库数据文件**（体积大、可能正在写入），
  数据库请用各自官方工具导出；
- 恢复前对每个将被覆盖的文件先做 ``.bak`` 备份，恢复后跑 ``nginx -t``；
  校验失败则**整体回滚**（把备份还原回去），保证不会留下半成品环境；
- 换机后路径不同，故恢复时按「逻辑条目」映射到当前环境的实际路径：
  ``config.json`` / ``<nginx>/nginx.conf`` / ``<nginx>/vhost/<name>`` / ``<ver>/php.ini``；
- hosts 托管块仅导出为 ``hosts.phpvm.txt`` 供人工参考，**恢复时不自动写回**
  （写 hosts 涉及系统提权，且冲突处理需人工确认）。
"""
import json
import os
import sys
import time
import zipfile

from . import file_backup, hosts_manager, version
from .config import Config, default_config_path
from .i18n import t
from .php_manager import PhpManager
from .vhost_manager import VhostManager

MANIFEST = "manifest.json"
HOSTS_REF = "hosts.phpvm.txt"


def _managed_block(text: str) -> str:
    """截取 hosts 中 phpvm 托管块（供导出为参考文本）。"""
    lines: list[str] = []
    inside = False
    for raw in text.splitlines():
        if hosts_manager.MANAGED_TAG in raw and ">>>" in raw:
            inside = True
        if inside:
            lines.append(raw)
        if inside and hosts_manager.MANAGED_TAG in raw and "<<<" in raw:
            inside = False
    return "\n".join(lines)


def _php_versions(config: Config):
    """有实际 php.ini 的 PHP 版本列表。"""
    out = []
    for v in PhpManager(config).scan_versions():
        if getattr(v, "ini", "") and os.path.isfile(v.ini):
            out.append(v)
    return out


def export_bundle(dest: str, config: Config | None = None) -> dict:
    """导出环境配置到 zip。返回 ``{ok, path, counts, manifest, message}``。"""
    config = config or Config()
    vm = VhostManager(config)
    cfg_path = getattr(config, "config_path", "") or default_config_path()

    entries: list[dict] = []  # {kind, arc, src}

    def add(kind: str, arc: str, src: str) -> None:
        if src and os.path.isfile(src):
            entries.append({"kind": kind, "arc": arc, "src": src})

    add("config", "config.json", cfg_path)
    add("nginx.conf", "nginx/nginx.conf", vm.main_conf)
    if os.path.isdir(vm.vhost_dir):
        for name in sorted(os.listdir(vm.vhost_dir)):
            if name.endswith(".conf") or name.endswith(".conf.disabled"):
                add("vhost", "nginx/vhost/" + name, os.path.join(vm.vhost_dir, name))
    php_planes = []
    for v in _php_versions(config):
        arc = f"php/{v.name}/php.ini"
        add("php.ini", arc, v.ini)
        php_planes.append({"version": v.name, "arc": arc})

    try:
        hosts_text = _managed_block(hosts_manager.read_text())
    except OSError:
        hosts_text = ""

    counts = {"config": 0, "nginx.conf": 0, "vhost": 0, "php.ini": 0}
    for e in entries:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1

    manifest = {
        "tool": "phpvm",
        "version": version.version(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": sys.platform,
        "counts": counts,
        "files": [{"kind": e["kind"], "arc": e["arc"]} for e in entries],
        "php": php_planes,
        "note": "仅配置；数据库数据请另行导出",
    }

    dest = os.path.abspath(os.path.expanduser(dest))
    parent = os.path.dirname(dest)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return {"ok": False, "message": t("无法创建目录：{err}", err=e)}
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
            if hosts_text:
                z.writestr(HOSTS_REF, hosts_text)
            for e in entries:
                z.write(e["src"], e["arc"])
    except OSError as e:
        return {"ok": False, "message": t("导出失败：{err}", err=e)}
    return {"ok": True, "path": dest, "counts": counts, "manifest": manifest,
            "message": t("已导出 {n} 个配置文件到 {path}", n=len(entries), path=dest)}


def _php_ini_for(config: Config, ver: str) -> str:
    if not ver:
        return ""
    for v in PhpManager(config).scan_versions():
        if v.name == ver:
            return v.ini
    return ""


def _restore_plan(manifest: dict, config: Config, vm: VhostManager) -> list[dict]:
    """把 manifest 的逻辑条目映射为当前环境的实际写入路径。"""
    plan: list[dict] = []
    for f in manifest.get("files", []):
        kind, arc = f.get("kind", ""), f.get("arc", "")
        target = ""
        if kind == "config":
            target = default_config_path()
        elif kind == "nginx.conf":
            target = vm.main_conf
        elif kind == "vhost":
            target = os.path.join(vm.vhost_dir, os.path.basename(arc))
        elif kind == "php.ini":
            parts = arc.split("/")
            ver = parts[1] if len(parts) >= 3 else ""
            target = _php_ini_for(config, ver)
        if target:
            plan.append({"kind": kind, "arc": arc, "target": target})
    return plan


def _validate(vm: VhostManager) -> tuple[bool, str]:
    if not os.path.exists(vm.nginx.exe):
        return True, t("未找到 nginx，已跳过配置校验。")
    out = vm.nginx.test_config()
    low = (out or "").lower()
    return ("successful" in low and "failed" not in low), out


def restore_bundle(src: str, config: Config | None = None,
                   dry_run: bool = False) -> dict:
    """从 zip 恢复环境配置；``nginx -t`` 失败整体回滚。"""
    config = config or Config()
    vm = VhostManager(config)
    src = os.path.abspath(os.path.expanduser(src))
    if not os.path.isfile(src):
        return {"ok": False, "message": t("备份文件不存在：{path}", path=src)}

    try:
        z = zipfile.ZipFile(src)
    except (OSError, zipfile.BadZipFile) as e:
        return {"ok": False, "message": t("无法打开备份：{err}", err=e)}
    with z:
        names = set(z.namelist())
        if MANIFEST not in names:
            return {"ok": False, "message": t("不是有效的 phpvm 备份（缺少 manifest.json）。")}
        try:
            manifest = json.loads(z.read(MANIFEST).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            return {"ok": False, "message": t("备份清单损坏：{err}", err=e)}
        plan = _restore_plan(manifest, config, vm)
        if dry_run:
            return {"ok": True, "dry_run": True, "manifest": manifest,
                    "count": len(plan),
                    "items": [{"kind": p["kind"], "target": p["target"]} for p in plan],
                    "message": t("将恢复 {n} 个配置文件，并执行 nginx -t 校验。", n=len(plan))}

        backups: list[tuple[str, str]] = []
        written: list[str] = []
        try:
            for p in plan:
                arc, target = p["arc"], p["target"]
                if arc not in names:
                    continue
                if os.path.exists(target):
                    b = file_backup.backup(target)
                    if b:
                        backups.append((target, b))
                d = os.path.dirname(target)
                if d:
                    os.makedirs(d, exist_ok=True)
                with z.open(arc) as fsrc, open(target, "wb") as fdst:
                    fdst.write(fsrc.read())
                written.append(target)
        except OSError as e:
            for target, b in reversed(backups):
                file_backup.restore(b, target)
            return {"ok": False, "rolled_back": True,
                    "message": t("写入失败，已回滚：{err}", err=e)}

    ok, out = _validate(vm)
    if not ok:
        for target, b in reversed(backups):
            file_backup.restore(b, target)
        return {"ok": False, "rolled_back": True, "output": out,
                "message": t("nginx -t 校验失败，已整体回滚。\n{out}", out=out)}
    return {"ok": True, "restored": written, "count": len(written), "output": out,
            "message": t("已恢复 {n} 个配置文件。", n=len(written))}
