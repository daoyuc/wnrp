# -*- coding: utf-8 -*-
"""SSH 主机与命令片段的持久化（凭据与普通配置分离）。

- 主机含密码 / 私钥口令，存 ``app_paths.data_file("ssh_hosts.json")`` 并收紧
  权限（POSIX 0600 / Windows icacls 仅当前用户），**不进入备份包**；
- 命令片段不含敏感信息，存 ``config.json`` 的 ``settings.ssh_snippets``，
  随现有备份/迁移流程带走；
- 写入前 ``core/file_backup.backup``，再临时文件 + ``os.replace`` 原子替换，
  备份同样收紧权限；
- 不 import paramiko：未装依赖也能管理配置。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid

from . import app_paths, file_backup
from .i18n import t

IS_WIN = sys.platform.startswith("win")
HOSTS_FILE = "ssh_hosts.json"
SNIPPETS_KEY = "ssh_snippets"
DATA_VERSION = 1
MAX_NAME = 64
MAX_HOST = 255
MAX_COMMAND = 4096
MAX_TAG = 32
AUTH_METHODS = ("password", "key")
# 私钥留空时按 OpenSSH 客户端的惯例顺序兜底（只作为表单默认值）
DEFAULT_KEY_NAMES = ("id_ed25519", "id_ecdsa", "id_rsa", "id_dsa")

# 交互式终端会把主机/用户名拼进 cmd /k 或 osascript 命令串（见
# core/process_utils.open_terminal），必须杜绝 shell 元字符。
_RE_HOST = re.compile(r"^[A-Za-z0-9._:\-\[\]]+$")
_RE_USER = re.compile(r"^[A-Za-z0-9._\-]+$")


def hosts_path() -> str:
    """主机文件路径（``PHPVM_SSH_HOSTS`` 可覆盖，供测试隔离）。"""
    env = (os.environ.get("PHPVM_SSH_HOSTS") or "").strip()
    return os.path.expanduser(env) if env else app_paths.data_file(HOSTS_FILE)


def default_key_dir() -> str:
    """默认私钥目录：``PHPVM_SSH_KEY_DIR`` 可覆盖（供测试隔离），否则 ``~/.ssh``。"""
    env = (os.environ.get("PHPVM_SSH_KEY_DIR") or "").strip()
    return os.path.expanduser(env) if env else os.path.expanduser("~/.ssh")


def default_key_path() -> str:
    """按 OpenSSH 惯例顺序挑一个本机已有的默认私钥，找不到返回 ``""``。

    这只是「表单里的默认值」：路径会写进主机配置、界面上看得见也能改，连接时
    照旧显式传给 ``connect(key_filename=...)``——**不启用 ssh-agent、不隐式尝试**
    其它未列出的密钥（见 ADR 24），所以自动选用不等于放宽认证边界。
    """
    base = default_key_dir()
    for name in DEFAULT_KEY_NAMES:
        path = os.path.join(base, name)
        if os.path.isfile(path):
            return path
    return ""


def set_secret_permissions(path: str) -> bool:
    """收紧为「仅当前用户可读写」；失败静默返回 False（不影响写入成功）。"""
    if not path or not os.path.exists(path):
        return False
    try:
        if IS_WIN:
            user = (os.environ.get("USERNAME") or "").strip()
            if not user:
                return False
            subprocess.run(
                ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False, timeout=10,
            )
            return True
        os.chmod(path, 0o600)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _atomic_write_json(path: str, payload: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ssh-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        set_secret_permissions(tmp)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    set_secret_permissions(path)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def clean_tags(tags) -> list[str]:
    """标签清洗：逗号串或列表 → 去空、去重、限长。"""
    if isinstance(tags, str):
        tags = tags.replace("，", ",").split(",")
    if not isinstance(tags, (list, tuple)):
        return []
    out: list[str] = []
    for raw in tags:
        tag = str(raw).strip()[:MAX_TAG]
        if tag and tag not in out:
            out.append(tag)
    return out


def validate_host(data: dict) -> str | None:
    """校验主机字段，返回错误文案；合法返回 None。"""
    if not isinstance(data, dict):
        return t("主机配置格式不正确")
    name = str(data.get("name", "")).strip()
    if not name:
        return t("请填写主机名称")
    if len(name) > MAX_NAME:
        return t("主机名称过长（最多 {n} 个字符）", n=MAX_NAME)
    host = str(data.get("host", "")).strip()
    if not host:
        return t("请填写主机地址")
    if len(host) > MAX_HOST or not _RE_HOST.match(host):
        return t("主机地址含有不允许的字符（只允许字母、数字与 . _ : - [ ]）")
    try:
        port = int(str(data.get("port", 22)).strip())
    except (TypeError, ValueError):
        return t("端口必须是整数")
    if not (1 <= port <= 65535):
        return t("端口必须在 1-65535 之间")
    user = str(data.get("user", "")).strip()
    if not user:
        return t("请填写登录用户名")
    if not _RE_USER.match(user):
        return t("用户名含有不允许的字符（只允许字母、数字与 . _ -）")
    auth = str(data.get("auth", "password")).strip()
    if auth not in AUTH_METHODS:
        return t("认证方式只能是密码或私钥")
    # 凭据允许先空着存下来（连不上 / 还没拿到密码的主机也要能登记），
    # 只有「填了路径却指不到文件」这种明显笔误才拦。缺什么由 missing_secret() 报。
    key_path = os.path.expanduser(str(data.get("key_path", "")).strip())
    if auth == "key" and key_path and not os.path.isfile(key_path):
        return t("私钥文件不存在：{path}", path=key_path)
    return None


def missing_secret(host: dict) -> str:
    """这台主机还缺哪样凭据：``password`` / ``key`` / ``""``（已配置）。

    保存时不强制填凭据，所以「能不能连」与「能不能存」解耦；连接前用本函数
    给出「还差什么」的可读提示。
    """
    if str(host.get("auth", "password")) == "key":
        path = os.path.expanduser(str(host.get("key_path", "")).strip())
        return "" if path else "key"
    return "" if str(host.get("password", "")) else "password"


def validate_snippet(data: dict) -> str | None:
    """校验命令片段字段，返回错误文案；合法返回 None。"""
    if not isinstance(data, dict):
        return t("命令片段格式不正确")
    name = str(data.get("name", "")).strip()
    if not name:
        return t("请填写片段名称")
    if len(name) > MAX_NAME:
        return t("片段名称过长（最多 {n} 个字符）", n=MAX_NAME)
    command = str(data.get("command", ""))
    if not command.strip():
        return t("请填写要执行的命令")
    if len(command) > MAX_COMMAND:
        return t("命令过长（最多 {n} 个字符）", n=MAX_COMMAND)
    if "\x00" in command:
        return t("命令不能包含空字符")
    return None


def normalize_host(data: dict) -> dict:
    """收窄成已知字段（不生成 id / 时间戳）。"""
    auth = str(data.get("auth", "password")).strip() or "password"
    key_path = os.path.expanduser(str(data.get("key_path", "")).strip())
    return {
        "name": str(data.get("name", "")).strip(),
        "host": str(data.get("host", "")).strip(),
        "port": int(str(data.get("port", 22)).strip() or 22),
        "user": str(data.get("user", "")).strip(),
        "auth": auth,
        "password": str(data.get("password", "")) if auth == "password" else "",
        "key_path": key_path if auth == "key" else "",
        "passphrase": str(data.get("passphrase", "")) if auth == "key" else "",
        "tags": clean_tags(data.get("tags")),
        "note": str(data.get("note", "")).strip(),
    }


def normalize_snippet(data: dict) -> dict:
    """收窄成已知片段字段（不生成 id / 时间戳）。"""
    raw_ids = data.get("host_ids")
    host_ids = [str(x).strip() for x in raw_ids if str(x).strip()] \
        if isinstance(raw_ids, (list, tuple)) else []
    return {
        "name": str(data.get("name", "")).strip(),
        "command": str(data.get("command", "")),
        "host_ids": host_ids,
        "tags": clean_tags(data.get("tags")),
        "note": str(data.get("note", "")).strip(),
    }


def public(host: dict) -> dict:
    """展示用副本：剔除密码 / 口令，只保留「是否已设置」。"""
    out = {k: v for k, v in dict(host).items()
           if k not in ("password", "passphrase")}
    out["has_password"] = bool(host.get("password"))
    out["has_passphrase"] = bool(host.get("passphrase"))
    out["credential_ready"] = missing_secret(host) == ""
    out["tags"] = clean_tags(host.get("tags"))
    return out


# --------------------------------------------------------------------------- #
# 主机读写
# --------------------------------------------------------------------------- #
def load_hosts() -> list[dict]:
    """读取主机列表；文件缺失 / 损坏时返回空表（不抛错）。"""
    try:
        with open(hosts_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    items = data.get("hosts") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    hosts: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        host = normalize_host(raw)
        host["id"] = str(raw["id"])
        host["created"] = str(raw.get("created", ""))
        host["updated"] = str(raw.get("updated", ""))
        hosts.append(host)
    return hosts


def save_hosts(hosts: list[dict]) -> None:
    """整表落盘（先备份，再原子替换；备份同样收紧权限）。"""
    path = hosts_path()
    bak = file_backup.backup(path)
    if bak:
        set_secret_permissions(bak)
    _atomic_write_json(path, {"version": DATA_VERSION, "hosts": list(hosts)})


def find_host(hosts: list[dict], target: str) -> dict | None:
    """按 id 或名称查找（CLI / GUI 都允许用名称指代）。"""
    target = (target or "").strip()
    if not target:
        return None
    for h in hosts:
        if str(h.get("id")) == target:
            return h
    for h in hosts:
        if str(h.get("name")) == target:
            return h
    return None


def match_host(host: dict, query: str = "") -> bool:
    """名称 / 地址 / 用户名 / 备注 / 标签 子串匹配（忽略大小写）。"""
    q = (query or "").strip().lower()
    if not q:
        return True
    fields = [host.get("name", ""), host.get("host", ""), host.get("user", ""),
              host.get("note", "")]
    fields += list(host.get("tags") or [])
    return any(q in str(x).lower() for x in fields)


def search_hosts(hosts: list[dict], query: str = "", tag: str = "") -> list[dict]:
    """按关键字与标签过滤，结果按名称排序。"""
    tag = (tag or "").strip()
    out = [h for h in hosts
           if match_host(h, query)
           and (not tag or tag in (h.get("tags") or []))]
    return sorted(out, key=lambda h: str(h.get("name", "")).lower())


def add_host(data: dict) -> tuple[dict | None, str]:
    """新增主机；返回 ``(host, 错误信息)``。"""
    err = validate_host(data)
    if err:
        return None, err
    hosts = load_hosts()
    name = str(data.get("name", "")).strip()
    if any(str(h.get("name")) == name for h in hosts):
        return None, t("主机名称已存在：{name}", name=name)
    host = normalize_host(data)
    host["id"] = new_id("host")
    host["created"] = host["updated"] = _now()
    hosts.append(host)
    try:
        save_hosts(hosts)
    except OSError as e:
        return None, t("保存主机失败：{err}", err=e)
    return host, ""


def update_host(target: str, patch: dict) -> tuple[dict | None, str]:
    """更新主机；空密码表示「不修改」（编辑表单不必每次重输）。"""
    hosts = load_hosts()
    host = find_host(hosts, target)
    if host is None:
        return None, t("未找到主机：{name}", name=target)
    merged = dict(host)
    patch = patch or {}
    for key in ("name", "host", "port", "user", "auth", "note", "tags",
                "key_path", "passphrase"):
        if key in patch and patch[key] not in (None, ""):
            merged[key] = patch[key]
    if patch.get("password"):
        merged["password"] = str(patch["password"])
    auth = str(merged.get("auth", "password"))
    if auth == "password" and not merged.get("password"):
        return None, t("请填写登录密码（或改用私钥认证）")
    if auth == "key" and not merged.get("password"):
        merged["password"] = ""
    err = validate_host(merged)
    if err:
        return None, err
    name = str(merged.get("name", "")).strip()
    others = [h for h in hosts if str(h.get("id")) != str(host.get("id"))]
    if any(str(h.get("name")) == name for h in others):
        return None, t("主机名称已存在：{name}", name=name)
    host.update(merged)
    host["updated"] = _now()
    try:
        save_hosts(hosts)
    except OSError as e:
        return None, t("保存主机失败：{err}", err=e)
    return host, ""


def remove_host(target: str) -> bool:
    """删除主机。

    不主动改写片段里的 ``host_ids``：:func:`snippets_for` 读取时会忽略悬空 id，
    片段在主机删掉后自然变成通用片段（恢复备份后重新加回主机还能接上关联）。
    """
    hosts = load_hosts()
    host = find_host(hosts, target)
    if host is None:
        return False
    kept = [h for h in hosts if str(h.get("id")) != str(host.get("id"))]
    try:
        save_hosts(kept)
    except OSError:
        return False
    return True


def all_tags(hosts: list[dict]) -> list[str]:
    """主机标签全集（供面板筛选下拉）。"""
    out: list[str] = []
    for h in hosts:
        for tag in h.get("tags") or []:
            if tag not in out:
                out.append(tag)
    return sorted(out)


# --------------------------------------------------------------------------- #
# 命令片段（config.json → settings.ssh_snippets）
# --------------------------------------------------------------------------- #
def load_snippets(config) -> list[dict]:
    """读取片段列表；结构不合法时返回空表。"""
    items = config.get_setting(SNIPPETS_KEY, [])
    if not isinstance(items, (list, tuple)):
        return []
    out: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        snip = normalize_snippet(raw)
        snip["id"] = str(raw["id"])
        out.append(snip)
    return out


def save_snippets(config, items: list[dict]) -> None:
    """整表写回配置（``set_setting`` 会落盘）。"""
    config.set_setting(SNIPPETS_KEY, list(items))


def find_snippet(items: list[dict], target: str) -> dict | None:
    """按 id 或名称查找片段。"""
    target = (target or "").strip()
    if not target:
        return None
    for s in items:
        if str(s.get("id")) == target:
            return s
    for s in items:
        if str(s.get("name")) == target:
            return s
    return None


def snippets_for(config, host: dict | None = None, query: str = "",
                 tag: str = "", known_host_ids=None) -> list[dict]:
    """列出片段：可按主机过滤，并忽略指向已删除主机的悬空关联。

    ``known_host_ids`` 由调用方传入可避免重复读盘；``host_ids`` 为空表示通用片段。
    """
    items = load_snippets(config)
    if known_host_ids is None:
        known_host_ids = {str(h.get("id")) for h in load_hosts()}
    known = {str(x) for x in known_host_ids}
    host_id = str((host or {}).get("id", ""))
    q = (query or "").strip().lower()
    tag = (tag or "").strip()
    out: list[dict] = []
    for snip in items:
        live = [i for i in snip.get("host_ids") or [] if i in known]
        if live and host_id and host_id not in live:
            continue
        if tag and tag not in (snip.get("tags") or []):
            continue
        if q:
            fields = [snip.get("name", ""), snip.get("command", ""),
                      snip.get("note", "")] + list(snip.get("tags") or [])
            if not any(q in str(x).lower() for x in fields):
                continue
        out.append(snip)
    return sorted(out, key=lambda s: str(s.get("name", "")).lower())


def add_snippet(config, data: dict) -> tuple[dict | None, str]:
    """新增片段；返回 ``(snippet, 错误信息)``。"""
    err = validate_snippet(data)
    if err:
        return None, err
    items = load_snippets(config)
    name = str(data.get("name", "")).strip()
    if any(str(s.get("name")) == name for s in items):
        return None, t("片段名称已存在：{name}", name=name)
    snip = normalize_snippet(data)
    snip["id"] = new_id("snip")
    items.append(snip)
    save_snippets(config, items)
    return snip, ""


def update_snippet(config, target: str, patch: dict) -> tuple[dict | None, str]:
    """更新片段（只覆盖 patch 里出现的字段）。"""
    items = load_snippets(config)
    snip = find_snippet(items, target)
    if snip is None:
        return None, t("未找到命令片段：{name}", name=target)
    merged = dict(snip)
    for key in ("name", "command", "note"):
        if key in (patch or {}) and patch[key] not in (None, ""):
            merged[key] = patch[key]
    if "host_ids" in (patch or {}):
        merged["host_ids"] = patch["host_ids"]
    if "tags" in (patch or {}):
        merged["tags"] = patch["tags"]
    err = validate_snippet(merged)
    if err:
        return None, err
    name = str(merged.get("name", "")).strip()
    others = [s for s in items if str(s.get("id")) != str(snip.get("id"))]
    if any(str(s.get("name")) == name for s in others):
        return None, t("片段名称已存在：{name}", name=name)
    snip.update(merged)
    save_snippets(config, items)
    return snip, ""


def remove_snippet(config, target: str) -> bool:
    """删除片段。"""
    items = load_snippets(config)
    snip = find_snippet(items, target)
    if snip is None:
        return False
    save_snippets(config, [s for s in items
                           if str(s.get("id")) != str(snip.get("id"))])
    return True
