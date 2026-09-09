# -*- coding: utf-8 -*-
"""vhost ↔ PHP 端口映射与一键同步。

站点配置目录（主配置 / vhost 写入目录）跟随 NginxManager 推导出的
「实际生效」布局，而非写死 WNRP_ROOT：Windows/自定义 root 布局用
<prefix>/conf，Homebrew 布局用 <prefix>（/opt/homebrew/etc/nginx）。

- 解析主配置 nginx.conf 与 vhost/*.conf 中的 server 块，提取
  server_name / root / fastcgi_pass 端口，并通过 Config.ports 反查 PHP 版本；
- sync_port() 一键替换所有引用旧端口的 fastcgi_pass，随后 nginx -t 校验，
  校验失败自动还原备份，杜绝「改坏配置导致站点全挂」；
- include_status() 自动检测「生效 nginx.conf 是否 include 了站点目录」，
  ensure_include() 未覆盖时注入绝对路径 include（经备份，可回滚）。
"""
import os
import re
import shutil

from dataclasses import dataclass

from .config import Config
from .i18n import t
from .nginx_manager import NginxManager

# 匹配行首（可带缩进）fastcgi_pass 指向本机端口，保留行尾注释
_RE_FCGI_LOCAL = re.compile(r"^(\s*)fastcgi_pass\s+127\.0\.0\.1:(\d+)(\s*;.*)$", re.M)
# 块内任意位置出现的 fastcgi_pass 目标（用于判断指向 upstream 名称）
_RE_FCGI_TARGET = re.compile(r"^\s*fastcgi_pass\s+(\S+)\s*;", re.M)
_RE_SERVER_NAME = re.compile(r"^\s*server_name\s+(.+?)\s*;\s*$", re.M)
_RE_ROOT = re.compile(r"^\s*root\s+(.+?)\s*;\s*$", re.M)
_RE_SERVER_KEYWORD = re.compile(r"\bserver\s*$")
_RE_LINE_COMMENT = re.compile(r"#[^\n]*")
# 行首 include 指令（目标到分号前，可带引号）
_RE_INCLUDE = re.compile(r"^\s*include\s+(\S+)\s*;", re.M)


@dataclass
class VhostEntry:
    """一个 server 块的解析结果。"""

    server_name: str  # 全部域名，空格分隔
    file: str  # 配置文件绝对路径
    root: str = ""
    port: int | None = None  # fastcgi_pass 指向的本机端口；无 PHP 处理/指向 upstream 时为 None
    php_version: str | None = None  # 反查到的 PHP 版本名（可多个，逗号分隔）
    note: str = ""  # 异常说明（如端口未映射到任何版本）
    conf_dir: str = ""  # 主配置所在目录（相对展示基准）；空则显示绝对路径

    @property
    def file_rel(self) -> str:
        """相对主配置所在目录的展示路径；不在其下则原样返回。"""
        if not self.conf_dir:
            return self.file
        rel = os.path.relpath(self.file, self.conf_dir)
        return rel if not rel.startswith("..") else self.file


class VhostManager:
    """nginx vhost 扫描与端口一键同步。"""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.nginx = NginxManager()

    # ------------------------------------------------------------------ #
    # 站点目录（派生自实际生效的 nginx 布局，而非硬编码 WNRP_ROOT）
    # ------------------------------------------------------------------ #
    @property
    def conf_dir(self) -> str:
        """实际生效主配置所在目录（扫描 / 相对展示基准）。"""
        return self.nginx.site_base

    @property
    def main_conf(self) -> str:
        """实际生效主配置 nginx.conf 的绝对路径。"""
        return self.nginx.main_conf

    @property
    def vhost_dir(self) -> str:
        """phpvm 生成的站点配置文件目录（须被主配置 include 才会加载）。"""
        return self.nginx.vhost_dir

    # ------------------------------------------------------------------ #
    # 扫描
    # ------------------------------------------------------------------ #
    def scan_files(self) -> list[str]:
        """返回需扫描的配置文件：生效主配置 nginx.conf + 站点目录 vhost/*.conf。"""
        files: list[str] = []
        if os.path.exists(self.main_conf):
            files.append(self.main_conf)
        if os.path.isdir(self.vhost_dir):
            files.extend(
                os.path.join(self.vhost_dir, f)
                for f in sorted(os.listdir(self.vhost_dir))
                if f.endswith(".conf")
            )
        return files

    def port_to_versions(self) -> dict[int, list[str]]:
        """端口 -> 配置了该端口的 PHP 版本名列表（含默认端口）。"""
        mapping: dict[int, list[str]] = {}
        for name, port in self.config.ports.items():
            mapping.setdefault(port, []).append(name)
        return mapping

    def scan(self) -> list[VhostEntry]:
        """扫描全部 server 块并反查端口对应 PHP 版本。"""
        port_versions = self.port_to_versions()
        entries: list[VhostEntry] = []
        for path in self.scan_files():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            for block in _iter_server_blocks(text):
                entries.append(_parse_block(block, path, port_versions, self.conf_dir))
        return entries

    def entries_with_port(self, port: int) -> list[VhostEntry]:
        """返回 fastcgi_pass 引用指定端口的所有 server 条目。"""
        return [e for e in self.scan() if e.port == port]

    # ------------------------------------------------------------------ #
    # 一键同步
    # ------------------------------------------------------------------ #
    def _find_files_with_port(self, port: int) -> list[str]:
        """扫描全部配置文件，返回引用指定端口的文件绝对路径列表。"""
        pattern = re.compile(
            r"^(\s*)fastcgi_pass\s+127\.0\.0\.1:" + str(port) + r"(\s*;.*)$", re.M
        )
        found: list[str] = []
        for path in self.scan_files():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    if pattern.search(f.read()):
                        found.append(path)
            except OSError:
                continue
        return found

    def sync_port(self, old_port: int, new_port: int,
                  files: list[str] | None = None) -> list[dict]:
        """一键同步：将所有引用 old_port 的 fastcgi_pass 替换为 new_port。

        每个文件先备份为 <file>.bak，全部替换后执行 nginx -t 校验；
        校验失败自动还原所有备份（成功则保留 .bak 便于手动回滚）。
        返回每文件结果：[{file, replaced, ok, message, backup}]。
        """
        if old_port == new_port:
            return [{"file": f, "replaced": 0, "ok": True,
                     "message": t("端口未变化，跳过"), "backup": None}
                    for f in (files or self._find_files_with_port(old_port))]
        files = files if files is not None else self._find_files_with_port(old_port)
        if not files:
            return []
        applied: list[tuple[str, str, int]] = []  # (path, backup, replaced)
        results: list[dict] = []
        try:
            for path in files:
                try:
                    replaced, backup = self._replace_in_file(path, old_port, new_port)
                    applied.append((path, backup, replaced))
                except OSError as e:
                    results.append({"file": path, "replaced": 0, "ok": False,
                                    "message": t("写入失败：{err}", err=e), "backup": None})
            output = self.nginx.test_config()
            ok = "successful" in output.lower() and "failed" not in output.lower()
            for path, backup, replaced in applied:
                if ok:
                    results.append({"file": path, "replaced": replaced, "ok": True,
                                    "message": t("已同步，备份保留于 .bak"), "backup": backup})
                else:
                    self._restore_backup(backup, path)
                    results.append({"file": path, "replaced": replaced, "ok": False,
                                    "message": t("nginx -t 校验失败，已自动还原：{output}",
                                                 output=output),
                                    "backup": None})
        except Exception as e:  # noqa: BLE001 —— 兜底还原已写文件
            for path, backup, _ in applied:
                self._restore_backup(backup, path)
            if not results:
                results.append({"file": "", "replaced": 0, "ok": False,
                                "message": t("同步过程异常：{err}", err=e), "backup": None})
        return results

    def _replace_in_file(self, path: str, old_port: int, new_port: int) -> tuple[int, str]:
        """备份并替换单个文件中引用 old_port 的 fastcgi_pass。返回 (替换数, 备份路径)。"""
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        backup = path + ".bak"
        shutil.copy2(path, backup)

        def _sub(m: re.Match) -> str:
            if int(m.group(2)) == old_port:
                return f"{m.group(1)}fastcgi_pass 127.0.0.1:{new_port}{m.group(3)}"
            return m.group(0)

        new_text, count = _RE_FCGI_LOCAL.subn(_sub, text)
        if count:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(new_text)
        return count, backup

    @staticmethod
    def _restore_backup(backup: str, path: str) -> None:
        """用备份还原文件并清理备份。"""
        if backup and os.path.exists(backup):
            try:
                shutil.copy2(backup, path)
                os.remove(backup)
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    # 新建站点（向导用）
    # ------------------------------------------------------------------ #
    def ensure_vhost_dir(self) -> None:
        """确保站点目录存在。"""
        try:
            os.makedirs(self.vhost_dir, exist_ok=True)
        except OSError:
            pass

    def write_vhost(self, filename: str, content: str) -> dict:
        """把站点配置写入站点目录/<filename>。

        已存在时先备份为 <filename>.bak（不覆盖删除）。
        返回 {path, existed, backup, ok, message}。
        """
        self.ensure_vhost_dir()
        path = os.path.join(self.vhost_dir, filename)
        backup = None
        existed = os.path.exists(path)
        try:
            if existed:
                backup = path + ".bak"
                shutil.copy2(path, backup)
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            return {"path": path, "existed": existed, "backup": backup,
                    "ok": True, "message": t("已写入")}
        except OSError as e:
            return {"path": path, "existed": existed, "backup": backup,
                    "ok": False, "message": t("写入失败：{err}", err=e)}

    # ------------------------------------------------------------------ #
    # include 自动检测 / 补全（针对「实际生效的 nginx.conf」）
    # ------------------------------------------------------------------ #
    def include_status(self) -> dict:
        """检测生效 nginx.conf 是否已 include phpvm 站点目录。

        返回 {covered, main_conf, vhost_dir, reason, lines}：
        - covered=True：站点目录已被主配置加载（新站点写入即可生效）；
        - 否则 reason 说明为何未加载（主配置缺失 / http 块缺失 / 尚未 include）。
        """
        info = {"covered": False, "main_conf": self.main_conf,
                "vhost_dir": self.vhost_dir, "reason": "", "lines": []}
        if not os.path.exists(self.main_conf):
            info["reason"] = t("未找到生效主配置：{path}\n站点不会被 nginx 加载",
                               path=self.main_conf)
            return info
        try:
            with open(self.main_conf, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            info["reason"] = t("读取主配置失败：{err}", err=e)
            return info
        http = _http_block_text(_strip_comments(text))
        if http is None:
            info["reason"] = t("主配置 {path} 中未找到 http 块，无法确认站点加载",
                               path=self.main_conf)
            return info
        targets = [m.group(1).strip().strip('"').strip("'") for m in _RE_INCLUDE.finditer(http)]
        info["lines"] = targets
        vhost = os.path.normpath(self.vhost_dir)
        conf_root = os.path.normpath(self.conf_dir)
        for t in targets:
            head = t.split("*")[0].rstrip("/") or t.rstrip("/")
            if not head:
                continue
            full = head if os.path.isabs(head) else os.path.join(conf_root, head)
            if os.path.normpath(full) == vhost:
                info["covered"] = True
                break
        if not info["covered"]:
            cur = "、".join(targets) or t("（无）")
            info["reason"] = (t("生效主配置 {main} 尚未 include 站点目录：{vhost}\n"
                                "当前 http 块 include：{cur}",
                                main=self.main_conf, vhost=self.vhost_dir, cur=cur))
        return info

    def include_exists(self) -> bool:
        """是否已 include 站点目录（兼容旧调用）。"""
        return bool(self.include_status()["covered"])

    def ensure_include(self) -> dict:
        """若生效 nginx.conf 未 include 站点目录，在 http 块内自动补一行（绝对路径）。

        改前备份 nginx.conf → <name>.bak。返回
        {changed, ok, message, backup}。校验失败回滚由调用方（向导）负责。
        """
        status = self.include_status()
        if status["covered"]:
            return {"changed": False, "ok": True,
                    "message": t("生效 nginx.conf 已 include 站点目录，无需修改"), "backup": None}
        if not os.path.exists(self.main_conf):
            return {"changed": False, "ok": False,
                    "message": t("未找到生效主配置 {path}，无法自动补 include，请手动配置",
                                 path=self.main_conf),
                    "backup": None}
        try:
            with open(self.main_conf, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            return {"changed": False, "ok": False,
                    "message": t("读取失败：{err}", err=e), "backup": None}

        close_idx = _find_http_close_index(text)
        if close_idx is None:
            return {"changed": False, "ok": False,
                    "message": t("未在生效 nginx.conf 中找到 http 块，无法自动补 include"),
                    "backup": None}
        backup = self.main_conf + ".bak"
        try:
            shutil.copy2(self.main_conf, backup)
        except OSError as e:
            return {"changed": False, "ok": False,
                    "message": t("备份失败：{err}", err=e), "backup": None}
        include_dir = self.vhost_dir.replace("\\", "/")
        insert = (f"\n    # phpvm: 自动加载站点目录 {include_dir} 下的配置\n"
                  f"    include {include_dir}/*.conf;\n")
        new_text = text[:close_idx] + insert + text[close_idx:]
        try:
            with open(self.main_conf, "w", encoding="utf-8", newline="") as f:
                f.write(new_text)
        except OSError as e:
            return {"changed": False, "ok": False,
                    "message": t("写入失败：{err}", err=e), "backup": backup}
        return {"changed": True, "ok": True,
                "message": t("已自动在 http 块补上 include {path}/*.conf", path=include_dir),
                "backup": backup}


# --------------------------------------------------------------------- #
# 文本解析工具
# --------------------------------------------------------------------- #

def _strip_comments(text: str) -> str:
    """行注释替换为等长空格，保持坐标一致。"""
    return _RE_LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), text)


def _find_http_close_index(text: str) -> int | None:
    """返回 http 块闭合 '}' 在原文中的下标；未找到返回 None。"""
    no_comment = _strip_comments(text)
    m = re.search(r"\bhttp\s*\{", no_comment)
    if not m:
        return None
    open_idx = no_comment.find("{", m.start())
    depth = 0
    n = len(no_comment)
    i = open_idx
    while i < n:
        c = no_comment[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _http_block_text(no_comment: str) -> str | None:
    """返回 http 块文本切片（含首尾大括号）；未找到 http 块返回 None。"""
    m = re.search(r"\bhttp\s*\{", no_comment)
    if not m:
        return None
    open_idx = no_comment.find("{", m.start())
    depth = 0
    n = len(no_comment)
    i = open_idx
    while i < n:
        c = no_comment[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return no_comment[open_idx:i + 1]
        i += 1
    return None


def _iter_server_blocks(text: str):
    """按大括号匹配切分所有 server 块（支持嵌套 location、忽略行注释）。

    对每个 server 块 yield 其在原始 text 中的切片（含首尾行）。
    """
    # 行注释替换为等长空格，保持原始坐标一致
    no_comment = _RE_LINE_COMMENT.sub(lambda m: " " * len(m.group(0)), text)
    n = len(no_comment)
    i = 0
    while i < n:
        brace = no_comment.find("{", i)
        if brace == -1:
            break
        # 确认该 { 是 server 块入口：{ 之前同行为 server 关键字
        line_start = no_comment.rfind("\n", 0, brace) + 1
        prefix = no_comment[line_start:brace].strip()
        if not _RE_SERVER_KEYWORD.search(prefix):
            i = brace + 1
            continue
        # 从 { 起匹配大括号（处理嵌套 location 块）
        depth = 1
        j = brace + 1
        while j < n and depth > 0:
            c = no_comment[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        if depth > 0:
            break  # 括号未闭合，文件损坏，终止扫描
        block_end = j  # 闭合 } 之后的位置
        yield text[line_start:block_end]
        i = block_end


def _parse_block(block: str, path: str, port_versions: dict[int, list[str]],
                 conf_dir: str = "") -> VhostEntry:
    """解析单个 server 块文本为 VhostEntry。"""
    m = _RE_SERVER_NAME.search(block)
    server_name = m.group(1).strip() if m else "(无 server_name)"
    m_root = _RE_ROOT.search(block)
    root = m_root.group(1).strip().strip('"') if m_root else ""

    # fastcgi_pass 指向本机端口则记录，指向 upstream 名称等则不解析
    port: int | None = None
    for mf in _RE_FCGI_TARGET.finditer(block):
        target = mf.group(1).strip()
        mm = re.match(r"^127\.0\.0\.1:(\d+)$", target)
        if mm:
            port = int(mm.group(1))
            break

    versions = port_versions.get(port, []) if port else []
    php_version = ", ".join(versions) if versions else None
    note = t("端口 {port} 未映射到任何 PHP 版本", port=port) if port and not versions else ""
    return VhostEntry(
        server_name=server_name, file=path, root=root,
        port=port, php_version=php_version, note=note,
        conf_dir=conf_dir,
    )
