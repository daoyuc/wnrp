# -*- coding: utf-8 -*-
"""键鼠统计后台守护进程（独立进程，phpvm 关闭后继续记录）。

约定（与 ``core/crash_watchdog.py`` 同一套范式）：
- 单实例：``daemon.lock`` 记录 PID，``daemon.heartbeat`` 每 2 秒刷新；
  ``is_running()`` = PID 存活 + 心跳新鲜（心跳还能识别「进程在但循环卡死」）；
- 与 GUI / CLI 只通过三个文件通信：``paused``（暂停标记）、``stop``（停止请求）、
  以及只读的 SQLite 统计库，不做 IPC / socket；
- 插件在设置页停用后，本进程下一轮配置检查（≤60s）自行 flush 并退出；
- 日志写 ``plugins/keymouse/daemon.log``（面板「查看日志」直接读它），
  不写共享的 run_log.log，避免多进程并发追加互相截断。

启动方式：由 GUI / CLI / 开机自启以 ``pythonw core/plugins/keymouse/daemon.py``
拉起（无控制台窗口）。
"""
import os
import subprocess
import sys
import time

# 直接以脚本运行（不是 -m）时，sys.path[0] 是 keymouse/，需要自举到仓库根
_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from core import app_paths, autostart  # noqa: E402
from core import modules  # noqa: E402
from core import process_utils as pu  # noqa: E402
from core.config import Config, IS_WIN  # noqa: E402
from core.plugins import settings as plugin_settings  # noqa: E402
from core.plugins.keymouse import collector as collector_mod  # noqa: E402
from core.plugins.keymouse import permission_state, supported  # noqa: E402
from core.plugins.keymouse import store  # noqa: E402

STATE_DIR_NAME = os.path.join("plugins", "keymouse")
LOCK_NAME = "daemon.lock"
HEARTBEAT_NAME = "daemon.heartbeat"
STOP_NAME = "daemon.stop"
PAUSE_NAME = "paused"
LOG_NAME = "daemon.log"

FLUSH_INTERVAL = 2.0      # 秒：内存聚合批量落库间隔
FLUSH_MAX_EVENTS = 600    # 攒够这么多事件立即落库（不用等间隔）
HEARTBEAT_INTERVAL = 2.0
PAUSE_POLL = 1.0          # 暂停标记轮询间隔（秒）
CONFIG_POLL = 60.0        # 插件是否仍启用的检查间隔（秒）
HOOK_CHECK_INTERVAL = 5.0  # 钩子健康看护的检查间隔（秒，重装周期见 collector）
CLEANUP_INTERVAL = 24 * 3600
STALE_SECONDS = 20.0      # 心跳超过该时长视为进程失联
LOG_MAX_BYTES = 512 * 1024


# --------------------------------------------------------------------------- #
# 路径 / 日志
# --------------------------------------------------------------------------- #
def _state_path(name: str) -> str:
    path = os.path.join(app_paths.data_dir(), STATE_DIR_NAME, name)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    return path


def lock_path() -> str:
    return _state_path(LOCK_NAME)


def heartbeat_path() -> str:
    return _state_path(HEARTBEAT_NAME)


def stop_path() -> str:
    return _state_path(STOP_NAME)


def pause_path() -> str:
    return _state_path(PAUSE_NAME)


def log_path() -> str:
    return _state_path(LOG_NAME)


def _log(text: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}"
    path = log_path()
    try:
        if os.path.getsize(path) > LOG_MAX_BYTES:
            try:
                os.replace(path, path + ".1")
            except OSError:
                pass
    except OSError:
        pass
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def tail_log(lines: int = 120) -> str:
    """读取守护进程日志尾部（面板「查看日志」用）。"""
    try:
        with open(log_path(), "r", encoding="utf-8", errors="replace") as f:
            data = f.read().splitlines()
    except OSError:
        return ""
    return "\n".join(data[-max(1, int(lines)):])


# --------------------------------------------------------------------------- #
# 单实例 / 心跳
# --------------------------------------------------------------------------- #
def _read_int(path: str) -> int | None:
    try:
        with open(path, "r", encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def lock_pid() -> int | None:
    """锁文件记录的 PID（缺失 / 损坏返回 None）。"""
    return _read_int(lock_path())


def _heartbeat_age() -> float | None:
    """心跳距今秒数；文件不存在返回 None。"""
    ts = _read_int(heartbeat_path())
    if not ts:
        return None
    return max(0.0, time.time() - ts)


def is_running() -> bool:
    """是否有存活的守护进程（PID 存活 + 心跳新鲜；心跳缺失视为刚启动）。"""
    pid = lock_pid()
    if not pid:
        return False
    try:
        if not pu.is_pid_alive_fast(pid):
            return False
    except Exception:  # noqa: BLE001
        return False
    age = _heartbeat_age()
    return age is None or age <= STALE_SECONDS


def _write_pid(path: str) -> None:
    try:
        with open(path, "w", encoding="ascii") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def _acquire() -> bool:
    """尝试接管单实例锁；已有存活实例返回 False。"""
    if is_running():
        return False
    _write_pid(lock_path())
    return True


def _release() -> None:
    if lock_pid() != os.getpid():
        return
    for path in (lock_path(), heartbeat_path(), stop_path()):
        try:
            os.remove(path)
        except OSError:
            pass


def _touch_heartbeat() -> None:
    """刷新心跳：写入**当前时间戳**（不是 PID —— 用 PID 会被当成远古时间）。"""
    try:
        with open(heartbeat_path(), "w", encoding="ascii") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 启动 / 停止（供 GUI / CLI / 框架调用）
# --------------------------------------------------------------------------- #
def spawn() -> tuple[bool, str]:
    """幂等拉起守护进程（无窗口）。返回 (是否在跑, 提示文本)。"""
    if not supported():
        return False, "当前平台不支持键盘采集（仅 Windows / macOS）"
    if is_running():
        return True, "键鼠记录已在后台运行"
    perm = permission_state()
    if perm.get("required") and not perm.get("granted"):
        return False, ("需要先在「系统设置 → 隐私与安全性 → 输入监控」中"
                       "授权本程序，再启动后台记录")
    try:
        os.remove(stop_path())
    except OSError:
        pass
    script = os.path.abspath(__file__)
    args = autostart.hidden_python_args(script)
    kwargs = {"cwd": _APP_DIR, "close_fds": True}
    if IS_WIN:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen(args, **kwargs)
    except OSError as e:
        return False, f"启动后台记录失败：{e}"
    # 等到心跳出现，才能确认钩子真的装上了（安装失败会立即退出）
    for _ in range(20):
        time.sleep(0.15)
        if is_running() and _heartbeat_age() is not None:
            return True, "键鼠记录已在后台启动"
    if is_running():
        return True, "键鼠记录已在后台启动"
    return False, f"后台记录启动失败，请查看日志：{log_path()}"


def stop() -> tuple[bool, str]:
    """请求守护进程优雅退出（flush 后自行清理），超时则强杀。"""
    pid = lock_pid()
    if not pid:
        _cleanup_state()
        return True, "键鼠记录未在运行"
    if not is_running():
        # 锁残留但心跳已过期：进程可能还活着（钩子仍在装），能杀就杀干净
        try:
            if pu.is_pid_alive_fast(pid):
                pu.kill_pid(pid)
                time.sleep(0.3)
        except Exception:  # noqa: BLE001
            pass
        _cleanup_state()
        return True, "已清理残留的键鼠记录进程"
    try:
        with open(stop_path(), "w", encoding="ascii") as f:
            f.write("stop")
    except OSError as e:
        return False, f"无法写入停止标记：{e}"
    for _ in range(25):  # 最多等 5 秒（守护进程每 1 秒检查一次）
        time.sleep(0.2)
        if not is_running():
            _cleanup_state()
            return True, "已停止键鼠记录"
    try:
        pu.kill_pid(pid)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.3)
    if not is_running():
        _cleanup_state()
        return True, "已停止键鼠记录"
    return False, f"停止失败，请手动结束后台进程（PID {pid}）"


def _cleanup_state() -> None:
    """清掉可能残留的锁 / 心跳 / 停止标记（进程崩溃后的自愈）。"""
    for path in (lock_path(), heartbeat_path(), stop_path()):
        try:
            os.remove(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 主循环
# --------------------------------------------------------------------------- #
def _flush(writer: store.Writer, key_buf: dict, min_buf: dict) -> int:
    if not key_buf and not min_buf:
        return 0
    minutes = {ts: (slot[0], slot[1]) for ts, slot in min_buf.items()}
    written = writer.flush(dict(key_buf), minutes)
    key_buf.clear()
    min_buf.clear()
    return written


def main() -> int:
    if not supported():
        _log("当前平台不支持键盘采集（仅 Windows / macOS），退出")
        return 2
    perm = permission_state()
    if perm.get("required") and not perm.get("granted"):
        _log("未获「输入监控」授权，无法记录按键：请在「系统设置 → 隐私与安全性"
             " → 输入监控」中授权本程序后重试")
        return 6
    if not _acquire():
        _log("已有键鼠记录实例在运行，本进程退出")
        return 0

    _log(f"启动（PID {os.getpid()}，数据目录 {app_paths.data_dir()}）")
    config = Config()
    retention = int(plugin_settings.get(
        config, "keymouse", "retention_days", 730) or 0)

    collector = collector_mod.KeyboardCollector()
    if not collector.install():
        _log(f"安装键盘采集失败：{collector.error}")
        _release()
        return 3

    try:
        writer = store.Writer()
    except Exception as e:  # noqa: BLE001
        _log(f"打开统计数据库失败：{type(e).__name__}: {e}")
        collector.uninstall()
        _release()
        return 4

    key_buf: dict[tuple[str, int], int] = {}
    min_buf: dict[int, list[int]] = {}   # ts_minute -> [presses, peak_kpm]
    window: list[float] = []             # 最近 60 秒的按键时间戳（算瞬时速度）
    last_flush = time.monotonic()
    last_beat = 0.0
    last_pause_poll = 0.0
    last_config = time.monotonic()
    last_cleanup = time.monotonic() - CLEANUP_INTERVAL  # 启动即清理一次
    last_hook_check = time.monotonic()
    hook_fail_logged = False

    _touch_heartbeat()
    _log("键盘采集已安装，开始记录")
    try:
        while True:
            if time.monotonic() - last_pause_poll >= PAUSE_POLL:
                last_pause_poll = time.monotonic()
                collector.paused = os.path.exists(pause_path())

            collector.pump(idle_sleep=0.05)
            now = time.time()
            events = collector.take(FLUSH_MAX_EVENTS)
            for ts, vk in events:
                day = time.strftime("%Y-%m-%d", time.localtime(ts))
                key_buf[(day, vk)] = key_buf.get((day, vk), 0) + 1
                minute = int(ts) // 60
                slot = min_buf.setdefault(minute, [0, 0])
                slot[0] += 1
                window.append(ts)

            if window:
                cutoff = now - 60.0
                if window[0] < cutoff:
                    window[:] = [t for t in window if t >= cutoff]
            if events:
                slot = min_buf.setdefault(int(now) // 60, [0, 0])
                slot[1] = max(slot[1], len(window))

            mono = time.monotonic()
            if key_buf or min_buf:
                if (mono - last_flush >= FLUSH_INTERVAL
                        or len(events) >= FLUSH_MAX_EVENTS):
                    written = _flush(writer, key_buf, min_buf)
                    last_flush = mono
                    if written:
                        _log(f"落库 {written} 行")
            if mono - last_beat >= HEARTBEAT_INTERVAL:
                _touch_heartbeat()
                last_beat = mono

            # 低层钩子可能被系统静默移除（回调超时 / 安全软件）：定期重装兜底，
            # 失败只在状态翻转时记一条日志，避免每 5 秒刷屏。
            if mono - last_hook_check >= HOOK_CHECK_INTERVAL:
                last_hook_check = mono
                if collector.ensure_hook():
                    hook_fail_logged = False
                elif not hook_fail_logged:
                    _log(f"重装键盘钩子失败：{collector.error}")
                    hook_fail_logged = True

            if os.path.exists(stop_path()):
                _log("收到停止请求，准备退出")
                break

            if mono - last_config >= CONFIG_POLL:
                last_config = mono
                try:
                    cfg = Config()
                    if not modules.is_enabled("keymouse", cfg):
                        _log("插件已被停用，退出记录")
                        break
                    retention = int(plugin_settings.get(
                        cfg, "keymouse", "retention_days", retention) or 0)
                except Exception as e:  # noqa: BLE001 - 配置读失败不致命
                    _log(f"读取配置失败（继续记录）：{type(e).__name__}: {e}")

            if mono - last_cleanup >= CLEANUP_INTERVAL:
                last_cleanup = mono
                try:
                    removed = writer.cleanup(retention)
                    if removed:
                        _log(f"清理超期数据 {removed} 行（保留 {retention} 天）")
                except Exception as e:  # noqa: BLE001
                    _log(f"清理超期数据失败：{type(e).__name__}: {e}")
    except KeyboardInterrupt:
        _log("收到中断信号，退出")
    except Exception as e:  # noqa: BLE001 - 任何异常都要先把数据落盘
        _log(f"主循环异常退出：{type(e).__name__}: {e}")
        return 5
    finally:
        try:
            _flush(writer, key_buf, min_buf)
        except Exception:  # noqa: BLE001
            pass
        collector.uninstall()
        writer.close()
        _log("已退出")
        _release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
