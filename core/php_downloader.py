# -*- coding: utf-8 -*-
"""PHP 官方 Windows 下载源解析与安全下载（纯标准库实现）。

数据源：
- releases.json（机器可读，7.4~8.5 权威数据）
- archives/ 目录索引 HTML（历史版本补充，7.3 及以下）

功能：
- 按当前系统架构 / 线程安全模式自动匹配官方包键（兼容编译器演进 vs17/vs16/vc15）
- 流式下载 + 进度回调、SHA-256 完整性校验、防路径穿越安全解压
- 大文件多连接并行分块下载（服务器支持 Range 时自动启用，不支持退回单流），
  分片与单流都支持断点续传，最终以 SHA-256 兜底完整性
"""
import hashlib
import os
import platform
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .i18n import t
from .net_ssl import ssl_context

RELEASES_URL = "https://windows.php.net/downloads/releases/releases.json"
ARCHIVES_URL = "https://windows.php.net/downloads/releases/archives/"
DOWNLOAD_BASE = "https://windows.php.net/downloads/releases/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) phpvm-installer"
# 元数据（releases.json / archives 索引）请求超时
TIMEOUT = 20
# 下载 socket 读超时：任一次读取超过该秒数收不到字节即判定连接停滞，
# 中断本次传输并带着 Range 续传重试（避免「进度条不动也不报错」的永久卡住）
DOWNLOAD_TIMEOUT = 20
# 下载失败自动重试次数（续传，不是从头再来）
RETRIES = 4
CHUNK = 256 * 1024
# 进度回调节流间隔（秒）：过密会把 UI 消息队列刷爆，过疏则进度条看起来卡住
PROGRESS_INTERVAL = 0.15
# 并行分块下载：文件不小于该字节数且服务器支持 Range 才启用
PARALLEL_MIN_SIZE = 2 * 1024 * 1024
# 并行分块下载的最大连接数
PARALLEL_CONNECTIONS = 4

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
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
        return resp.read()


def fetch_releases_json(timeout: int = TIMEOUT) -> dict:
    """拉取官方 releases.json。失败时抛异常（由上层转为友好提示）。"""
    data = _http_get(RELEASES_URL, timeout)
    try:
        parsed = __import__("json").loads(data.decode("utf-8"))
    except Exception:
        raise RuntimeError(t("releases.json 内容无法解析"))
    if not isinstance(parsed, dict):
        raise RuntimeError(t("releases.json 结构异常"))
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
            # 旧版只是补充数据源：索引页体积大，给更短的超时，避免候选列表
            # 长时间停在「正在获取可用版本」被误认为卡死
            archives = fetch_archives_candidates(min(timeout, 12))
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
class DownloadCancelled(RuntimeError):
    """调用方通过 stop_event 主动取消下载（GUI「取消」按钮）。"""


def _probe_total(url: str) -> int:
    """Range 探测：服务器支持 Range（206 + Content-Range）返回总字节数，否则 0。

    探测失败一律返回 0 走单流路径，绝不因为探测把下载判死。
    """
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Range": "bytes=0-0",
            "Accept-Encoding": "identity"})
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl_context()) as resp:
            if int(getattr(resp, "status", 200) or 200) != 206:
                return 0
            m = re.search(r"/\s*(\d+)\s*$",
                          (resp.headers.get("Content-Range") or "").strip())
            resp.read(64)  # 读掉分片体（1 字节），释放连接
            return int(m.group(1)) if m else 0
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def _chunk_plan(total: int) -> list[tuple[int, int]] | None:
    """文件大小 → 并行分块的 [(start, end)]（含端点）；不满足并行条件返回 None。"""
    if total < PARALLEL_MIN_SIZE:
        return None
    n = max(2, min(PARALLEL_CONNECTIONS, -(-total // PARALLEL_MIN_SIZE)))
    size, extra = divmod(total, n)
    ranges: list[tuple[int, int]] = []
    start = 0
    for i in range(n):
        end = start + size - 1 + (1 if i < extra else 0)
        ranges.append((start, end))
        start = end + 1
    return ranges


class _ProgressAgg:
    """跨线程聚合各分片的已下载字节并节流回调（同单流回调节奏）。"""

    def __init__(self, total: int, cb):
        self._lock = threading.Lock()
        self._total = total
        self._cb = cb
        self._done = 0
        self._last = 0.0

    def add(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self._done += n
            now = time.time()
            if self._cb and (now - self._last >= PROGRESS_INTERVAL
                             or self._done >= self._total):
                self._last = now
                self._cb(self._done, self._total)


def _download_chunk_once(url: str, path: str, start: int, end: int,
                         agg: _ProgressAgg, stop_event) -> None:
    """传输一个分片 [start, end]（含端点）到 path；分片文件本身可续传。"""
    expected = end - start + 1
    offset = os.path.getsize(path) if os.path.isfile(path) else 0
    if offset > expected:
        # 分片比预期还长（服务器内容可能已变）：丢弃重来
        try:
            os.remove(path)
        except OSError:
            pass
        offset = 0
    if offset >= expected:
        return
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Accept-Encoding": "identity",
        "Range": f"bytes={start + offset}-{end}"})
    with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT,
                                context=ssl_context()) as resp:
        status = int(getattr(resp, "status", 200) or 200)
        # 服务器没按分片范围回 206，绝不能把整份内容写进分片
        if status != 206:
            raise RuntimeError(t("服务器不支持分块下载（HTTP {code}）", code=status))
        m = re.match(r"bytes\s+(\d+)-",
                     (resp.headers.get("Content-Range") or "").strip())
        if not m or int(m.group(1)) != start + offset:
            raise RuntimeError(t("服务器不支持分块下载（HTTP {code}）", code=status))
        read_chunk = getattr(resp, "read1", None) or resp.read
        got = offset
        with open(path, "ab") as f:
            while got < expected:
                if stop_event is not None and stop_event.is_set():
                    raise DownloadCancelled(t("已取消下载"))
                chunk = read_chunk(CHUNK)
                if not chunk:
                    break
                if got + len(chunk) > expected:  # 服务端多发时按分片边界截断
                    chunk = chunk[:expected - got]
                f.write(chunk)
                got += len(chunk)
                agg.add(len(chunk))
    size = os.path.getsize(path)
    if size < expected:
        raise RuntimeError(t("下载不完整：已收到 {done} 字节，应为 {total} 字节",
                             done=size, total=expected))


def _download_parallel(url: str, tmp: str, ranges: list[tuple[int, int]],
                       progress_cb, stop_event) -> None:
    """并行下载全部分片到 ``tmp.<i>``，全部到位后按顺序合并为 tmp。

    任一分片失败即抛错（保留全部分片，外层重试时各分片从断点继续）；
    取消同样保留分片，下次续传。
    """
    paths = [f"{tmp}.{i}" for i in range(len(ranges))]
    total = sum(e - s + 1 for s, e in ranges)
    agg = _ProgressAgg(total, progress_cb)
    for path, (s, e) in zip(paths, ranges):
        # 已就位的分片补报进度，避免续传时进度条从 0 开始
        done = os.path.getsize(path) if os.path.isfile(path) else 0
        if done > e - s + 1:
            try:
                os.remove(path)
            except OSError:
                pass
        elif done:
            agg.add(done)
    last_err: Exception | None = None
    with ThreadPoolExecutor(max_workers=len(ranges)) as ex:
        futures = [ex.submit(_download_chunk_once, url, p, s, e, agg, stop_event)
                   for p, (s, e) in zip(paths, ranges)]
        for fut in as_completed(futures):
            try:
                fut.result()
            except DownloadCancelled:
                raise
            except Exception as e:  # noqa: BLE001 - 记首个错误，其余分片跑完收尾
                if last_err is None:
                    last_err = e
    if last_err is not None:
        raise last_err
    with open(tmp, "wb") as out:
        for p in paths:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out)
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass
    if progress_cb:
        progress_cb(total, total)


def _download_request(url: str, offset: int = 0):
    """构造下载请求；offset>0 时带 Range 头做断点续传。"""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT,
                                 context=ssl_context())


def _download_once(url: str, tmp: str, progress_cb, stop_event) -> None:
    """单次传输：支持 Range 续传，读满 Content-Length 才算完成。"""
    offset = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
    try:
        resp = _download_request(url, offset)
    except urllib.error.HTTPError as e:
        if e.code == 416 and offset:
            # .part 已不短于服务端文件（多为上次下载其实已完成），丢弃后从头下
            try:
                os.remove(tmp)
            except OSError:
                pass
            return _download_once(url, tmp, progress_cb, stop_event)
        raise

    with resp:
        status = int(getattr(resp, "status", 200) or 200)
        # 只有 206 Partial Content 才说明服务端认可续传；否则清空重下
        if offset and status != 206:
            offset = 0
        content_range = resp.headers.get("Content-Range") or ""
        m = re.match(r"bytes\s+(\d+)-", content_range.strip())
        if m and int(m.group(1)) != offset:
            offset = 0  # 服务端起止点与本地不一致，丢弃已有分片
        length = int(resp.headers.get("Content-Length") or 0)
        total = offset + length if length else 0
        written = offset
        last_report = 0.0
        # read1 有数据就返回，不必凑满 CHUNK：慢速链路上进度也能持续跳动
        read_chunk = getattr(resp, "read1", None) or resp.read
        with open(tmp, "ab" if offset else "wb") as f:
            while True:
                if stop_event is not None and stop_event.is_set():
                    raise DownloadCancelled(t("已取消下载"))
                chunk = read_chunk(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
                if progress_cb:
                    now = time.time()
                    if now - last_report >= PROGRESS_INTERVAL or (
                        total and written >= total
                    ):
                        last_report = now
                        progress_cb(written, total)
        if length and written - offset != length:
            # 连接被提前关闭：保留 .part，交给上层续传重试
            raise RuntimeError(
                t("下载不完整：已收到 {done} 字节，应为 {total} 字节",
                  done=written - offset, total=length))
    if progress_cb:
        progress_cb(written, total or written)


def download_with_progress(url: str, dest: str, progress_cb=None,
                           stop_event=None, retries: int = RETRIES) -> None:
    """下载到 dest（先写 .part / 分片再原子改名）。进度回调 (done_bytes, total_bytes)。

    服务器支持 Range 且文件不小于 ``PARALLEL_MIN_SIZE`` 时自动走**多连接并行
    分块**（提速；服务器不支持或不满足条件退回单流），分片与单流都支持断点续传。
    Content-Length 未知时 total 为 0（进度回调仍按已下载字节数持续上报）。
    抗「卡住」的四层保护：
    - 停滞即断：单次读取超过 ``DOWNLOAD_TIMEOUT`` 秒没字节 → 中断本次传输；
    - 断点续传：失败保留 ``.part`` / 分片，重试带 Range 从断点继续，不必从头再来；
    - 有界重试：最多重试 ``retries`` 次，之后抛错由上层提示，绝不无限等待；
    - SHA-256 兜底：合并/落盘后由上层按官方校验和验证，防拼接与篡改错误。
    ``stop_event``（threading.Event）置位后立即抛 ``DownloadCancelled``。
    """
    tmp = dest + ".part"
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        if stop_event is not None and stop_event.is_set():
            raise DownloadCancelled(t("已取消下载"))
        try:
            ranges = _chunk_plan(_probe_total(url))
            if ranges:
                _download_parallel(url, tmp, ranges, progress_cb, stop_event)
            else:
                _download_once(url, tmp, progress_cb, stop_event)
            os.replace(tmp, dest)
            return
        except DownloadCancelled:
            raise
        except Exception as e:  # noqa: BLE001 - 网络错误种类多，统一重试
            if stop_event is not None and stop_event.is_set():
                raise DownloadCancelled(t("已取消下载")) from e
            last_err = e
            if attempt < retries:
                time.sleep(min(1.5 * (attempt + 1), 5))
    raise RuntimeError(
        t("下载失败（已自动重试 {n} 次）：{err}", n=retries, err=last_err)) from last_err


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
        last_report = 0.0
        for i, name in enumerate(names):
            clean = name.replace("\\", "/")
            parts = clean.split("/")
            if clean.startswith("/") or ".." in parts or (parts and ":" in parts[0]):
                raise RuntimeError(t("压缩包包含非法路径：{path}", path=name))
            target = os.path.join(dest_dir, *parts)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if progress_cb and total:
                # PHP 包内文件上千个，逐文件回调会把 UI 队列刷爆（表现为解压时界面卡顿）
                now = time.time()
                if i + 1 >= total or now - last_report >= PROGRESS_INTERVAL:
                    last_report = now
                    progress_cb(i + 1, total)
