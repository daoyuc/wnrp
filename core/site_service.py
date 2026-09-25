# -*- coding: utf-8 -*-
"""新建站点编排：CLI（`cli.py site create`）与 GUI 向导共用的唯一实现。

为什么需要：此前同一套流程（渲染配置 → 写 vhost → 补 include → ``nginx -t``
→ 写 hosts → 平滑重载 + 失败回滚）在 CLI 与向导里各写了一遍，两处的失败策略
与回滚范围已经开始漂移。本模块把编排与回滚下沉到 core，调用方只负责：

1. 组装 :class:`SitePlan`（参数校验仍在调用方，因为错误呈现方式不同）；
2. 逐步展示 :class:`SiteResult` 的 ``steps``（顺序固定，见下）；
3. 失败时决定「保留还是回滚」——CLI 直接回滚，GUI 弹框让用户选择。

流程与失败策略：

    cert（可选，HTTPS 证书） → file（写 vhost） → include（补 nginx.conf）
    → test（nginx -t） → hosts（写 hosts，可选） → reload（平滑重载，可选）

- ``cert`` / ``file`` / ``include`` / ``test`` 为**硬项**：任一步失败即停止
  后续写入（不再写 hosts、不重载），由调用方决定是否 :func:`rollback_site`；
- ``include`` 在「生效主配置缺失」时，按 ``plan.allow_missing_main_conf``
  记为 ``skip``（GUI 向导的宽容行为）；CLI 保持严格（记为失败）以暴露环境问题；
- ``test`` 在「未找到 nginx 可执行文件」时记为 ``skip``，此时仍继续写 hosts；
- ``hosts`` / ``reload`` 为**软项**：失败只提示，不影响站点配置是否就绪。

回滚边界（与历史行为一致）：只还原**被覆盖**的文件（改前已存在的 vhost 与
nginx.conf），本次**新建**的配置文件不删除，避免误删用户刚生成的配置。
"""
import os
import re
from dataclasses import dataclass, field

from . import cert_manager, file_backup, hosts_manager
from . import site_templates
from .config import Config
from .i18n import t
from .vhost_manager import VhostManager, safe_conf_base

#: 硬项步骤（失败即阻断后续写入）
HARD_STEPS = ("cert", "file", "include", "test")


@dataclass
class SitePlan:
    """一次新建站点的全部入参（调用方已完成参数校验）。"""

    domains: list[str]
    template_key: str
    docroot: str
    port: int | None = None
    conf_name: str = ""
    https: bool = False
    hosts: bool = False
    reload: bool = True
    #: True：生效主配置缺失时 include 步骤记为 skip 而非失败（GUI 向导行为）
    allow_missing_main_conf: bool = False


@dataclass
class SiteRollback:
    """本次创建产生的可回滚改动记录（由 :func:`create_site` 填充）。"""

    vhost: list[tuple[str, str]] = field(default_factory=list)  # (path, backup) 覆盖过的
    main_conf: str = ""  # 改过的 nginx.conf 备份路径（空＝未改）
    hosts: list[str] = field(default_factory=list)  # 本次写入 hosts 的域名
    certs: list[str] = field(default_factory=list)  # 本次生成的证书域名

    def changed(self) -> bool:
        """本次是否产生过可回滚改动。"""
        return bool(self.vhost or self.main_conf or self.hosts or self.certs)


@dataclass
class SiteResult:
    """编排结果：步骤明细 + 回滚记录。"""

    steps: list[tuple[str, dict]] = field(default_factory=list)
    rollback: SiteRollback = field(default_factory=SiteRollback)
    path: str = ""
    content: str = ""
    fatal: bool = False  # 硬项失败（调用方据此决定回滚）

    def step(self, tag: str) -> dict:
        """取某一步的结果（不存在返回空 dict）。"""
        for name, res in self.steps:
            if name == tag:
                return res or {}
        return {}

    def failed(self, tag: str) -> bool:
        """该步骤是否「失败且未被跳过」。"""
        res = self.step(tag)
        return bool(res) and not res.get("ok") and not res.get("skip")


def test_output_ok(output: str) -> bool:
    """``nginx -t`` 输出是否代表校验通过。"""
    low = (output or "").lower()
    return "successful" in low and "failed" not in low


def render_config_for(plan: SitePlan,
                      rb: SiteRollback | None = None) -> tuple[str | None, dict | None]:
    """渲染站点 nginx 配置文本。

    HTTPS 时先生成证书（``created=True`` 的域名记入 ``rb.certs``，便于失败回滚）；
    返回 ``(content, cert_step)``：``content`` 为 None 表示证书等前置失败，
    此时 ``cert_step`` 里是失败原因（没有 HTTPS 时为 None）。
    """
    ssl_cert = ssl_key = ""
    cert_step: dict | None = None
    if plan.https:
        real = [d for d in plan.domains if d and not d.startswith("*.")]
        if not real:
            return None, {"ok": False, "created": False,
                          "message": t("没有可用于签发证书的域名（仅填写了泛解析）")}
        cert_step = cert_manager.ensure_site_cert(real[0])
        if not cert_step.get("ok"):
            return None, cert_step
        if cert_step.get("created") and rb is not None:
            rb.certs.append(real[0])
        ssl_cert, ssl_key = cert_step.get("cert", ""), cert_step.get("key", "")
    content = site_templates.render_config(
        plan.template_key, server_name=" ".join(plan.domains), docroot=plan.docroot,
        port=plan.port, ssl_cert=ssl_cert, ssl_key=ssl_key)
    return content, cert_step


# --------------------------------------------------------------------------- #
# F4 · 既有站点一键 HTTPS（在既有 vhost 上启用 / 关闭 443）
# --------------------------------------------------------------------------- #
_RE_SERVER_OPEN = re.compile(r"(?m)^[ \t]*server[ \t]*\{")


def server_block_spans(text: str) -> list[tuple[int, int]]:
    """返回文本中每个顶层 ``server { ... }`` 块的下标区间 ``[(start, end)]``。

    用花括号配对定位（不依赖缩进 / 单行写法），足以覆盖 phpvm 生成的配置与常见手写。
    """
    spans: list[tuple[int, int]] = []
    for m in _RE_SERVER_OPEN.finditer(text):
        i = text.index("{", m.start())
        depth = 0
        for j in range(i, len(text)):
            ch = text[j]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    spans.append((m.start(), j + 1))
                    break
    return spans


def _block_for_domains(text: str, domains: list[str]) -> tuple[int, int] | None:
    """定位包含指定域名（任一）的 server 块；找不到时回落第一个 server 块。"""
    want = [d.lower() for d in domains if d]
    spans = server_block_spans(text)
    for start, end in spans:
        low = text[start:end].lower()
        if any(d in low for d in want):
            return start, end
    return spans[0] if spans else None


def secure_site(entry, config: Config | None = None, reload: bool = True) -> dict:
    """为**既有**站点启用 HTTPS：生成证书 → 备份并追加 443 server 块 → ``nginx -t``。

    追加的 443 块由该站点原 server 块变换而来（保留用户改过的 location / 规则），
    因此原 80 块保持不变，站点同时支持 http 与 https。
    校验失败自动回滚到修改前；返回 ``{ok, path, cert, backup, output, message}``。
    """
    path = getattr(entry, "file", "") or ""
    if not path or not os.path.isfile(path):
        return {"ok": False, "message": t("配置文件不存在：{path}", path=path or "—")}
    if path.endswith(".disabled"):
        return {"ok": False, "message": t("站点已禁用，请先启用站点再启用 HTTPS。")}
    vm = VhostManager(config)
    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(vm.main_conf)):
        return {"ok": False,
                "message": t("该 server 块位于主配置（nginx.conf）中，无法自动启用 HTTPS；"
                             "请先为它单独建立站点配置。")}
    domains = [d for d in (getattr(entry, "server_name", "") or "").split()
               if d and not d.startswith("*.")]
    if not domains:
        return {"ok": False, "message": t("该站点没有可直接签发证书的域名（仅泛解析）。")}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return {"ok": False, "message": t("读取配置失败：{err}", err=e)}
    if "listen 443" in content:
        return {"ok": True, "already": True, "path": path,
                "message": t("该站点已启用 HTTPS，无需重复操作。")}

    cert = cert_manager.ensure_site_cert(domains[0])
    if not cert.get("ok"):
        return {"ok": False, "cert": cert, "message": cert.get("message", "")}
    span = _block_for_domains(content, domains)
    if span is None:
        return {"ok": False, "message": t("未在配置文件中找到 server 块。")}
    block = content[span[0]:span[1]]
    https_block = site_templates.apply_https(block, cert.get("cert", ""), cert.get("key", ""))
    new_content = content.rstrip() + "\n\n" + https_block.strip() + "\n"

    try:
        backup = file_backup.backup(path)
    except OSError as e:
        return {"ok": False, "message": t("备份失败，未做修改：{err}", err=e)}
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new_content)
    except OSError as e:
        return {"ok": False, "message": t("写入失败：{err}", err=e)}

    if not os.path.exists(vm.nginx.exe):
        return {"ok": True, "path": path, "cert": cert, "backup": backup,
                "test": {"ok": False, "skip": True},
                "message": t("已追加 443 server 块（未找到 nginx，已跳过校验）。")}
    output = vm.nginx.test_config()
    if not test_output_ok(output):
        file_backup.restore(backup, path)
        return {"ok": False, "rolled_back": True, "output": output,
                "message": t("nginx -t 校验失败，已回滚到修改前。\n{out}", out=output)}
    res = {"ok": True, "path": path, "cert": cert, "backup": backup, "output": output,
           "message": t("已启用 HTTPS：{dom}", dom=domains[0])}
    if reload:
        running, _ = vm.nginx.get_status()
        if running:
            res["reload"] = vm.nginx.reload()
    return res


def unsecure_site(entry, config: Config | None = None, reload: bool = True) -> dict:
    """关闭既有站点的 HTTPS：移除全部 443 server 块（改前备份，校验失败回滚）。

    证书文件保留（可能被其它站点 / 用户复用），仅去掉 443 配置。
    """
    path = getattr(entry, "file", "") or ""
    if not path or not os.path.isfile(path):
        return {"ok": False, "message": t("配置文件不存在：{path}", path=path or "—")}
    vm = VhostManager(config)
    if os.path.normcase(os.path.abspath(path)) == os.path.normcase(
            os.path.abspath(vm.main_conf)):
        return {"ok": False,
                "message": t("该 server 块位于主配置（nginx.conf）中，请手动关闭其 HTTPS。")}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as e:
        return {"ok": False, "message": t("读取配置失败：{err}", err=e)}
    if "listen 443" not in content:
        return {"ok": True, "unchanged": True, "path": path,
                "message": t("该站点未启用 HTTPS，无需操作。")}

    kept: list[str] = []
    prev = 0
    removed = 0
    for start, end in server_block_spans(content):
        kept.append(content[prev:start])
        if "listen 443" in content[start:end]:
            removed += 1
        else:
            kept.append(content[start:end])
        prev = end
    kept.append(content[prev:])
    new_content = re.sub(r"\n{3,}", "\n\n", "".join(kept)).rstrip() + "\n"

    try:
        backup = file_backup.backup(path)
    except OSError as e:
        return {"ok": False, "message": t("备份失败，未做修改：{err}", err=e)}
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new_content)
    except OSError as e:
        return {"ok": False, "message": t("写入失败：{err}", err=e)}

    if not os.path.exists(vm.nginx.exe):
        return {"ok": True, "path": path, "removed": removed, "backup": backup,
                "test": {"ok": False, "skip": True},
                "message": t("已移除 {n} 个 443 server 块（未找到 nginx，已跳过校验）。",
                             n=removed)}
    output = vm.nginx.test_config()
    if not test_output_ok(output):
        file_backup.restore(backup, path)
        return {"ok": False, "rolled_back": True, "output": output,
                "message": t("nginx -t 校验失败，已回滚到修改前。\n{out}", out=output)}
    res = {"ok": True, "path": path, "removed": removed, "backup": backup, "output": output,
           "message": t("已关闭 HTTPS（移除 {n} 个 443 server 块）。", n=removed)}
    if reload:
        running, _ = vm.nginx.get_status()
        if running:
            res["reload"] = vm.nginx.reload()
    return res


def create_site(plan: SitePlan, config: Config | None = None) -> SiteResult:
    """按固定顺序执行建站流程；不抛异常，全部结果落在返回值里。"""
    vm = VhostManager(config)
    result = SiteResult()
    rb = result.rollback

    # ① 证书（可选）+ ② 渲染配置
    content, cert_step = render_config_for(plan, rb)
    if cert_step is not None:
        result.steps.append(("cert", cert_step))
    if content is None:
        result.fatal = True
        return result
    result.content = content

    # ③ 写入 vhost
    conf_name = plan.conf_name or (safe_conf_base(plan.domains[0]) + ".conf")
    file_res = vm.write_vhost(conf_name, content)
    result.path = file_res.get("path", "")
    if file_res.get("ok") and file_res.get("existed") and file_res.get("backup"):
        rb.vhost.append((file_res["path"], file_res["backup"]))
    result.steps.append(("file", file_res))
    if not file_res.get("ok"):
        result.fatal = True
        return result

    # ④ 自动补 include（主配置未加载站点目录时）
    inc_res = vm.ensure_include()
    if inc_res.get("changed") and inc_res.get("backup"):
        rb.main_conf = inc_res["backup"]
    if plan.allow_missing_main_conf and not os.path.exists(vm.main_conf):
        # 工具环境还没有生效的 nginx 主配置：不视为失败，仅提示（延续向导行为）
        inc_res = dict(inc_res, ok=False, skip=True,
                       message=(inc_res.get("message")
                                or t("未找到生效主配置，跳过 include 自动补全")))
    result.steps.append(("include", inc_res))

    # ⑤ nginx -t 校验（无 nginx 可执行文件时跳过，仍继续写 hosts）
    if not os.path.exists(vm.nginx.exe):
        result.steps.append(("test", {
            "ok": False, "skip": True, "output": "",
            "message": t("未找到 nginx（{path}），已跳过配置校验；"
                         "请配置好 nginx 后手动执行「配置检查」。", path=vm.nginx.exe)}))
    else:
        output = vm.nginx.test_config()
        ok = test_output_ok(output)
        result.steps.append(("test", {
            "ok": ok, "output": output,
            "message": t("配置检查通过") if ok else t("nginx -t 校验失败（详见上方输出）")}))

    if any(result.failed(tag) for tag in HARD_STEPS):
        result.fatal = True
        return result

    # ⑥ 写入 hosts（软项：失败只提示）
    real = [d for d in plan.domains if d and not d.startswith("*.")]
    if plan.hosts and real:
        hosts_res = hosts_manager.ensure_entries(real)
        if hosts_res.get("ok") and hosts_res.get("added"):
            rb.hosts = list(hosts_res["added"])
        result.steps.append(("hosts", hosts_res))
    elif plan.hosts:
        result.steps.append(("hosts", {
            "ok": True, "skip": True,
            "message": t("没有可写入 hosts 的域名（通配项已跳过）")}))

    # ⑦ 平滑重载（软项）
    if plan.reload and result.step("test").get("ok"):
        running, _ = vm.nginx.get_status()
        if running:
            result.steps.append(("reload", {"ok": True, "message": vm.nginx.reload()}))
        else:
            result.steps.append(("reload", {
                "ok": True, "skip": True,
                "message": t("nginx 未运行，启动后会自动加载新站点")}))
    return result


def rollback_site(rb: SiteRollback, config: Config | None = None) -> list[dict]:
    """执行回滚：还原被覆盖的 vhost / nginx.conf、移除本次写过的 hosts 映射、
    删除本次生成的证书。

    返回每项结果 ``[{target, path, ok, message}]``（不抛异常）。文件还原项不带
    通用提示文案（调用方自行汇总，避免为同一件事造多份词条）；证书 / hosts 项的
    ``message`` 为可直接展示的说明。
    """
    out: list[dict] = []
    for path, backup in reversed(rb.vhost):
        ok = file_backup.restore(backup, path)
        out.append({"target": "vhost", "path": path, "ok": ok, "backup": backup,
                    "message": "" if ok else t("还原失败：{path}（备份 {backup}）",
                                               path=path, backup=backup)})
    if rb.main_conf:
        vm = VhostManager(config)
        ok = file_backup.restore(rb.main_conf, vm.main_conf)
        out.append({"target": "main_conf", "path": vm.main_conf, "ok": ok,
                    "backup": rb.main_conf,
                    "message": "" if ok else t("还原失败：{path}（备份 {backup}）",
                                               path=vm.main_conf, backup=rb.main_conf)})
    for dom in list(rb.certs):
        try:
            cert_manager.remove_cert(dom)
            out.append({"target": "cert", "path": dom, "ok": True,
                        "message": t("已删除本次生成的证书：{dom}", dom=dom)})
        except Exception as e:  # noqa: BLE001 —— 删除失败不阻断其它还原
            out.append({"target": "cert", "path": dom, "ok": False,
                        "message": t("证书删除失败：{err}", err=e)})
    rb.certs = []
    added = list(rb.hosts)
    if added:
        try:
            res = hosts_manager.remove_entries(added)
            ok = bool(res.get("ok"))
            if ok and res.get("removed"):
                msg = t("已同步移除 hosts 映射：{doms}", doms="、".join(res["removed"]))
            elif ok:
                msg = t("hosts 中无本次写入的条目，无需移除。")
            else:
                msg = t("hosts 映射移除失败，请手动清理：{msg}", msg=res.get("message", ""))
            out.append({"target": "hosts", "path": ", ".join(added), "ok": ok, "message": msg})
        except Exception as e:  # noqa: BLE001
            out.append({"target": "hosts", "path": ", ".join(added), "ok": False,
                        "message": t("hosts 映射移除异常：{err}", err=e)})
        rb.hosts = []
    return out
