# -*- coding: utf-8 -*-
"""命令清单：内置模板白名单 + 用户自定义命令（只落 ``plugin_settings``）。

``.phpvm.json`` **不参与命令来源**：它可能被团队提交到项目仓库，若把它的字段
当命令执行，克隆一个仓库就等于执行任意代码。这里只提供两类来源：

1. ``BUILTIN`` 固定模板，按项目内的标志文件（composer.json / artisan …）筛选；
2. 用户在面板或 CLI 里手动添加、存进 ``config.json`` 的自定义命令。

自定义命令还会在执行前过一遍元字符校验，避免破坏 ``open_terminal`` 的命令链。
"""
import uuid
import webbrowser

from ... import process_utils
from ...i18n import t
from .. import settings as plugin_settings
from . import projects

KEY = projects.KEY
CUSTOM_KEY = "custom_commands"
MAX_COMMAND_LEN = 300
MAX_LABEL_LEN = 80

# open_terminal 会把 command 拼进 && 链；这些字符会破坏链或形成组合指令
_FORBIDDEN = ('"', "'", "&", "|", ";", "`", "<", ">", "$",
              "\n", "\r", "\t", "\x00")

# 内置模板白名单：label 是 msgid（展示时由调用方 t()），requires 是项目标志文件
BUILTIN: tuple[dict, ...] = (
    {"id": "php-v", "label": "查看 PHP 版本",
     "command": "php -v", "requires": ()},
    {"id": "composer-install", "label": "安装 Composer 依赖",
     "command": "composer install", "requires": ("composer.json",)},
    {"id": "composer-update", "label": "更新 Composer 依赖",
     "command": "composer update", "requires": ("composer.json",)},
    {"id": "artisan-serve", "label": "启动 Laravel 开发服务器",
     "command": "php artisan serve", "requires": ("artisan",)},
    {"id": "artisan-migrate", "label": "执行数据库迁移",
     "command": "php artisan migrate", "requires": ("artisan",)},
    {"id": "php-serve", "label": "启动 PHP 内置服务器",
     "command": "php -S 127.0.0.1:8000 -t public",
     "requires": ("public/index.php",)},
    {"id": "npm-install", "label": "安装前端依赖",
     "command": "npm install", "requires": ("package.json",)},
    {"id": "npm-dev", "label": "启动前端开发服务器",
     "command": "npm run dev", "requires": ("package.json",)},
    {"id": "phpunit", "label": "运行测试",
     "command": "php vendor/bin/phpunit",
     "requires": ("vendor/bin/phpunit",)},
)


def validate_command(text: str) -> tuple[bool, str]:
    """校验一条命令能否安全地交给 ``open_terminal``；返回 (是否通过, 错误说明)。"""
    command = str(text or "").strip()
    if not command:
        return False, t("命令不能为空")
    if len(command) > MAX_COMMAND_LEN:
        return False, t("命令过长（最多 {n} 字符）", n=MAX_COMMAND_LEN)
    for char in _FORBIDDEN:
        if char in command:
            return False, t("命令包含不允许的字符：{ch}", ch=repr(char))
    if any(ord(char) < 32 for char in command):
        return False, t("命令包含控制字符")
    return True, ""


def custom_commands(config) -> list[dict]:
    """读取用户自定义命令；过滤掉结构不完整的残留项。"""
    raw = plugin_settings.get(config, KEY, CUSTOM_KEY, [])
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip()
        label = str(item.get("label") or "").strip()
        command = str(item.get("command") or "").strip()
        if cid and label and command:
            out.append({"id": cid, "label": label, "command": command,
                        "project": str(item.get("project") or "")})
    return out


def add_custom(config, label: str, command: str,
               project_root: str = "") -> tuple[dict | None, str]:
    """添加一条自定义命令；``project_root`` 为空表示所有项目可见。"""
    clean_label = str(label or "").strip()
    if not clean_label or len(clean_label) > MAX_LABEL_LEN:
        return None, t("名称不能为空且不超过 {n} 字符", n=MAX_LABEL_LEN)
    ok, err = validate_command(command)
    if not ok:
        return None, err
    scope = projects.norm_key(project_root) if project_root else ""
    items = custom_commands(config)
    entry = {"id": "custom-" + uuid.uuid4().hex[:8],
             "label": clean_label, "command": str(command).strip(),
             "project": scope}
    items.append(entry)
    plugin_settings.set(config, KEY, CUSTOM_KEY, items)
    return entry, ""


def remove_custom(config, command_id: str) -> bool:
    """删除自定义命令（内置模板不可删）；返回是否命中。"""
    items = custom_commands(config)
    kept = [item for item in items if item["id"] != command_id]
    if len(kept) == len(items):
        return False
    plugin_settings.set(config, KEY, CUSTOM_KEY, kept)
    return True


def for_project(config, project) -> list[dict]:
    """某项目可用的命令：内置模板（按标志文件筛）+ 全局/本项目自定义命令。

    返回项统一为 ``{id, label, command, builtin, project}``，label 保留 msgid 原文，
    由 UI / CLI 在展示时 ``t()``。
    """
    markers = set(project.markers)
    out: list[dict] = []
    for tpl in BUILTIN:
        if all(need in markers for need in tpl["requires"]):
            out.append({"id": tpl["id"], "label": tpl["label"],
                        "command": tpl["command"], "builtin": True,
                        "project": ""})
    for item in custom_commands(config):
        scope = item.get("project") or ""
        if not scope or scope == project.key:
            out.append({"id": item["id"], "label": item["label"],
                        "command": item["command"], "builtin": False,
                        "project": scope})
    return out


def find(command_list, command_id: str) -> dict | None:
    return next((c for c in command_list if c["id"] == command_id), None)


def launch(config, project, command_id: str, *,
           dry_run: bool = False, opener=None) -> dict:
    """在终端里打开并执行一条已确认的命令（用户主动触发，无自动执行）。

    ``opener`` 可注入以便测试；默认 ``process_utils.open_terminal(cwd, path_prepend,
    command)``。dry_run 只返回将要做什么，不碰系统。
    """
    hit = find(for_project(config, project), command_id)
    if hit is None:
        return {"ok": False, "error": t("未找到命令：{id}", id=command_id)}
    ok, err = validate_command(hit["command"])
    if not ok:
        return {"ok": False, "error": err}

    path_prepend = projects.resolve_php_dir(config, project.php)
    report = {
        "ok": True,
        "project": project.name,
        "cwd": project.root,
        "command": hit["command"],
        "label": hit["label"],
        "path_prepend": path_prepend,
        "dry_run": bool(dry_run),
        "opened": False,
    }
    if dry_run:
        return report
    opener = opener or process_utils.open_terminal
    try:
        opened = bool(opener(project.root, path_prepend, hit["command"]))
    except Exception as e:  # noqa: BLE001 - 终端不可用要给出明确失败
        return {**report, "ok": False, "opened": False, "error": str(e)}
    if not opened:
        return {**report, "ok": False,
                "error": t("未找到可用的终端程序")}
    return {**report, "opened": True}


def open_site(project, url: str = "", *, dry_run: bool = False,
              opener=None) -> dict:
    """在浏览器打开项目站点；URL 只能取自 vhost 扫描结果（``Project.urls``）。

    ``.phpvm.json`` 的 domains 只作展示，不参与 URL 生成；显式传入的 url 也必须是
    该项目的候选之一，避免把任意字符串交给 ``webbrowser.open``（ADR 16）。
    """
    candidates = projects.site_urls(project)
    chosen = str(url or "").strip() or (candidates[0] if candidates else "")
    if not chosen:
        return {"ok": False,
                "error": t("该项目没有可访问的站点（需在站点映射里配置域名）"),
                "available": []}
    if chosen not in candidates:
        return {"ok": False,
                "error": t("URL 不在该项目的站点列表中：{url}", url=chosen),
                "available": candidates}
    report = {"ok": True, "project": project.name, "url": chosen,
              "dry_run": bool(dry_run), "opened": False}
    if dry_run:
        return report
    opener = opener or webbrowser.open
    try:
        opened = bool(opener(chosen))
    except Exception as e:  # noqa: BLE001 - 无浏览器时要给出明确失败
        return {**report, "ok": False, "error": str(e)}
    if not opened:
        return {**report, "ok": False, "error": t("未找到可用的浏览器")}
    return {**report, "opened": True}
