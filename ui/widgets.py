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

import re
import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable, Sequence

from core.i18n import t

from . import theme

# MetricCard 数字缓动 / 状态闪
_METRIC_NUM_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)(.*)$")
_ANIM_MS = 280
_ANIM_STEP_MS = 16
_FLASH_MS = 180

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


def parse_metric_number(text: str) -> tuple[float, str] | None:
    """解析「数字 + 可选后缀」（如 ``12`` / ``12%`` / ``6.8``）。

    整串必须是「可选符号 + 数字 + 后缀」；空白会先 strip。无法解析返回 ``None``。
    """
    s = (text or "").strip()
    if not s:
        return None
    m = _METRIC_NUM_RE.match(s)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    return num, m.group(2)


def _format_metric_number(num: float, suffix: str, *, as_int: bool) -> str:
    if as_int:
        return f"{int(round(num))}{suffix}"
    # 保留最多 1 位小数，去掉无意义的 .0
    text = f"{num:.1f}".rstrip("0").rstrip(".")
    return f"{text}{suffix}"


class MetricCard:
    """仪表盘数字/状态卡（标签 + 大值 + 可选说明）。"""

    def __init__(self, master, *, label: str, value: str = "—",
                 hint: str = "", on_click: Callable | None = None,
                 value_var: tk.StringVar | None = None,
                 hint_var: tk.StringVar | None = None):
        self.frame = ttk.Frame(master, style="MetricCard.TFrame",
                               padding=theme.PAD_MD)
        self.label_var = tk.StringVar(value=label)
        # 可传入外部 StringVar，便于面板在 _render 里直接 .set()
        self.value_var = value_var if value_var is not None else tk.StringVar(value=value)
        self.hint_var = hint_var if hint_var is not None else tk.StringVar(value=hint)
        ttk.Label(self.frame, textvariable=self.label_var,
                  style="MetricLabel.TLabel").pack(anchor="w")
        self.value_lbl = ttk.Label(self.frame, textvariable=self.value_var,
                                   style="MetricValue.TLabel")
        self.value_lbl.pack(anchor="w", pady=(2, 0))
        self.hint_lbl = ttk.Label(self.frame, textvariable=self.hint_var,
                                  style="MetricHint.TLabel")
        # 外部 hint_var 始终占位，避免后续 .set 时还要手动 pack
        if hint or hint_var is not None:
            self.hint_lbl.pack(anchor="w", pady=(2, 0))
        self._on_click = on_click
        self._anim_after: str | None = None
        self._flash_after: str | None = None
        self._hint_flash_after: str | None = None
        if on_click:
            for widget in (self.frame, self.value_lbl, self.hint_lbl):
                widget.bind("<Button-1>", lambda e: on_click())
                try:
                    widget.configure(cursor="hand2")
                except tk.TclError:
                    pass

    def set(self, value: str, hint: str | None = None, *,
            animate: bool = True) -> None:
        """更新大值 / 说明。

        ``animate=True``（默认）时：纯数字缓动；非数字文案轻闪；hint 变化轻闪。
        值与 hint 都未变则短路，避免无意义重绘。
        """
        new_value = value if value is not None else ""
        old_value = self.value_var.get()
        hint_changed = False
        if hint is not None:
            old_hint = self.hint_var.get()
            hint_changed = (hint != old_hint)
            self._apply_hint(hint)
            if hint_changed and animate and hint:
                self._flash_hint()

        if new_value == old_value:
            return

        if not animate:
            self._cancel_anim()
            self.value_var.set(new_value)
            return

        old_num = parse_metric_number(old_value)
        new_num = parse_metric_number(new_value)
        if (old_num is not None and new_num is not None
                and old_num[1] == new_num[1]):
            self._animate_number(old_num[0], new_num[0], new_num[1],
                                 as_int=("." not in old_value
                                         and "." not in new_value))
            return

        self._cancel_anim()
        self.value_var.set(new_value)
        self._flash_value()

    def _apply_hint(self, hint: str) -> None:
        self.hint_var.set(hint)
        if hint:
            if not self.hint_lbl.winfo_ismapped():
                self.hint_lbl.pack(anchor="w", pady=(2, 0))
        else:
            self.hint_lbl.pack_forget()

    def _cancel_anim(self) -> None:
        if self._anim_after is not None:
            try:
                self.frame.after_cancel(self._anim_after)
            except (tk.TclError, ValueError):
                pass
            self._anim_after = None

    def _animate_number(self, start: float, end: float, suffix: str,
                        *, as_int: bool) -> None:
        self._cancel_anim()
        steps = max(1, _ANIM_MS // _ANIM_STEP_MS)
        state = {"i": 0}

        def tick() -> None:
            self._anim_after = None
            try:
                if not self.frame.winfo_exists():
                    return
            except tk.TclError:
                return
            state["i"] += 1
            i = state["i"]
            # ease-out：1 - (1-t)^3
            t = min(1.0, i / steps)
            eased = 1.0 - (1.0 - t) ** 3
            cur = start + (end - start) * eased
            self.value_var.set(_format_metric_number(cur, suffix, as_int=as_int))
            if i < steps:
                self._anim_after = self.frame.after(_ANIM_STEP_MS, tick)
            else:
                self.value_var.set(
                    _format_metric_number(end, suffix, as_int=as_int))
                self._flash_value()

        self._anim_after = self.frame.after(_ANIM_STEP_MS, tick)

    def _flash_value(self) -> None:
        if self._flash_after is not None:
            try:
                self.frame.after_cancel(self._flash_after)
            except (tk.TclError, ValueError):
                pass
            self._flash_after = None
        try:
            self.value_lbl.configure(style="MetricValueFlash.TLabel")
        except tk.TclError:
            return

        def restore() -> None:
            self._flash_after = None
            try:
                if self.value_lbl.winfo_exists():
                    self.value_lbl.configure(style="MetricValue.TLabel")
            except tk.TclError:
                pass

        self._flash_after = self.frame.after(_FLASH_MS, restore)

    def _flash_hint(self) -> None:
        if self._hint_flash_after is not None:
            try:
                self.frame.after_cancel(self._hint_flash_after)
            except (tk.TclError, ValueError):
                pass
            self._hint_flash_after = None
        try:
            self.hint_lbl.configure(style="MetricHintFlash.TLabel")
        except tk.TclError:
            return

        def restore() -> None:
            self._hint_flash_after = None
            try:
                if self.hint_lbl.winfo_exists():
                    self.hint_lbl.configure(style="MetricHint.TLabel")
            except tk.TclError:
                pass

        self._hint_flash_after = self.frame.after(_FLASH_MS, restore)

    def pack(self, **kw):
        return self.frame.pack(**kw)

    def grid(self, **kw):
        return self.frame.grid(**kw)

    def destroy(self) -> None:
        self._cancel_anim()
        for attr in ("_flash_after", "_hint_flash_after"):
            aid = getattr(self, attr, None)
            if aid is not None:
                try:
                    self.frame.after_cancel(aid)
                except (tk.TclError, ValueError):
                    pass
                setattr(self, attr, None)
        try:
            self.frame.destroy()
        except tk.TclError:
            pass


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
