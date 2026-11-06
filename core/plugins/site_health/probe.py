# -*- coding: utf-8 -*-
"""站点巡检核心：把 vhost 站点展开为域名，逐个做一次只读 GET 探测。

口径与边界：

- 探测地址只由 nginx ``server_name`` 生成（泛解析 / 非法域名 / 禁用站点跳过），
  不接受「传任意路径 / 任意 URL」的入口；
- 只发 ``GET /``，不跟随重定向（3xx 原样记录 Location），不携带 Cookie / 认证头；
- 响应体最多读 :data:`MAX_BODY` 字节用于计数，读完即关闭连接；
- 不经过系统代理：``http.client`` 直连，本机站点不会被打到代理上；
- HTTPS 先按系统根校验证书；校验失败（本地自签）降级为不校验证书重试，
  此时 ``insecure=True`` 且不读取证书到期时间；校验通过时给出 ``cert_days``；
- 不落库、不写文件、不起后台进程。
"""
import http.client
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

DEFAULT_TIMEOUT = 5.0
MAX_BODY = 64 * 1024
USER_AGENT = "phpvm-site-health/1.0"
SCHEMES = ("http", "https")
#: 错误码 → i18n 源文案（界面与 CLI 统一用 ``t(ERROR_MESSAGES[code])`` 展示）
ERROR_MESSAGES = {
    "bad_url": "地址非法",
    "timeout": "连接超时",
    "refused": "连接被拒绝（站点或 PHP 未启动？）",
    "dns": "域名无法解析（hosts 未映射？）",
    "tls": "TLS 握手失败",
    "network": "网络错误",
}
ERROR_CODES = tuple(ERROR_MESSAGES)


# --------------------------------------------------------------------------- #
# 站点来源
# --------------------------------------------------------------------------- #
def targets(entries, include_disabled: bool = False) -> list[dict]:
    """把 ``VhostEntry`` 列表展开为待探测域名（去重、排序、跳过不可探测项）。

    ``server_name`` 一行可能含多个域名；``*.x`` 泛解析无法解析出具体主机，
    单标签 ``_``（nginx 默认 server 的写法）同样跳过。已禁用站点默认不探测
    （nginx 根本没加载它，探测必然失败，只会制造噪音）。
    """
    from ...vhost_manager import valid_domain

    out: list[dict] = []
    seen: set[str] = set()
    for entry in entries or []:
        if getattr(entry, "disabled", False) and not include_disabled:
            continue
        names = str(getattr(entry, "server_name", "") or "").split()
        for raw in names:
            name = raw.strip().lower()
            if not name or name.startswith("*") or name.startswith("_"):
                continue
            if not valid_domain(name) or name in seen:
                continue
            seen.add(name)
            out.append({
                "domain": name,
                "root": str(getattr(entry, "root", "") or ""),
                "php_version": str(getattr(entry, "php_version", "") or ""),
                "file": str(getattr(entry, "file", "") or ""),
            })
    out.sort(key=lambda it: it["domain"])
    return out


def sites(config=None, entries=None, include_disabled: bool = False) -> list[dict]:
    """当前 nginx 站点的待探测域名清单（只读扫描，不接触网络）。"""
    if entries is None:
        from ...vhost_manager import VhostManager

        entries = VhostManager(config).scan(include_disabled=include_disabled)
    return targets(entries, include_disabled=include_disabled)


# --------------------------------------------------------------------------- #
# 单站点探测
# --------------------------------------------------------------------------- #
def _unverified_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _connect(scheme: str, host: str, port: int, timeout: float,
             verify: bool = True):
    if scheme == "https":
        ctx = ssl.create_default_context() if verify else _unverified_context()
        conn = http.client.HTTPSConnection(host, port, timeout=timeout,
                                           context=ctx)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    conn.connect()
    return conn


def _cert_days(sock) -> int | None:
    """TLS 证书剩余天数（按 ``notAfter`` 计算；取不到返回 None）。"""
    try:
        cert = sock.getpeercert() or {}
    except (AttributeError, ValueError):
        return None
    not_after = cert.get("notAfter")
    if not not_after:
        return None
    try:
        expires = ssl.cert_time_to_seconds(not_after)
    except ValueError:
        return None
    return int((expires - time.time()) // 86400)


def error_code(exc: BaseException) -> str:
    """把网络异常归一为稳定的错误码（界面 / CLI 负责翻译成文案）。"""
    if isinstance(exc, socket.timeout):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, ssl.SSLError):
        return "tls"
    return "network"


def _failed(url: str, scheme: str, code: str, started: float,
            host: str = "", port: int | None = None,
            insecure: bool = False) -> dict:
    return {
        "url": url, "scheme": scheme, "host": host, "port": port,
        "status": None, "state": "down", "ok": False,
        "ms": int((time.monotonic() - started) * 1000),
        "bytes": 0, "location": "", "error": code,
        "insecure": insecure, "cert_days": None,
    }


def probe(url: str, timeout: float = DEFAULT_TIMEOUT,
          max_body: int = MAX_BODY) -> dict:
    """对单个 URL 做一次 GET 探测，返回可 JSON 序列化的结果字典。

    ``state``：``ok``（<400）/ ``warn``（4xx）/ ``down``（5xx 或连不上）。
    任何异常都转成 ``error`` 错误码，绝不向上抛（面板与 CLI 共用）。
    """
    started = time.monotonic()
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in SCHEMES or not parts.hostname:
        return _failed(url, scheme or "http", "bad_url", started)
    host = parts.hostname
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return _failed(url, scheme, "bad_url", started, host=host)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query

    insecure = False
    try:
        conn = _connect(scheme, host, port, timeout)
    except ssl.SSLCertVerificationError:
        insecure = True
        try:
            conn = _connect(scheme, host, port, timeout, verify=False)
        except Exception as e:  # noqa: BLE001 - 统一转错误码
            return _failed(url, scheme, error_code(e), started, host, port,
                           insecure=True)
    except Exception as e:  # noqa: BLE001
        return _failed(url, scheme, error_code(e), started, host, port)

    try:
        cert_days = None if insecure else (
            _cert_days(getattr(conn, "sock", None)) if scheme == "https" else None)
        conn.request("GET", path, headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Connection": "close",
        })
        resp = conn.getresponse()
        body = resp.read(max_body)
        status = int(resp.status)
        state = "ok" if status < 400 else ("warn" if status < 500 else "down")
        return {
            "url": url, "scheme": scheme, "host": host, "port": port,
            "status": status, "state": state, "ok": state != "down",
            "ms": int((time.monotonic() - started) * 1000),
            "bytes": len(body), "location": resp.getheader("Location") or "",
            "error": "", "insecure": insecure, "cert_days": cert_days,
        }
    except Exception as e:  # noqa: BLE001
        return _failed(url, scheme, error_code(e), started, host, port,
                       insecure=insecure)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响结果
            pass


# --------------------------------------------------------------------------- #
# 批量探测
# --------------------------------------------------------------------------- #
def report(config=None, entries=None, scheme: str = "http",
           timeout: float = DEFAULT_TIMEOUT, only: str = "",
           workers: int = 6, limit: int = 0) -> dict:
    """并发探测全部站点，返回站点明细与汇总。

    ``only`` 按域名子串过滤（CLI ``--domain`` / 面板搜索）；``limit`` 限制数量
    用于「只测前 N 个」的快速验证。``entries`` 可注入（测试用，免读真实配置）。
    """
    scheme = "https" if str(scheme).lower() == "https" else "http"
    items = sites(config, entries=entries)
    needle = (only or "").strip().lower()
    if needle:
        items = [it for it in items if needle in it["domain"]]
    if limit and limit > 0:
        items = items[:limit]

    urls = [f"{scheme}://{it['domain']}/" for it in items]
    results: list[dict] = []
    if urls:
        pool_size = max(1, min(int(workers or 1), len(urls)))
        with ThreadPoolExecutor(max_workers=pool_size) as pool:
            results = list(pool.map(
                lambda u: probe(u, timeout=timeout), urls))

    rows: list[dict] = []
    for item, res in zip(items, results):
        row = dict(item)
        for key in ("url", "scheme", "status", "state", "ms", "bytes",
                    "location", "error", "insecure", "cert_days"):
            row[key] = res.get(key)
        rows.append(row)

    measured = [r["ms"] for r in rows
                if r.get("state") != "down" and isinstance(r.get("ms"), int)]
    return {
        "ok": True,
        "scheme": scheme,
        "timeout": timeout,
        "total": len(rows),
        "up": sum(1 for r in rows if r.get("state") == "ok"),
        "warn": sum(1 for r in rows if r.get("state") == "warn"),
        "down": sum(1 for r in rows if r.get("state") == "down"),
        "avg_ms": int(sum(measured) / len(measured)) if measured else None,
        "sites": rows,
    }
