# -*- coding: utf-8 -*-
"""Windows 低层键盘钩子（只统计键码与时间点）。

铁律：**钩子回调内零 I/O**。低层钩子（WH_KEYBOARD_LL）在回调里停留过久会被
系统静默卸载（``LowLevelHooksTimeout``，Win10 起默认 300ms 量级），表现就是
「记录一段时间后自己停了」。因此回调只做：

1. 过滤注入事件（``LLKHF_INJECTED``：远控 / 宏 / 自动化会产生大量伪按键）；
2. 「抬起 → 按下」跳变才计数（长按自动重复由系统反复上报，不去重会虚高数倍）；
3. ``deque.append``（CPython 下与另一线程的 ``popleft`` 原子安全）；
4. 立刻 ``CallNextHookEx`` 把事件传下去，绝不吞键。

真正的落库由守护进程的写入线程批量完成（见 ``daemon.py``）。
"""
import ctypes
import sys
import time
from collections import deque

IS_WIN = sys.platform.startswith("win")

WH_KEYBOARD_LL = 13
HC_ACTION = 0
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
LLKHF_INJECTED = 0x10
PM_REMOVE = 0x0001
HOOK_REINSTALL_INTERVAL = 60.0   # 秒：定期重装，兜住被系统静默移除的低层钩子

if IS_WIN:  # pragma: no cover - 非 Windows 上不定义这些结构
    from ctypes import wintypes

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class KeyboardCollector:
    """键盘钩子的安装 / 消息泵 / 事件队列（单线程使用：安装线程负责 pump）。"""

    def __init__(self, filter_injected: bool = True):
        self.events: deque = deque()      # (ts: float, vk: int)，仅「新按下」
        self.filter_injected = filter_injected
        self.paused = False               # 由控制线程设置（只是内存标志）
        self.error = ""
        self._down: set[int] = set()
        self._hook = None
        self._proc = None                 # 必须持有回调引用，否则会被 GC 掉
        self._user32 = None
        self._msg = None
        self._installed_at = 0.0        # 上次成功安装钩子的时间（定期重装计时）
        self.last_event_ts = 0.0        # 最近一次有效按键（重装时避开正在打字）
        self.reinstall_count = 0

    # ------------------------------------------------------------------ #
    # 安装 / 卸载
    # ------------------------------------------------------------------ #
    def install(self) -> bool:
        """安装全局低层键盘钩子；失败时 ``error`` 给出原因。"""
        if not IS_WIN:
            self.error = "当前平台不支持键盘采集（仅 Windows）"
            return False
        if self._hook:
            return True
        try:
            self._user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._user32.SetWindowsHookExW.restype = ctypes.c_void_p
            self._user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int, HOOKPROC, ctypes.c_void_p, ctypes.c_uint]
            self._user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
            self._user32.CallNextHookEx.restype = ctypes.c_ssize_t
            self._user32.CallNextHookEx.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
            self._user32.PeekMessageW.argtypes = [
                ctypes.POINTER(wintypes.MSG), ctypes.c_void_p,
                ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
            self._user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            self._user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            self._proc = HOOKPROC(self._callback)
            self._hook = self._user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._proc, None, 0)
            self._msg = wintypes.MSG()
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            self._hook = None
            return False
        if not self._hook:
            err = ctypes.get_last_error()
            self.error = f"SetWindowsHookEx 失败（错误码 {err}，可能被安全软件拦截）"
            return False
        self._installed_at = time.time()
        return True

    def uninstall(self) -> None:
        if self._hook and self._user32:
            try:
                self._user32.UnhookWindowsHookEx(self._hook)
            except Exception:  # noqa: BLE001
                pass
        self._hook = None
        self._proc = None

    def reinstall(self) -> bool:
        """重新装钩子（系统可能静默卸载超时的低层钩子）。

        卸载后立刻重装，句柄、回调引用与「当前按下的键」集合全部重建；
        失败时 ``error`` 给出原因，由守护进程决定下一轮是否继续尝试。
        """
        self.uninstall()
        self._down.clear()
        self.error = ""
        ok = self.install()
        if ok:
            self.reinstall_count += 1
        return ok

    def ensure_hook(self, now: float | None = None) -> bool:
        """保证钩子有效：句柄丢失或到达重装周期就重装，返回当前是否可用。

        ``now`` 只用于测试注入。正在打字（2 秒内有事件）时推迟重装，
        避免在一次连续输入中间重建句柄。
        """
        if self._hook is None:
            return self.reinstall()
        now = time.time() if now is None else now
        if now - self._installed_at < HOOK_REINSTALL_INTERVAL:
            return True
        if now - self.last_event_ts < 2.0:
            return True
        return self.reinstall()

    # ------------------------------------------------------------------ #
    # 回调（运行在钩子线程 = 安装线程）
    # ------------------------------------------------------------------ #
    def _callback(self, n_code, w_param, l_param):
        try:
            if n_code == HC_ACTION:
                msg = int(w_param)
                if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    kb = ctypes.cast(
                        l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if not (self.filter_injected and (kb.flags & LLKHF_INJECTED)):
                        vk = int(kb.vkCode)
                        if vk not in self._down:   # 过滤长按自动重复
                            self._down.add(vk)
                            if not self.paused:
                                ts = time.time()
                                self.events.append((ts, vk))
                                self.last_event_ts = ts
                elif msg in (WM_KEYUP, WM_SYSKEYUP):
                    kb = ctypes.cast(
                        l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    self._down.discard(int(kb.vkCode))
        except Exception:  # noqa: BLE001 - 回调里绝不抛错
            pass
        return self._user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

    # ------------------------------------------------------------------ #
    # 消息泵 / 取事件
    # ------------------------------------------------------------------ #
    def pump(self, idle_sleep: float = 0.05) -> int:
        """处理已排队的 Windows 消息；无事可做时短暂休眠（避免忙等烧 CPU）。"""
        if not self._user32 or self._msg is None:
            time.sleep(idle_sleep)
            return 0
        count = 0
        msg = self._msg
        while self._user32.PeekMessageW(
                ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            self._user32.TranslateMessage(ctypes.byref(msg))
            self._user32.DispatchMessageW(ctypes.byref(msg))
            count += 1
        if not count:
            time.sleep(idle_sleep)
        return count

    def take(self, limit: int = 2000) -> list[tuple[float, int]]:
        """取出至多 ``limit`` 条事件（钩子线程与写入线程之间唯一的交接点）。"""
        out: list[tuple[float, int]] = []
        while self.events and len(out) < limit:
            try:
                out.append(self.events.popleft())
            except IndexError:  # pragma: no cover - 并发兜底
                break
        return out

    def pending(self) -> int:
        return len(self.events)
