# -*- coding: utf-8 -*-
"""「PHP 错误日志」页签：按级别与消息聚合各版本 / 各站点的错误日志。

契约（主窗口 / 主题系统）：

- ``auto_refresh()``：主窗口 8 秒 tick 调用，内部按 :data:`AUTO_INTERVAL` 节流，
  避免每 8 秒重读一遍所有日志尾部；
- ``refresh_theme()``：主题切换时重设斑马纹与级别色。

安全边界：只读日志尾部，**不写文件、不清理日志**；双击某行只是用系统默认
程序打开对应日志文件。
"""
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from core import process_utils as pu
from core import run_log
from core.i18n import t
from core.plugins.php_errors import MANIFEST, aggregate
from .. import theme
from ..window_utils import rearm_poll

#: 自动刷新最小间隔（秒）：一次要读最多 40 个日志尾部
AUTO_INTERVAL = 60.0
_LEVEL_TAGS = {"fatal": "fatal", "error": "error", "warning": "warn",
               "notice": "dim", "deprecated": "dim", "other": "dim"}


class PhpErrorsPanel(ttk.Frame):
    """PHP 错误日志面板。"""

    def __init__(self, master, config, notify):
        super().__init__(master, padding=8)
        self.config = config
        self.notify = notify
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._data: dict = {}
        self._rows: dict[str, dict] = {}
        self._last_scan = 0.0
        self._status = tk.StringVar(value=t("就绪"))
        self._filter_var = tk.StringVar()
        self._level_var = tk.StringVar(value=t("全部级别"))
        self._v_files = tk.StringVar(value="—")
        self._v_errors = tk.StringVar(value="—")
        self._v_warns = tk.StringVar(value="—")
        self._v_last = tk.StringVar(value="—")
        self._level_labels = {t("全部级别"): ""}
        for key in aggregate.LEVELS:
            self._level_labels[t(aggregate.LEVEL_MESSAGES[key])] = key
        self._build()
        self.refresh()
        self._poll()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text=t("PHP 错误日志"),
                  style="Title.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self._status,
                  style="CardDim.TLabel").pack(side="left",
                                             padx=(theme.PAD_MD, 0))
        ttk.Button(bar, text=t("刷新"), command=self.refresh,
                   style="Toolbar.TButton").pack(side="right")

        self._level_combo = ttk.Combobox(
            bar, textvariable=self._level_var, values=list(self._level_labels),
            width=10, state="readonly")
        self._level_combo.pack(side="right", padx=(0, theme.PAD_SM))
        self._level_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Label(bar, text=t("级别"),
                  style="CardDim.TLabel").pack(side="right", padx=(0, 4))

        self._filter_entry = ttk.Entry(bar, textvariable=self._filter_var,
                                       width=20)
        self._filter_entry.pack(side="right", padx=(0, theme.PAD_SM))
        self._filter_entry.bind("<Return>", lambda e: self.refresh())
        ttk.Label(bar, text=t("路径过滤"),
                  style="CardDim.TLabel").pack(side="right", padx=(0, 4))

        cards = ttk.Frame(self)
        cards.pack(fill="x", pady=(theme.PAD_MD, 0))
        for title, var in ((t("日志文件"), self._v_files),
                           (t("错误"), self._v_errors),
                           (t("警告"), self._v_warns),
                           (t("最近一条"), self._v_last)):
            self._card(cards, title, var)

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, pady=(theme.PAD_MD, 0))
        body = ttk.Frame(wrap)
        body.pack(side="top", fill="both", expand=True)
        heads = (t("最近时间"), t("级别"), t("次数"), t("来源"), t("消息"))
        widths = (160, 80, 60, 130, 520)
        self._tree = ttk.Treeview(body, columns=heads, show="headings",
                                  selectmode="browse")
        for col, width in zip(heads, widths):
            self._tree.heading(col, text=col)
            self._tree.column(col, width=width, anchor="w", stretch=False)
        sb = ttk.Scrollbar(body, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self._tree.xview)
        self._tree.configure(xscrollcommand=hsb.set)
        hsb.pack(side="bottom", fill="x")
        self._tree.bind("<Double-1>", self._on_open)

        ttk.Label(self, text=t("只读：读每个日志尾部 256 KB、最多 40 个文件；"
                               "同一消息（忽略行号与路径）合并计数，"
                               "双击打开对应日志"),
                  style="CardDim.TLabel").pack(anchor="w",
                                               pady=(theme.PAD_SM, 0))
        self.refresh_theme()

    def _card(self, parent, title: str, var: tk.StringVar) -> None:
        box = ttk.Frame(parent, style="Card.TFrame",
                        padding=(theme.PAD_MD, theme.PAD_SM))
        box.pack(side="left", fill="x", expand=True, padx=(0, theme.PAD_SM))
        ttk.Label(box, text=title, style="CardDim.TLabel").pack(anchor="w")
        ttk.Label(box, textvariable=var, style="Title.TLabel").pack(anchor="w")

    # ------------------------------------------------------------------ #
    # 采集
    # ------------------------------------------------------------------ #
    def auto_refresh(self) -> None:
        if self._busy:
            return
        if time.monotonic() - self._last_scan < AUTO_INTERVAL:
            return
        self.refresh()

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._last_scan = time.monotonic()
        self._status.set(t("正在汇总错误日志…"))
        threading.Thread(
            target=self._worker,
            args=(self._filter_var.get().strip(),
                  self._level_labels.get(self._level_var.get(), "")),
            daemon=True).start()

    def _worker(self, only: str, level: str) -> None:
        try:
            data = aggregate.report(self.config, only=only, level=level)
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
                try:
                    self._render()
                except Exception as e:  # noqa: BLE001
                    run_log.error("plugin", t("PHP 错误日志渲染失败：{err}", err=e))
            else:
                self._busy = False
                self._status.set(t("汇总失败"))
                run_log.error("plugin",
                              t("PHP 错误日志汇总失败：{err}", err=msg[1]))
        rearm_poll(self, self._poll, 200)

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def refresh_theme(self) -> None:
        tree = getattr(self, "_tree", None)
        if tree is None:
            return
        tree.tag_configure("odd", background=theme.ROW_ALT)
        tree.tag_configure("even", background=theme.CARD_BG)
        tree.tag_configure("fatal", foreground=theme.ERR)
        tree.tag_configure("error", foreground=theme.ERR)
        tree.tag_configure("warn", foreground=theme.WARN)
        tree.tag_configure("dim", foreground=theme.TEXT_DIM)

    def _render(self) -> None:
        d = self._data
        counts = d.get("counts") or {}
        self._v_files.set(str(d.get("files") or 0))
        self._v_errors.set(str(int(counts.get("fatal") or 0)
                               + int(counts.get("error") or 0)))
        self._v_warns.set(str(counts.get("warning") or 0))
        rows = d.get("top") or []
        self._v_last.set(rows[0]["last"] if rows else "—")

        error = str(d.get("error") or "")
        if error:
            self._status.set(t(aggregate.ERROR_MESSAGES.get(error, "读取失败")))
        else:
            self._status.set(t("{files} 个文件 · {entries} 条错误 · "
                               "{lines} 行已读", files=d.get("files") or 0,
                               entries=d.get("entries") or 0,
                               lines=d.get("lines") or 0))

        self._tree.delete(*self._tree.get_children())
        self._rows.clear()
        for i, it in enumerate(rows):
            level = str(it.get("level") or "other")
            item_id = self._tree.insert(
                "", "end",
                values=(it.get("last") or "—",
                        t(aggregate.LEVEL_MESSAGES.get(level, "其他")),
                        it.get("count") or 0,
                        it.get("source") or "—",
                        it.get("message") or ""),
                tags=("odd" if i % 2 else "even",
                      _LEVEL_TAGS.get(level, "dim")))
            self._rows[item_id] = it

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_open(self, _event=None) -> None:
        selection = self._tree.selection()
        if not selection:
            return
        row = self._rows.get(selection[0]) or {}
        path = str(row.get("path") or "")
        if not path:
            return
        try:
            pu.open_path(path)
        except Exception as e:  # noqa: BLE001
            run_log.error("plugin", t("打开日志失败：{err}", err=e))
            return
        self.notify(t("已打开日志：{path}", path=path))
