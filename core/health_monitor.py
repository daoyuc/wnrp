# -*- coding: utf-8 -*-
"""运行健康监测：php-cgi 崩溃检测 + 版本一键自检。

崩溃事件数据源（fetch_crash_events 自动按平台分派）：
- Windows：PowerShell Get-WinEvent 读取 Application/1000 事件，过滤
  Faulting application 为 php-cgi.exe 的记录（JIT/扩展导致的 0xc0000005）；
- macOS：扫描 ~/Library/Logs/DiagnosticReports 中 php-cgi-*.ips 崩溃报告
  （ReportCrash 产出，JSON 行格式），提取异常/故障模块/偏移/栈顶；
- Linux：无等效数据源，返回 None（调用方不推进检测游标）。
进程内维护游标仅返回新增事件；支持启动时回溯最近 24h。

版本自检：对指定版本执行 php -v / php -m / php -c <ini>，
核对关键扩展与配置加载，返回分级结果。
"""
import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta

from . import process_utils as pu
from .config import IS_WIN
from .i18n import t
from .php_manager import PhpVersion

_IS_DARWIN = sys.platform == "darwin"

KEY_EXTENSIONS = ["redis", "pdo_mysql", "mysqli", "openssl", "curl",
                  "mbstring", "gd", "fileinfo", "zip"]
_TS_FMT = "%Y-%m-%d %H:%M:%S"  # Python strftime 格式（since 时间戳生成）
_NET_TS_FMT = "yyyy-MM-dd HH:mm:ss"  # .NET 自定义格式（PS ToString 使用，勿与 strftime 混淆）
# 中英文系统语言事件 1000 消息字段均需兼容（中文冒号：/英文冒号:）
_RE = {k: re.compile(p) for k, p in {
    "app": r"(?:Faulting application name|出错应用程序名称)[：:]\s*([^\s，,]+)",
    "module": r"(?:Faulting module name|出错模块名称)[：:]\s*([^\s，,]+)",
    "code": r"(?:Exception code|异常代码)[：:]\s*(\S+)",
    "offset": r"(?:Fault offset|错误偏移|错误偏移量)[：:]\s*(\S+)",
    "path": r"(?:Faulting application path|Faulting 应用程序路径|出错的应用程序路径)[：:]\s*(\S+)",
    "verdir": r"[\\/](php\d{0,2})[\\/]php-cgi\.exe",
}.items()}


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, _TS_FMT)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# macOS 崩溃报告（~/Library/Logs/DiagnosticReports 的 php-cgi-*.ips）
# --------------------------------------------------------------------------- #
def _mac_crash_dirs() -> list[str]:
    """候选崩溃报告目录（按优先级，只保留存在的）：
    PHPVM_MAC_CRASH_DIR 环境变量（测试用）→ 用户报告 → 系统报告。"""
    cands = []
    env = os.environ.get("PHPVM_MAC_CRASH_DIR", "")
    if env:
        cands.append(env)
    cands.append(os.path.join(
        os.path.expanduser("~"), "Library", "Logs", "DiagnosticReports"))
    cands.append("/Library/Logs/DiagnosticReports")
    out: list[str] = []
    for d in cands:
        if d and d not in out and os.path.isdir(d):
            out.append(d)
    return out


# 现代 ReportCrash 的 .ips：首行为元信息 JSON，第二行起为完整 JSON 载荷
_MAC_TS_FMTS = (
    "%Y-%m-%d %H:%M:%S.%f %z",   # "2026-09-08 01:23:45.00 +0800"
    "%Y-%m-%d %H:%M:%S %z",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
)


def _mac_ts_to_local(text: str) -> str | None:
    """把 .ips 时间戳转本地时区字符串（_TS_FMT），解析失败返回 None。"""
    text = (text or "").strip()
    for fmt in _MAC_TS_FMTS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if dt.tzinfo is not None:
            dt = dt.astimezone()  # 转本机时区
        return dt.strftime(_TS_FMT)
    return None


def _mac_version_from_path(path: str) -> str | None:
    """从崩溃报告中的可执行路径推断版本短名：
    /…/Cellar/php@7.4/7.4.33/bin/php-cgi → php74；php → php；php74 → php74。
    推断不到（非 phpvm 管理的 php-cgi）返回 None。"""
    if not path:
        return None
    for tok in path.split("/"):
        if tok == "php":
            return "php"
        m = re.fullmatch(r"php@(\d+)\.(\d+)", tok)
        if m:
            return f"php{m.group(1)}{m.group(2)}"
        if re.fullmatch(r"php\d{1,3}", tok):
            return tok
    return None


def _fmt_off(val) -> str:
    if isinstance(val, int):
        return hex(val)
    return str(val) if val else ""


def _parse_mac_ips(path: str) -> dict | None:
    """解析单份 php-cgi-*.ips，转统一崩溃事件 dict；失败返回 None。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None
    nl = text.find("\n")
    if nl <= 0:
        return None
    try:
        meta = json.loads(text[:nl])
    except json.JSONDecodeError:
        meta = {}
    body: dict = {}
    try:
        rest = text[nl + 1:].lstrip()
        if rest:
            obj, _ = json.JSONDecoder().raw_decode(rest)
            if isinstance(obj, dict):
                body = obj
    except (json.JSONDecodeError, ValueError):
        body = {}

    name = (meta.get("name") or meta.get("app_name") or "php-cgi") if isinstance(meta, dict) else "php-cgi"
    ts = _mac_ts_to_local(meta.get("timestamp") or body.get("captureTime") or "")
    if not ts:
        try:
            ts = datetime.fromtimestamp(os.path.getmtime(path)).strftime(_TS_FMT)
        except OSError:
            ts = ""

    # 崩溃现场：faultingThread 首帧的镜像 = 故障模块 + 偏移
    threads = body.get("threads") if isinstance(body.get("threads"), list) else []
    frames: list[dict] = []
    ft = body.get("faultingThread")
    if isinstance(ft, int) and 0 <= ft < len(threads) and isinstance(threads[ft], dict):
        fr = threads[ft].get("frames")
        frames = fr if isinstance(fr, list) else []
    imgs = body.get("usedImages") if isinstance(body.get("usedImages"), list) else []
    module, offset, img_path = "", "", ""
    if frames:
        f0 = frames[0] if isinstance(frames[0], dict) else {}
        ii = f0.get("imageIndex")
        if isinstance(ii, int) and 0 <= ii < len(imgs) and isinstance(imgs[ii], dict):
            img = imgs[ii]
            module = img.get("name") or ""
            img_path = img.get("path") or ""
        offset = _fmt_off(f0.get("imageOffset") or f0.get("symbolLocation"))

    proc_path = body.get("procPath") or img_path or meta.get("path") or ""
    exception_str = ""
    exc = body.get("exception") if isinstance(body.get("exception"), dict) else {}
    if exc:
        etype = exc.get("type") or ""
        sig = exc.get("signal") or ""
        exception_str = etype + (f" ({sig})" if sig else "")
    if not exception_str:
        exception_str = t("未知")

    term = body.get("termination") if isinstance(body.get("termination"), dict) else {}
    lines = [t("报告：{file}", file=os.path.basename(path))]
    if proc_path:
        lines.append(t("进程路径：{path}", path=proc_path))
    if ts:
        lines.append(t("崩溃时间：{ts}", ts=ts))
    lines.append(t("异常：{exception}", exception=exception_str))
    if term:
        ind = term.get("indicator")
        if ind:
            lines.append(t("终止原因：{reason}", reason=ind))
            reasons = term.get("reasons")
            if reasons:
                lines.append(t("           {reasons}", reasons=reasons))
    if module or offset:
        lines.append(t("崩溃位置：模块 {module} 偏移 {offset}",
                       module=module or "?", offset=offset or "?"))
    shown: list[str] = []
    for fr in frames[:6]:
        sym = fr.get("symbol") if isinstance(fr, dict) else ""
        if sym and sym not in shown:
            shown.append(f"  at {sym}")
    if shown:
        lines.append(t("调用栈："))
        lines.extend(shown)

    return {
        "time": ts,
        "app": os.path.basename(str(name)) or "php-cgi",
        "module": module or os.path.basename(str(proc_path)) or "php-cgi",
        "exception": exception_str,
        "offset": offset,
        "path": proc_path,
        "version": _mac_version_from_path(proc_path or ""),
        "message": " | ".join(lines),
    }


def _fetch_mac_crash_events(hours: int) -> list[dict]:
    """扫描所有候选目录中的 php-cgi-*.ips，返回最近 hours 小时内事件（时间倒序）。"""
    since = datetime.now() - timedelta(hours=hours)
    events: list[dict] = []
    seen: set[str] = set()
    for d in _mac_crash_dirs():
        try:
            names = sorted(os.listdir(d), reverse=True)
        except OSError:
            continue
        for fn in names:
            if not (fn.startswith("php-cgi-") and fn.endswith(".ips")):
                continue
            if fn in seen:
                continue
            seen.add(fn)
            ev = _parse_mac_ips(os.path.join(d, fn))
            if not ev:
                continue
            dt = _parse_ts(ev["time"])
            if dt and dt < since:
                continue
            events.append(ev)
            if len(events) >= 25:
                break
        if len(events) >= 25:
            break
    events.sort(key=lambda e: e["time"], reverse=True)
    return events


class HealthMonitor:
    """崩溃事件检测与版本自检（run_cmd 子进程实现，零第三方依赖）。"""

    def __init__(self):
        self.last_event_time: datetime | None = None
        self.lock = threading.Lock()
        self.recent_crashes: list[dict] = []

# ---------------------------- 崩溃检测 ---------------------------- #
    def fetch_crash_events(self, hours: int = 24) -> list[dict] | None:
        """查询最近 N 小时 php-cgi 崩溃事件（时间倒序），按平台自动分派：

        - Windows：Application/1000（Get-WinEvent）；
        - macOS：~/Library/Logs/DiagnosticReports 的 php-cgi-*.ips；
        - 其它：返回 None（无数据源）。
        返回 None 表示查询失败/不可用，调用方不应推进检测游标，否则会漏掉
        失败窗口内产生的崩溃事件。
        """
        if _IS_DARWIN:
            return _fetch_mac_crash_events(hours)
        if not IS_WIN:
            return None
        since = (datetime.now() - timedelta(hours=hours)).strftime(_TS_FMT)
        ps = (
            "$e = Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000} "
            "-MaxEvents 50 -ErrorAction SilentlyContinue | "
            "Where-Object { $_.TimeCreated -ge [datetime]'" + since + "' }; "
            "$e | ForEach-Object { $_.TimeCreated.ToString('" + _NET_TS_FMT +
            "') + '|' + ($_.Message -replace \"[\\r\\n]\", ' | ') }"
        )
        code, out, _err = pu.run_cmd(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=10
        )
        if code != 0:
            return None
        events = []
        for line in out.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            ts, _, msg = line.partition("|")
            if "php-cgi" not in msg:
                continue
            events.append(self._parse_event(ts, msg))
        return events

    def _parse_event(self, ts: str, msg: str) -> dict:
        m_path = _RE["path"].search(msg)
        path = m_path.group(1) if m_path else ""
        mv = _RE["verdir"].search(path) if path else None
        return {
            "time": ts,
            "app": _g1(_RE["app"], msg, "php-cgi.exe"),
            "module": _g1(_RE["module"], msg),
            "exception": _g1(_RE["code"], msg),
            "offset": _g1(_RE["offset"], msg),
            "path": path,
            "version": mv.group(1) if mv else None,
            "message": msg,
        }

    def poll_new_crashes(self, hours: int = 24) -> list[dict]:
        """返回自上次轮询以来的新增崩溃事件并推进游标。

        首次调用（游标为空）返回最近 hours 小时全部事件，供启动提示；
        后续仅返回严格晚于游标的事件。
        """
        events = self.fetch_crash_events(hours=hours)
        if events is None:
            # 查询失败：不推进游标，避免漏掉窗口内崩溃，下轮重试
            return []
        now = datetime.now()
        with self.lock:
            last = self.last_event_time
            self.last_event_time = now
        if not events:
            return []
        if last is None:
            self.recent_crashes = events
            return list(events)
        new = [e for e in events if (t := _parse_ts(e["time"])) and t > last]
        if new:
            self.recent_crashes = new + self.recent_crashes[:20]
        return new

    def reset(self) -> None:
        """清空已读告警：本地列表置空，游标推进到当前时刻。

        注意：不能把 last_event_time 置 None——否则下一次轮询会把最近
        hours 小时的历史事件全部当作新增再次告警（“清空”后又冒出来）。
        """
        with self.lock:
            self.last_event_time = datetime.now()
            self.recent_crashes = []

    # ---------------------------- 版本自检 ---------------------------- #
    def self_check(self, v: PhpVersion) -> dict:
        """对指定版本执行自检，返回 {version, ini, ok, checks:[...]}。"""
        exe = os.path.join(v.dir, "php.exe")
        if not os.path.exists(exe):
            exe = v.cgi
        checks: list[dict] = []

        code, out, err = pu.run_cmd([exe, "-v"], timeout=20)
        first = (out or err).strip().splitlines()[0] if (out or err).strip() else ""
        ok_v = code == 0 and "PHP" in first
        checks.append({"name": t("PHP 版本"), "ok": ok_v,
                       "detail": first or (err.strip()[:200] or t("无法获取版本"))})

        code, out, _ = pu.run_cmd([exe, "-m"], timeout=20)
        modules = {m for m in re.findall(r"^([A-Za-z0-9_]+)$", out, re.M)}
        missing = [k for k in KEY_EXTENSIONS if k not in modules]
        checks.append({"name": t("关键扩展"), "ok": not missing,
                       "detail": t("缺失：{list}", list=", ".join(missing)) if missing
                       else t("{count} 项全部就绪", count=len(KEY_EXTENSIONS))})

        ini_name = os.path.basename(v.ini)
        code, out, err = pu.run_cmd([exe, "-c", v.ini, "-r", "echo 'OK';"], timeout=20)
        warn = err.strip()
        load_ok = code == 0 and "OK" in out and "Unable to load" not in warn
        detail = ""
        if not load_ok:
            detail = warn[:300] if warn else out.strip()[:200]
        else:
            detail = t("{ini} 加载正常{extra}", ini=ini_name,
                       extra=t("，存在警告") if warn else "")
        checks.append({"name": t("配置加载 ({ini})", ini=ini_name),
                       "ok": load_ok, "detail": detail})

        return {
            "version": v.display or v.name,
            "ini": v.ini,
            "ok": all(c["ok"] for c in checks),
            "checks": checks,
        }


def _g1(pattern: re.Pattern, text: str, default: str = "") -> str:
    m = pattern.search(text)
    return m.group(1) if m else (default or t("未知"))
