# -*- coding: utf-8 -*-
"""Git 工作区扫描：对每个本地项目跑一次只读 git 查询，产出可序列化的状态。

口径与边界：

- 只跑 ``git status --porcelain=v2 --branch`` 与 ``git log -1``，**不** fetch / pull / add；
- ``--no-optional-locks`` + ``GIT_OPTIONAL_LOCKS=0`` 保证 status 不写 index 锁文件；
- ``GIT_TERMINAL_PROMPT=0`` 保证任何情况下都不会卡在凭据输入；
- 仓库判定：项目根自己的 ``.git``（目录或 worktree/submodule 的 gitfile）；
  目录本身不是仓库根时**回退到上一级**再找一次（站点 root 常指向 Laravel 的
  ``public`` 等子目录）—— 只回退一级、不无限向上，否则整个 ``www`` 会被当成一个巨大仓库；
- 全部并发执行（默认 4 线程），单仓库超时 6s，异常归一为错误码，绝不向上抛。
"""
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

GIT = "git"
DEFAULT_TIMEOUT = 6.0
DEFAULT_WORKERS = 4
MAX_CHANGES = 200
CHANGE_PATH_LIMIT = 300

#: 状态码 → i18n 源文案（界面与 CLI 统一用 ``t(STATE_MESSAGES[code])`` 展示）
STATE_MESSAGES = {
    "clean": "干净",
    "dirty": "有未提交改动",
    "ahead": "有待推送提交",
    "none": "不是 Git 仓库",
    "missing": "目录不存在",
    "error": "查询失败",
}

#: 错误码 → i18n 源文案
ERROR_MESSAGES = {
    "no_git": "未找到 git 命令（请安装 Git 并加入 PATH）",
    "timeout": "git 命令超时（仓库过大或磁盘繁忙）",
    "not_repo": "不是 Git 仓库",
    "missing": "目录不存在",
    "failed": "git 命令执行失败",
}


# --------------------------------------------------------------------------- #
# git 调用
# --------------------------------------------------------------------------- #
def git_executable() -> str:
    """git 可执行文件路径（找不到返回空串，调用方据此给 ``no_git``）。"""
    return shutil.which(GIT) or ""


def is_repo(path: str) -> bool:
    """项目根自己是不是仓库根（``.git`` 可以是目录或 worktree 的 gitfile）。"""
    if not path or not os.path.isdir(path):
        return False
    dot = os.path.join(path, ".git")
    return os.path.isdir(dot) or os.path.isfile(dot)


def resolve_repo(path: str) -> str:
    """项目目录对应的仓库根：自己是仓库用自己，否则回退**上一级**再找一次。

    站点 root 常指向 Laravel 的 ``public`` 等子目录，真正的 ``.git`` 在上一级；
    只回退一级、不无限向上，避免把整个 ``www`` 当成一个巨大仓库。
    找不到返回空串。
    """
    p = os.path.abspath(path or "")
    if is_repo(p):
        return p
    parent = os.path.dirname(p)
    if parent and parent != p and is_repo(parent):
        return parent
    return ""


def _env() -> dict:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"   # 绝不等待用户名 / 密码
    env["GIT_OPTIONAL_LOCKS"] = "0"    # status 不刷新 index
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env.pop("GIT_DIR", None)           # 防止被外部环境钉死到别的仓库
    env.pop("GIT_WORK_TREE", None)
    return env


def _run(path: str, args: list[str], timeout: float = DEFAULT_TIMEOUT):
    """执行一条只读 git 子命令（argv 形式，不经 shell）。"""
    exe = git_executable()
    if not exe:
        raise FileNotFoundError("git")
    # core.quotePath=false：否则中文文件名会被转义成 "\344\270\255" 八进制串，
    # 面板里显示成乱码（git 默认开启，macOS 中文目录尤其常见）。
    cmd = [exe, "--no-optional-locks", "-c", "core.quotePath=false",
           "-C", path, *args]
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, env=_env(),
        encoding="utf-8", errors="replace", **kwargs)


# --------------------------------------------------------------------------- #
# porcelain=v2 解析（纯函数，便于单测）
# --------------------------------------------------------------------------- #
def _path_of(line: str, fields: int) -> str:
    """取 porcelain 行尾的路径字段（``fields`` 是路径前的空格段数）。"""
    parts = line.split(" ", fields)
    if len(parts) <= fields:
        return ""
    return parts[fields].split("\t")[0].strip()


def _add_change(info: dict, path: str) -> None:
    if not path or len(info["changes"]) >= MAX_CHANGES:
        return
    info["changes"].append(path[:CHANGE_PATH_LIMIT])


def parse_status(text: str) -> dict:
    """解析 ``git status --porcelain=v2 --branch`` 输出。

    返回分支 / 上游 / 领先落后 / 四类改动计数与前 :data:`MAX_CHANGES` 条路径。
    格式不明或空输入都返回全零结构，不抛异常。
    """
    info = {
        "branch": "", "upstream": "", "ahead": 0, "behind": 0,
        "staged": 0, "modified": 0, "untracked": 0, "conflict": 0,
        "changes": [],
    }
    for line in (text or "").splitlines():
        if not line:
            continue
        if line.startswith("# "):
            key, _, value = line[2:].partition(" ")
            value = value.strip()
            if key == "branch.head":
                info["branch"] = value
            elif key == "branch.upstream":
                info["upstream"] = value
            elif key == "branch.ab":
                for token in value.split():
                    try:
                        if token.startswith("+"):
                            info["ahead"] = int(token[1:])
                        elif token.startswith("-"):
                            info["behind"] = int(token[1:])
                    except ValueError:
                        continue
            continue

        kind = line[0]
        if kind == "?":
            info["untracked"] += 1
            _add_change(info, line[2:].strip())
        elif kind == "u":
            info["conflict"] += 1
            _add_change(info, _path_of(line, 10))
        elif kind in ("1", "2"):
            xy = line[2:4]
            if len(xy) == 2:
                if xy[0] not in ". ":
                    info["staged"] += 1
                if xy[1] not in ". ":
                    info["modified"] += 1
                if "U" in xy:
                    info["conflict"] += 1
            _add_change(info, _path_of(line, 8 if kind == "1" else 9))
    return info


def _state(row: dict) -> str:
    """状态码：有改动 > 有未推送 > 干净。"""
    if int(row.get("dirty") or 0) > 0:
        return "dirty"
    if int(row.get("ahead") or 0) > 0:
        return "ahead"
    return "clean"


# --------------------------------------------------------------------------- #
# 单仓库
# --------------------------------------------------------------------------- #
def describe(path: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """查询一个项目根目录的 git 状态（任何异常都转成错误码，不向外抛）。"""
    started = time.monotonic()
    row = {
        "root": str(path or ""), "exists": bool(path) and os.path.isdir(path),
        "repo_root": "", "is_repo": False,
        "branch": "", "upstream": "", "ahead": 0, "behind": 0,
        "staged": 0, "modified": 0, "untracked": 0, "conflict": 0, "dirty": 0,
        "changes": [], "last_hash": "", "last_subject": "", "last_ts": 0,
        "status": "none", "error": "", "detail": "", "ms": 0,
    }
    if not row["exists"]:
        row["status"] = "missing"
        row["error"] = "missing"
        return row
    if not git_executable():
        row["status"] = "error"
        row["error"] = "no_git"
        return row
    repo_root = resolve_repo(row["root"])
    if not repo_root:
        row["status"] = "none"
        row["error"] = "not_repo"
        return row

    row["is_repo"] = True
    row["repo_root"] = repo_root
    try:
        res = _run(repo_root, ["status", "--porcelain=v2", "--branch",
                               "--untracked-files=normal"], timeout)
    except subprocess.TimeoutExpired:
        row.update(status="error", error="timeout")
        row["ms"] = int((time.monotonic() - started) * 1000)
        return row
    except (OSError, FileNotFoundError):
        row.update(status="error", error="failed")
        row["ms"] = int((time.monotonic() - started) * 1000)
        return row
    if res.returncode != 0:
        row.update(status="error", error="failed",
                   detail=(res.stderr or "").strip()[:CHANGE_PATH_LIMIT])
        row["ms"] = int((time.monotonic() - started) * 1000)
        return row

    info = parse_status(res.stdout)
    row.update({k: v for k, v in info.items() if k != "changes"})
    row["changes"] = info["changes"]
    row["dirty"] = (row["staged"] + row["modified"] + row["untracked"]
                    + row["conflict"])

    try:
        log = _run(repo_root, ["log", "-1", "--pretty=%h%x1f%ct%x1f%s"], timeout)
        if log.returncode == 0 and log.stdout.strip():
            parts = log.stdout.strip().split("\x1f")
            if len(parts) >= 3:
                row["last_hash"] = parts[0].strip()
                try:
                    row["last_ts"] = int(parts[1].strip())
                except ValueError:
                    row["last_ts"] = 0
                row["last_subject"] = parts[2].strip()[:200]
    except (subprocess.TimeoutExpired, OSError):
        pass  # 空仓库 / 超时都不影响状态本身

    row["status"] = _state(row)
    row["ms"] = int((time.monotonic() - started) * 1000)
    return row


def format_time(ts) -> str:
    """时间戳 → 本地 ``YYYY-MM-DD HH:MM``（0 或非法值返回空串）。"""
    try:
        value = int(ts or 0)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
    except (OverflowError, OSError, ValueError):
        return ""


# --------------------------------------------------------------------------- #
# 批量
# --------------------------------------------------------------------------- #
def extra_repos(base: str = "") -> list[str]:
    """环境根下一层里的 Git 仓库（只收真仓库，避免把 php82 之类环境目录当项目）。

    ``www`` 之外的仓库（如 phpvm 自身）也要能看到：只扫一层且必须有 ``.git``，
    没有仓库的目录不会进入列表，因此不会制造「非仓库」噪音。
    """
    from ...config import WNRP_ROOT

    root = base or WNRP_ROOT
    if not root or not os.path.isdir(root):
        return []
    out: list[str] = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.name.startswith(".") or not entry.is_dir():
                    continue
                if is_repo(entry.path):
                    out.append(entry.path)
    except OSError:
        pass
    return sorted(out)


def _projects(config):
    """复用项目启动器的发现口径（www 一层 + vhost root），再补环境根下的仓库。"""
    from ..project_launcher import projects as project_source

    items = list(project_source.scan(config))
    seen = {os.path.normcase(os.path.abspath(str(p.root))) for p in items}
    for root in extra_repos():
        key = os.path.normcase(os.path.abspath(root))
        if key in seen:
            continue
        seen.add(key)
        items.append(project_source.Project(
            name=os.path.basename(root) or root, root=root, sources=("root",)))
    items.sort(key=lambda p: (p.name.lower(), p.root.lower()))
    return items


def report(config=None, projects=None, only: str = "", limit: int = 0,
           timeout: float = DEFAULT_TIMEOUT,
           workers: int = DEFAULT_WORKERS) -> dict:
    """并发查询全部项目的 git 状态并汇总。

    ``only`` 按项目名 / 路径子串过滤；``limit`` 限制数量；
    ``projects`` 可注入（测试用，免扫真实目录）。
    """
    items = list(projects) if projects is not None else _projects(config)
    needle = (only or "").strip().lower()
    if needle:
        items = [p for p in items
                 if needle in str(getattr(p, "name", "")).lower()
                 or needle in str(getattr(p, "root", "")).lower()]
    if limit and limit > 0:
        items = items[:limit]

    timeout = max(1.0, float(timeout or DEFAULT_TIMEOUT))
    rows: list[dict] = []
    if items:
        pool_size = max(1, min(int(workers or 1), len(items)))
        with ThreadPoolExecutor(max_workers=pool_size) as pool:
            rows = list(pool.map(
                lambda p: _row(p, timeout), items))

    repos = [r for r in rows if r.get("is_repo")]
    return {
        "ok": True,
        "git": bool(git_executable()),
        "timeout": timeout,
        "total": len(rows),
        "repos": len(repos),
        "clean": sum(1 for r in rows if r.get("status") == "clean"),
        "dirty": sum(1 for r in rows if r.get("status") == "dirty"),
        "ahead": sum(1 for r in rows if int(r.get("ahead") or 0) > 0),
        "behind": sum(1 for r in rows if int(r.get("behind") or 0) > 0),
        "none": sum(1 for r in rows if r.get("status") == "none"),
        "errors": sum(1 for r in rows if r.get("status") in ("error", "missing")),
        "items": rows,
    }


def _row(project, timeout: float) -> dict:
    root = str(getattr(project, "root", "") or "")
    row = describe(root, timeout)
    row.update(
        name=str(getattr(project, "name", "") or os.path.basename(root)),
        php=str(getattr(project, "php", "") or ""),
        domains=list(getattr(project, "domains", ()) or ()),
        urls=list(getattr(project, "urls", ()) or ()),
        last_time=format_time(row.get("last_ts")),
    )
    return row


def find(items, query: str) -> dict | None:
    """按项目名（不区分大小写）或路径查找；先精确、后子串。"""
    needle = (query or "").strip()
    if not needle:
        return None
    low = needle.lower()
    for row in items or []:
        if str(row.get("name", "")).lower() == low:
            return row
    for row in items or []:
        if os.path.normcase(os.path.abspath(str(row.get("root", "")))) == \
                os.path.normcase(os.path.abspath(needle)):
            return row
    for row in items or []:
        if low in str(row.get("name", "")).lower():
            return row
    return None
