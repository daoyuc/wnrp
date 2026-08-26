# -*- coding: utf-8 -*-
"""PHP 版本安装编排：下载 → SHA-256 校验 → 解压 → 落盘 → ini 生成 → 端口分配 → 运行验证。

设计原则：
- 安装原子性：下载到 phpvm/.tmp 临时区，校验通过后再落盘，任何一步失败均清理
- 目录/端口命名与现有扫描规则完全一致（php83 / 9083 …），落盘即被 scan_versions 识别
- ini 生成基于包内 php.ini-development，输出 php.ini（CLI）与 php-web.ini（FastCGI）
"""
import os
import re
import shutil
from dataclasses import dataclass

from . import process_utils as pu
from .config import DEFAULT_PORTS, WNRP_ROOT, Config
from .php_downloader import (
    PhpPackage,
    download_with_progress,
    extract_zip_safe,
    verify_sha256,
)

CLI_NAME = "php.exe"
INI_NAME = "php.ini"
WEB_INI_NAME = "php-web.ini"
TMP_ROOT = os.path.join(WNRP_ROOT, "phpvm", ".tmp")

# 启用扩展清单（从 php.ini-development 取消注释）
ENABLE_EXTENSIONS = {
    "bz2", "curl", "exif", "fileinfo", "gd", "gettext", "gmp", "intl",
    "ldap", "mbstring", "mysqli", "opcache", "openssl", "pdo_mysql",
    "pdo_sqlite", "sodium", "soap", "sockets", "sqlite3", "zip",
}

# FastCGI 附加配置（追加到 php-web.ini 末尾，覆盖旧值）
FASTCGI_EXTRA = [
    "cgi.fix_pathinfo=1",
    "cgi.force_redirect=0",
    "cgi.fastcgi=1",
    "opcache.enable=1",
    "opcache.enable_cli=0",
]

MINIMAL_INI = """; phpvm 生成的最小可用配置（包内缺少 ini 模板时兜底）
extension_dir = "ext"
date.timezone = Asia/Shanghai
display_errors = On
cgi.force_redirect = 0
cgi.fix_pathinfo = 1
"""


@dataclass
class InstallResult:
    """安装结果。"""
    ok: bool
    message: str
    name: str | None = None       # 目录名 php83 / php70 ...
    version: str | None = None    # 安装后的真实版本号
    port: int | None = None
    dir: str | None = None


# --------------------------------------------------------------------------- #
# 命名规则（与现有扫描约定完全一致）
# --------------------------------------------------------------------------- #
def install_dir_for(series: str) -> str:
    """系列版本 → 目录名：8.4→php84、8.0→php8、7.3→php73、5.6→php56。"""
    try:
        major, minor = (int(x) for x in series.split("."))
    except (ValueError, AttributeError):
        raise ValueError(f"非法版本系列：{series!r}")
    if major == 8:
        return "php8" if minor == 0 else f"php{major}{minor}"
    if major == 5:
        return "php" if minor <= 5 else f"php{major}{minor}"
    return f"php{major}{minor}"


def default_port_for(name: str, config: Config) -> int:
    """目录名 → 默认端口：已有约定的按约定，否则按 90{主}{次} 规则。"""
    dflt = config.ports.get(name, DEFAULT_PORTS.get(name))
    if dflt is not None:
        return dflt
    m = re.search(r"(\d+)$", name)
    if m:
        return 9000 + int(m.group(1))
    return 9001


# --------------------------------------------------------------------------- #
# ini 生成
# --------------------------------------------------------------------------- #
def _process_ini_lines(lines: list[str]) -> list[str]:
    """逐行处理模板：启用扩展、固定 extension_dir / date.timezone。"""
    out: list[str] = []
    for raw in lines:
        line = raw.strip()
        m = re.match(r"^;?\s*extension\s*=\s*([A-Za-z0-9_.\-]+)\s*$", line, re.I)
        if m and m.group(1).lower() in ENABLE_EXTENSIONS:
            out.append(f"extension={m.group(1)}")
            continue
        m = re.match(r"^;?\s*extension_dir\s*=\s*\"?([^\";]*)\"?\s*$", line, re.I)
        if m and m.group(1).strip().lower() in ("ext", "."):
            out.append('extension_dir = "ext"')
            continue
        if re.match(r"^;?\s*date\.timezone\s*=", line, re.I):
            out.append("date.timezone = Asia/Shanghai")
            continue
        out.append(raw)
    return out


def generate_ini(php_dir: str) -> None:
    """基于 php.ini-development 生成 php.ini（CLI）与 php-web.ini（FastCGI）。"""
    dev = os.path.join(php_dir, "php.ini-development")
    prod = os.path.join(php_dir, "php.ini-production")
    src = dev if os.path.exists(dev) else (prod if os.path.exists(prod) else None)
    if src:
        with open(src, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    else:
        lines = MINIMAL_INI.splitlines()

    ini_text = "\n".join(_process_ini_lines(lines)) + "\n"
    with open(os.path.join(php_dir, INI_NAME), "w", encoding="utf-8", newline="\n") as f:
        f.write(ini_text)

    # FastCGI 版本：额外追加关键项（出现在末尾，PHP 解析后值覆盖先前值）
    web_text = ini_text.rstrip() + "\n\n; ---------- phpvm FastCGI ----------\n" + "\n".join(FASTCGI_EXTRA) + "\n"
    with open(os.path.join(php_dir, WEB_INI_NAME), "w", encoding="utf-8", newline="\n") as f:
        f.write(web_text)


def _find_real_root(extract_dir: str) -> str:
    """部分旧包 zip 内带单层前缀目录，剥掉它。"""
    entries = [e for e in os.listdir(extract_dir) if e not in {"__MACOSX"}]
    if len(entries) == 1:
        sub = os.path.join(extract_dir, entries[0])
        if os.path.isdir(sub) and os.path.exists(os.path.join(sub, CLI_NAME)):
            return sub
    return extract_dir


def _detect_vc_missing(text: str) -> bool:
    """识别缺少 VC 运行时（VCRUNTIME140.dll 等）的典型报错。"""
    low = text.lower()
    return "vcruntime" in low or "msvcp" in low or "api-ms-win" in low or "无法继续执行代码" in text


# --------------------------------------------------------------------------- #
# 安装编排
# --------------------------------------------------------------------------- #
def install(pkg: PhpPackage, config: Config, progress=None) -> InstallResult:
    """执行完整安装流程。

    progress(stage: str, ratio: float | None)：stage 为 下载/校验/解压/落盘/生成配置/验证；
    ratio 为 0~1 进度（None 表示不确定阶段）。
    任何一步失败均清理临时文件；目标目录冲突 / 端口冲突提前返回不触碰磁盘。
    """
    def report(stage: str, ratio: float | None = None):
        if progress:
            progress(stage, ratio)

    name = install_dir_for(pkg.series)
    target_dir = os.path.join(WNRP_ROOT, name)
    if os.path.exists(target_dir):
        return InstallResult(False, f"目标目录 {target_dir} 已存在，请勿重复安装。", name=name)

    port = default_port_for(name, config)
    conflict = config.validate_unique(name, port)
    if conflict:
        return InstallResult(
            False,
            f"{conflict}。\n请在安装完成后到「编辑端口」中为该版本调整端口，"
            f"并同步修改 nginx vhost 的 fastcgi_pass。",
            name=name, port=port,
        )

    tmp_root = TMP_ROOT
    os.makedirs(tmp_root, exist_ok=True)
    zip_path = os.path.join(tmp_root, os.path.basename(pkg.url))
    extract_dir = os.path.join(tmp_root, f"{name}_src")
    target_ready = False
    try:
        # 1. 下载（带进度）
        report("下载", 0.0)
        download_with_progress(
            pkg.url, zip_path,
            lambda done, total: report("下载", (done / total) if total else None),
        )

        # 2. SHA-256 校验（archives 无元数据时跳过，空串 verify_sha256 返回 True）
        report("校验", None)
        if not verify_sha256(zip_path, pkg.sha256):
            return InstallResult(False, "SHA-256 校验失败：文件可能被篡改或下载不完整，已自动清理临时文件。", name=name)

        # 3. 解压到临时目录
        report("解压", 0.0)
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        os.makedirs(extract_dir, exist_ok=True)
        extract_zip_safe(zip_path, extract_dir, lambda done, total: report("解压", done / total))
        root = _find_real_root(extract_dir)
        if not os.path.exists(os.path.join(root, "php-cgi.exe")) and \
                not os.path.exists(os.path.join(root, CLI_NAME)):
            raise RuntimeError("解压内容异常：未找到 php.exe / php-cgi.exe")

        # 4. 落盘到 C:\wnrp\php{XX}
        report("落盘", None)
        os.makedirs(target_dir, exist_ok=True)
        for item in os.listdir(root):
            shutil.move(os.path.join(root, item), os.path.join(target_dir, item))
        target_ready = True

        # 5. 生成 php.ini / php-web.ini
        report("生成配置", None)
        generate_ini(target_dir)

        # 6. 端口持久化
        config.set_port(name, port)

        # 7. php -v 运行验证（探测 VC 运行时缺失）
        report("验证", None)
        exe = os.path.join(target_dir, CLI_NAME)
        if not os.path.exists(exe):
            exe = os.path.join(target_dir, "php-cgi.exe")
        _, out, err = pu.run_cmd([exe, "-v"], timeout=15)
        text = (out or "") + (err or "")
        ver_m = re.search(r"PHP\s+([0-9]+\.[0-9]+\.[0-9]+)", text)
        if ver_m:
            return InstallResult(
                True, f"PHP {ver_m.group(1)} 安装成功（{name}，默认端口 {port}）",
                name=name, version=ver_m.group(1), port=port, dir=target_dir,
            )
        if _detect_vc_missing(text):
            return InstallResult(
                False,
                f"{name} 已安装，但缺少 VC 运行时导致无法运行：\n{text.strip()[:300]}\n"
                f"请下载安装对应的 vc_redist.x64.exe（{pkg.compiler} 对应运行库）后重试。",
                name=name, port=port, dir=target_dir,
            )
        return InstallResult(
            False, f"{name} 已安装，但 php -v 验证失败：\n{text.strip()[:300]}",
            name=name, port=port, dir=target_dir,
        )
    except Exception as e:  # noqa: BLE001
        return InstallResult(False, f"安装失败：{type(e).__name__}：{e}", name=name)
    finally:
        # 清理临时文件（下载包 + 解压目录）
        for p in (zip_path, extract_dir):
            try:
                if os.path.isfile(p):
                    os.remove(p)
                elif os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
        # 落盘未完成时清理目标目录，避免残留半成品
        if not target_ready and os.path.exists(target_dir):
            shutil.rmtree(target_dir, ignore_errors=True)
