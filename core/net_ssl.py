# -*- coding: utf-8 -*-
"""core 层 HTTPS 请求共用的 SSL 上下文。

现象：Windows 系统证书库可能残留已过期的根 / 交叉签名证书（典型是 Let's Encrypt
的 DST Root CA X3，2021-09-30 过期）。OpenSSL 1.1.x 依此构建信任链，会对
windows.php.net、www.adminer.org、xdebug.org 等站点报
'[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate has
expired'，即使站点证书本身有效；GitHub 这类非 LE 链则不受影响。

处理：在系统默认根证书（企业 / 私有 CA、SSL_CERT_FILE 环境变量仍生效）之外，
额外加载随仓库分发的新 CA 包 core/cacert.pem，让 OpenSSL 能选到未过期的信任链。
全程保持证书校验与主机名校验开启，不降级为「不校验」，也不引入第三方依赖。

core/cacert.pem 取自 certifi 打包的 Mozilla CA 证书集（MPL-2.0）。根证书有效期
以十年计，定期检查更新即可。所有 core/ 下的 HTTPS 请求都应通过 ssl_context() 取
上下文，别再直接用 urlopen 的默认上下文。
"""
import functools
import os
import ssl

_CA_BUNDLE = os.path.join(os.path.dirname(__file__), "cacert.pem")


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """返回供 urllib 使用的校验上下文（缓存复用，避免重复读盘）。

    信任源 = 系统默认根 + 随仓库分发的 core/cacert.pem；证书校验与主机名校验均
    保持开启。CA 包缺失时退回系统默认（不比现状更差），但会重新暴露过期根问题。
    """
    ctx = ssl.create_default_context()
    if os.path.isfile(_CA_BUNDLE):
        ctx.load_verify_locations(cafile=_CA_BUNDLE)
    return ctx
