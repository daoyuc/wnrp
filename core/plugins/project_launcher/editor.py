# -*- coding: utf-8 -*-
"""在编辑器 / IDE 里打开项目目录。

命令来源只有两处，与自定义命令同一安全口径（ADR 16）：

1. ``EDITORS`` 内置白名单：只探测可执行文件是否在 PATH 上，不解释任何参数；
2. 用户在设置里写的模板（``plugin_settings.project_launcher.editor``，例如
   ``code {path}``），``{path}`` 缺省时把项目目录追加到参数末尾。

参数以 argv 列表交给 ``subprocess.Popen``，**不拼 shell 字符串**，因此目录名里
有空格、括号也不会被解释成命令；Windows 上的 ``code.cmd`` 这类批处理垫片走
``cmd /c`` 包装。
"""
import os
import shlex
import shutil
import subprocess

from ...i18n import t
from .. import settings as plugin_settings
from . import projects

KEY = projects.KEY
SETTING = "editor"
PLACEHOLDER = "{path}"
MAX_TEMPLATE_LEN = 200

# 只用于探测可执行文件是否存在；``label`` 供 UI 展示
EDITORS: tuple[dict, ...] = (
    {"id": "code", "label": "VS Code", "exe": "code"},
    {"id": "cursor", "label": "Cursor", "exe": "cursor"},
    {"id": "phpstorm", "label": "PhpStorm", "exe": "phpstorm"},
    {"id": "subl", "label": "Sublime Text", "exe": "subl"},
    {"id": "zed", "label": "Zed", "exe": "zed"},
)


def template(config=None) -> str:
    """用户设置的编辑器模板（空串表示「自动挑选已安装的编辑器」）。"""
    return str(plugin_settings.get(config, KEY, SETTING, "") or "").strip()


def set_template(config, text: str) -> tuple[str, str]:
    """持久化编辑器模板；返回 ``(保存后的值, 错误说明)``。"""
    raw = str(text or "").strip()
    if len(raw) > MAX_TEMPLATE_LEN:
        return "", t("编辑器命令过长（最多 {n} 字符）", n=MAX_TEMPLATE_LEN)
    if raw and "\n" in raw:
        return "", t("编辑器命令不能包含换行")
    plugin_settings.set(config, KEY, SETTING, raw)
    return raw, ""


def detected() -> list[dict]:
    """PATH 上能找到的编辑器（顺序即优先级）。"""
    return [dict(item) for item in EDITORS if shutil.which(item["exe"])]


def choices(config=None) -> list[str]:
    """UI 下拉候选：已安装的编辑器 + 用户自定义模板（去重、保序）。"""
    values = [item["exe"] for item in detected()]
    current = template(config)
    if current and current not in values:
        values.insert(0, current)
    return values


def resolve(config=None, override: str = "") -> str:
    """实际使用的模板：调用方指定 → 用户设置 → 第一个探测到的编辑器。"""
    text = str(override or "").strip() or template(config)
    if text:
        return text
    found = detected()
    return found[0]["exe"] if found else ""


def argv_of(tpl: str, path: str) -> list[str]:
    """把模板拆成 argv；``{path}`` 做字面替换，缺省则把目录追加到末尾。"""
    try:
        parts = shlex.split(str(tpl or ""), posix=(os.name != "nt"))
    except ValueError:  # 引号不配对等
        return []
    out: list[str] = []
    used = False
    for part in parts:
        if PLACEHOLDER in part:
            out.append(part.replace(PLACEHOLDER, path))
            used = True
        else:
            out.append(part)
    if out and not used:
        out.append(path)
    return out


def _spawn(argv: list[str], cwd: str = "") -> None:
    """无 shell 启动编辑器进程（不阻塞、不接管输出）。"""
    exe = shutil.which(argv[0]) or argv[0]
    if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c", exe] + argv[1:]
    else:
        cmd = [exe] + argv[1:]
    kwargs = {"cwd": cwd or None, "stdin": subprocess.DEVNULL,
              "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                  | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def open_project(project, config=None, override: str = "", *,
                 dry_run: bool = False, spawn=None) -> dict:
    """用编辑器打开项目目录；``spawn`` 可注入，dry_run 只报告要做什么。"""
    if not projects.root_exists(project):
        return {"ok": False,
                "error": t("项目目录不存在：{path}", path=project.root)}
    tpl = resolve(config, override)
    if not tpl:
        return {"ok": False,
                "error": t("未找到可用的编辑器（可在设置里填写编辑器命令）")}
    argv = argv_of(tpl, project.root)
    if not argv:
        return {"ok": False, "error": t("编辑器命令不能为空")}
    report = {"ok": True, "project": project.name, "editor": tpl,
              "argv": argv, "dry_run": bool(dry_run), "opened": False}
    if dry_run:
        return report
    try:
        (spawn or _spawn)(argv, project.root)
    except Exception as e:  # noqa: BLE001 - 编辑器没装好要给出明确失败
        return {**report, "ok": False, "error": str(e)}
    return {**report, "opened": True}
