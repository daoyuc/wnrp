# -*- coding: utf-8 -*-
"""本地 HTTPS 证书：探测 openssl / mkcert 并为站点生成自签证书。

边界（重要）：
- Python 标准库无法签发 X.509 证书，必须依赖外部 openssl / mkcert；
- 探测不到时**只返回检测报告与安装指引**，不内置下载任何第三方二进制；
- 生成方式优先级：mkcert（自动信任本地 CA）> openssl 自签（浏览器需手动信任）。
"""
import os
import re
import shutil

from . import process_utils as pu
from .config import IS_WIN
from .i18n import t
from .nginx_manager import NginxManager

_MKCERT = "mkcert.exe" if IS_WIN else "mkcert"
_OPENSSL = "openssl.exe" if IS_WIN else "openssl"
# 常见安装位置（Git for Windows / OpenSSL-Win64 / Homebrew）
_EXTRA_OPENSSL = (
    r"C:\Program Files\Git\usr\bin\openssl.exe",
    r"C:\Program Files (x86)\Git\usr\bin\openssl.exe",
    r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
    r"C:\Program Files\OpenSSL-Win32\bin\openssl.exe",
    r"C:\OpenSSL-Win64\bin\openssl.exe",
    "/opt/homebrew/opt/openssl@3/bin/openssl",
    "/usr/local/opt/openssl@3/bin/openssl",
    "/usr/bin/openssl",
)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def ssl_dir() -> str:
    """证书输出目录：<nginx 前缀>/SSL（不存在则尝试创建）。"""
    prefix = NginxManager().prefix
    base = os.path.join(prefix, "SSL")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        pass
    return base


def _which(name: str) -> str:
    """在 PATH 中查找可执行文件；返回绝对路径或空串。"""
    found = shutil.which(name)
    return found or ""


def detect_mkcert() -> str:
    """mkcert 可执行文件路径；未安装返回空串。"""
    return _which(_MKCERT)


def detect_openssl() -> str:
    """openssl 可执行文件路径（PATH + 常见安装位置）；未找到返回空串。"""
    found = _which(_OPENSSL)
    if found:
        return found
    for p in _EXTRA_OPENSSL:
        if os.path.exists(p):
            return p
    return ""


def status() -> dict:
    """证书能力检测：{ok, mkcert, openssl, dir, message}。"""
    mk = detect_mkcert()
    ox = detect_openssl()
    info = {
        "ok": bool(mk or ox),
        "mkcert": mk,
        "openssl": ox,
        "dir": ssl_dir(),
        "message": "",
    }
    if mk:
        info["message"] = t("已检测到 mkcert：{path}（生成的证书会被本机自动信任）", path=mk)
    elif ox:
        info["message"] = t("已检测到 openssl：{path}（自签证书需浏览器手动信任）", path=ox)
    else:
        info["message"] = t(
            "未找到 openssl 与 mkcert，无法生成本地 HTTPS 证书。\n"
            "可选安装方式：\n"
            "  · Windows（推荐）：choco install mkcert  或  scoop install mkcert\n"
            "  · macOS：brew install mkcert nss\n"
            "  · 或安装 Git for Windows / OpenSSL-Win64 后重启 phpvm 自动探测")
    return info


def cert_paths(domain: str) -> tuple[str, str]:
    """域名对应的证书 / 私钥路径。"""
    safe = _UNSAFE.sub("_", (domain or "").strip().lower().lstrip("*.")).strip("._-") or "site"
    base = os.path.join(ssl_dir(), safe)
    return base + ".crt", base + ".key"


def ensure_site_cert(domain: str) -> dict:
    """为域名生成证书（已存在则复用）。返回 {ok, cert, key, created, message}。"""
    dom = (domain or "").strip().lower()
    if not dom:
        return {"ok": False, "cert": "", "key": "", "created": False,
                "message": t("未指定域名")}
    cert, key = cert_paths(dom)
    if os.path.exists(cert) and os.path.exists(key):
        return {"ok": True, "cert": cert, "key": key, "created": False,
                "message": t("已存在证书，直接复用：{path}", path=cert)}

    mk = detect_mkcert()
    if mk:
        code, out, err = pu.run_cmd(
            [mk, "-cert-file", cert, "-key-file", key, dom], timeout=180)
        if code == 0 and os.path.exists(cert) and os.path.exists(key):
            return {"ok": True, "cert": cert, "key": key, "created": True,
                    "message": t("已用 mkcert 生成证书：{path}", path=cert)}
        mk_err = (err or out or "").strip()
    else:
        mk_err = ""

    ox = detect_openssl()
    if not ox:
        st = status()
        return {"ok": False, "cert": cert, "key": key, "created": False,
                "message": (mk_err + "\n" if mk_err else "") + st["message"]}

    cmd = [ox, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
           "-keyout", key, "-out", cert, "-days", "3650",
           "-subj", f"/CN={dom}",
           "-addext", f"subjectAltName=DNS:{dom},DNS:*.{dom}"]
    code, out, err = pu.run_cmd(cmd, timeout=180)
    if code == 0 and os.path.exists(cert) and os.path.exists(key):
        return {"ok": True, "cert": cert, "key": key, "created": True,
                "message": t("已用 openssl 生成自签证书（浏览器需手动信任）：{path}",
                             path=cert)}
    return {"ok": False, "cert": cert, "key": key, "created": False,
            "message": t("证书生成失败：{err}", err=(err or out or t("无输出")).strip())}


def remove_cert(domain: str) -> None:
    """删除该域名本次生成的证书与私钥（失败静默）。"""
    cert, key = cert_paths(domain)
    for p in (cert, key):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
