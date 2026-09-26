# -*- coding: utf-8 -*-
"""Xdebug 调试开关：状态判定 + ini 写入（`zend_extension` + 调试指令）+ 自检失败回滚。

设计边界
--------
- 只做「配置层」：不启停 PHP 进程（那是 `PhpManager` 的职责），避免出现第二套
  进程管理实现；调用方写完后自行决定要不要重启该版本。
- 写 ini 一律走既有链路：`file_backup` 备份 → 行级改写 → `php -c <ini> -m` 自检 →
  失败用最早的备份整体还原（与 `docs/DECISIONS.md` 05 一致）。
- 跨平台：Windows 官方包为 `php_xdebug.dll`（ext 目录），Homebrew / Linux 为
  `xdebug.so`（可能在版本目录外，故以 `php -m` 兜底判定是否已装）。
"""
import os
import re

from . import file_backup, ini_editor
from . import process_utils as pu
from .config import IS_WIN
from .i18n import t
from .php_manager import CLI_NAME, PhpVersion

#: 扩展键名与默认调试参数（Xdebug 3 的默认端口为 9003）
EXT_KEY = "xdebug"
DEFAULT_PORT = 9003
DEFAULT_HOST = "127.0.0.1"
DEFAULT_IDEKEY = "PHPVM"

#: 调试模式下维护的指令（未列出的 xdebug.* 保持原样，不擅自清理用户配置）
DEBUG_DIRECTIVES = (
    "xdebug.mode",
    "xdebug.start_with_request",
    "xdebug.client_port",
    "xdebug.client_host",
    "xdebug.idekey",
)

#: xdebug 加载行：`(zend_)extension = <值>`，可选行首注释符
_LOADER_RE = re.compile(r"^(\s*(;?)\s*(?:zend_)?extension\s*=\s*)(.*)$", re.IGNORECASE)

#: 追加块标记（与 hosts 的 phpvm-managed 同思路：可辨识、可整块清理）
_MARK = "; >>> phpvm-managed: xdebug >>>"


def php_cli(v: PhpVersion) -> str:
    """版本目录内的 CLI 可执行文件（php.exe / php），没有则退回 php-cgi。"""
    exe = os.path.join(v.dir, CLI_NAME)
    if os.path.exists(exe):
        return exe
    return v.cgi


def find_ext_file(v: PhpVersion) -> str:
    """在版本目录内定位 xdebug 扩展文件（ext/ → lib/php/extensions/ → 目录根）。"""
    for rel in ("ext", os.path.join("lib", "php", "extensions"), ""):
        d = os.path.join(v.dir, rel) if rel else v.dir
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for fn in names:
            low = fn.lower()
            if EXT_KEY in low and low.endswith((".dll", ".so")):
                return os.path.join(d, fn)
    return ""


def _ext_value(v: PhpVersion) -> str:
    """加载行取值：能定位到文件就用绝对路径（不依赖 extension_dir）。"""
    found = find_ext_file(v)
    if found:
        return found.replace("\\", "/")
    return "php_xdebug.dll" if IS_WIN else "xdebug.so"


def module_loaded(v: PhpVersion, timeout: int = 20) -> bool:
    """`php -m` 是否已加载 xdebug（覆盖 Homebrew / 系统包管理器安装到目录外的情况）。"""
    exe = php_cli(v)
    if not os.path.exists(exe):
        return False
    args = [exe, "-m"]
    if v.ini and os.path.exists(v.ini):
        args += ["-c", v.ini]
    _code, out, err = pu.run_cmd(args, timeout=timeout)
    return EXT_KEY in (out + err).lower()


def _loader_line(ini_path: str) -> str:
    """返回 ini 中 xdebug 的加载行原文（含可能的行首 `;`）；没有返回 ""。"""
    if not ini_path or not os.path.exists(ini_path):
        return ""
    try:
        with open(ini_path, "rb") as f:
            raw = f.read()
    except OSError:
        return ""
    for line in raw.decode("latin-1").splitlines():
        m = _LOADER_RE.match(line)
        if m and EXT_KEY in m.group(3).lower():
            return line
    return ""


def loader_enabled(v: PhpVersion) -> bool:
    """仅按 ini 加载行判定是否启用调试（纯读盘，**不执行** `php -m`）。

    供版本列表等需要批量展示状态的场景复用；需要完整状态（安装情况 / 端口 /
    mode / idekey）时用 :func:`status`。
    """
    loader = _loader_line(v.ini or "")
    return bool(loader) and not loader.lstrip().startswith(";")


def status(v: PhpVersion) -> dict:
    """读取当前调试状态（不写任何文件）。"""
    ini = v.ini or ""
    ini_exists = bool(ini) and os.path.exists(ini)
    values = ini_editor.load_any(ini, list(DEBUG_DIRECTIVES)) if ini_exists else {}
    enabled = loader_enabled(v)
    try:
        port = int(str(values.get("xdebug.client_port", DEFAULT_PORT)).strip())
    except ValueError:
        port = DEFAULT_PORT
    return {
        "name": v.name,
        "ini": ini,
        "ini_exists": ini_exists,
        "installed": bool(find_ext_file(v)) or module_loaded(v),
        "enabled": enabled,
        "debugging": enabled and "debug" in (values.get("xdebug.mode", "") or "").lower(),
        "port": port,
        "mode": values.get("xdebug.mode", ""),
        "client_host": values.get("xdebug.client_host", ""),
        "idekey": values.get("xdebug.idekey", ""),
        "running": bool(getattr(v, "running", False)),
        "ext_file": find_ext_file(v),
    }


# --------------------------------------------------------------------------- #
# 写入
# --------------------------------------------------------------------------- #
def _set_loader(ini_path: str, value: str, on: bool) -> str:
    """改写 / 追加 xdebug 加载行；返回 "replaced" / "appended" / "unchanged"。

    重复开关是幂等的：已存在加载行时只切换注释状态（关闭时用**原值**注释，
    不替换成探测出来的路径），不存在且要开启时才追加一个带标记的小块。
    """
    with open(ini_path, "rb") as f:
        lines = f.read().decode("latin-1").splitlines(keepends=True)

    out: list[str] = []
    found = False
    changed = False
    for line in lines:
        m = _LOADER_RE.match(line)
        if m and EXT_KEY in m.group(3).lower():
            found = True
            old = (m.group(3) or "").strip()
            new_line = (f"zend_extension={value}\n" if on
                        else f";zend_extension={old or value}\n")
            if line.strip() == new_line.strip():
                out.append(line)
            else:
                out.append(new_line)
                changed = True
            continue
        out.append(line)

    appended = False
    if on and not found:
        # 没有加载行：追加一个可辨识的小块
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append(f"\n{_MARK}\nzend_extension={value}\n")
        changed = True
        appended = True

    with open(ini_path, "wb") as f:
        f.write("".join(out).encode("latin-1"))
    if appended:
        return "appended"
    return "replaced" if changed else "unchanged"


def verify(v: PhpVersion, expect_loaded: bool) -> tuple[bool, str]:
    """自检：ini 能被 PHP 正常加载，且 xdebug 的加载情况符合预期。"""
    exe = php_cli(v)
    if not os.path.exists(exe):
        return False, t("未找到可执行文件：{path}", path=exe)
    args = [exe, "-c", v.ini, "-r", "echo 'OK';"]
    code, out, err = pu.run_cmd(args, timeout=25)
    text = (out or "") + (err or "")
    if code != 0 or "OK" not in out:
        return False, (err or out).strip()[:300] or t("配置加载失败")
    loaded = EXT_KEY in text.lower() or module_loaded(v)
    if expect_loaded and not loaded:
        return False, t("ini 已写入但 Xdebug 未加载，请检查扩展文件与 PHP 版本是否匹配")
    if not expect_loaded and loaded:
        # 已关闭加载行但仍被加载：通常是 php.ini 之外还有 conf.d 扫描目录
        return False, t("加载行已注释但 Xdebug 仍在加载，请检查额外的 ini 扫描目录")
    return True, ""


def enable(v: PhpVersion, port: int = DEFAULT_PORT,
           host: str = DEFAULT_HOST, idekey: str = DEFAULT_IDEKEY) -> tuple[bool, str, str]:
    """开启调试：写加载行 + 调试指令，自检失败整体还原。返回 (ok, 消息, 备份路径)。"""
    if not v.ini or not os.path.exists(v.ini):
        return False, t("[{name}] 还没有生效的 php.ini，请先生成后再开启调试。",
                        name=v.name), ""
    if not (find_ext_file(v) or module_loaded(v)):
        return False, t("[{name}] 未检测到 Xdebug 扩展，请先在「安装扩展」中安装 Xdebug。",
                        name=v.name), ""
    backup = file_backup.backup(v.ini)
    try:
        # 顺序要紧：save_values 会自己备份一次 <ini>.bak（它备份的是「当时」的内容），
        # 因此必须先写指令、后写加载行 —— 才能让 .bak 始终是**原始内容**，
        # 自检失败时一次还原就能回到未改状态。
        ini_editor.save_values(v.ini, {
            "xdebug.mode": "debug",
            "xdebug.start_with_request": "yes",
            "xdebug.client_port": str(port),
            "xdebug.client_host": host,
            "xdebug.idekey": idekey,
        })
        _set_loader(v.ini, _ext_value(v), True)
    except OSError as e:
        file_backup.restore(backup, v.ini, remove_backup=False)
        return False, t("[{name}] 写入配置失败：{err}", name=v.name, err=e), backup
    ok, detail = verify(v, expect_loaded=True)
    if not ok:
        file_backup.restore(backup, v.ini, remove_backup=False)
        return False, t("[{name}] 自检未通过，已还原配置：{detail}",
                        name=v.name, detail=detail), backup
    return True, t("[{name}] 已开启 Xdebug 调试（端口 {port}）；重启该版本后生效。",
                   name=v.name, port=port), backup


def disable(v: PhpVersion) -> tuple[bool, str, str]:
    """关闭调试：注释加载行（调试指令保留，便于再次开启）。返回 (ok, 消息, 备份路径)。"""
    if not v.ini or not os.path.exists(v.ini):
        return False, t("[{name}] 还没有生效的 php.ini，无需关闭调试。", name=v.name), ""
    if not _loader_line(v.ini):
        return False, t("[{name}] 的 php.ini 中没有 Xdebug 加载行，未做改动。",
                        name=v.name), ""
    backup = file_backup.backup(v.ini)
    try:
        _set_loader(v.ini, _ext_value(v), False)
    except OSError as e:
        file_backup.restore(backup, v.ini, remove_backup=False)
        return False, t("[{name}] 写入配置失败：{err}", name=v.name, err=e), backup
    ok, detail = verify(v, expect_loaded=False)
    if not ok:
        file_backup.restore(backup, v.ini, remove_backup=False)
        return False, t("[{name}] 自检未通过，已还原配置：{detail}",
                        name=v.name, detail=detail), backup
    return True, t("[{name}] 已关闭 Xdebug 调试；重启该版本后生效。", name=v.name), backup
