# -*- coding: utf-8 -*-
"""开发环境配置推荐（PHP / Nginx）：按本机硬件与平台算建议值，用户勾选后写入。

设计要点
--------
1. **按机器分档**：CPU 核数 + 物理内存 → low / mid / high，决定 opcache 内存、
   worker 连接数、缓冲区等容量型参数；未知内存时按 low 档（保守）。
2. **只面向本地开发**：凡是会让「改代码不立即生效」的生产向激进缓存
   （``opcache.validate_timestamps=0``、``open_file_cache`` 长缓存）一律不推荐；
   反而明确推荐 ``validate_timestamps=1`` + ``revalidate_freq=0``。
3. **平台差异是硬约束**：Windows 版 nginx 只有单 worker 且用 select 事件模型，
   ``worker_connections`` 上限 1024、不支持 ``worker_rlimit_nofile`` / ``use epoll``；
   Windows 下 php-cgi 也不能靠多子进程扩展并发，故不推荐进程数类参数。
4. **只给建议不改文件**：:func:`php_suggestions` / :func:`nginx_suggestions` 纯只读，
   写入必须显式调用 :func:`apply_php` / :func:`apply_nginx`（自动备份 + 校验回滚）。

数值来源：nginx 官方 Windows 版限制与 PHP opcache 官方默认值，结合本工具
（每版本单 php-cgi 进程 + 单 nginx）的实际负载形态取的分档值。
"""
import ctypes
import os
import re
import sys
from dataclasses import dataclass

from . import file_backup, ini_editor, nginx_conf
from .config import IS_WIN
from .i18n import t

# --------------------------------------------------------------------------- #
# 机器画像
# --------------------------------------------------------------------------- #
@dataclass
class MachineProfile:
    """本机性能画像（只做只读探测）。"""

    cpu: int = 1
    mem_gb: float = 0.0
    platform: str = ""      # windows / macos / linux / unknown
    tier: str = "low"       # low / mid / high

    def summary(self) -> str:
        mem = f"{self.mem_gb:.0f} GB" if self.mem_gb else t("未知")
        return t("{cpu} 核 CPU · {mem} 内存 · {platform}",
                 cpu=self.cpu, mem=mem, platform=t(_PLATFORM_NAME.get(self.platform, "未知")))


_PLATFORM_NAME = {
    "windows": "Windows",
    "macos": "macOS",
    "linux": "Linux",
    "unknown": "未知",
}


def _current_platform() -> str:
    if IS_WIN:
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


def _total_memory_bytes() -> int:
    """物理内存总字节数；取不到返回 0（调用方按 low 档保守处理）。

    Windows 用 GlobalMemoryStatusEx；posix 用 sysconf（无子进程），
    失败再回落 /proc/meminfo。
    """
    if IS_WIN:
        try:
            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(stat)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys)
        except Exception:  # noqa: BLE001 —— 探测失败不应影响主流程
            pass
        return 0
    try:
        return int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):
        pass
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, IndexError, ValueError):
        pass
    return 0


def _tier(cpu: int, mem_gb: float) -> str:
    """按 CPU / 内存分档：容量型参数取值的唯一依据。"""
    if cpu >= 10 or mem_gb >= 32:
        return "high"
    if cpu >= 6 or mem_gb >= 16:
        return "mid"
    return "low"


def detect_machine(cpu: int | None = None, mem_bytes: int | None = None,
                   platform: str | None = None) -> MachineProfile:
    """探测本机画像（参数仅用于测试注入）。"""
    cpu_count = int(cpu) if cpu else (os.cpu_count() or 1)
    # mem_bytes 显式传 0 表示「探测不到」，不能回落到真实探测
    total = int(mem_bytes) if mem_bytes is not None else _total_memory_bytes()
    plat = platform or _current_platform()
    return MachineProfile(cpu=cpu_count, mem_gb=round(total / (1024 ** 3), 1),
                          platform=plat, tier=_tier(cpu_count, total / (1024 ** 3)))


# --------------------------------------------------------------------------- #
# 建议项
# --------------------------------------------------------------------------- #
@dataclass
class Suggestion:
    """一条建议：键名 / 建议值 / 当前值 / 理由。

    ``cmp`` 控制「当前值已达标就别再推荐」的判定：
    - ``eq``：不等即推荐（开关、策略类）；
    - ``min``：建议值是下限，当前已 >= 建议则跳过（容量型，只增不减）；
    - ``max``：建议值是上限，当前已 <= 建议则跳过（超时类，只减不增）。
    """

    key: str
    value: str
    current: str = ""
    reason: str = ""
    context: str = ""       # nginx 上下文（main / events / http）；PHP 为空串
    group: str = ""         # 展示分组
    cmp: str = "eq"         # eq / min / max

    def to_dict(self) -> dict:
        return {
            "key": self.key, "value": self.value, "current": self.current,
            "reason": self.reason, "context": self.context, "group": self.group,
        }


_NUM_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*([kmgb]?)")
_UNIT_MB = {"": 1 / 1024 / 1024, "b": 1 / 1024 / 1024, "k": 1 / 1024, "m": 1.0, "g": 1024.0}


def _num(value: str) -> float | None:
    """把 '128M' / '64m' / '1024' / '1G' 归一为 MB；'-1'（不限）视为无穷。"""
    m = _NUM_RE.fullmatch((value or "").strip().lower())
    if not m:
        return None
    n = float(m.group(1))
    if n < 0:
        return float("inf")
    return n * _UNIT_MB.get(m.group(2), 1.0)


def _skip(current: str, value: str, mode: str) -> bool:
    """当前值是否已满足建议（满足则不打扰用户）。"""
    cur = (current or "").strip()
    if not cur:
        return False
    if cur.lower() == value.strip().lower():
        return True
    if mode in ("min", "max"):
        a, b = _num(cur), _num(value)
        if a is not None and b is not None:
            return a >= b if mode == "min" else a <= b
    return False


# --------------------------------------------------------------------------- #
# PHP 建议
# --------------------------------------------------------------------------- #
_PHP_TIERS = {
    # opcache 内存 / interned 缓冲 / 可缓存文件数 / realpath 缓存 / 单请求内存上限
    "low": {"opcache": "128", "interned": "16", "files": "7963",
            "realpath": "1024K", "memory": "256M"},
    "mid": {"opcache": "192", "interned": "24", "files": "16229",
            "realpath": "4096K", "memory": "512M"},
    "high": {"opcache": "256", "interned": "32", "files": "32531",
             "realpath": "4096K", "memory": "1G"},
}


def _php_items(profile: MachineProfile, php_version: str = "") -> list[dict]:
    """PHP 建议项（不含当前值判定）。"""
    p = _PHP_TIERS[profile.tier]
    items: list[dict] = [
        {"key": "opcache.enable", "value": "1", "group": "opcache",
         "reason": t("开启字节码缓存：省掉每个请求重复编译 PHP 的开销，本地开发同样受益")},
        {"key": "opcache.enable_cli", "value": "1", "group": "opcache",
         "reason": t("让 composer / artisan / phpunit 等命令行也走缓存，明显加快依赖脚本")},
        {"key": "opcache.memory_consumption", "value": p["opcache"], "cmp": "min",
         "group": "opcache",
         "reason": t("按本机内存给的共享内存大小；不够时 opcache 会频繁重启缓存，命中率骤降")},
        {"key": "opcache.interned_strings_buffer", "value": p["interned"], "cmp": "min",
         "group": "opcache",
         "reason": t("驻留字符串池：框架类名/数组键复用，默认 8M 对 Laravel 一类项目偏小")},
        {"key": "opcache.max_accelerated_files", "value": p["files"], "cmp": "min",
         "group": "opcache",
         "reason": t("可缓存文件数上限需覆盖 vendor 规模，默认 10000 以下会漏缓存大量依赖文件")},
        {"key": "opcache.validate_timestamps", "value": "1", "group": "opcache",
         "reason": t("开发必须开启：检查文件修改时间，改了代码自动失效，不用手动清缓存")},
        {"key": "opcache.revalidate_freq", "value": "0", "group": "opcache",
         "reason": t("每次请求都检查文件时间戳（默认 2 秒），改完代码刷新即生效")},
        {"key": "opcache.save_comments", "value": "1", "group": "opcache",
         "reason": t("保留注释：框架注解 / 文档解析（Doctrine、PHPUnit）依赖它")},
        {"key": "realpath_cache_size", "value": p["realpath"], "cmp": "min",
         "group": "opcache",
         "reason": t("路径解析缓存：框架每次请求上千次 stat()，默认 16K 太小")},
        {"key": "realpath_cache_ttl", "value": "600", "cmp": "min", "group": "opcache",
         "reason": t("延长路径缓存有效期，减少重复磁盘查询")},
        {"key": "memory_limit", "value": p["memory"], "cmp": "min", "group": "limits",
         "reason": t("按本机内存给的单请求上限：太小会白屏（Allowed memory size exhausted）")},
        {"key": "max_execution_time", "value": "300", "cmp": "min", "group": "limits",
         "reason": t("调试/断点/慢脚本不至于 30 秒被掐断")},
        {"key": "max_input_time", "value": "300", "cmp": "min", "group": "limits",
         "reason": t("与执行超时对齐，避免大表单或慢请求解析阶段超时")},
        {"key": "post_max_size", "value": "64M", "cmp": "min", "group": "limits",
         "reason": t("与上传上限配套（需 >= upload_max_filesize），调试大表单/接口更省心")},
        {"key": "upload_max_filesize", "value": "64M", "cmp": "min", "group": "limits",
         "reason": t("本地调试上传（含 nginx client_max_body_size 需同步调大）")},
        {"key": "max_file_uploads", "value": "50", "cmp": "min", "group": "limits",
         "reason": t("默认 20 在批量上传调试时容易撞到")},
        {"key": "display_errors", "value": "On", "group": "dev",
         "reason": t("开发期直接把错误显示在页面上（生产应 Off）")},
        {"key": "error_reporting", "value": "E_ALL", "group": "dev",
         "reason": t("报告全部错误等级，提前暴露废弃与隐患写法")},
        {"key": "log_errors", "value": "On", "group": "dev",
         "reason": t("同时写入错误日志，便于回溯页面上看不到的错误")},
        {"key": "zend.assertions", "value": "1", "group": "dev",
         "reason": t("开发模式开启断言（生产应为 -1）")},
        {"key": "assert.exception", "value": "1", "group": "dev",
         "reason": t("断言失败抛异常而不是静默警告")},
    ]
    # JIT 仅 PHP 8+ 存在，且对典型 Web 请求收益有限、还占内存 → 明确关掉
    if _php_major(php_version) >= 8:
        items.append({"key": "opcache.jit_buffer_size", "value": "0", "group": "opcache",
                      "reason": t("关闭 JIT：对典型 Web 请求收益有限，却额外占用内存、拖慢首次编译")})
    return items


def _php_major(version: str) -> int:
    """从 '8.2.4' 之类版本号取主版本；无法解析返回 0。"""
    m = re.match(r"\s*(\d+)", version or "")
    return int(m.group(1)) if m else 0


#: 只在「当前未设置」时才建议的兜底项
OPTIONAL_PHP_KEYS = ("date.timezone", "default_charset")


def php_suggestions(ini_path: str, profile: MachineProfile,
                    php_version: str = "") -> list[Suggestion]:
    """读取 php.ini 当前值 → 返回待改进的建议项（已达标的不出现）。"""
    items = _php_items(profile, php_version)
    keys = [i["key"] for i in items] + list(OPTIONAL_PHP_KEYS)
    current = ini_editor.load_any(ini_path, keys)
    out: list[Suggestion] = []
    for it in items:
        cur = current.get(it["key"], "")
        # 未设置时只有「非空才建议」的项（时区/字符集）才建议，见 _fill_optional
        if _skip(cur, it["value"], it.get("cmp", "eq")):
            continue
        out.append(Suggestion(
            key=it["key"], value=it["value"], current=cur,
            reason=it["reason"], group=it.get("group", ""),
            cmp=it.get("cmp", "eq"),
        ))
    out.extend(_optional_php_items(current))
    return out


def _optional_php_items(current: dict) -> list[Suggestion]:
    """只在「当前未设置」时才给出的兜底项（时区 / 字符集）。"""
    out: list[Suggestion] = []
    if not (current.get("date.timezone") or "").strip():
        out.append(Suggestion(
            key="date.timezone", value="Asia/Shanghai", current="", group="dev",
            reason=t("未设置时区会触发 PHP 警告，且 date() 结果可能与预期不符")))
    if not (current.get("default_charset") or "").strip():
        out.append(Suggestion(
            key="default_charset", value="UTF-8", current="", group="dev",
            reason=t("未声明默认字符集时，响应头可能不带 charset，页面易出现乱码")))
    return out


def apply_php(ini_path: str, items: list[Suggestion]) -> dict:
    """写入勾选的 PHP 建议项（ini_editor 自带备份与失败还原）。"""
    changes = {s.key: s.value for s in items}
    if not changes:
        return {"file": ini_path, "changed": 0, "keys": []}
    count, backup = ini_editor.save_values(ini_path, changes)
    return {"file": ini_path, "changed": count, "backup": backup, "keys": sorted(changes)}


# --------------------------------------------------------------------------- #
# Nginx 建议
# --------------------------------------------------------------------------- #
_NGINX_TIERS = {
    "low": {"conns": "1024", "rlimit": "2048", "buffers": "8 16k"},
    "mid": {"conns": "2048", "rlimit": "4096", "buffers": "16 16k"},
    "high": {"conns": "4096", "rlimit": "8192", "buffers": "32 16k"},
}


def _nginx_items(profile: MachineProfile) -> list[dict]:
    """Nginx 建议项（平台相关的硬约束在此过滤）。"""
    p = _NGINX_TIERS[profile.tier]
    win = profile.platform == "windows"
    unix = not win
    items: list[dict] = [
        {"key": "worker_processes", "value": "1" if win else "auto",
         "context": nginx_conf.MAIN, "group": "core",
         "reason": (t("Windows 版 nginx 只支持单 worker（无 fork），写多进程无效")
                    if win else t("auto 自动按 CPU 核数起 worker，吃满多核"))},
        {"key": "worker_connections", "value": "1024" if win else p["conns"],
         "context": nginx_conf.EVENTS, "cmp": "min", "group": "core",
         "reason": (t("Windows 用 select 事件模型，连接数上限就是 1024，写更大只会告警")
                    if win else t("单 worker 可承载的连接数，按本机档位给；太小会排队"))},
        {"key": "tcp_nodelay", "value": "on", "context": nginx_conf.HTTP, "group": "core",
         "reason": t("小响应立即发出，降低本地请求的等待感")},
        {"key": "keepalive_timeout", "value": "30", "context": nginx_conf.HTTP,
         "cmp": "max", "group": "core",
         "reason": t("默认 75 秒过长：空闲连接长时间占着 worker，本地多站点调试时更明显")},
        {"key": "client_max_body_size", "value": "64m", "context": nginx_conf.HTTP,
         "cmp": "min", "group": "limits",
         "reason": t("与 PHP 上传上限（64M）配套，否则大文件在 nginx 层就被 413 拦下")},
        {"key": "client_body_buffer_size", "value": "1m", "context": nginx_conf.HTTP,
         "cmp": "min", "group": "limits",
         "reason": t("请求体先在内存缓冲，减少写临时文件带来的磁盘 IO")},
        {"key": "types_hash_max_size", "value": "2048", "context": nginx_conf.HTTP,
         "cmp": "min", "group": "core",
         "reason": t("站点多了 MIME 哈希表容易撞默认 1024，日志里会出现 types_hash 告警")},
        {"key": "server_tokens", "value": "off", "context": nginx_conf.HTTP, "group": "dev",
         "reason": t("不在响应头暴露 nginx 版本号（顺便少几十字节）")},
        {"key": "gzip", "value": "off", "context": nginx_conf.HTTP, "group": "dev",
         "reason": t("本地没有带宽瓶颈，压缩反而多花 CPU、拉长首字节时间；抓包看明文也更方便")},
        {"key": "access_log", "value": "off", "context": nginx_conf.HTTP, "group": "dev",
         "reason": t("开发期访问日志写入量很大（尤其前端热更新），关掉能明显减少磁盘 IO")},
        {"key": "fastcgi_read_timeout", "value": "300", "context": nginx_conf.HTTP,
         "cmp": "min", "group": "limits",
         "reason": t("与 PHP max_execution_time=300 对齐，否则调试慢脚本时 nginx 先返回 504")},
        {"key": "fastcgi_send_timeout", "value": "300", "context": nginx_conf.HTTP,
         "cmp": "min", "group": "limits",
         "reason": t("同上：两次 FastCGI 写之间的超时也要放开")},
        {"key": "fastcgi_buffers", "value": p["buffers"], "context": nginx_conf.HTTP,
         "group": "limits",
         "reason": t("响应缓冲区按档位放大，避免大响应落临时文件")},
    ]
    if unix:
        items.insert(2, {"key": "worker_rlimit_nofile", "value": p["rlimit"],
                         "context": nginx_conf.MAIN, "cmp": "min", "group": "core",
                         "reason": t("抬高单进程可打开文件数上限，配合上面的连接数（Windows 无此指令）")})
        items.insert(3, {"key": "multi_accept", "value": "on",
                         "context": nginx_conf.EVENTS, "group": "core",
                         "reason": t("一次 accept 全部新连接，减少事件循环往返")})
        items.insert(4, {"key": "sendfile", "value": "on", "context": nginx_conf.HTTP,
                         "group": "core",
                         "reason": t("静态文件走内核零拷贝发送，省一次用户态拷贝")})
        items.insert(5, {"key": "tcp_nopush", "value": "on", "context": nginx_conf.HTTP,
                         "group": "core",
                         "reason": t("配合 sendfile，等包攒满再发出，减少小包数量")})
        if profile.platform == "linux":
            items.insert(6, {"key": "use", "value": "epoll", "context": nginx_conf.EVENTS,
                             "group": "core",
                             "reason": t("Linux 下显式指定 epoll（默认已自动选择，写出来便于确认）")})
    return items


def nginx_notes(profile: MachineProfile) -> list[str]:
    """与具体指令无关、但需要让用户知道的平台说明。"""
    notes: list[str] = []
    if profile.platform == "windows":
        notes.append(t("Windows 版 nginx 为单 worker + select 事件模型：并发上限约 1024 连接，"
                       "且不支持 worker_rlimit_nofile / multi_accept / epoll。"
                       "本地开发够用，压测请换 Linux / WSL2。"))
    notes.append(t("全部建议都按「本地开发」取的值：生产环境请不要直接套用"
                   "（尤其 display_errors、access_log off、gzip off）。"))
    return notes


def nginx_suggestions(conf_text: str, profile: MachineProfile) -> list[Suggestion]:
    """基于 nginx.conf 文本返回待改进的建议项。"""
    items = _nginx_items(profile)
    out: list[Suggestion] = []
    for it in items:
        cur = nginx_conf.get_directive(conf_text, it["key"], it.get("context")) or ""
        if _skip(cur, it["value"], it.get("cmp", "eq")):
            continue
        out.append(Suggestion(
            key=it["key"], value=it["value"], current=cur,
            reason=it["reason"], context=it.get("context", ""),
            group=it.get("group", ""), cmp=it.get("cmp", "eq"),
        ))
    # 开发期若显式开了文件缓存，反而会让「改了文件不生效」
    if nginx_conf.get_directive(conf_text, "open_file_cache", nginx_conf.HTTP):
        out.append(Suggestion(
            key="open_file_cache", value="off", context=nginx_conf.HTTP, group="dev",
            current=nginx_conf.get_directive(conf_text, "open_file_cache", nginx_conf.HTTP) or "",
            reason=t("开发期文件频繁变更，文件描述符缓存会导致改完不生效，建议关闭")))
    return out


def apply_nginx(conf_path: str, items: list[Suggestion], verify: bool = True,
                reload: bool = False) -> dict:
    """写入勾选的 nginx 建议项：备份 → 写入 → `nginx -t` 校验（失败自动还原）。

    返回 dict 含 changed / backup / items；校验失败时 rolled_back=True。
    """
    result: dict = {"file": conf_path, "changed": 0, "keys": [], "rolled_back": False}
    if not items:
        return result
    try:
        with open(conf_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        result.update(ok=False, error=t("读取配置失败：{err}", err=e))
        return result

    changes = [(s.key, s.value, s.context or nginx_conf.HTTP) for s in items]
    new_text, applied = nginx_conf.apply_changes(text, changes)
    if new_text == text:
        return result

    backup = file_backup.backup(conf_path)
    try:
        with open(conf_path, "w", encoding="utf-8") as f:
            f.write(new_text)
    except OSError as e:
        if backup:
            file_backup.restore(backup, conf_path, remove_backup=False)
        result.update(ok=False, rolled_back=bool(backup),
                      error=t("写入配置失败：{err}", err=e))
        return result

    result.update(changed=sum(1 for a in applied if a["action"] != "unchanged"),
                  keys=[a["key"] for a in applied], backup=backup, items=applied)

    if verify:
        from .nginx_manager import NginxManager

        mgr = NginxManager()
        if os.path.exists(mgr.exe):
            code, out = mgr.verify_config()
            result["test_output"] = out
            if code != 0:
                rolled = file_backup.restore(backup, conf_path, remove_backup=False)
                result.update(ok=False, rolled_back=rolled, changed=0, keys=[],
                              error=t("配置检查未通过，已用备份还原：{out}", out=out))
                return result
        else:
            result["test_output"] = t("未找到 nginx 可执行文件，跳过配置检查")
    if reload:
        from .nginx_manager import NginxManager

        result["reload"] = NginxManager().reload()
    result.setdefault("ok", True)
    return result


def php_notes() -> list[str]:
    """PHP 建议的通用说明。"""
    return [
        t("写入后需重启对应 PHP 版本（停止再启动）才会生效——php-cgi 不会热加载 ini。"),
        t("opcache 相关项只有在扩展已加载时才起作用；"
          "若 php.ini 里没有 opcache 扩展行，这些值会被忽略（不影响启动）。"),
    ]


#: 供 UI / CLI 复用的展示分组顺序
GROUP_ORDER = ["core", "opcache", "limits", "dev"]
