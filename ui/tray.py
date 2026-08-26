# -*- coding: utf-8 -*-
"""零依赖系统托盘：仅用 ctypes 调用 Win32 API（Shell_NotifyIcon + 子类化 Tk 窗口 WndProc）。

不引入任何第三方包。把托盘图标挂载到 Tk 根窗口的 HWND 上，由 Tk 自身的消息循环
分派托盘回调消息，无需额外窗口或线程。
"""
import ctypes
import itertools
import os
from ctypes import wintypes

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32

# ---- 常量 ----
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_WARNING = 0x00000002
WM_RBUTTONUP = 0x0205
WM_LBUTTONDBLCLK = 0x0203
WM_COMMAND = 0x0111
GWLP_WNDPROC = -4
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_SHARED = 0x8000
TPM_RETURNCMD = 0x0100
TPM_NONOTIFY = 0x0080
TPM_RIGHTBUTTON = 0x0002

# 菜单标志
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
MF_POPUP = 0x0010
MF_CHECKED = 0x0008
MF_GRAYED = 0x0001
MF_DISABLED = 0x0002

MENU_SHOW = 1001
MENU_EXIT = 1002
MENU_ID_BASE = 2000  # 动态菜单项 ID 从此递增

# 动态菜单项字典结构（由 menu_builder 返回）：
#   {"type": "sep"}
#   {"type": "item", "label": str, "cmd": callable, "enabled": bool, "checked": bool}
#   {"type": "submenu", "label": str, "items": [条目...]}


def _loword(v):
    return v & 0xFFFF


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATA(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.DWORD),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

# 设置关键 API 签名，避免 64 位下指针被截断
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, WNDPROC]
user32.SetWindowLongPtrW.restype = ctypes.c_void_p
user32.CallWindowProcW.argtypes = [
    ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
user32.CallWindowProcW.restype = wintypes.LPARAM
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.TrackPopupMenuEx.argtypes = [
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, ctypes.c_void_p,
]
user32.TrackPopupMenuEx.restype = wintypes.UINT
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyMenu.restype = wintypes.BOOL
user32.LoadImageW.argtypes = [
    wintypes.HANDLE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT
]
user32.LoadImageW.restype = wintypes.HICON
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL


class TrayIcon:
    """在给定 Tk 窗口的 HWND 上挂载系统托盘图标。"""

    def __init__(self, hwnd, tip, on_show=None, on_exit=None, menu_builder=None):
        self.hwnd = hwnd
        self.tip = tip
        self.on_show = on_show
        self.on_exit = on_exit
        self.menu_builder = menu_builder  # 每次右键弹出时调用，返回动态菜单条目
        self.uID = 1
        self._menu = None
        self._old_wndproc = None
        self._nid = NOTIFYICONDATA()
        self._wndproc = WNDPROC(self._wndproc_impl)  # 保持引用，防止 GC
        self._install()

    def _load_icon(self):
        ico_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "phpvm.ico")
        if os.path.exists(ico_file):
            hicon = user32.LoadImageW(0, ico_file, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
            if hicon:
                return hicon
        return user32.LoadImageW(0, wintypes.LPCWSTR(IDI_APPLICATION), IMAGE_ICON, 0, 0, LR_SHARED)

    def _install(self):
        self._old_wndproc = user32.SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, self._wndproc)
        hicon = self._load_icon()
        nid = NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
        nid.hWnd = self.hwnd
        nid.uID = self.uID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = hicon
        nid.szTip = self.tip[:127]
        self._nid = nid
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

    def _wndproc_impl(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            event = _loword(lparam)
            if event == WM_RBUTTONUP:
                self._popup_menu()
            elif event == WM_LBUTTONDBLCLK and self.on_show:
                self.on_show()
            return 0
        if msg == WM_COMMAND:
            cmd = _loword(wparam)
            if cmd == MENU_SHOW and self.on_show:
                self.on_show()
            elif cmd == MENU_EXIT and self.on_exit:
                self.on_exit()
            return 0
        return user32.CallWindowProcW(self._old_wndproc, hwnd, msg, wparam, lparam)

    def show_balloon(self, title: str, message: str) -> None:
        """在托盘图标上弹出气泡通知（NIF_INFO）。

        仅修改 uFlags 增加 NIF_INFO 后 NIM_MODIFY，之后恢复原 flags。
        """
        nid = self._nid
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = message[:255]
        nid.dwInfoFlags = NIIF_WARNING
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        # 恢复常规 flags，避免后续重复展示旧气泡内容
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.szInfo = ""
        nid.szInfoTitle = ""
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def _popup_menu(self):
        """每次右键重建菜单（动态反映运行状态），弹完后统一销毁。

        固定头尾：显示 phpvm / 退出；中间由 menu_builder 动态生成。
        """
        try:
            items = self.menu_builder() if self.menu_builder else []
        except Exception:  # noqa: BLE001
            items = []

        menu = user32.CreatePopupMenu()
        all_menus = [menu]
        cmd_map: dict[int, object] = {}
        id_gen = itertools.count(MENU_ID_BASE)

        def append(hmenu, item):
            kind = item.get("type", "item")
            if kind == "sep":
                user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                return
            if kind == "submenu":
                sub = user32.CreatePopupMenu()
                all_menus.append(sub)
                for child in item.get("items", []):
                    append(sub, child)
                user32.AppendMenuW(hmenu, MF_POPUP | MF_STRING, sub, item.get("label", ""))
                return
            flags = MF_STRING
            if item.get("checked"):
                flags |= MF_CHECKED
            if not item.get("enabled", True):
                flags |= MF_GRAYED | MF_DISABLED
            cmd = next(id_gen)
            cmd_map[cmd] = item.get("cmd")
            user32.AppendMenuW(hmenu, flags, cmd, item.get("label", ""))

        user32.AppendMenuW(menu, MF_STRING, MENU_SHOW, "显示 phpvm")
        if items:
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            for item in items:
                append(menu, item)
            user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, MENU_EXIT, "退出")

        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenuEx(
            menu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
            pt.x, pt.y, self.hwnd, None,
        )
        for m in all_menus:
            user32.DestroyMenu(m)
        if cmd == MENU_SHOW and self.on_show:
            self.on_show()
        elif cmd == MENU_EXIT and self.on_exit:
            self.on_exit()
        else:
            cb = cmd_map.get(cmd)
            if callable(cb):
                cb()

    def remove(self):
        try:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
        except Exception:  # noqa: BLE001
            pass
        if self._old_wndproc:
            try:
                user32.SetWindowLongPtrW(self.hwnd, GWLP_WNDPROC, self._old_wndproc)
            except Exception:  # noqa: BLE001
                pass
        if self._menu:
            try:
                user32.DestroyMenu(self._menu)
            except Exception:  # noqa: BLE001
                pass
            self._menu = None
