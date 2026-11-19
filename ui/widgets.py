# -*- coding: utf-8 -*-
"""页面壳与共享交互原语（面板共用，避免各页硬拼布局）。

用法
----
- :func:`page_header`：页顶标题 + 说明 + 右侧主操作区
- :func:`toolbar`：主 / 次 /「更多」菜单按钮分层
- :func:`empty_state`：无数据 / 未选中时的居中提示
- :func:`status_pill` / :func:`health_banner`：状态胶囊与健康横幅
- :func:`metric_card`：仪表盘数字卡
- :func:`set_busy`：统一禁用一组按钮

只依赖 ``ui.theme`` 与标准库 tk/ttk；业务逻辑仍留在各面板。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable, Sequence

from core.i18n import t

from . import theme

# status_pill / health_banner 的稳定状态名
PILL_OK = "ok"
PILL_WARN = "warn"
PILL_ERR = "err"
PILL_OFF = "off"

_PILL_STYLES = {
    PILL_OK: "PillOk.TLabel",
    PILL_WARN: "PillWarn.TLabel",
    PILL_ERR: "PillErr.TLabel",
    PILL_OFF: "PillOff.TLabel",
}
_BANNER_FRAME = {
    PILL_OK: "BannerOk.TFrame",
    PILL_WARN: "BannerWarn.TFrame",
    PILL_ERR: "BannerErr.TFrame",
    PILL_OFF: "BannerWarn.TFrame",
}
_BANNER_LABEL = {
    PILL_OK: "BannerOk.TLabel",
    PILL_WARN: "BannerWarn.TLabel",
    PILL_ERR: "BannerErr.TLabel",
    PILL_OFF: "BannerWarn.TLabel",
}


class PageHeader:
    """页顶：左侧标题/副标题，右侧动作按钮容器。"""

    def __init__(self, master, *, title: str, subtitle: str = ""):
        self.frame = ttk.Frame(master)
        self.frame.pack(fill="x", pady=(0, theme.PAD_MD))
        left = ttk.Frame(self.frame)
        left.pack(side="left", fill="x", expand=True)
        self.title_var = tk.StringVar(value=title)
        self.subtitle_var = tk.StringVar(value=subtitle)
        ttk.Label(left, textvariable=self.title_var,
                  style="PageTitle.TLabel").pack(anchor="w")
        self.subtitle_lbl = ttk.Label(left, textvariable=self.subtitle_var,
                                      style="PageSub.TLabel")
        if subtitle:
            self.subtitle_lbl.pack(anchor="w", pady=(2, 0))
        self.actions = ttk.Frame(self.frame)
        self.actions.pack(side="right", padx=(theme.PAD_MD, 0))

    def set_subtitle(self, text: str) -> None:
        self.subtitle_var.set(text or "")
        if text:
            if not self.subtitle_lbl.winfo_ismapped():
                self.subtitle_lbl.pack(anchor="w", pady=(2, 0))
        else:
            self.subtitle_lbl.pack_forget()


class Toolbar:
    """工具栏：primary（Accent/Danger）→ secondary → overflow「更多」。"""

    def __init__(self, master, *, primary: Sequence[dict] | None = None,
                 secondary: Sequence[dict] | None = None,
                 overflow: Sequence[dict] | None = None,
                 overflow_label: str | None = None):
        self.frame = ttk.Frame(master)
        self.frame.pack(fill="x", pady=(0, theme.PAD_SM))
        self.buttons: list[ttk.Button] = []
        self._menu_btn: ttk.Menubutton | None = None
        self.overflow_menu: tk.Menu | None = None

        for spec in primary or ():
            self.buttons.append(self._add_button(spec, primary=True))
        for spec in secondary or ():
            self.buttons.append(self._add_button(spec, primary=False))
        if overflow:
            label = overflow_label if overflow_label is not None else t("更多")
            self._menu_btn = ttk.Menubutton(self.frame, text=label,
                                            style="Toolbar.TButton")
            self.overflow_menu = tk.Menu(self._menu_btn, tearoff=0)
            self._menu_btn["menu"] = self.overflow_menu
            for spec in overflow:
                cmd = spec.get("command")
                state = spec.get("state", "normal")
                self.overflow_menu.add_command(
                    label=spec.get("text", ""),
                    command=cmd if state != "disabled" else None,
                    state=state,
                )
            self._menu_btn.pack(side="left", padx=(0, theme.PAD_XS))

    def _add_button(self, spec: dict, *, primary: bool) -> ttk.Button:
        style = spec.get("style")
        if not style:
            kind = spec.get("kind", "accent" if primary else "normal")
            if kind == "danger":
                style = "Danger.TButton"
            elif kind == "accent" or primary:
                style = "Accent.TButton"
            else:
                style = "Toolbar.TButton"
        btn = ttk.Button(
            self.frame,
            text=spec.get("text", ""),
            command=spec.get("command"),
            style=style,
            state=spec.get("state", "normal"),
        )
        btn.pack(side="left", padx=(0, theme.PAD_XS))
        return btn


class EmptyState:
    """居中空态：短标题 + 说明 + 可选动作。"""

    def __init__(self, master, *, title: str, hint: str = "",
                 action_text: str = "", command: Callable | None = None):
        self.frame = ttk.Frame(master)
        inner = ttk.Frame(self.frame)
        inner.place(relx=0.5, rely=0.45, anchor="center")
        self.title_var = tk.StringVar(value=title)
        self.hint_var = tk.StringVar(value=hint)
        ttk.Label(inner, textvariable=self.title_var,
                  style="EmptyTitle.TLabel").pack()
        self.hint_lbl = ttk.Label(inner, textvariable=self.hint_var,
                                  style="EmptyHint.TLabel",
                                  wraplength=420, justify="center")
        if hint:
            self.hint_lbl.pack(pady=(theme.PAD_XS, 0))
        self._btn: ttk.Button | None = None
        if action_text and command:
            self._btn = ttk.Button(inner, text=action_text, command=command,
                                   style="Accent.TButton")
            self._btn.pack(pady=(theme.PAD_MD, 0))

    def show(self) -> None:
        self.frame.pack(fill="both", expand=True)

    def hide(self) -> None:
        self.frame.pack_forget()

    def set_text(self, title: str, hint: str = "") -> None:
        self.title_var.set(title)
        self.hint_var.set(hint or "")
        if hint:
            if not self.hint_lbl.winfo_ismapped():
                self.hint_lbl.pack(pady=(theme.PAD_XS, 0))
        else:
            self.hint_lbl.pack_forget()


class StatusPill(ttk.Label):
    """小状态胶囊（ok / warn / err / off）。"""

    def __init__(self, master, *, state: str = PILL_OFF, text: str = "", **kw):
        style = _PILL_STYLES.get(state, _PILL_STYLES[PILL_OFF])
        super().__init__(master, text=text, style=style, **kw)
        self._state = state

    def set_state(self, state: str, text: str | None = None) -> None:
        self._state = state if state in _PILL_STYLES else PILL_OFF
        self.configure(style=_PILL_STYLES[self._state])
        if text is not None:
            self.configure(text=text)


class HealthBanner:
    """一行健康结论横幅（底色随 ok/warn/err 变）。"""

    def __init__(self, master):
        self.frame = ttk.Frame(master, style="BannerOk.TFrame",
                               padding=(theme.PAD_MD, theme.PAD_SM))
        self.frame.pack(fill="x", pady=(0, theme.PAD_MD))
        self._var = tk.StringVar(value="")
        self._pill = StatusPill(self.frame, state=PILL_OK, text="")
        self._pill.pack(side="left", padx=(0, theme.PAD_SM))
        self._label = ttk.Label(self.frame, textvariable=self._var,
                                style="BannerOk.TLabel")
        self._label.pack(side="left", fill="x", expand=True)

    def set(self, state: str, message: str, pill_text: str = "") -> None:
        st = state if state in _BANNER_FRAME else PILL_WARN
        self.frame.configure(style=_BANNER_FRAME[st])
        self._label.configure(style=_BANNER_LABEL[st])
        self._var.set(message)
        self._pill.set_state(st, pill_text or _default_pill_text(st))


def _default_pill_text(state: str) -> str:
    if state == PILL_OK:
        return t("健康")
    if state == PILL_ERR:
        return t("异常")
    if state == PILL_WARN:
        return t("告警")
    return t("未知")


class MetricCard:
    """仪表盘数字/状态卡（标签 + 大值 + 可选说明）。"""

    def __init__(self, master, *, label: str, value: str = "—",
                 hint: str = "", on_click: Callable | None = None):
        self.frame = ttk.Frame(master, style="MetricCard.TFrame",
                               padding=theme.PAD_MD)
        self.label_var = tk.StringVar(value=label)
        self.value_var = tk.StringVar(value=value)
        self.hint_var = tk.StringVar(value=hint)
        ttk.Label(self.frame, textvariable=self.label_var,
                  style="MetricLabel.TLabel").pack(anchor="w")
        self.value_lbl = ttk.Label(self.frame, textvariable=self.value_var,
                                   style="MetricValue.TLabel")
        self.value_lbl.pack(anchor="w", pady=(2, 0))
        self.hint_lbl = ttk.Label(self.frame, textvariable=self.hint_var,
                                  style="MetricHint.TLabel")
        if hint:
            self.hint_lbl.pack(anchor="w", pady=(2, 0))
        self._on_click = on_click
        if on_click:
            for w in (self.frame, self.value_lbl, self.hint_lbl):
                w.bind("<Button-1>", lambda e: on_click())
                try:
                    w.configure(cursor="hand2")
                except tk.TclError:
                    pass

    def set(self, value: str, hint: str | None = None) -> None:
        self.value_var.set(value)
        if hint is not None:
            self.hint_var.set(hint)
            if hint:
                if not self.hint_lbl.winfo_ismapped():
                    self.hint_lbl.pack(anchor="w", pady=(2, 0))
            else:
                self.hint_lbl.pack_forget()

    def pack(self, **kw):
        return self.frame.pack(**kw)

    def grid(self, **kw):
        return self.frame.grid(**kw)


def set_busy(widgets: Iterable[tk.Misc], busy: bool) -> None:
    """统一切换一组控件的 disabled/normal（忽略不支持 state 的控件）。"""
    state = "disabled" if busy else "normal"
    for w in widgets:
        try:
            w.configure(state=state)
        except tk.TclError:
            pass


def status_pill(master, *, state: str = PILL_OFF, text: str = "") -> StatusPill:
    """工厂：创建状态胶囊。"""
    return StatusPill(master, state=state, text=text)
