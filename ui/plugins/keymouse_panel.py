# -*- coding: utf-8 -*-
"""键鼠统计面板：时间范围切换、数字卡、键位排名、按小时分布。

数据全部来自 ``core/plugins/keymouse/stats.py``（只读 SQLite），面板本身不碰
钩子、不碰后台进程；采集与展示彻底分离，面板卡顿不影响记录。

契约（主窗口 / 主题系统）：
- ``auto_refresh()``：8 秒 tick 调用，仅在页签可见时调度；
- ``refresh_theme()``：主题切换时重绘 Canvas（颜色不能固化在初始化时）。
"""
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core import run_log
from core.i18n import t
from core.plugins.keymouse import (
    MANIFEST,
    ensure_daemon,
    is_paused,
    is_running,
    set_paused,
    stats,
    status,
    stop_daemon,
    store,
    supported,
)
from .. import theme
from ..window_utils import rearm_poll

_RANK_ROWS = 8        # 排名榜显示条数（再多会挤掉小时图）
_RANK_ROW_H = 24
_HOUR_H = 118


class KeymousePanel(ttk.Frame):
    """「键鼠统计」页签。"""

    def __init__(self, master, config, notify):
        super().__init__(master, padding=8)
        self.config = config
        self.notify = notify
        self.range_key = "today"
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._data: dict = {}
        self._state: dict = {}
        self._hover = -1
        self._row_rects: list[int] = []
        self._empty_visible = False
        self._build()
        self._update_range_buttons()
        self.refresh()
        self._poll()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        # ---- 顶部工具条：时间范围 + 采集状态 --------------------------
        bar = ttk.Frame(self)
        bar.pack(fill="x")

        self._range_btns: dict[str, ttk.Button] = {}
        for key in stats.RANGES:
            btn = ttk.Button(bar, text=t(stats.RANGE_LABELS[key]), width=7,
                             command=lambda k=key: self.set_range(k))
            btn.pack(side="left", padx=(0, 6))
            self._range_btns[key] = btn

        self._btn_daemon = ttk.Button(bar, text=t("启动后台记录"),
                                      command=self._on_daemon)
        self._btn_daemon.pack(side="right")
        self._btn_pause = ttk.Button(bar, text=t("暂停采集"),
                                     command=self._on_pause)
        self._btn_pause.pack(side="right", padx=(0, 6))

        self._status_text = tk.StringVar(value=t("检测中…"))
        ttk.Label(bar, textvariable=self._status_text, style="SubTitle.TLabel").pack(
            side="right", padx=(0, 12))
        self._dot = tk.Label(bar, text="●", font=(theme.FONT, 12, "bold"),
                             background=theme.BG, foreground=theme.GRAY)
        self._dot.pack(side="right", padx=(8, 2))

        # ---- 数字卡 ---------------------------------------------------
        cards = ttk.Frame(self)
        cards.pack(fill="x", pady=(10, 10))
        cards.columnconfigure(0, weight=1, uniform="km")
        cards.columnconfigure(1, weight=1, uniform="km")

        card_key = ttk.Frame(cards, style="Card.TFrame", padding=14)
        card_key.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(card_key, text=t("键盘按键"), style="CardDim.TLabel").pack(anchor="w")
        self._total_var = tk.StringVar(value="—")
        tk.Label(card_key, textvariable=self._total_var, font=theme.font(26, True),
                 background=theme.CARD_BG, foreground=theme.PRIMARY).pack(anchor="w")
        self._total_sub = tk.StringVar(value="")
        ttk.Label(card_key, textvariable=self._total_sub,
                  style="CardDim.TLabel").pack(anchor="w")

        card_kpm = ttk.Frame(cards, style="Card.TFrame", padding=14)
        card_kpm.grid(row=0, column=1, sticky="nsew")
        ttk.Label(card_kpm, text=t("打字速度"), style="CardDim.TLabel").pack(anchor="w")
        row = ttk.Frame(card_kpm, style="Card.TFrame")
        row.pack(anchor="w")
        self._kpm_var = tk.StringVar(value="—")
        self._kpm_label = tk.Label(row, textvariable=self._kpm_var,
                                   font=theme.font(26, True), background=theme.CARD_BG,
                                   foreground=theme.PRIMARY)
        self._kpm_label.pack(side="left")
        ttk.Label(row, text=t("次/分"), style="CardDim.TLabel").pack(
            side="left", padx=(6, 0), pady=(10, 0))
        self._kpm_sub = tk.StringVar(value="")
        ttk.Label(card_kpm, textvariable=self._kpm_sub,
                  style="CardDim.TLabel").pack(anchor="w")

        # ---- 键位排名 -------------------------------------------------
        rank_box = ttk.Frame(self)
        rank_box.pack(fill="both", expand=True)
        self._rank_title = tk.StringVar(value=t("键位排名"))
        ttk.Label(rank_box, textvariable=self._rank_title,
                  style="Section.TLabel").pack(anchor="w")
        self._rank_canvas = tk.Canvas(
            rank_box, height=_RANK_ROWS * _RANK_ROW_H + 8, highlightthickness=0,
            background=theme.CARD_BG, bd=0)
        self._rank_canvas.pack(fill="both", expand=True, pady=(4, 0))
        self._rank_canvas.bind("<Configure>", lambda e: self._draw_rank())

        # 空状态：只在没有任何记录时出现，给一个直达「启动后台记录」的入口
        self._empty_box = ttk.Frame(rank_box)
        self._empty_btn = ttk.Button(self._empty_box, text=t("启动后台记录"),
                                     command=self._on_daemon,
                                     style="Accent.TButton")
        self._empty_btn.pack(side="left")

        # ---- 按小时分布 -----------------------------------------------
        hour_box = ttk.Frame(self)
        hour_box.pack(fill="x", pady=(8, 0))
        self._hour_title = tk.StringVar(value=t("按小时活动"))
        ttk.Label(hour_box, textvariable=self._hour_title,
                  style="Section.TLabel").pack(anchor="w")
        self._hour_canvas = tk.Canvas(
            hour_box, height=_HOUR_H, highlightthickness=0,
            background=theme.CARD_BG, bd=0)
        self._hour_canvas.pack(fill="x", pady=(4, 0))
        self._hour_canvas.bind("<Configure>", lambda e: self._draw_hours())

        # ---- 底部操作 -------------------------------------------------
        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text=t("显示报告"),
                   command=self._show_report).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text=t("保存报告…"),
                   command=self._save_report).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text=t("清空记录"),
                   command=self._on_clear).pack(side="right")

    # ------------------------------------------------------------------ #
    # 时间范围
    # ------------------------------------------------------------------ #
    def _update_range_buttons(self) -> None:
        for key, btn in self._range_btns.items():
            btn.configure(style="Accent.TButton" if key == self.range_key
                          else "TButton")

    def set_range(self, key: str) -> None:
        if key == self.range_key or key not in stats.RANGES:
            return
        self.range_key = key
        self._update_range_buttons()
        self.refresh()

    # ------------------------------------------------------------------ #
    # 数据刷新（worker + 队列回主线程）
    # ------------------------------------------------------------------ #
    def auto_refresh(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        rng = self.range_key
        try:
            data = stats.summary(rng, top=_RANK_ROWS)
            state = status(self.config)
            self._queue.put(("data", rng, data, state))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("error", f"{type(e).__name__}: {e}"))

    def _poll(self) -> None:
        while True:
            try:
                msg = self._queue.get_nowait()
            except queue.Empty:
                break
            self._busy = False
            kind = msg[0]
            if kind == "data":
                _, rng, data, state = msg
                if rng != self.range_key:
                    continue  # 用户已切换范围，丢弃过期结果
                self._data = data
                self._state = state
                try:
                    self._render()
                except Exception as e:  # noqa: BLE001 - 单次渲染失败不中断轮询
                    run_log.error("plugin", t("键鼠统计渲染失败：{err}", err=e))
            elif kind == "error":
                self._status_text.set(t("检测失败"))
                run_log.error("plugin", t("键鼠统计读取失败：{err}", err=msg[1]))
        rearm_poll(self, self._poll, 150)

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _render(self) -> None:
        data = self._data or {}
        state = self._state or {}
        self._render_status(state)
        self._render_cards(data)
        self._rank_title.set(t("键位排名 · {label}", label=data.get("label", "")))
        if data.get("hourly_supported"):
            self._hour_title.set(t("按小时活动"))
        else:
            self._hour_title.set(t("按小时活动（仅今日 / 本周）"))
        self._draw_rank()
        self._draw_hours()
        self._render_empty(data, state)

    def _render_empty(self, data: dict, state: dict) -> None:
        """无数据时给一个直达入口，不留空白死区（复用已有文案，不新增词条）。"""
        if data.get("has_data"):
            if self._empty_visible:
                self._empty_box.pack_forget()
                self._empty_visible = False
            return
        running = bool(state.get("running"))
        ok_platform = bool(state.get("supported", supported()))
        if not ok_platform:
            self._empty_btn.configure(text=t("当前平台不支持（仅 Windows）"))
            self._empty_btn.state(["disabled"])
        elif running:
            self._empty_btn.configure(text=t("采集中"))
            self._empty_btn.state(["disabled"])
        else:
            self._empty_btn.configure(text=t("启动后台记录"))
            self._empty_btn.state(["!disabled"])
        if not self._empty_visible:
            self._empty_box.pack(fill="x", pady=(4, 0))
            self._empty_visible = True

    def _render_status(self, state: dict) -> None:
        running = bool(state.get("running"))
        paused = bool(state.get("paused"))
        ok_platform = bool(state.get("supported", supported()))
        if not ok_platform:
            self._dot.configure(foreground=theme.ERR)
            self._status_text.set(t("当前平台不支持（仅 Windows）"))
            self._btn_pause.state(["disabled"])
            self._btn_daemon.state(["disabled"])
            return
        if not running:
            self._dot.configure(foreground=theme.GRAY)
            self._status_text.set(t("后台未运行"))
            self._btn_daemon.configure(text=t("启动后台记录"))
            self._btn_pause.state(["disabled"])
            return
        self._dot.configure(foreground=theme.WARN if paused else theme.OK)
        self._status_text.set(t("已暂停") if paused else t("采集中"))
        self._btn_daemon.configure(text=t("停止后台记录"))
        self._btn_pause.state(["!disabled"])
        self._btn_pause.configure(text=t("继续采集") if paused else t("暂停采集"))

    def _render_cards(self, data: dict) -> None:
        total = int(data.get("total_presses") or 0)
        if not data.get("has_data"):
            self._total_var.set("—")
            self._total_sub.set(t("暂无记录"))
            self._kpm_var.set("—")
            self._kpm_sub.set(t("峰值 — · 均值 —"))
            return
        self._total_var.set(f"{total:,}")
        self._total_sub.set(t("日均 {avg} 次 · 覆盖 {days} 天",
                              avg=data.get("daily_avg", 0), days=data.get("days", 1)))
        kpm = data.get("kpm_last") or data.get("kpm_avg") or 0
        self._kpm_var.set(f"{kpm:g}")
        self._kpm_sub.set(t("峰值 {peak} · 均值 {avg}",
                            peak=data.get("kpm_peak", 0),
                            avg=data.get("kpm_avg", 0)))

    # ---- Canvas 自绘 -------------------------------------------------- #
    def _draw_rank(self) -> None:
        c = self._rank_canvas
        if not c.winfo_exists():
            return
        c.delete("all")
        self._row_rects = []
        width = max(360, c.winfo_width())
        items = (self._data or {}).get("top_keys") or []
        if not items:
            c.create_text(width / 2, 40, text=t("暂无记录"),
                          fill=theme.TEXT_DIM, font=theme.font(9))
            return
        label_w, value_w = 76, 64
        bar_x = label_w
        bar_max = max(60, width - label_w - value_w - 16)
        for i, item in enumerate(items):
            tag = f"row{i}"
            top = i * _RANK_ROW_H + 2
            bottom = top + _RANK_ROW_H - 4
            rect = c.create_rectangle(0, top, width, bottom,
                                      fill=theme.CARD_BG, outline="", tags=tag)
            self._row_rects.append(rect)
            c.create_text(6, (top + bottom) / 2, text=item["key"], anchor="w",
                          fill=theme.TEXT, font=theme.mono(9), tags=tag)
            bar_w = max(2, int(bar_max * float(item.get("pct") or 0)))
            c.create_rectangle(bar_x, top + 4, bar_x + bar_w, bottom - 4,
                               fill=theme.PRIMARY, outline="", tags=tag)
            c.create_text(bar_x + bar_w + 8, (top + bottom) / 2,
                          text=str(item["count"]), anchor="w",
                          fill=theme.TEXT_DIM, font=theme.font(9), tags=tag)
            c.tag_bind(tag, "<Enter>", lambda _e, idx=i: self._set_hover(idx))
            c.tag_bind(tag, "<Leave>", lambda _e: self._set_hover(-1))

    def _set_hover(self, idx: int) -> None:
        if idx == self._hover:
            return
        self._hover = idx
        for i, rect in enumerate(self._row_rects):
            try:
                self._rank_canvas.itemconfigure(
                    rect, fill=theme.HOVER if i == idx else theme.CARD_BG)
            except tk.TclError:
                return

    def _draw_hours(self) -> None:
        c = self._hour_canvas
        if not c.winfo_exists():
            return
        c.delete("all")
        width = max(360, c.winfo_width())
        hours = (self._data or {}).get("hourly") or []
        if not hours:
            c.create_text(width / 2, _HOUR_H / 2,
                          text=t("该时间范围不提供按小时分布"),
                          fill=theme.TEXT_DIM, font=theme.font(9))
            return
        peak = max(hours) or 1
        left, bottom = 26, 16
        chart_h = _HOUR_H - bottom - 6
        step = (width - left) / 24
        bar_w = max(3, step * 0.55)
        for hour, value in enumerate(hours):
            if not value:
                continue
            bar_h = max(1, chart_h * value / peak)
            x = left + step * hour + (step - bar_w) / 2
            c.create_rectangle(x, 6 + chart_h - bar_h, x + bar_w, 6 + chart_h,
                               fill=theme.PRIMARY, outline="")
        for hour in range(0, 24, 4):
            x = left + step * hour + step / 2
            c.create_text(x, 6 + chart_h + 8, text=f"{hour:02d}",
                          fill=theme.TEXT_DIM, font=theme.font(8))
        c.create_text(width - 4, 4, anchor="ne",
                      text=t("峰值 {v}", v=peak), fill=theme.TEXT_DIM,
                      font=theme.font(8))

    # ------------------------------------------------------------------ #
    # 操作
    # ------------------------------------------------------------------ #
    def _on_pause(self) -> None:
        if not supported():
            return
        paused = set_paused(not is_paused())
        self.notify(t("已暂停键盘采集") if paused else t("已恢复键盘采集"))
        self.refresh()

    def _on_daemon(self) -> None:
        if not supported():
            messagebox.showinfo(t("键鼠统计"),
                                t("当前平台不支持键盘采集（仅 Windows）。"),
                                parent=self)
            return
        if is_running():
            if not messagebox.askyesno(
                    t("停止后台记录"),
                    t("停止后将不再记录新的按键（已记录数据保留）。确定继续？"),
                    parent=self):
                return
            ok, msg = stop_daemon()
        else:
            ok, msg = ensure_daemon(self.config)
        self.notify(msg)
        if not ok:
            messagebox.showwarning(t("键鼠统计"), msg, parent=self)
        self.refresh()

    def _on_clear(self) -> None:
        if not messagebox.askyesno(
                t("清空记录"),
                t("将删除全部键鼠统计记录（不可恢复）。确定继续？"), parent=self):
            return
        try:
            writer = store.Writer()
            writer.clear()
            writer.close()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(t("清空记录"),
                                 t("清空失败：{err}", err=e), parent=self)
            return
        self.notify(t("已清空键鼠统计记录"))
        self.refresh()

    def _show_report(self) -> None:
        data = self._data or stats.summary(self.range_key, top=_RANK_ROWS)
        win = tk.Toplevel(self)
        win.title(t("键鼠统计报告"))
        win.transient(self.winfo_toplevel())
        box = ttk.Frame(win, padding=8)
        box.pack(fill="both", expand=True)
        text = tk.Text(box, wrap="word", font=theme.mono(9),
                       background=theme.LOG_BG, foreground=theme.LOG_FG,
                       relief="flat", padx=10, pady=8, height=24, width=72)
        scroll = ttk.Scrollbar(box, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", stats.report_text(data))
        text.configure(state="disabled")
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _save_report(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title=t("保存报告"),
            defaultextension=".csv",
            initialfile=f"keymouse-{self.range_key}.csv",
            filetypes=[(t("CSV 文件"), "*.csv"), ("*", "*.*")])
        if not path:
            return
        res = stats.export_csv(self.range_key, path, top=_RANK_ROWS)
        if res.get("ok"):
            self.notify(t("已保存报告：{path}", path=path))
        else:
            messagebox.showerror(t("保存报告"), res.get("message", ""), parent=self)

    # ------------------------------------------------------------------ #
    # 主题
    # ------------------------------------------------------------------ #
    def refresh_theme(self) -> None:
        """主题切换：Canvas 内容与固化的颜色都按新调色板重绘。"""
        self._dot.configure(background=theme.BG)
        for canvas in (self._rank_canvas, self._hour_canvas):
            try:
                canvas.configure(background=theme.CARD_BG)
            except tk.TclError:
                return
        self._set_hover(-1)
        self._draw_rank()
        self._draw_hours()
