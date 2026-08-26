# -*- coding: utf-8 -*-
"""PHP 官方 Windows 下载源解析与安全下载（纯标准库实现）。

数据源：
- releases.json（机器可读，7.4~8.5 权威数据）
- archives/ 目录索引 HTML（历史版本补充，7.3 及以下）

功能：
- 按当前系统架构 / 线程安全模式自动匹配官方包键（兼容编译器演进 vs17/vs16/vc15）
- 流式下载 + 进度回调、SHA-256 完整性校验、防路径穿越安全解压
"""
import hashlib
import os
import platform
import re
import shutil
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field

RELEASES_URL = "https://windows.php.net/downloads/releases/releases.json"
ARCHIVES_URL = "https://windows.php.net/downloads/releases/archives/"
DOWNLOAD_BASE = "https://windows.php.net/downloads/releases/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) phpvm-installer"
TIMEOUT = 30
CHUNK = 64 * 1024

# 编译器优先级：新编译器优先（部分版本过渡期同时提供 vs16/vs17）
_COMPILER_ORDER = ("vs17", "vs16", "vc15", "vc14", "vc11", "vc9")


@dataclass
class PhpPackage:
    """一个可下载的官方 Windows 包。"""
    version: str       # 完整版本号 "8.4.25"
    series: str        # 主版本 "8.4"
    arch: str          # "x64" | "x86"
    ts_mode: str       # "ts" | "nts"
    compiler: str      # "vs17" | "vs16" | "vc15" ...
    url: str           # 完整下载 URL
    size: str          # 展示用大小 "34.49MB"
    sha256: str        # 校验和
    source: str        # "releases" | "archives"


@dataclass
class SeriesCandidate:
    """某主版本系列的最新可用包集合（UI 展示 / 安装用）。"""
    series: str                              # "8.4"
    version: str                             # 该系列最新完整版本号
    packages: dict[tuple[str, str], PhpPackage] = field(default_factory=dict)  # {(ts_mode, arch): PhpPackage}

    def package(self, ts_mode: str, arch: str) -> PhpPackage | None:
        """按线程安全模式 + 架构取包；缺省回退到同架构的任意包。"""
        pkg = self.packages.get((ts_mode, arch))
        if pkg is not None:
            return pkg
        for (t, a), p in self.packages.items():
            if a == arch:
                return p
        for p in self.packages.values():
            return p
        return None


# --------------------------------------------------------------------------- #
# 架构 / 编译器检测
# --------------------------------------------------------------------------- #
def detect_arch() -> str:
    """按当前操作系统返回目标架构：x64 | x86。"""
    m = platform.machine().lower()
    return "x64" if ("64" in m or "amd64" in m or "x86_64" in m) else "x86"


def detect_os_compiler(series: str) -> str | None:
    """按主版本推断官方编译器：8.4/8.5→vs17、8.0~8.3→vs16、7.x→vc15。

    仅作展示用；实际安装以 releases.json 中真实存在的包键为准。
    """
    try:
        major, minor = (int(x) for x in series.split("."))
    except (ValueError, AttributeError):
        return None
    if major == 8:
        return "vs17" if minor >= 4 else "vs16"
    if major == 7:
        return "vc15"
    return None


# --------------------------------------------------------------------------- #
# 数据源解析
# --------------------------------------------------------------------------- #
def _http_get(url: str, timeout: int = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_releases_json(timeout: int = TIMEOUT) -> dict:
    """拉取官方 releases.json。失败时抛异常（由上层转为友好提示）。"""
    data = _http_get(RELEASES_URL, timeout)
    try:
        parsed = __import__("json").loads(data.decode("utf-8"))
    except Exception:
        raise RuntimeError("releases.json 内容无法解析")
    if not isinstance(parsed, dict):
        raise RuntimeError("releases.json 结构异常")
    return parsed


def pick_package(entry: dict[str, dict], arch: str, ts: str) -> dict | None:
    """在 releases.json 条目中按 f"{ts}-*-{arch}" 匹配实际存在的包键。

    兼容编译器演进：8.4/8.5 为 ts-vs17-x64、8.0~8.3 为 ts-vs16-x64、
    7.4 为 ts-vc15-x64；过渡版本同时存在多个编译器时优先取新编译器。
    """
    for compiler in _COMPILER_ORDER:
        key = f"{ts}-{compiler}-{arch}"
        obj = entry.get(key)
        if isinstance(obj, dict):
            return obj
    return None


def _to_package(series: str, version: str, ts: str, arch: str, compiler: str,
                obj: dict[str, dict], source: str) -> PhpPackage | None:
    """从 releases.json 包对象（含 zip 字段）构造 PhpPackage。"""
    zip_meta = obj.get("zip")
    if not isinstance(zip_meta, dict):
        return None
    path = zip_meta.get("path")
    if not path:
        return None
    return PhpPackage(
        version=version,
        series=series,
        arch=arch,
        ts_mode=ts,
        compiler=compiler.lower(),
        url=DOWNLOAD_BASE + path,
        size=str(zip_meta.get("size", "")),
        sha256=str(zip_meta.get("sha256", "")),
        source=source,
    )


# archives 目录索引链接：php-8.1.34-nts-Win32-vs16-x64.zip
_ARCHIVE_RE = re.compile(
    r"php-(\d+\.\d+\.\d+)-(?:([A-Za-z]+)-)?Win32-([A-Za-z0-9]+)-(x64|x86)\.zip",
    re.IGNORECASE,
)


def _parse_archive_link(fname: str) -> tuple[str, str, str, str] | None:
    """解析 archives 文件名 → (version, ts_mode, compiler, arch) | None。"""
    m = _ARCHIVE_RE.match(fname)
    if not m:
        return None
    version, ts_tag, compiler, arch = m.groups()
    ts_mode = "nts" if (ts_tag or "").lower() == "nts" else "ts"
    return version, ts_mode, compiler.lower(), arch.lower()


def fetch_archives_candidates(timeout: int = TIMEOUT) -> dict[str, SeriesCandidate]:
    """解析 archives/ 目录索引，返回 {series: SeriesCandidate}（含 7.x 及以下）。

    官方把 8.2+ 不再归档，此源主要补充 7.0~7.3（7.4 在 releases.json）。
    每主版本只保留最新补丁版本；包键可能缺失 x86 或 nts，允许部分缺失。
    """
    html = _http_get(ARCHIVES_URL, timeout).decode("utf-8", errors="replace")
    result: dict[str, SeriesCandidate] = {}
    for href in re.findall(r'href="([^"]+\.zip)"', html, re.IGNORECASE):
        fname = href.split("/")[-1].strip()
        parsed = _parse_archive_link(fname)
        if not parsed:
            continue
        version, ts_mode, compiler, arch = parsed
        major, minor = version.split(".")[:2]
        # 仅补充 releases.json 不覆盖的旧系列（7.3 及以下）
        if int(major) > 7 or (int(major) == 7 and int(minor) >= 4):
            continue
        series = f"{major}.{minor}"
        cand = result.get(series)
        if cand is None:
            cand = SeriesCandidate(series=series, version=version)
            result[series] = cand
        elif version_key(version) < version_key(cand.version):
            continue  # 跳过更旧版本，保留该系列最新版本号与包
        elif version_key(version) > version_key(cand.version):
            cand.version = version  # 系列出现更新补丁时同步版本号
        cand.packages[(ts_mode, arch)] = PhpPackage(
            version=version,
            series=series,
            arch=arch,
            ts_mode=ts_mode,
            compiler=compiler,
            url=DOWNLOAD_BASE + "archives/" + fname,
            size="",
            sha256="",  # archives 无 sha256 元数据，跳过强校验
            source="archives",
        )
    return result


def version_key(version: str) -> tuple[int | str, ...]:
    """版本号 → 可比较元组（8.5.10 → (8,5,10)），用于排序与新旧比较。"""
    parts: list[int | str] = []
    for seg in re.split(r"[._-]", version):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(seg)
    return tuple(parts)


def build_candidates(include_legacy: bool = True,
                     timeout: int = TIMEOUT) -> list[SeriesCandidate]:
    """拉取两源并合并为候选列表（按主版本降序）。

    include_legacy=True 时额外解析 archives/ 补充 7.3 及以下旧版。
    """
    arch = detect_arch()
    releases = fetch_releases_json(timeout)
    candidates: list[SeriesCandidate] = []

    for series, entry in releases.items():
        if not isinstance(entry, dict) or not series.count("."):
            continue
        version = str(entry.get("version", series))
        cand = SeriesCandidate(series=series, version=version)
        for ts in ("nts", "ts"):
            obj = pick_package(entry, arch, ts)
            if obj is None:
                continue
            pkg = _to_package(series, version, ts, arch,
                              next((c for c in _COMPILER_ORDER
                                    if f"{ts}-{c}-{arch}" in entry), ""),
                              obj, "releases")
            if pkg is not None:
                cand.packages[(ts, arch)] = pkg
        if cand.packages:
            candidates.append(cand)

    if include_legacy:
        try:
            archives = fetch_archives_candidates(timeout)
        except Exception:
            archives = {}
        for series, cand in archives.items():
            if not any(c.series == series for c in candidates):
                candidates.append(cand)

    candidates.sort(key=lambda c: version_key(c.series), reverse=True)
    return candidates


# --------------------------------------------------------------------------- #
# 下载 / 校验 / 解压
# --------------------------------------------------------------------------- #
def download_with_progress(url: str, dest: str, progress_cb=None) -> None:
    """流式下载到 dest（先写 .part 再原子改名）。进度回调 (done_bytes, total_bytes)。

    Content-Length 未知时 total 为 0；回调节流 ~6 次/秒避免高频刷新 UI。
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest + ".part"
    last_report = 0.0
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            written = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    if progress_cb and (
                        written >= total or time.time() - last_report >= 0.15
                    ):
                        last_report = time.time()
                        progress_cb(written, total)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def verify_sha256(path: str, expected: str) -> bool:
    """校验文件 SHA-256 是否与官方元数据一致。expected 为空视为无法校验。"""
    if not expected:
        return True
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest().lower() == expected.lower()


def extract_zip_safe(zip_path: str, dest_dir: str, progress_cb=None) -> None:
    """安全解压：拒绝绝对路径 / .. 穿越，逐文件写入 dest_dir。

    进度回调 (已完成文件数, 文件总数)。
    """
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        total = len(names)
        for i, name in enumerate(names):
            clean = name.replace("\\", "/")
            parts = clean.split("/")
            if clean.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                raise RuntimeError(f"压缩包包含非法路径：{name}")
            target = os.path.join(dest_dir, *parts)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if progress_cb and total:
                progress_cb(i + 1, total)
