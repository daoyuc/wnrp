# -*- coding: utf-8 -*-
"""vhost ↔ PHP 端口映射与一键同步。

- 解析 nginx/conf/nginx.conf 与 vhost/*.conf 中的 server 块，
  提取 server_name / root / fastcgi_pass 端口，并通过 Config.ports 反查 PHP 版本；
- sync_port() 一键替换所有引用旧端口的 fastcgi_pass，随后 nginx -t 校验，
  校验失败自动还原备份，杜绝「改坏配置导致站点全挂」。
"""
import os
import re
import shutil

from dataclasses import dataclass

from .config import WNRP_ROOT, Config
from .nginx_manager import NginxManager

NGINX_CONF_DIR = os.path.join(WNRP_ROOT, "nginx", "conf")
NGINX_MAIN_CONF = os.path.join(NGINX_CONF_DIR, "nginx.conf")
VHOST_DIR = os.path.join(NGINX_CONF_DIR, "vhost")

# 匹配行首（可带缩进）fastcgi_pass 指向本机端口，保留行尾注释
_RE_FCGI_LOCAL = re.compile(r"^(\s*)fastcgi_pass\s+127\.0\.0\.1:(\d+)(\s*;.*)$", re.M)
# 块内任意位置出现的 fastcgi_pass 目标（用于判断指向 upstream 名称）
_RE_FCGI_TARGET = re.compile(r"^\s*fastcgi_pass\s+(\S+)\s*;", re.M)
_RE_SERVER_NAME = re.compile(r"^\s*server_name\s+(.+?)\s*;\s*$", re.M)
_RE_ROOT = re.compile(r"^\s*root\s+(.+?)\s*;\s*$", re.M)
_RE_SERVER_KEYWORD = re.compile(r"\bserver\s*$")
_RE_LINE_COMMENT = re.compile(r"#[^\n]*")


@dataclass
class VhostEntry:
    """一个 server 块的解析结果。"""

    server_name: str  # 全部域名，空格分隔
    file: str  # 配置文件绝对路径
    root: str = ""
    port: int | None = None  # fastcgi_pass 指向的本机端口；无 PHP 处理/指向 upstream 时为 None
    php_version: str | None = None  # 反查到的 PHP 版本名（可多个，逗号分隔）
    note: str = ""  # 异常说明（如端口未映射到任何版本）

    @property
    def file_rel(self) -> str:
        """相对 nginx/conf 的展示路径；不在其下则原样返回。"""
        rel = os.path.relpath(self.file, NGINX_CONF_DIR)
        return rel if not rel.startswith("..") else self.file


class VhostManager:
    """nginx vhost 扫描与端口一键同步。"""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.nginx = NginxManager()

    # ------------------------------------------------------------------ #
    # 扫描
    # ------------------------------------------------------------------ #
    def scan_files(self) -> list[str]:
        """返回需扫描的配置文件：主配置 nginx.conf + vhost/*.conf。"""
        files: list[str] = []
        if os.path.exists(NGINX_MAIN_CONF):
            files.append(NGINX_MAIN_CONF)
        if os.path.isdir(VHOST_DIR):
            files.extend(
                os.path.join(VHOST_DIR, f)
                for f in sorted(os.listdir(VHOST_DIR))
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
                entries.append(_parse_block(block, path, port_versions))
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
                     "message": "端口未变化，跳过", "backup": None}
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
                                    "message": f"写入失败：{e}", "backup": None})
            output = self.nginx.test_config()
            ok = "successful" in output.lower() and "failed" not in output.lower()
            for path, backup, replaced in applied:
                if ok:
                    results.append({"file": path, "replaced": replaced, "ok": True,
                                    "message": "已同步，备份保留于 .bak", "backup": backup})
                else:
                    self._restore_backup(backup, path)
                    results.append({"file": path, "replaced": replaced, "ok": False,
                                    "message": f"nginx -t 校验失败，已自动还原：{output}",
                                    "backup": None})
        except Exception as e:  # noqa: BLE001 —— 兜底还原已写文件
            for path, backup, _ in applied:
                self._restore_backup(backup, path)
            if not results:
                results.append({"file": "", "replaced": 0, "ok": False,
                                "message": f"同步过程异常：{e}", "backup": None})
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


# --------------------------------------------------------------------- #
# 文本解析工具
# --------------------------------------------------------------------- #
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


def _parse_block(block: str, path: str, port_versions: dict[int, list[str]]) -> VhostEntry:
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
    note = f"端口 {port} 未映射到任何 PHP 版本" if port and not versions else ""
    return VhostEntry(
        server_name=server_name, file=path, root=root,
        port=port, php_version=php_version, note=note,
    )
