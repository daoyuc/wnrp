# -*- coding: utf-8 -*-
"""一键体检 / 502 诊断：把散落的只读检测串成一份分级报告。

设计边界
--------
- **只读**：所有检查都不改任何状态；修复动作由调用方（UI 按钮 / 既有
  ``site``/``hosts``/``services`` 命令）执行，保持命令职责单一（见
  ``docs/DECISIONS.md``）。每个 ``DiagItem`` 只携带「能否安全修复」的人类可读
  说明（``fix`` 字段），真正的写操作复用既有 manager。
- **避免误报**：没有 ``fastcgi_pass`` 的 server 块（静态站点 / 反代）不报错误，
  而是标记 ``skip``（不适用）；只有「版本未运行 / 端口未映射 / 被禁用」这类
  确实会导致 502 的情况才报 ``err``。
- **可测试**：核心检查都是纯函数，接受显式输入；``diagnose_site`` 编排时若未
  传入就绪数据，则从真实 manager 现取（测试可注入假数据）。
"""
import os
import re

from .config import Config
from .i18n import t
from .nginx_manager import NginxManager
from .php_manager import PhpManager, PhpVersion
from .vhost_manager import VhostManager, VhostEntry

#: 检查项级别
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_ERR = "err"
LEVEL_SKIP = "skip"

#: 级别权重（用于汇总；skip「不适用」为中性，不应比 ok 更严重）
_WEIGHT = {LEVEL_ERR: 3, LEVEL_WARN: 2, LEVEL_OK: 0, LEVEL_SKIP: 0}

#: 本地回环地址（hosts 指向这些才算「解析到本机」）
_LOCAL_IPS = ("127.0.0.1", "::1", "0.0.0.0")

#: 入口文件候选
_ENTRY_CANDIDATES = ("index.php", "public/index.php", "index.html", "public/index.html")


class DiagItem:
    """单条诊断结果。

    ``fix_key`` 仅在「可安全一键修复」的检查上设置，供 UI 渲染修复按钮；
    取值：``nginx`` / ``include`` / ``enable`` / ``hosts`` / ``php``。
    ``fix`` 始终是人类可读的修复说明（即便没有按钮也展示，便于手动操作）。
    """

    __slots__ = ("name", "level", "detail", "fix", "fix_key")

    def __init__(self, name: str, level: str, detail: str = "", fix: str = "",
                 fix_key: str = ""):
        self.name = name
        self.level = level
        self.detail = detail
        self.fix = fix
        self.fix_key = fix_key

    def to_dict(self) -> dict:
        return {"name": self.name, "level": self.level,
                "detail": self.detail, "fix": self.fix, "fix_key": self.fix_key}


# --------------------------------------------------------------------------- #
# 纯函数检查项（每个接受显式输入，便于测试注入）
# --------------------------------------------------------------------------- #
def check_nginx_running(running: bool) -> DiagItem:
    if running:
        return DiagItem(t("Nginx 运行"), LEVEL_OK, t("Nginx 正在运行"))
    return DiagItem(
        t("Nginx 运行"), LEVEL_ERR,
        t("Nginx 未运行，所有站点都无法访问（502/无法连接）"),
        t("启动 Nginx（服务编排 → 启动全部，或 nginx start）"),
        fix_key="nginx",
    )


def check_include(covered: bool, vhost_dir: str = "") -> DiagItem:
    if covered:
        return DiagItem(t("站点目录被加载"), LEVEL_OK,
                        t("主配置已 include 站点目录，所有 vhost 会被读取"))
    return DiagItem(
        t("站点目录被加载"), LEVEL_WARN,
        t("主配置未 include 站点目录（{dir}），站点配置不会生效",
          dir=vhost_dir or t("未知")),
        t("一键补 include（在 http 块内加入站点目录）"),
        fix_key="include",
    )


def check_vhost_loaded(entry: VhostEntry) -> DiagItem:
    if not entry.disabled:
        return DiagItem(t("站点未被禁用"), LEVEL_OK,
                        t("配置文件正常参与加载（{f}）", f=entry.file_rel))
    return DiagItem(
        t("站点未被禁用"), LEVEL_ERR,
        t("站点配置已被禁用（.conf.disabled），Nginx 不会加载它"),
        t("重新启用该站点"),
        fix_key="enable",
    )


def check_hosts(entry: VhostEntry, hosts_map: dict) -> DiagItem:
    domains = entry.server_name.split()
    if not domains:
        return DiagItem(t("域名解析"), LEVEL_SKIP, t("无域名（仅 listen 端口的 server 块）"))
    bad = []
    for d in domains:
        ip = (hosts_map or {}).get(d.lower())
        if not ip or ip not in _LOCAL_IPS:
            bad.append(d)
    if not bad:
        return DiagItem(t("域名解析"), LEVEL_OK,
                        t("全部域名已在 hosts 指向本机：{doms}", doms=", ".join(domains)))
    return DiagItem(
        t("域名解析"), LEVEL_WARN,
        t("以下域名未指向本机（hosts 中缺失或非回环）：{doms}", doms=", ".join(bad)),
        t("把缺失域名写入 hosts 指向 127.0.0.1"),
        fix_key="hosts",
    )


def check_port_mapping(entry: VhostEntry, config: Config) -> DiagItem:
    if entry.port is None:
        return DiagItem(
            t("fastcgi 端口映射"), LEVEL_SKIP,
            t("该 server 块没有 fastcgi_pass（静态站点 / 反代），不涉及 PHP，无需 PHP 诊断"),
        )
    if entry.php_version:
        return DiagItem(
            t("fastcgi 端口映射"), LEVEL_OK,
            t("端口 {port} 映射到 PHP 版本 {ver}", port=entry.port, ver=entry.php_version),
        )
    # 端口没映射到任何版本：给出当前映射表便于排错
    mapping = ", ".join(f"{n}→{p}" for n, p in sorted(config.ports.items(),
                                                      key=lambda kv: kv[1]))
    return DiagItem(
        t("fastcgi 端口映射"), LEVEL_ERR,
        t("端口 {port} 未映射到任何 PHP 版本。当前映射：{mapping}",
          port=entry.port, mapping=mapping or t("（空）")),
        t("为该站点指定 PHP 版本（站点 → 切换 PHP 版本）"),
    )


def check_php_running(entry: VhostEntry, php_versions: list) -> DiagItem:
    if entry.port is None or not entry.php_version:
        # 前一条已用 skip/err 说明，这里不再重复
        return DiagItem(t("PHP 版本运行"), LEVEL_SKIP,
                        t("无 PHP 版本关联，跳过运行检查"))
    ver = entry.php_version.split(",")[0].strip()
    found = next((v for v in php_versions if v.name == ver), None)
    if found is None:
        return DiagItem(
            t("PHP 版本运行"), LEVEL_WARN,
            t("未检测到 PHP 版本 {ver}（可能未安装或未扫描到）", ver=ver),
            t("安装该 PHP 版本或改用已安装版本"),
        )
    if found.running:
        return DiagItem(t("PHP 版本运行"), LEVEL_OK,
                        t("PHP {ver} 正在运行（PID {pid}）",
                          ver=ver, pid=getattr(found, "pid", None) or "—"))
    return DiagItem(
        t("PHP 版本运行"), LEVEL_ERR,
        t("PHP {ver} 未运行（端口 {port} 无监听），访问该站点会 502",
          ver=ver, port=entry.port),
        t("启动该 PHP 版本"),
        fix_key="php",
    )


def check_root(entry: VhostEntry) -> DiagItem:
    if not entry.root:
        return DiagItem(
            t("项目根目录"), LEVEL_WARN,
            t("未配置 root（站点无文档根）"),
            t("检查站点配置中的 root 指令"),
        )
    if not os.path.isdir(entry.root):
        return DiagItem(
            t("项目根目录"), LEVEL_ERR,
            t("项目根目录不存在：{root}", root=entry.root),
            t("修正站点 root 路径或部署项目文件"),
        )
    if not any(os.path.exists(os.path.join(entry.root, c)) for c in _ENTRY_CANDIDATES):
        return DiagItem(
            t("项目根目录"), LEVEL_WARN,
            t("根目录下未找到入口文件（index.php / public/index.php 等）：{root}",
              root=entry.root),
            t("确认项目已部署到该目录"),
        )
    return DiagItem(t("项目根目录"), LEVEL_OK,
                    t("根目录存在且含入口文件：{root}", root=entry.root))


def _is_https(entry: VhostEntry) -> bool:
    try:
        with open(entry.file, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    if re.search(r"listen\s+[^;]*\b443\b", text):
        return True
    return "ssl_certificate" in text


def check_https_cert(entry: VhostEntry) -> DiagItem:
    if not _is_https(entry):
        return DiagItem(t("HTTPS 证书"), LEVEL_SKIP,
                        t("非 HTTPS 站点，跳过证书检查"))
    try:
        with open(entry.file, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return DiagItem(t("HTTPS 证书"), LEVEL_WARN,
                        t("无法读取配置文件以确认证书路径"))
    m = re.search(r"ssl_certificate\s+(\S+?)\s*;", text)
    if not m:
        return DiagItem(t("HTTPS 证书"), LEVEL_WARN,
                        t("未找到 ssl_certificate 指令（证书可能由上层配置提供）"))
    cert = m.group(1).strip().strip("\"'")
    if not cert:
        return DiagItem(t("HTTPS 证书"), LEVEL_WARN, t("ssl_certificate 指令为空"))
    if os.path.isabs(cert) and os.path.exists(cert):
        return DiagItem(t("HTTPS 证书"), LEVEL_OK, t("证书文件存在：{p}", p=cert))
    # 相对路径：尝试相对配置所在目录解析
    base = os.path.dirname(entry.file)
    cand = cert if os.path.isabs(cert) else os.path.join(base, cert)
    if os.path.exists(cand):
        return DiagItem(t("HTTPS 证书"), LEVEL_OK, t("证书文件存在：{p}", p=cand))
    return DiagItem(
        t("HTTPS 证书"), LEVEL_WARN,
        t("证书文件不存在：{p}（访问 HTTPS 会报 SSL 错误）", p=cert),
        t("重新生成 / 配置证书（站点 → 启用 HTTPS）"),
    )


def check_error_log(entry: VhostEntry, logs_dir: str, max_lines: int = 80) -> DiagItem:
    if not logs_dir:
        return DiagItem(t("错误日志"), LEVEL_SKIP, t("无日志目录，跳过"))
    log_path = os.path.join(logs_dir, "error.log")
    if not os.path.exists(log_path):
        return DiagItem(t("错误日志"), LEVEL_SKIP, t("未找到 error.log，跳过"))
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-max_lines:]
    except OSError:
        return DiagItem(t("错误日志"), LEVEL_SKIP, t("无法读取 error.log，跳过"))
    domains = [d.lower() for d in entry.server_name.split()]
    hits = []
    for ln in lines:
        low = ln.lower()
        if any(d in low for d in domains) and re.search(r"\b5\d\d\b", ln):
            hits.append(ln.rstrip("\n"))
    if not hits:
        return DiagItem(t("错误日志"), LEVEL_OK,
                        t("最近 {n} 行错误日志中未发现与该域名相关的 5xx",
                          n=max_lines))
    sample = "\n".join(hits[:3])
    return DiagItem(
        t("错误日志"), LEVEL_ERR if len(hits) <= 3 else LEVEL_WARN,
        t("错误日志中发现 {count} 条与该域名相关的 5xx：\n{sample}",
          count=len(hits), sample=sample),
        t("打开 Nginx 日志查看完整错误上下文"),
    )


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def diagnose_site(entry: VhostEntry, config: Config | None = None,
                  vhost_mgr: VhostManager | None = None,
                  php_versions: list | None = None,
                  nginx_running: bool | None = None,
                  hosts_map: dict | None = None,
                  logs_dir: str | None = None) -> list[DiagItem]:
    """对一个站点做完整体检（只读）。未传入的就绪数据从真实 manager 现取。"""
    config = config or Config()
    vhost_mgr = vhost_mgr or VhostManager(config)

    if php_versions is None:
        pm = PhpManager(config)
        pm.scan_versions()
        pm.resolve(refresh_status=True, fast=False)
        php_versions = pm.versions
    if nginx_running is None:
        nginx_running = NginxManager().get_status()[0]
    if logs_dir is None:
        logs_dir = NginxManager().logs_dir
    if hosts_map is None:
        hosts_map = _read_hosts_map()

    items = [
        check_nginx_running(nginx_running),
        check_include(vhost_mgr.include_exists(), vhost_mgr.vhost_dir),
        check_vhost_loaded(entry),
        check_hosts(entry, hosts_map),
        check_port_mapping(entry, config),
        check_php_running(entry, php_versions),
        check_root(entry),
        check_https_cert(entry),
        check_error_log(entry, logs_dir),
    ]
    return items


def _read_hosts_map() -> dict:
    from . import hosts_manager as hm

    path = hm.hosts_path()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    return hm.parse_mapping(text)


def summarize(items: list[DiagItem]) -> str:
    """汇总级别：有 err → err，否则有 warn → warn，否则 ok。"""
    level = LEVEL_OK
    for it in items:
        if _WEIGHT[it.level] > _WEIGHT[level]:
            level = it.level
    return level


def level_label(level: str) -> str:
    """级别对应的展示符号 / 文案（i18n）。"""
    return {
        LEVEL_OK: "✓",
        LEVEL_WARN: "!",
        LEVEL_ERR: "✗",
        LEVEL_SKIP: "·",
    }.get(level, "?")
