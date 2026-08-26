# -*- coding: utf-8 -*-
"""运行健康监测：php-cgi 崩溃检测（Windows 事件日志）+ 版本一键自检。

崩溃检测：周期调用 PowerShell Get-WinEvent 读取 Application/1000 事件，
过滤 Faulting application 为 php-cgi.exe 的记录（如 JIT/扩展导致的 0xc0000005）；
进程内维护游标仅返回新增事件；支持启动时回溯最近 24h。

版本自检：对指定版本执行 php -v / php -m / php -c <ini>，
核对关键扩展与配置加载，返回分级结果。
"""
import os
import re
import threading
from datetime import datetime, timedelta

from . import process_utils as pu
from .php_manager import PhpVersion

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


class HealthMonitor:
    """崩溃事件检测与版本自检（run_cmd 子进程实现，零第三方依赖）。"""

    def __init__(self):
        self.last_event_time: datetime | None = None
        self.lock = threading.Lock()
        self.recent_crashes: list[dict] = []

    # ---------------------------- 崩溃检测 ---------------------------- #
    def fetch_crash_events(self, hours: int = 24) -> list[dict]:
        """查询最近 N 小时 Application/1000 中 php-cgi.exe 崩溃（时间倒序）。"""
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
            return []
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
        checks.append({"name": "PHP 版本", "ok": ok_v,
                       "detail": first or (err.strip()[:200] or "无法获取版本")})

        code, out, _ = pu.run_cmd([exe, "-m"], timeout=20)
        modules = {m for m in re.findall(r"^([A-Za-z0-9_]+)$", out, re.M)}
        missing = [k for k in KEY_EXTENSIONS if k not in modules]
        checks.append({"name": "关键扩展", "ok": not missing,
                       "detail": "缺失：" + ", ".join(missing) if missing
                       else f"{len(KEY_EXTENSIONS)} 项全部就绪"})

        ini_name = os.path.basename(v.ini)
        code, out, err = pu.run_cmd([exe, "-c", v.ini, "-r", "echo 'OK';"], timeout=20)
        warn = err.strip()
        load_ok = code == 0 and "OK" in out and "Unable to load" not in warn
        detail = ""
        if not load_ok:
            detail = warn[:300] if warn else out.strip()[:200]
        else:
            detail = f"{ini_name} 加载正常" + ("，存在警告" if warn else "")
        checks.append({"name": f"配置加载 ({ini_name})", "ok": load_ok, "detail": detail})

        return {
            "version": v.display or v.name,
            "ini": v.ini,
            "ok": all(c["ok"] for c in checks),
            "checks": checks,
        }


def _g1(pattern: re.Pattern, text: str, default: str = "未知") -> str:
    m = pattern.search(text)
    return m.group(1) if m else default
