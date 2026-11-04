# -*- coding: utf-8 -*-
"""macOS 键盘采集端（CGEventTap，只统计键码与时间点）。

与 Windows 端（:mod:`.collector`）同一套公开接口，守护进程与面板无需分支：
``install / uninstall / reinstall / ensure_hook / pump / take / pending``，
外加 ``events``（deque）、``paused``、``error``、``last_event_ts``、``reinstall_count``。

与 Windows 对齐的三条铁律：

1. **回调内零 I/O**：只做「去重 + ``deque.append``」，落库由守护进程写入线程批量完成；
2. **过滤长按自动重复**：``kCGKeyboardEventAutorepeat`` 与「抬起 → 按下」跳变双重判定；
3. **绝不吞键**：tap 是 ``ListenOnly``，回调原样返回事件。

平台差异：

- 事件源是 ``CFRunLoop``（不是消息泵）：tap 与 run loop 必须同线程，故采集跑在独立
  守护线程里，``pump()`` 退化为休眠；主循环仍按原节奏做 flush / 心跳 / 暂停。
- 需要系统授权：未授权时 ``CGEventTapCreate`` 返回 NULL，``error`` 给出可读原因，
  ``permission()`` 供面板 / CLI 显示状态并直达「系统设置 → 隐私与安全性」。
- macOS 虚拟键码与 Windows ``VK_*`` 完全不同，入队前统一换算成 Windows 键码，
  这样 ``keymap`` / 聚合表 / CSV / 既有测试全部不用改，跨平台键名也一致。
"""
import ctypes
import subprocess
import sys
import threading
import time
from collections import deque

IS_MAC = sys.platform == "darwin"

# CoreGraphics / CoreFoundation 常量（CGEventTypes.h / CGEventSource.h）
CG_EVENT_TAP_HID = 0                  # kCGHIDEventTap：最靠近硬件，能拿到所有按键
CG_HEAD_INSERT = 0                    # kCGHeadInsertEventTap
CG_TAP_LISTEN_ONLY = 1                # 只监听，不改写事件
CG_EVENT_KEY_DOWN = 10
CG_EVENT_KEY_UP = 11
CG_EVENT_FLAGS_CHANGED = 12
CG_EVENT_TAP_DISABLED_BY_TIMEOUT = 0xFFFFFFFE
CG_EVENT_TAP_DISABLED_BY_USER_INPUT = 0xFFFFFFFF
CG_FIELD_KEYCODE = 9                  # kCGKeyboardEventKeycode
CG_FIELD_AUTOREPEAT = 8               # kCGKeyboardEventAutorepeat
CG_FIELD_SOURCE_STATE_ID = 45         # kCGEventSourceStateID
CG_SOURCE_HID = 1                     # kCGEventSourceStateHIDSystemState

EVENT_MASK = ((1 << CG_EVENT_KEY_DOWN) | (1 << CG_EVENT_KEY_UP)
              | (1 << CG_EVENT_FLAGS_CHANGED))

TAP_ENABLE_INTERVAL = 5.0             # 秒：确保 tap 处于启用状态的检查间隔

# 监听键盘（CGEventTap listen-only）要的是「输入监控」，不是「辅助功能」：
# 辅助功能只决定能不能改写事件；只有输入监控放行后系统才会把按键投递给 tap，
# 未授权时 tap 照样能创建、CGEventTapIsEnabled 也可能为真，但一个事件都收不到。
PERMISSION_URL = ("x-apple.systempreferences:com.apple.preference.security"
                  "?Privacy_ListenEvent")

# macOS 虚拟键码（Carbon HIToolbox Events.h 的 kVK_*）→ Windows 虚拟键码。
# 换算后与 Windows 端写进统计库的值同义，键名表、聚合、导出完全共用。
MAC_KEYCODE_TO_VK: dict[int, int] = {
    0: 0x41, 1: 0x53, 2: 0x44, 3: 0x46, 4: 0x48, 5: 0x47, 6: 0x5A, 7: 0x58,
    8: 0x43, 9: 0x56, 10: 0xC0, 11: 0x42, 12: 0x51, 13: 0x57, 14: 0x45,
    15: 0x52, 16: 0x59, 17: 0x54, 18: 0x31, 19: 0x32, 20: 0x33, 21: 0x34,
    22: 0x36, 23: 0x35, 24: 0xBB, 25: 0x39, 26: 0x37, 27: 0xBD, 28: 0x38,
    29: 0x30, 30: 0xDD, 31: 0x4F, 32: 0x55, 33: 0xDB, 34: 0x49, 35: 0x50,
    36: 0x0D, 37: 0x4C, 38: 0x4A, 39: 0xDE, 40: 0x4B, 41: 0xBA, 42: 0xDC,
    43: 0xBC, 44: 0xBF, 45: 0x4E, 46: 0x4D, 47: 0xBE, 48: 0x09, 49: 0x20,
    50: 0xC0, 51: 0x08, 52: 0x0D, 53: 0x1B,
    # 修饰键（走 flagsChanged，不产生 keyDown）
    54: 0x5C, 55: 0x5B, 56: 0xA0, 57: 0x14, 58: 0xA4, 59: 0xA2,
    60: 0xA1, 61: 0xA5, 62: 0xA3, 63: 0x5D,
    # 小键盘
    65: 0x6E, 67: 0x6A, 69: 0x6B, 71: 0x90, 75: 0x6F, 76: 0x0D, 78: 0x6D,
    81: 0xBB, 82: 0x60, 83: 0x61, 84: 0x62, 85: 0x63, 86: 0x64, 87: 0x65,
    88: 0x66, 89: 0x67, 91: 0x68, 92: 0x69,
    # 功能键与编辑键
    64: 0x80, 79: 0x81, 80: 0x82,
    96: 0x74, 97: 0x75, 98: 0x76, 99: 0x72, 100: 0x77, 101: 0x78,
    103: 0x7A, 105: 0x7C, 106: 0x7F, 107: 0x7D, 109: 0x79, 111: 0x7B,
    113: 0x7E, 114: 0x2D, 115: 0x24, 116: 0x21, 117: 0x2E, 118: 0x73,
    119: 0x23, 120: 0x71, 121: 0x22, 122: 0x70, 123: 0x25, 124: 0x27,
    125: 0x28, 126: 0x26,
}

_CALLBACK = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                             ctypes.c_void_p, ctypes.c_void_p)

_libs_cache: tuple | None = None


# --------------------------------------------------------------------------- #
# 框架加载 / 授权
# --------------------------------------------------------------------------- #
def _libs():
    """惰性加载三个框架；非 macOS 上抛 OSError，调用方按不支持处理。"""
    global _libs_cache
    if _libs_cache is not None:
        return _libs_cache
    cf = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cg = ctypes.CDLL(
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    hi = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/Frameworks"
        "/HIServices.framework/HIServices")

    cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.c_void_p]
    cf.CFRunLoopAddSource.restype = None
    cf.CFRunLoopRun.restype = None
    cf.CFRunLoopStop.argtypes = [ctypes.c_void_p]
    cf.CFRunLoopStop.restype = None
    cf.CFRunLoopWakeUp.argtypes = [ctypes.c_void_p]
    cf.CFRunLoopWakeUp.restype = None
    cf.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
    cf.CFMachPortCreateRunLoopSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                 ctypes.c_long]
    cf.CFMachPortInvalidate.argtypes = [ctypes.c_void_p]
    cf.CFMachPortInvalidate.restype = None
    cf.CFDictionaryCreate.restype = ctypes.c_void_p
    cf.CFDictionaryCreate.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_long, ctypes.c_void_p,
        ctypes.c_void_p]

    cg.CGEventTapCreate.restype = ctypes.c_void_p
    cg.CGEventTapCreate.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64,
        ctypes.c_void_p, ctypes.c_void_p]
    cg.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    cg.CGEventTapEnable.restype = None
    cg.CGEventGetIntegerValueField.restype = ctypes.c_int64
    cg.CGEventGetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

    cg.CGEventTapIsEnabled.restype = ctypes.c_bool
    cg.CGEventTapIsEnabled.argtypes = [ctypes.c_void_p]

    # 输入监控授权（10.15+）。旧系统没有这对符号，回落到辅助功能判断。
    for name in ("CGPreflightListenEventAccess", "CGRequestListenEventAccess"):
        fn = getattr(cg, name, None)
        if fn is not None:
            fn.restype = ctypes.c_bool
            fn.argtypes = []

    hi.AXIsProcessTrusted.restype = ctypes.c_bool
    hi.AXIsProcessTrusted.argtypes = []
    hi.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
    hi.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]

    _libs_cache = (cf, cg, hi)
    return _libs_cache


def is_trusted() -> bool:
    """是否已获「输入监控」授权（未授权时 tap 收不到任何按键）。"""
    if not IS_MAC:
        return False
    try:
        _, cg, hi = _libs()
        preflight = getattr(cg, "CGPreflightListenEventAccess", None)
        if preflight is not None:
            return bool(preflight())
        return bool(hi.AXIsProcessTrusted())   # 10.15 之前没有输入监控概念
    except Exception:  # noqa: BLE001
        return False


def request_trust() -> bool:
    """弹一次「输入监控」授权对话框（已授权直接返回 True）。

    系统把「解释器 / 宿主程序」加进列表；勾选后需重启采集进程才生效。
    旧系统没有该 API，退回辅助功能提示。
    """
    if not IS_MAC:
        return False
    try:
        cf, cg, hi = _libs()
        request = getattr(cg, "CGRequestListenEventAccess", None)
        if request is not None:
            return bool(request())
        key = ctypes.c_void_p.in_dll(hi, "kAXTrustedCheckOptionPrompt")
        value = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
        keys = (ctypes.c_void_p * 1)(key.value)
        values = (ctypes.c_void_p * 1)(value.value)
        options = cf.CFDictionaryCreate(None, keys, values, 1, None, None)
        return bool(hi.AXIsProcessTrustedWithOptions(ctypes.c_void_p(options)))
    except Exception:  # noqa: BLE001
        return False


def open_permission_settings() -> bool:
    """打开「系统设置 → 隐私与安全性 → 输入监控」面板。"""
    if not IS_MAC:
        return False
    try:
        subprocess.Popen(["open", PERMISSION_URL],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def permission() -> dict:
    """授权状态（面板 / CLI 共用）：是否必需、是否已给、设置页地址。"""
    required = IS_MAC
    return {
        "required": required,
        "granted": is_trusted() if required else True,
        "settings_url": PERMISSION_URL if required else "",
    }


def vk_of(keycode: int) -> int:
    """macOS 键码 → Windows 虚拟键码；未收录的键原样返回（键名回落 ``0x3C``）。"""
    try:
        code = int(keycode)
    except (TypeError, ValueError):
        return -1
    return MAC_KEYCODE_TO_VK.get(code, code)


# --------------------------------------------------------------------------- #
# 采集端
# --------------------------------------------------------------------------- #
class KeyboardCollector:
    """CGEventTap 键盘采集（tap 与 run loop 在同一个守护线程里）。"""

    def __init__(self, filter_injected: bool = True):
        self.events: deque = deque()      # (ts: float, vk: int)，仅「新按下」
        self.filter_injected = filter_injected
        self.paused = False
        self.error = ""
        self.trusted = False
        self.reinstall_count = 0
        self.last_event_ts = 0.0
        self._down: set[int] = set()
        self._tap = None
        self._source = None
        self._runloop = None
        self._thread: threading.Thread | None = None
        self._callback = None             # 必须持有回调引用，否则会被 GC 掉
        self._ready = threading.Event()
        self._installed_at = 0.0
        self._enabled_checked = 0.0

    # ------------------------------------------------------------------ #
    # 安装 / 卸载
    # ------------------------------------------------------------------ #
    def install(self) -> bool:
        """起采集线程并建 tap；失败时 ``error`` 给出原因。"""
        if not IS_MAC:
            self.error = "当前平台不支持键盘采集（仅 Windows / macOS）"
            return False
        if self._tap:
            return True
        if not is_trusted():
            self.error = ("需要在「系统设置 → 隐私与安全性 → 输入监控」中"
                          "授权本程序后才能记录按键")
            return False
        self.error = ""
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run_tap, name="keymouse-tap", daemon=True)
        self._thread.start()
        # 等线程把 tap 建好（含失败），避免调用方看到「线程在跑但没装上」的假象
        self._ready.wait(timeout=3.0)
        if self._tap is None:
            return False
        if not self.enabled():
            # tap 建得出来但被系统挂起：几乎都是「输入监控」没放行
            self.error = ("键盘监听被系统挂起：请在「系统设置 → 隐私与安全性"
                          "→ 输入监控」中授权后重启后台记录")
            self.uninstall()
            return False
        return True

    def enabled(self) -> bool:
        """tap 当前是否处于启用状态（被系统禁用时返回 False）。"""
        if self._tap is None:
            return False
        try:
            _, cg, _ = _libs()
            return bool(cg.CGEventTapIsEnabled(self._tap))
        except Exception:  # noqa: BLE001
            return False

    def _run_tap(self) -> None:  # pragma: no cover - 需要真实系统事件
        try:
            cf, cg, _ = _libs()
            self._callback = _CALLBACK(self._on_event)
            tap = cg.CGEventTapCreate(
                CG_EVENT_TAP_HID, CG_HEAD_INSERT, CG_TAP_LISTEN_ONLY,
                EVENT_MASK, self._callback, None)
            if not tap:
                self.error = ("创建键盘事件监听失败：需要在「系统设置 → 隐私与安全性"
                              "→ 输入监控」中授权")
                return
            source = cf.CFMachPortCreateRunLoopSource(None, tap, 0)
            runloop = cf.CFRunLoopGetCurrent()
            mode = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
            cf.CFRunLoopAddSource(ctypes.c_void_p(runloop),
                                  ctypes.c_void_p(source),
                                  ctypes.c_void_p(mode.value))
            self._tap = ctypes.c_void_p(tap)
            self._source = ctypes.c_void_p(source)
            self._runloop = ctypes.c_void_p(runloop)
            cg.CGEventTapEnable(self._tap, True)
            self._installed_at = time.time()
            self.trusted = True
            cf.CFRunLoopRun()
        except Exception as e:  # noqa: BLE001 - 线程内异常只能记在 error 上
            self.error = f"{type(e).__name__}: {e}"
        finally:
            self._ready.set()

    def uninstall(self) -> None:
        if self._runloop is not None:
            try:
                cf, cg, _ = _libs()
                if self._tap is not None:
                    cg.CGEventTapEnable(self._tap, False)
                cf.CFRunLoopStop(self._runloop)
                cf.CFRunLoopWakeUp(self._runloop)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._tap is not None:
            try:
                cf, _, _ = _libs()
                cf.CFMachPortInvalidate(self._tap)
            except Exception:  # noqa: BLE001
                pass
        self._tap = None
        self._source = None
        self._runloop = None
        self._thread = None
        self._callback = None
        self._down.clear()

    def reinstall(self) -> bool:
        self.uninstall()
        self.error = ""
        ok = self.install()
        if ok:
            self.reinstall_count += 1
        return ok

    def ensure_hook(self, now: float | None = None) -> bool:
        """采集线程活着就顺手重新启用 tap；线程没了才整体重装。

        macOS 在事件积压 / 输入源切换时会把 tap 置为 disabled（回调收到
        ``kCGEventTapDisabledByTimeout`` 已立即重新启用，这里再兜一层）。
        """
        if self._tap is None or self._thread is None or not self._thread.is_alive():
            return self.reinstall()
        now = time.time() if now is None else now
        if now - self._enabled_checked < TAP_ENABLE_INTERVAL:
            return True
        self._enabled_checked = now
        try:
            _, cg, _ = _libs()
            cg.CGEventTapEnable(self._tap, True)
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            return False
        return True

    # ------------------------------------------------------------------ #
    # 事件处理（回调只做字段提取，判定逻辑收在 handle/press 里，便于测试）
    # ------------------------------------------------------------------ #
    def _on_event(self, _proxy, etype, event, _userinfo):  # pragma: no cover
        try:
            if etype in (CG_EVENT_TAP_DISABLED_BY_TIMEOUT,
                         CG_EVENT_TAP_DISABLED_BY_USER_INPUT):
                if self._tap is not None:
                    _, cg, _ = _libs()
                    cg.CGEventTapEnable(self._tap, True)
                return event
            if event:
                _, cg, _ = _libs()
                keycode = cg.CGEventGetIntegerValueField(event, CG_FIELD_KEYCODE)
                repeat = bool(cg.CGEventGetIntegerValueField(
                    event, CG_FIELD_AUTOREPEAT))
                state = cg.CGEventGetIntegerValueField(
                    event, CG_FIELD_SOURCE_STATE_ID)
                self.handle(etype, keycode, repeat=repeat, source_state=state)
        except Exception:  # noqa: BLE001 - 回调里绝不抛错
            pass
        return event

    def handle(self, etype: int, keycode: int, repeat: bool = False,
               source_state: int | None = CG_SOURCE_HID) -> None:
        """按事件类型更新按键状态（纯逻辑，测试可脱离系统事件调用）。"""
        vk = vk_of(keycode)
        if vk < 0:
            return
        if etype == CG_EVENT_KEY_UP:
            self._down.discard(vk)
            return
        if etype == CG_EVENT_FLAGS_CHANGED:   # 修饰键：同一键码交替表示按下 / 抬起
            if vk in self._down:
                self._down.discard(vk)
            else:
                self.press(vk, source_state=source_state)
            return
        self.press(vk, repeat=repeat, source_state=source_state)

    def press(self, vk: int, repeat: bool = False,
              source_state: int | None = CG_SOURCE_HID) -> bool:
        """记录一次「新按下」；返回是否真的计入统计。"""
        if repeat or vk in self._down:        # 过滤长按自动重复
            return False
        self._down.add(vk)
        if self.paused:
            return False
        if self.filter_injected and self._injected(source_state):
            return False
        ts = time.time()
        self.events.append((ts, vk))
        self.last_event_ts = ts
        return True

    @staticmethod
    def _injected(source_state: int | None) -> bool:
        """来源是私有事件源（其它进程 CGEventPost 合成）时视为注入。"""
        return source_state is not None and source_state < 0

    # ------------------------------------------------------------------ #
    # 取事件（接口与 Windows 端一致；事件由采集线程入队，这里只休眠）
    # ------------------------------------------------------------------ #
    def pump(self, idle_sleep: float = 0.05) -> int:
        time.sleep(idle_sleep)
        return 0

    def take(self, limit: int = 2000) -> list[tuple[float, int]]:
        out: list[tuple[float, int]] = []
        while self.events and len(out) < limit:
            try:
                out.append(self.events.popleft())
            except IndexError:  # pragma: no cover - 并发兜底
                break
        return out

    def pending(self) -> int:
        return len(self.events)
