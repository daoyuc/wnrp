# -*- coding: utf-8 -*-
"""PHP 扩展管理：本地 ext/ 扫描、ini 启用/禁用写回、第三方扩展在线下载安装。

- 本地：扫描 <php_dir>/ext/*.dll，对照 ini 中 extension= 行判定启用状态；
- 写回：二进制安全（latin-1 无损），先备份 <ini>.bak，逐行替换 extension 行；
- 在线：PECL（windows.php.net/downloads/pecl/releases）与 xdebug.org 两源，
  按「本机 PHP 主版本 + NTS/TS + 编译器 + 架构」自动匹配最新可用 dll；
  版本目录从新到旧回退，保证旧版 PHP 也能装到兼容扩展。
"""
import os
import re
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass

from . import process_utils as pu
from .php_downloader import (
    USER_AGENT,
    _http_get,
    detect_arch,
    detect_os_compiler,
    extract_zip_safe,
)

# 在线扩展目录（key 与 dll 文件名 / PECL 目录名一致）
EXT_CATALOG = [
    {"key": "redis", "name": "Redis", "source": "pecl",
     "desc": "Redis 客户端（PECL 官方 Windows 构建）"},
    {"key": "xdebug", "name": "Xdebug", "source": "xdebug",
     "desc": "调试 / 性能分析（xdebug.org 官方构建）"},
    {"key": "imagick", "name": "ImageMagick", "source": "pecl",
     "desc": "图像处理（使用本机已安装的 ImageMagick 库）"},
    {"key": "swoole", "name": "Swoole", "source": "pecl",
     "desc": "高性能网络框架 / 协程（生产环境常用）"},
    {"key": "memcached", "name": "Memcached", "source": "pecl",
     "desc": "Memcached 客户端（依赖 libmemcached，缺失 VC 运行库可能无法加载）"},
]

PECL_BASE = "https://windows.php.net/downloads/pecl/releases"
XDEBUG_URL = "https://xdebug.org/files/"


@dataclass
class ExtInfo:
    """本地扩展文件信息。"""
    key: str          # 规范化键名（去 php_ 前缀 / .dll）
    dll: str          # dll 文件名，如 php_gd.dll
    path: str         # 完整路径
    enabled: bool     # 是否已在 ini 中启用
    desc: str = ""    # 常见扩展说明


@dataclass
class RuntimeInfo:
    """目标 PHP 运行时特征（用于在线匹配）。"""
    series: str       # 主版本 "8.3"
    ts: str           # "nts" | "ts"
    arch: str         # "x64" | "x86"
    compiler: str     # "vs17" / "vs16" / "vc15" ...（空则无法精确匹配）


# 常见扩展中文说明（本地列表展示用）
_COMMON_DESC = {
    "curl": "HTTP 客户端（cURL）", "gd": "图像处理（GD）",
    "mbstring": "多字节字符串", "mysqli": "MySQL 扩展（旧接口）",
    "pdo_mysql": "MySQL PDO 驱动", "pdo_sqlite": "SQLite PDO 驱动",
    "openssl": "TLS / 加密", "redis": "Redis 客户端",
    "xdebug": "调试 / 性能分析", "zip": "ZIP 归档",
    "intl": "国际化（ICU）", "bcmath": "任意精度数学",
    "soap": "SOAP 客户端", "sockets": "Socket 编程",
    "sodium": "加密（libsodium）", "sqlite3": "SQLite 3",
    "fileinfo": "文件类型识别", "opcache": "字节码缓存",
    "imagick": "ImageMagick 图像处理", "swoole": "协程网络框架",
    "memcached": "Memcached 客户端",
}


# --------------------------------------------------------------------------- #
# 规范化 / 解析
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    """extension 值规范化：php_redis.dll / php_redis / redis → redis。"""
    s = (s or "").strip().lower()
    if s.endswith(".dll"):
        s = s[:-4]
    if s.startswith("php_"):
        s = s[4:]
    return s


def scan_ext_dir(php_dir: str) -> list[ExtInfo]:
    """扫描 <php_dir>/ext/ 下的扩展 dll（忽略 phpdbg 等非扩展文件）。"""
    ext_dir = os.path.join(php_dir, "ext")
    infos: list[ExtInfo] = []
    if not os.path.isdir(ext_dir):
        return infos
    for fname in sorted(os.listdir(ext_dir)):
        if not fname.lower().endswith(".dll"):
            continue
        if not fname.lower().startswith("php_"):
            continue
        infos.append(ExtInfo(
            key=_norm(fname),
            dll=fname,
            path=os.path.join(ext_dir, fname),
            enabled=False,
            desc=_COMMON_DESC.get(_norm(fname), ""),
        ))
    return infos


def read_enabled_exts(ini_path: str) -> set[str]:
    """解析 ini 中已启用的扩展（规范化键集合）。"""
    enabled: set[str] = set()
    try:
        with open(ini_path, "rb") as f:
            raw = f.read()
    except OSError:
        return enabled
    for line in raw.decode("latin-1").splitlines():
        if line.lstrip().startswith(";"):
            continue
        m = re.match(r"^\s*extension\s*=\s*(\S+)", line, re.IGNORECASE)
        if m:
            enabled.add(_norm(m.group(1)))
    return enabled


# --------------------------------------------------------------------------- #
# ini 写回（启用 / 禁用）
# --------------------------------------------------------------------------- #
def apply_extensions(ini_path: str, enable: set[str], disable: set[str]) -> tuple[int, str]:
    """写回 extension 行：enable 取消注释/追加，disable 注释。

    返回 (修改行数, 备份路径)。二进制 latin-1 无损；写入失败自动还原备份。
    """
    enable = {_norm(e) for e in enable} - {_norm(d) for d in disable}
    disable = {_norm(e) for e in disable}
    with open(ini_path, "rb") as f:
        raw = f.read()
    lines = raw.decode("latin-1").splitlines(keepends=True)
    backup = ini_path + ".bak"
    shutil.copy2(ini_path, backup)

    def _to_dll_name(key: str) -> str:
        return f"php_{key}" if not key.startswith("php_") else key

    out: list[str] = []
    handled: set[str] = set()
    for line in lines:
        m = re.match(r"^(\s*;?\s*extension\s*=\s*)(\S+)(.*)$", line, re.IGNORECASE)
        if m:
            key = _norm(m.group(2))
            if key in enable:
                out.append(f"extension={_to_dll_name(key)}{m.group(3)}\n")
                handled.add(key)
                continue
            if key in disable:
                out.append(f";extension={_to_dll_name(key)}\n")
                handled.add(key)
                continue
        out.append(line)

    for key in sorted(enable):
        if key not in handled:
            out.append(f"extension={_to_dll_name(key)}\n")

    try:
        with open(ini_path, "wb") as f:
            f.write("".join(out).encode("latin-1"))
    except OSError:
        shutil.copy2(backup, ini_path)
        raise
    return len(enable | disable), backup


# --------------------------------------------------------------------------- #
# 运行时特征探测
# --------------------------------------------------------------------------- #
def detect_runtime(php_dir: str) -> RuntimeInfo:
    """探测目标 PHP 的主版本 / NTS-TS / 架构 / 编译器。"""
    arch = detect_arch()
    exe = os.path.join(php_dir, "php.exe")
    if not os.path.exists(exe):
        exe = os.path.join(php_dir, "php-cgi.exe")
    text = ""
    if os.path.exists(exe):
        _, out, err = pu.run_cmd([exe, "-v"], timeout=10)
        text = (out or "") + (err or "")
    m = re.search(r"PHP\s+(\d+\.\d+)", text)
    series = m.group(1) if m else ""
    low = text.lower()
    if "nts" in low:
        ts = "nts"
    elif re.search(r"\bts\b|\(zts", low):
        ts = "ts"
    else:
        ts = "nts"
    return RuntimeInfo(
        series=series,
        ts=ts,
        arch=arch,
        compiler=detect_os_compiler(series) or "",
    )


# --------------------------------------------------------------------------- #
# 在线下载匹配
# --------------------------------------------------------------------------- #
# PECL 文件：php_redis-6.0.2-8.3-nts-vs16-x64.zip
_PECL_RE = re.compile(
    r"php_([A-Za-z0-9_]+)-([\d.]+)-(\d+\.\d+)-(nts|ts)-(vs\d+|vc\d+)-(x64|x86)\.zip",
    re.IGNORECASE,
)
# Xdebug 文件：php_xdebug-3.3.2-8.2-nts-vs16-x64.dll
_XDEBUG_RE = re.compile(
    r"php_([A-Za-z0-9_]+)-([\d.]+)-(\d+\.\d+)-(nts|ts)-(vs\d+|vc\d+)-(x64|x86)\.dll",
    re.IGNORECASE,
)
# 编译器优先级：新编译器优先（与官方包演进一致）
_COMPILER_ORDER = ("vs17", "vs16", "vc15", "vc14", "vc11", "vc9")


def _match_file(html: str, ext_key: str, rt: RuntimeInfo, dll_mode: bool) -> str | None:
    """在目录 HTML 中找匹配 dll 或 zip 文件名（从新编译器到旧编译器）。"""
    pattern = _XDEBUG_RE if dll_mode else _PECL_RE
    found: dict[str, str] = {}
    for m in pattern.finditer(html):
        key, ver, series, ts, compiler, arch = m.groups()
        if key.lower() != ext_key.lower():
            continue
        if series != rt.series:
            continue
        if ts.lower() != rt.ts:
            continue
        if arch.lower() != rt.arch:
            continue
        found.setdefault(compiler.lower(), m.group(0))
    if not found:
        return None
    for c in _COMPILER_ORDER:
        if c in found:
            return found[c]
    # 回退：任意匹配项
    return next(iter(found.values()))


def _pick_pecl(ext_key: str, rt: RuntimeInfo, progress=None) -> tuple[str, str]:
    """在 PECL 版本目录（新→旧）中找首个匹配的 zip，返回 (zip_url, zip_file)。"""
    base = f"{PECL_BASE}/{ext_key}"
    if progress:
        progress(f"获取 {ext_key} 版本列表…")
    html = _http_get(base + "/").decode("utf-8", errors="replace")
    versions = [h.split("/")[0] for h in re.findall(r'href="([\d.]+)/"', html)]
    versions.sort(key=_ver_key, reverse=True)
    if not versions:
        raise RuntimeError(f"PECL 未找到 {ext_key} 的版本目录")
    for ver in versions:
        if progress:
            progress(f"匹配 {ext_key}-{ver} 与 PHP {rt.series} {rt.ts.upper()} {rt.arch}…")
        page = _http_get(f"{base}/{ver}/").decode("utf-8", errors="replace")
        fname = _match_file(page, ext_key, rt, dll_mode=False)
        if fname:
            return f"{base}/{ver}/{fname}", fname
    raise RuntimeError(
        f"PECL 无适配本机 {rt.series} {rt.ts.upper()} {rt.arch} 的 {ext_key} 构建。\n"
        f"该扩展可能尚未提供当前 PHP 主版本的 Windows 包。"
    )


def _ver_key(v: str) -> tuple:
    return tuple(int(x) for x in re.split(r"\.", v) if x.isdigit())


def install_online(catalog_item: dict, php_dir: str, rt: RuntimeInfo,
                   progress=None) -> tuple[bool, str, str]:
    """在线安装单个扩展。返回 (ok, message, dll 文件名)。

    progress(msg: str) 阶段文本回调。dll 下载/解压到临时区后原子落入 ext/。
    """
    def report(msg):
        if progress:
            progress(msg)

    ext_key = catalog_item["key"]
    ext_dir = os.path.join(php_dir, "ext")
    os.makedirs(ext_dir, exist_ok=True)

    # 已存在同名 dll（无论是否启用）直接跳过
    existing = [f for f in os.listdir(ext_dir) if _norm(f) == ext_key]
    if existing:
        return True, f"{ext_key} 扩展已存在于 {ext_dir}\\{existing[0]}，无需重复下载。", existing[0]

    tmp = os.path.join(php_dir, "..", "phpvm", ".tmp")
    os.makedirs(tmp, exist_ok=True)
    tmp_file = ""
    try:
        if catalog_item.get("source") == "xdebug":
            if progress:
                report("获取 xdebug.org 文件列表…")
            html = _http_get(XDEBUG_URL).decode("utf-8", errors="replace")
            fname = _match_file(html, ext_key, rt, dll_mode=True)
            if not fname:
                raise RuntimeError(
                    f"xdebug.org 无适配本机 {rt.series} {rt.ts.upper()} {rt.arch} 的构建。"
                )
            url = XDEBUG_URL + fname
            tmp_file = os.path.join(tmp, fname)
            report(f"下载 {fname} …")
            _download(url, tmp_file)
        else:
            url, fname = _pick_pecl(ext_key, rt, report)
            tmp_file = os.path.join(tmp, fname)
            report(f"下载 {fname} …")
            _download(url, tmp_file)

        # 从 zip 中提取 dll（PECL）；dll 模式直接使用
        dll_name = fname
        if fname.lower().endswith(".zip"):
            extract_dir = os.path.join(tmp, f"ext_{ext_key}_src")
            if os.path.exists(extract_dir):
                shutil.rmtree(extract_dir, ignore_errors=True)
            report(f"解压 {fname} …")
            extract_zip_safe(tmp_file, extract_dir)
            dlls = [f for f in os.listdir(extract_dir)
                    if f.lower().endswith(".dll") and f.lower().startswith("php_")]
            if not dlls:
                raise RuntimeError(f"压缩包内未找到 php_{ext_key}.dll")
            dll_name = dlls[0]
            src = os.path.join(extract_dir, dll_name)
        else:
            src = tmp_file
            if not src.lower().endswith(".dll"):
                raise RuntimeError(f"下载内容不是 .dll 文件：{fname}")

        # 落入 ext/：同名覆盖前保留 .bak
        dest = os.path.join(ext_dir, dll_name)
        if os.path.exists(dest):
            try:
                shutil.copy2(dest, dest + ".bak")
            except OSError:
                pass
        report(f"写入 {dll_name} …")
        shutil.copy2(src, dest)
        return True, f"已安装 {dll_name}（可通过「本地扩展」页启用）。", dll_name
    finally:
        for p in (tmp_file, os.path.join(tmp, f"ext_{ext_key}_src")):
            try:
                if p and os.path.isfile(p):
                    os.remove(p)
                elif p and os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass


def _download(url: str, dest: str) -> None:
    """简单下载（无进度条，小体积扩展）。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
