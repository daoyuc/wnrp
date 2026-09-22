# -*- coding: utf-8 -*-
"""界面主题：调色板（动态）+ ttk 样式 + 热切换。

用法
----
- 取颜色一律走**角色名**，且通过模块属性访问（不要 from-import 固定值）：::

      from . import theme
      tk.Label(master, background=theme.CARD_BG, foreground=theme.TEXT)

  ``theme.CARD_BG`` 由模块级 ``__getattr__``（PEP 562）按**当前**主题求值，
  因此切换主题后新建的窗口/控件自动用新色；已存在的控件由
  :func:`apply_mode` 递归刷新。

- 切换主题（立即生效，无需重启）::

      theme.apply_mode(root, "dark")     # 或 "light" / "system"

- 无法自动刷新的内容（Canvas 自绘、Treeview 的 tag 配色），由所在控件
  实现 ``refresh_theme(self)`` 方法，切换时会被自动调用。

角色一览见 ``core/theme.py`` 的 ``PALETTES``。
"""
import sys
import tkinter as tk
from tkinter import ttk

from core import theme as prefs

# 与主题无关的字体角色（直接暴露，方便静态检查）
FONT: str = prefs.FONT
MONO_FONT: str = prefs.MONO_FONT

# 字号层级（同一处定义，改这里即可整体放大/缩小）
FS_TITLE = 15     # 页签内主标题
FS_SECTION = 10   # 分组标题 / 表格表头
FS_BODY = 9       # 正文 / 按钮 / 表格行
FS_SMALL = 8      # 辅助说明

# 间距层级（避免各处硬编码 6/10/14 造成视觉节奏不一致）
PAD_XS = 4
PAD_SM = 6
PAD_MD = 10
PAD_LG = 14
PAD_XL = 18

# 当前生效的主题名与调色板（由 set_mode / apply_mode 更新）
_current: str = prefs.LIGHT
_pal: dict[str, str] = {}
_listeners: list = []

# 需要随主题改写的控件选项（不存在该选项的控件会抛 TclError，跳过即可）
_COLOR_OPTS = (
    "background", "foreground", "activebackground", "activeforeground",
    "disabledforeground", "highlightbackground", "highlightcolor",
    "selectcolor", "insertbackground", "readonlybackground", "troughcolor",
)
# 这些选项在 ttk 上是「空字符串 = 未设置」，不做替换
_TEXT_TAGS = ("foreground", "background")


def __getattr__(name: str):
    """按当前主题返回色板角色值（PEP 562 模块级属性钩子）。"""
    key = name.lower()
    if key in _pal:
        return _pal[key]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def mono(size: int = 9, bold: bool = False):
    """等宽字体元组（日志 / 配置预览统一用它，避免硬编码 Consolas/Menlo）。"""
    return (MONO_FONT, size, "bold") if bold else (MONO_FONT, size)


def font(size: int = FS_BODY, bold: bool = False):
    """正文字体元组（界面各处统一走它，便于整体调节字号）。"""
    return (FONT, size, "bold") if bold else (FONT, size)


def divider(master, orient: str = "horizontal"):
    """1px 分隔线（颜色随主题；切换时由颜色映射自动刷新）。"""
    if orient == "horizontal":
        return ttk.Frame(master, style="Divider.TFrame", height=1)
    return ttk.Frame(master, style="Divider.TFrame", width=1)


def accent_bar(master):
    """左侧强调色竖条（卡片/标题区用它做视觉锚点）。"""
    return ttk.Frame(master, style="Accent.TFrame", width=4)


def current_mode() -> str:
    """当前主题名（``light`` / ``dark``）。"""
    return _current


def is_dark() -> bool:
    return _current == prefs.DARK


def palette() -> dict[str, str]:
    """返回当前调色板副本。"""
    return dict(_pal)


def on_change(callback) -> None:
    """注册主题切换回调（签名 ``cb(mode: str)``）。"""
    if callback not in _listeners:
        _listeners.append(callback)


def set_mode(mode=None) -> str:
    """只切换内部调色板（不刷新界面）；返回生效的主题名。"""
    global _current, _pal
    _current = prefs.resolve(mode if mode is not None else _current)
    _pal = prefs.palette(_current)
    return _current


# --------------------------------------------------------------------------- #
# ttk 样式
# --------------------------------------------------------------------------- #
def setup_style(root: tk.Misc) -> ttk.Style:
    """把当前调色板应用到 ``root`` 的 ttk 样式上（可重复调用）。"""
    p = _pal or set_mode()
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", font=font(FS_BODY), background=p["bg"], foreground=p["text"])
    style.configure("TFrame", background=p["bg"])
    style.configure("TLabel", background=p["bg"], foreground=p["text"])

    # 结构类：分隔线 / 强调竖条 / 描边卡片
    style.configure("Divider.TFrame", background=p["border"], borderwidth=0)
    style.configure("Accent.TFrame", background=p["primary"], borderwidth=0)
    style.configure("Card.TFrame", background=p["card_bg"])
    style.configure("Panel.TFrame", background=p["card_bg"], borderwidth=1,
                    relief="solid", bordercolor=p["border"])

    # 标题 / 说明 / 分区 / 状态栏
    style.configure("Title.TLabel", font=font(FS_TITLE, True),
                    foreground=p["text"], background=p["card_bg"])
    style.configure("SubTitle.TLabel", font=font(FS_BODY),
                    foreground=p["text_dim"], background=p["card_bg"])
    style.configure("Section.TLabel", font=font(FS_SECTION, True),
                    foreground=p["text"], background=p["bg"])
    style.configure("Status.TLabel", font=font(FS_BODY),
                    foreground=p["text"], background=p["panel_alt"])
    style.configure("Status.TFrame", background=p["panel_alt"])
    # 状态栏内的可点击徽标（崩溃告警 / 新版本提示）
    style.configure("Alert.Status.TLabel", font=font(FS_BODY, True),
                    foreground=p["err"], background=p["panel_alt"])
    style.configure("Link.Status.TLabel", font=font(FS_BODY, True),
                    foreground=p["primary"], background=p["panel_alt"])
    # 卡片上的强调/弱化文本（标题区、分组内）
    style.configure("Card.TLabel", background=p["card_bg"], foreground=p["text"])
    style.configure("CardBold.TLabel", font=font(FS_BODY, True),
                    background=p["card_bg"], foreground=p["text"])
    style.configure("CardDim.TLabel", font=font(FS_BODY),
                    background=p["card_bg"], foreground=p["text_dim"])
    style.configure("Dot.TLabel", font=font(12), background=p["card_bg"],
                    foreground=p["gray"])
    style.configure("Mono.TLabel", font=mono(FS_BODY), background=p["card_bg"],
                    foreground=p["text"])

    # 按钮：常规按钮带细描边，主/危按钮为实心（层级更清楚）
    style.configure("TButton", font=font(FS_BODY), padding=(10, 5),
                    background=p["btn_bg"], foreground=p["text"],
                    borderwidth=1, bordercolor=p["border_strong"],
                    focuscolor=p["primary_light"])
    style.map("TButton",
              background=[("active", p["btn_active"]), ("pressed", p["btn_pressed"])],
              bordercolor=[("focus", p["primary"])])
    # 密集工具栏（按钮超过 8 个的操作条）用更紧凑的内边距
    style.configure("Toolbar.TButton", padding=(8, 4))

    style.configure("Accent.TButton", font=font(FS_BODY, True), padding=(16, 7),
                    background=p["primary"], foreground="#FFFFFF", borderwidth=0,
                    focuscolor=p["primary_light"])
    style.map("Accent.TButton",
              background=[("active", p["primary_dark"]),
                          ("pressed", p["primary_dark"]),
                          ("disabled", p["gray"])])

    style.configure("Danger.TButton", font=font(FS_BODY, True), padding=(16, 7),
                    background=p["err"], foreground="#FFFFFF", borderwidth=0,
                    focuscolor=p["primary_light"])
    style.map("Danger.TButton",
              background=[("active", p["err"]), ("pressed", p["err"]),
                          ("disabled", p["gray"])])

    # 页签：选中项用强调色描边 + 卡片底色（未选中压暗，减少噪声）
    style.configure("TNotebook", background=p["bg"], borderwidth=1,
                    bordercolor=p["border"], tabmargins=(8, 6, 8, 0))
    style.configure("TNotebook.Tab", font=font(FS_BODY), padding=(18, 8),
                    background=p["tab_bg"], foreground=p["text_dim"],
                    borderwidth=1, bordercolor=p["border"], lightcolor=p["bg"])
    style.map("TNotebook.Tab",
              background=[("selected", p["card_bg"]), ("active", p["hover"])],
              foreground=[("selected", p["primary"])],
              bordercolor=[("selected", p["primary"])],
              lightcolor=[("selected", p["card_bg"])])

    # 表格：表头独立底色 + 底部描边，行高略放宽；斑马纹由各面板用 odd/even tag 实现
    style.configure("Treeview", font=font(FS_BODY), rowheight=30,
                    background=p["card_bg"], fieldbackground=p["card_bg"],
                    foreground=p["text"], borderwidth=0, relief="flat")
    style.configure("Treeview.Heading", font=font(FS_SECTION, True),
                    background=p["header_bg"], foreground=p["text_dim"],
                    padding=(10, 7), borderwidth=0, relief="flat")
    style.map("Treeview",
              background=[("selected", p["select_bg"])],
              foreground=[("selected", p["text"])])
    style.map("Treeview.Heading", background=[("active", p["hover"])])

    # 分帧容器：分组标题用正文色 + 强调色左侧留白
    style.configure("TLabelframe", background=p["bg"], bordercolor=p["border_strong"],
                    relief="solid", borderwidth=1, padding=12)
    style.configure("TLabelframe.Label", font=font(FS_SECTION, True),
                    foreground=p["text"], background=p["bg"])
    # 卡片底色的分组框（关于页：整页为卡片，分组框需与卡片同色）
    style.configure("Card.TLabelframe", background=p["card_bg"],
                    bordercolor=p["border"], relief="solid", borderwidth=1, padding=12)
    style.configure("Card.TLabelframe.Label", font=font(FS_SECTION, True),
                    foreground=p["text"], background=p["card_bg"])

    # 输入类：统一描边 + 聚焦时强调色
    style.configure("TEntry", fieldbackground=p["card_bg"], foreground=p["text"],
                    padding=(8, 5), insertcolor=p["text"],
                    bordercolor=p["border_strong"], lightcolor=p["card_bg"], borderwidth=1)
    style.map("TEntry", bordercolor=[("focus", p["primary"])],
              lightcolor=[("focus", p["card_bg"])])
    style.configure("TCombobox", fieldbackground=p["card_bg"], foreground=p["text"],
                    background=p["card_bg"], arrowcolor=p["text"],
                    bordercolor=p["border_strong"], lightcolor=p["card_bg"],
                    padding=(6, 4))
    style.map("TCombobox", fieldbackground=[("readonly", p["card_bg"])],
              foreground=[("readonly", p["text"])],
              selectbackground=[("readonly", p["select_bg"])],
              selectforeground=[("readonly", p["text"])],
              bordercolor=[("focus", p["primary"])])
    style.configure("TSpinbox", fieldbackground=p["card_bg"], foreground=p["text"],
                    background=p["card_bg"], bordercolor=p["border_strong"],
                    arrowcolor=p["text"], padding=(6, 4))
    style.map("TSpinbox", bordercolor=[("focus", p["primary"])])
    style.configure("Card.TCheckbutton", background=p["card_bg"], foreground=p["text"],
                    indicatorcolor=p["card_bg"], focuscolor=p["card_bg"])
    style.map("Card.TCheckbutton",
              background=[("active", p["card_bg"])],
              indicatorcolor=[("selected", p["primary"])])
    style.configure("TCheckbutton", background=p["bg"], foreground=p["text"],
                    indicatorcolor=p["card_bg"], focuscolor=p["bg"])
    style.map("TCheckbutton",
              background=[("active", p["bg"])],
              indicatorcolor=[("selected", p["primary"])])
    style.configure("TRadiobutton", background=p["bg"], foreground=p["text"],
                    indicatorcolor=p["card_bg"], focuscolor=p["bg"])
    style.map("TRadiobutton",
              background=[("active", p["bg"])],
              indicatorcolor=[("selected", p["primary"])])
    style.configure("TProgressbar", background=p["primary"], troughcolor=p["panel_alt"],
                    bordercolor=p["border"], thickness=8, lightcolor=p["primary"])

    # 滚动条：细条 + 无描边（不与内容抢视觉）
    for orient in ("Vertical", "Horizontal"):
        style.configure(f"{orient}.TScrollbar", background=p["btn_bg"],
                        troughcolor=p["panel_alt"], bordercolor=p["panel_alt"],
                        arrowcolor=p["text_dim"], borderwidth=0,
                        width=10, arrowsize=12)
        style.map(f"{orient}.TScrollbar",
                  background=[("active", p["btn_active"]), ("pressed", p["btn_pressed"])])
    try:
        # 原生下拉列表（Combobox 的 popdown 是原生 Listbox，只能用 option 数据库设置）
        root.option_add("*TCombobox*Listbox.background", p["card_bg"])
        root.option_add("*TCombobox*Listbox.foreground", p["text"])
        root.option_add("*TCombobox*Listbox.selectBackground", p["select_bg"])
        root.option_add("*TCombobox*Listbox.selectForeground", p["text"])
        if sys.platform.startswith("win"):
            # macOS 的菜单是系统原生绘制，设置无效也无副作用
            root.option_add("*Menu.background", p["card_bg"])
            root.option_add("*Menu.foreground", p["text"])
            root.option_add("*Menu.activeBackground", p["select_bg"])
            root.option_add("*Menu.activeForeground", p["text"])
    except tk.TclError:
        pass

    return style


# --------------------------------------------------------------------------- #
# 热切换
# --------------------------------------------------------------------------- #
def _iter_widgets(w: tk.Misc):
    yield w
    try:
        children = list(w.winfo_children())
    except Exception:  # noqa: BLE001
        return
    for child in children:
        yield from _iter_widgets(child)


def _refresh_widget(w: tk.Misc, mapping: dict[str, str]) -> None:
    """把控件上命中「旧主题色」的颜色选项改写成新主题色。"""
    for opt in _COLOR_OPTS:
        try:
            val = w.cget(opt)  # type: ignore[arg-type]
        except (tk.TclError, TypeError, AttributeError):
            continue
        if isinstance(val, str) and val in mapping:
            try:
                w.configure({opt: mapping[val]})  # type: ignore[arg-type]
            except tk.TclError:
                pass
    # Text 的 tag 配色（日志高亮等）无法从控件选项推断，单独处理
    if isinstance(w, tk.Text):
        for tag in w.tag_names():
            if tag == "sel":
                continue
            for opt in _TEXT_TAGS:
                try:
                    val = w.tag_cget(tag, opt)
                except tk.TclError:
                    continue
                if isinstance(val, str) and val in mapping:
                    try:
                        w.tag_configure(tag, **{opt: mapping[val]})
                    except tk.TclError:
                        pass


def apply_mode(root: tk.Misc, mode=None) -> str:
    """切换主题并立即刷新界面（含已打开的 Toplevel）。

    步骤：更新调色板 → 重建 ttk 样式 → 递归改写原生控件颜色 →
    调用各控件的 ``refresh_theme()`` 钩子（Canvas 自绘 / Treeview tag）。
    """
    old = dict(_pal)
    new_mode = set_mode(mode)
    setup_style(root)

    mapping: dict[str, str] = {}
    if old:
        cur = prefs.palette(new_mode)
        for key, old_val in old.items():
            new_val = cur.get(key)
            if new_val and new_val != old_val:
                mapping[old_val] = new_val
    if mapping:
        for w in _iter_widgets(root):
            try:
                _refresh_widget(w, mapping)
            except Exception:  # noqa: BLE001
                continue

    # 自绘内容（Canvas / Treeview tag）由控件自己重绘
    for w in _iter_widgets(root):
        hook = getattr(w, "refresh_theme", None)
        if callable(hook):
            try:
                hook()
            except Exception:  # noqa: BLE001
                pass

    for cb in list(_listeners):
        try:
            cb(new_mode)
        except Exception:  # noqa: BLE001
            pass
    return new_mode


# 导入即按默认（跟随系统）初始化
set_mode(prefs.DEFAULT_MODE)
