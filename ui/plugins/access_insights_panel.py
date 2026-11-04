# -*- coding: utf-8 -*-
"""「访问日志聚合」页签：nginx access.log 的状态码 / 热门路径 / 异常请求概览。

契约（主窗口 / 主题系统）：

- ``auto_refresh()``：8 秒 tick 调用，解析走 worker + queue，主线程只 drain；
- ``refresh_theme()``：主题切换时重设 Treeview 配色并重绘小时分布图。

安全边界：只读 nginx 日志目录里的 access 日志，文件下拉只能选已有文件名，
不接受任意路径（核心逻辑见 ``core/plugins/access_insights/aggregate.py``）。
"""
import queue
import threading
import tkinter as tk
from tkinter import ttk

from core import run_log
from core.i18n import t
from core.plugins.access_insights import aggregate
from .. import theme
from ..window_utils import rearm_poll

_LINE_CHOICES = (2000, 20000, 100000)


class AccessInsightsPanel(ttk.Frame):
    """访问日志聚合面板。"""

    def __init__(self, master, config, notify):
        super().__init__(master, padding=8)
        self.config = config
        self.notify = notify
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._data: dict = {}
        self._file = ""
        self._lines = 20000
        self._status = tk.StringVar(value=t("就绪"))
        self._file_var = tk.StringVar()
        self._lines_var = tk.StringVar(value=str(self._lines))
        self._v_requests = tk.StringVar(value="—")
        self._v_ips = tk.StringVar(value="—")
        self._v_errors = tk.StringVar(value="—")
        self._v_avg = tk.StringVar(value="—")
        self._build()
        self.refresh()
        self._poll()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text=t("访问日志聚合"),
                  style="Title.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self._status,
                  style="CardDim.TLabel").pack(side="left", padx=(theme.PAD_MD, 0))
        ttk.Button(bar, text=t("刷新"), command=self.refresh,
                   style="Toolbar.TButton").pack(side="right")

        self._lines_combo = ttk.Combobox(
            bar, textvariable=self._lines_var, width=8, state="readonly",
            values=[str(n) for n in _LINE_CHOICES])
        self._lines_combo.pack(side="right", padx=(0, theme.PAD_SM))
        self._lines_combo.bind("<<ComboboxSelected>>", self._on_lines)
        ttk.Label(bar, text=t("统计行数"),
                  style="CardDim.TLabel").pack(side="right", padx=(0, 4))

        self._file_combo = ttk.Combobox(
            bar, textvariable=self._file_var, width=24, state="readonly")
        self._file_combo.pack(side="right", padx=(0, theme.PAD_SM))
        self._file_combo.bind("<<ComboboxSelected>>", self._on_file)
        ttk.Label(bar, text=t("日志文件"),
                  style="CardDim.TLabel").pack(side="right", padx=(0, 4))

        cards = ttk.Frame(self)
        cards.pack(fill="x", pady=(theme.PAD_MD, 0))
        for title, var in ((t("请求数"), self._v_requests),
                           (t("独立 IP"), self._v_ips),
                           (t("5xx 比例"), self._v_errors),
                           (t("平均耗时"), self._v_avg)):
            self._card(cards, title, var)

        self._mid = ttk.Frame(self)
        self._mid.pack(fill="both", expand=True, pady=(theme.PAD_MD, 0))
        left = self._section(self._mid, t("状态码分布"))
        self._code_tree = self._make_tree(
            left, (t("状态码"), t("次数"), t("占比")), (70, 60, 60))
        middle = self._section(self._mid, t("热门路径"))
        self._path_tree = self._make_tree(
            middle, (t("路径"), t("次数"), t("占比")), (220, 60, 60))
        right = self._section(self._mid, t("Top IP"))
        self._ip_tree = self._make_tree(
            right, (t("IP"), t("次数"), t("占比")), (140, 60, 60))

        self._empty = ttk.Label(self, text="", style="CardDim.TLabel")

        self._hour_box = ttk.Frame(self)
        self._hour_box.pack(fill="x", pady=(theme.PAD_MD, 0))
        ttk.Label(self._hour_box, text=t("按小时分布"),
                  style="Section.TLabel").pack(anchor="w")
        self._hour_canvas = tk.Canvas(
            self._hour_box, height=90, highlightthickness=0,
            background=theme.CARD_BG, bd=0)
        self._hour_canvas.pack(fill="x", pady=(4, 0))
        self._hour_canvas.bind("<Configure>", lambda e: self._draw_hours())

        err_box = ttk.Frame(self)
        err_box.pack(fill="both", expand=False, pady=(theme.PAD_MD, 0))
        ttk.Label(err_box, text=t("最近错误请求"),
                  style="Section.TLabel").pack(anchor="w")
        self._err_tree = self._make_tree(
            err_box, (t("时间"), t("状态码"), t("方法"), t("路径")),
            (150, 60, 60, 320), height=6)

        ttk.Label(self, text=t("日志只读自 nginx 日志目录，不读取项目内文件"),
                  style="CardDim.TLabel").pack(anchor="w", pady=(theme.PAD_SM, 0))
        self.refresh_theme()

    def _card(self, parent, title: str, var: tk.StringVar) -> None:
        box = ttk.Frame(parent, style="Card.TFrame",
                        padding=(theme.PAD_MD, theme.PAD_SM))
        box.pack(side="left", fill="x", expand=True, padx=(0, theme.PAD_SM))
        ttk.Label(box, text=title, style="CardDim.TLabel").pack(anchor="w")
        ttk.Label(box, textvariable=var, style="Title.TLabel").pack(anchor="w")

    def _section(self, parent, title: str) -> ttk.Frame:
        box = ttk.LabelFrame(parent, text=title, style="Card.TLabelframe",
                             padding=theme.PAD_SM)
        box.pack(side="left", fill="both", expand=True,
                 padx=(0, theme.PAD_SM))
        return box

    def _make_tree(self, parent, columns, widths, height: int = 9):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrap, columns=tuple(columns), show="headings",
                            selectmode="browse", height=height)
        for col, width in zip(columns, widths):
            tree.heading(col, text=col)
            tree.column(col, width=width, anchor="w", stretch=True)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tree

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
        try:
            data = aggregate.report(self._file, lines=self._lines)
            self._queue.put(("data", data))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("error", f"{type(e).__name__}: {e}"))

    def _poll(self) -> None:
        while True:
            try:
                msg = self._queue.get_nowait()
            except queue.Empty:
                break
            if msg[0] == "data":
                self._busy = False
                self._data = msg[1]
                self._update_files()
                try:
                    if self._data.get("ok"):
                        self._render()
                        self._show_empty(False)
                    else:
                        self._show_empty(True)
                except Exception as e:  # noqa: BLE001 - 单次渲染失败不中断轮询
                    run_log.error("plugin", t("访问日志聚合渲染失败：{err}", err=e))
            else:
                self._busy = False
                self._status.set(t("统计失败"))
                run_log.error("plugin", t("访问日志聚合读取失败：{err}", err=msg[1]))
        rearm_poll(self, self._poll, 150)

    def _update_files(self) -> None:
        names = list(self._data.get("available") or [])
        self._file_combo.configure(values=names)
        current = self._data.get("file") or (names[0] if names else "")
        if current and current != self._file_var.get():
            self._file_var.set(current)
        if not self._file:
            self._file = current

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def refresh_theme(self) -> None:
        for tree in (getattr(self, "_code_tree", None),
                     getattr(self, "_path_tree", None),
                     getattr(self, "_ip_tree", None),
                     getattr(self, "_err_tree", None)):
            if tree is None:
                continue
            tree.tag_configure("odd", background=theme.ROW_ALT)
            tree.tag_configure("even", background=theme.CARD_BG)
            tree.tag_configure("err4", foreground=theme.WARN)
            tree.tag_configure("err5", foreground=theme.ERR)
        canvas = getattr(self, "_hour_canvas", None)
        if canvas is not None:
            canvas.configure(background=theme.CARD_BG)
            self._draw_hours()

    def _render(self) -> None:
        d = self._data
        self._status.set(t("读取 {read} 行，解析 {parsed} 行（跳过 {skipped}）",
                           read=d.get("read", 0), parsed=d.get("parsed", 0),
                           skipped=d.get("skipped", 0)))
        self._v_requests.set(str(d.get("requests", 0)))
        self._v_ips.set(str(d.get("unique_ips", 0)))
        self._v_errors.set(f'{float(d.get("error_rate") or 0):.1f}%')
        avg = d.get("avg_seconds")
        self._v_avg.set("—" if avg is None else f"{avg * 1000:.0f} ms")

        self._fill(self._code_tree,
                   [(str(it["code"]), it["count"], f'{it["pct"]:.1f}%')
                    for it in d.get("status_codes") or []])
        self._fill(self._path_tree,
                   [(it["path"], it["count"], f'{it["pct"]:.1f}%')
                    for it in d.get("top_paths") or []])
        self._fill(self._ip_tree,
                   [(it["ip"], it["count"], f'{it["pct"]:.1f}%')
                    for it in d.get("top_ips") or []])
        self._fill(self._err_tree,
                   [(e["time"], e["status"], e["method"], e["path"])
                    for e in d.get("errors") or []],
                   tag_fn=lambda i, vals: ("err5" if int(vals[1]) >= 500
                                           else "err4"))
        self._draw_hours()

    def _fill(self, tree, rows, tag_fn=None) -> None:
        tree.delete(*tree.get_children())
        for i, vals in enumerate(rows):
            tags = ["odd" if i % 2 else "even"]
            if tag_fn is not None:
                extra = tag_fn(i, vals)
                if extra:
                    tags.append(extra)
            tree.insert("", "end", values=tuple(vals), tags=tuple(tags))

    def _show_empty(self, show: bool) -> None:
        if show:
            self._mid.pack_forget()
            if self._data.get("error") == "not_found":
                text = t("未找到该 access 日志：{name}", name=self._file)
            else:
                text = t("未找到 nginx access 日志"
                         "（可能站点未产生访问，或配置里关闭了 access_log）")
            self._status.set(text)
            self._empty.configure(text=text)
            self._empty.pack(fill="both", expand=True,
                             pady=(theme.PAD_MD, 0))
        else:
            self._empty.pack_forget()
            if not self._mid.winfo_ismapped():
                self._mid.pack(fill="both", expand=True,
                               pady=(theme.PAD_MD, 0), before=self._hour_box)

    def _draw_hours(self) -> None:
        c = getattr(self, "_hour_canvas", None)
        if c is None or not c.winfo_exists():
            return
        c.delete("all")
        width = c.winfo_width() or 640
        height = int(c.cget("height")) or 90
        data = [int(it.get("count") or 0)
                for it in (self._data.get("hourly") or [])]
        if not data or not any(data):
            c.create_text(width / 2, height / 2, text=t("暂无数据"),
                          fill=theme.TEXT_DIM, font=theme.font(theme.FS_SMALL))
            return
        peak = max(data)
        pad_l, pad_r, pad_t, pad_b = 34, 8, 10, 16
        plot_w = max(1, width - pad_l - pad_r)
        plot_h = max(1, height - pad_t - pad_b)
        step = plot_w / 24.0
        baseline = height - pad_b
        c.create_line(pad_l, baseline, width - pad_r, baseline,
                      fill=theme.BORDER)
        for i, n in enumerate(data):
            bar_w = max(1, int(step) - 2)
            bar_h = int(plot_h * n / peak)
            x0 = pad_l + int(i * step) + 1
            if bar_h:
                c.create_rectangle(x0, baseline - bar_h, x0 + bar_w, baseline,
                                   fill=theme.PRIMARY, outline="")
            if i % 3 == 0:
                c.create_text(x0, baseline + 6, text=str(i), anchor="n",
                              fill=theme.TEXT_DIM,
                              font=theme.font(theme.FS_SMALL))
        c.create_text(pad_l - 6, pad_t, text=str(peak), anchor="ne",
                      fill=theme.TEXT_DIM, font=theme.font(theme.FS_SMALL))

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_file(self, _event=None) -> None:
        name = self._file_var.get().strip()
        if not name or name == self._file:
            return
        self._file = name
        self.refresh()

    def _on_lines(self, _event=None) -> None:
        try:
            lines = int(self._lines_var.get())
        except ValueError:
            return
        if lines == self._lines:
            return
        self._lines = lines
        self.refresh()
