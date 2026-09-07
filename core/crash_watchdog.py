# -*- coding: utf-8 -*-
"""php-cgi 崩溃/失联自动重启守护进程（独立于 phpvm GUI 常驻）。

设计动机（修复记录 2026-09-07）：
- 早期自愈逻辑内嵌在 MainWindow（GUI）的 _tick 心跳里，GUI 退出即整体失效
  （当时故障期间 phpvm GUI 并未运行，无人执行自愈）；
- 且检测只依赖 Windows 事件日志 Application/1000（进程段错误 0xc0000005）。
  实际故障常是「进程被清理/异常退出但未产生崩溃事件」，nginx 表现为
  upstream timed out (10060)——事件日志检测完全覆盖不到。

本守护进程：
1) 由 phpvm 在 auto_recover_crash 开启时拉起（pythonw 隐藏运行），与 GUI
   生命周期解耦；settings.auto_recover_crash 关闭后下一轮自行退出；
2) 双重检测：
   - 事件日志新崩溃（Get-WinEvent Application/1000，复用 HealthMonitor）；
   - 看护版本的端口/进程失联探测（进程消失且无崩溃事件也能兜底恢复）；
3) 防抖 60s / 每版本每小时 auto_recover_limit 次 / 每次决策写入
   recover_history.json（供崩溃详情对话框可视化）；
4) 单实例：锁文件 + PID 校验，重复拉起自动退出。
"""
import json
import os
import subprocess
import sys
import time

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # phpvm 根目录
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from core import process_utils as pu  # noqa: E402
from core import recover_history  # noqa: E402
from core.config import Config  # noqa: E402
from core.health_monitor import HealthMonitor  # noqa: E402
from core.php_manager import PhpManager  # noqa: E402

LOCK_FILE = os.path.join(_APP_DIR, "crash_watchdog.lock")
STATE_FILE = os.path.join(_APP_DIR, "crash_watchdog.json")
LOG_FILE = os.path.join(_APP_DIR, "crash_watchdog.log")

POLL_INTERVAL = 10.0       # 失联探测轮询间隔（秒）
EVENT_EVERY = 3            # 每 N 轮查一次事件日志（≈30s）
LOOKBACK_HOURS = 6         # 事件日志回溯窗口
MIN_INTERVAL = 60.0        # 同版本两次重启最小间隔（防抖）
WINDOW = 3600.0            # 限次窗口（秒）
ESCALATE = 5               # 连续失败达到该次数 → 解除看护并提示人工介入
MANUAL_GRACE = 300.0       # GUI 手动停止某版本后的宽限：此期间不自动拉回


def _log(text: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------- #
# 单实例 / 进程探测
# --------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    try:
        return pu.is_pid_alive_fast(pid)
    except Exception:  # noqa: BLE001
        return False


def _pid_is_watchdog(pid: int) -> bool:
    """pid 对应的进程命令行是否包含本守护脚本（防误判其它 python）。"""
    code, out, _ = pu.run_cmd(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
        timeout=8,
    )
    return code == 0 and "crash_watchdog.py" in (out or "")


def _lock_owner() -> int | None:
    """读取锁文件记录的 PID；文件缺失/损坏返回 None。"""
    try:
        with open(LOCK_FILE, "r", encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def is_running() -> bool:
    """是否有存活的守护进程实例。"""
    pid = _lock_owner()
    return bool(pid and _pid_alive(pid) and _pid_is_watchdog(pid))


def _acquire() -> bool:
    """尝试接管单实例锁。已有存活实例 → False。"""
    owner = _lock_owner()
    if owner and _pid_alive(owner) and _pid_is_watchdog(owner):
        return False
    try:
        with open(LOCK_FILE, "w", encoding="ascii") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return True


def _release() -> None:
    if _lock_owner() == os.getpid():
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


def spawn() -> tuple[bool, str]:
    """以 pythonw（无窗口）拉起守护进程。幂等：已有实例则不重复拉起。"""
    if is_running():
        return True, "崩溃自愈守护进程已在运行"
    pyw = r"C:\Python312\pythonw.exe"
    if not os.path.exists(pyw):
        pyw = "pythonw"
    script = os.path.join(_APP_DIR, "core", "crash_watchdog.py")
    try:
        subprocess.Popen(
            [pyw, script],
            creationflags=subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
    except OSError as e:
        return False, f"启动守护进程失败：{e}"
    # 短暂等待，确认子进程接管锁后返回（GUI 侧在后台线程调用，可接受）
    for _ in range(10):
        time.sleep(0.3)
        if is_running():
            return True, "崩溃自愈守护进程已启动"
    return True, "守护进程已拉起（稍后自动接管）"


def watch_version(version: str) -> None:
    """将某版本纳入看护（GUI 启动/重启成功后调用）。

    同时清除该版本的手动停止宽限标记，避免宽限期内失联被误跳过。
    """
    state = _load_state()
    state.get("manual_stops", {}).pop(version, None)
    _ensure_watch(state, version)
    _save_state(state)


def unwatch(version: str) -> None:
    """移除某版本的看护（用户手动停止后调用）。

    同时记录手动停止时间戳：在 MANUAL_GRACE 宽限期内即使版本再次失联
    也不会被自动拉起，尊重用户主动停止的意图。
    """
    state = _load_state()
    watch = state.get("watch", {})
    if version in watch:
        watch.pop(version)
    state.setdefault("manual_stops", {})[version] = time.time()
    _save_state(state)
    _log(f"[{version}] 已被手动停止，移除崩溃看护并进入 {int(MANUAL_GRACE)}s 宽限")


def _manual_grace(version: str) -> bool:
    """版本是否处于手动停止后的宽限期内（宽限期内不自动重启）。"""
    state = _load_state()
    ts = state.get("manual_stops", {}).get(version)
    return bool(ts and time.time() - ts < MANUAL_GRACE)


# --------------------------------------------------------------------- #
# 看护状态
# --------------------------------------------------------------------- #
def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("watch"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"last_event_ts": None, "watch": {}}


def _save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _ensure_watch(state: dict, version: str, rec: dict | None = None) -> None:
    watch = state.setdefault("watch", {})
    if version not in watch:
        now = time.time()
        if rec is None:
            rec = {"since": now, "last_restart": 0.0, "count": 0,
                   "window_start": now, "fails": 0}
        watch[version] = rec


# --------------------------------------------------------------------- #
# 恢复动作（防抖 + 限次）
# --------------------------------------------------------------------- #
def _try_restart(cfg: Config, pm: PhpManager, versions: dict,
                 state: dict, version: str, reason: str) -> None:
    """对失联/崩溃版本执行重启（含防抖与限次），结果写 recover_history。"""
    v = versions.get(version)
    if v is None:
        return
    watch = state.setdefault("watch", {})
    _ensure_watch(state, version)
    rec = watch[version]
    now = time.time()

    # 已在运行（可能已被手动拉起）→ 不重复操作
    try:
        running, _ = pm.get_status(v, fast=False)
    except Exception:  # noqa: BLE001
        running = False
    if running:
        return
    # 刚被 GUI 手动停止 → 宽限期内不自动拉回
    if _manual_grace(version):
        _log(f"[{version}] {reason}：处于手动停止宽限期，跳过")
        return

    if rec["last_restart"] and now - rec["last_restart"] < MIN_INTERVAL:
        recover_history.append(version, "skip_interval",
                               f"{reason}：距上次自愈不足 {int(MIN_INTERVAL)}s，跳过")
        _log(f"[{version}] {reason}：防抖跳过")
        return
    if now - rec["window_start"] > WINDOW:
        rec["window_start"], rec["count"] = now, 0
    limit = int(cfg.get_setting("auto_recover_limit", 3) or 3)
    if rec["count"] >= limit:
        recover_history.append(version, "skip_limit",
                               f"{reason}：已达上限（{limit} 次/小时），暂停自动重启")
        _log(f"[{version}] {reason}：已达自愈上限（{limit} 次/小时）")
        return

    try:
        msg = pm.start(v)
        rec["last_restart"] = now
        rec["count"] += 1
        rec["fails"] = 0
        recover_history.append(version, "start", f"{reason} → {msg}")
        _log(f"[{version}] {reason} → {msg}")
    except Exception as ex:  # noqa: BLE001
        rec["fails"] += 1
        recover_history.append(version, "fail", f"{reason} → {type(ex).__name__}: {ex}")
        _log(f"[{version}] {reason} 失败：{ex}")
        if rec["fails"] >= ESCALATE:
            state["watch"].pop(version, None)
            recover_history.append(version, "fail",
                                   f"连续 {ESCALATE} 次失败，已解除看护，请手动检查 php.ini/端口")
            _log(f"[{version}] 连续 {ESCALATE} 次自愈失败，解除看护，需人工介入")


# --------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------- #
def _tick_once(cfg: Config, pm: PhpManager, versions: dict,
               hm: HealthMonitor, state: dict, event_round: bool) -> None:

    # 1) 事件日志：新崩溃事件 → 进入看护并尝试立即重启
    if event_round:
        events = hm.fetch_crash_events(hours=LOOKBACK_HOURS)
        if events is None:
            # 查询失败：本轮不推进游标，避免漏掉窗口内的崩溃
            _log("事件日志查询失败（Get-WinEvent），本轮跳过崩溃检测")
        else:
            latest = events[0]["time"] if events else None
            prev = state.get("last_event_ts")
            if prev is None:
                # 首次运行：把最近事件全部视为「已读」（避免误重启历史崩溃），
                # 但其版本仍进入看护，交给下方失联探测兜底
                state["last_event_ts"] = latest or time.strftime("%Y-%m-%d %H:%M:%S")
                for e in events:
                    ver = e.get("version")
                    if ver and versions.get(ver):
                        _ensure_watch(state, ver)
                _log(f"首轮就绪：事件游标已推进，{len(events)} 条历史事件进入看护待复核")
            elif latest and latest > prev:
                fresh = [e for e in events if e.get("time", "") > prev]
                state["last_event_ts"] = latest
                for e in fresh:
                    ver = e.get("version")
                    if not ver or ver not in versions:
                        continue
                    _log(f"[{ver}] 检测到崩溃事件：{e.get('time')} "
                         f"{e.get('module')} 异常码 {e.get('exception')}")
                    _try_restart(cfg, pm, versions, state, ver,
                                 f"崩溃事件 {e.get('exception')}")

    # 2) 失联探测：看护中的版本若已不在监听 → 兜底重启（无崩溃事件也能恢复）。
    #    看护持续有效（覆盖「进程消失但无崩溃事件」的故障），停止请走 phpvm
    #    GUI → 触发 unwatch 进入宽限期，或临时关闭自愈开关。
    for ver in list(state.get("watch", {})):
        v = versions.get(ver)
        if v is None:
            state["watch"].pop(ver, None)
            continue
        try:
            running, _ = pm.get_status(v, fast=False)
        except Exception:  # noqa: BLE001
            running = False
        if not running:
            _try_restart(cfg, pm, versions, state, ver,
                         f"进程失联（端口 {v.port} 无监听）")


def _baseline_watch(state: dict, pm: PhpManager, versions: dict) -> None:
    """守护启动首轮快照：把当前正在运行的 php-cgi 版本纳入看护。

    覆盖「进程消失/被清理但未产生崩溃事件」的故障（如 nginx 报 upstream
    timed out）：只要守护进程存活且曾观测到该版本在运行，失联即自动恢复。
    """
    added = 0
    for name, v in versions.items():
        try:
            running, _ = pm.get_status(v, fast=False)
        except Exception:  # noqa: BLE001
            running = False
        if running and name not in state.get("watch", {}):
            _ensure_watch(state, name)
            added += 1
    if added:
        _log(f"启动快照：{added} 个运行中的版本已纳入崩溃看护")
    state["boot"] = time.strftime("%Y-%m-%d %H:%M:%S")


def run(once: bool = False) -> None:
    if not _acquire():
        _log("已有守护进程在运行，本实例退出")
        print("crash_watchdog already running, exit") if not once else None
        return
    _log("=== 崩溃自愈守护进程启动 ===")
    try:
        state = _load_state()
        # 忽略历史手动停止标记（跨会话）；本会话新标记由 unwatch 写入
        state.pop("manual_stops", None)
        round_no = 0
        while True:
            cfg = Config()
            if not cfg.get_setting("auto_recover_crash", False):
                _log("auto_recover_crash 已关闭，守护进程退出")
                break
            event_round = (round_no % EVENT_EVERY == 0)
            try:
                pm = PhpManager(cfg)
                versions = {v.name: v for v in pm.scan_versions()}
                hm = HealthMonitor()
                if round_no == 0:
                    # 启动首轮：纳入当前运行中的版本作为看护基线
                    _baseline_watch(state, pm, versions)
                _tick_once(cfg, pm, versions, hm, state, event_round)
                _save_state(state)
            except Exception as ex:  # noqa: BLE001
                _log(f"守护循环异常：{type(ex).__name__}: {ex}")
            round_no += 1
            if once:
                break
            time.sleep(POLL_INTERVAL)
    finally:
        _release()
        _log("=== 崩溃自愈守护进程退出 ===")


if __name__ == "__main__":
    run(once=("--once" in sys.argv))
