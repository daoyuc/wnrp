# -*- coding: utf-8 -*-
"""「证书到期」页签：按剩余天数列出各站点 SSL 证书（只读，不续期）。

契约（主窗口 / 主题系统）：

- ``auto_refresh()``：主窗口 8 秒 tick 调用，内部按 :data:`AUTO_INTERVAL`
  节流（证书到期变化很慢，300 秒足够）；
- ``refresh_theme()``：主题切换时重设斑马纹与到期级别色。

安全边界：只读证书文件；双击某行用系统文件管理器打开证书所在目录。
"""
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from core import process_utils as pu
from core import run_log
from core.i18n import t
from core.plugins.cert_watch import watch
from .. import theme
from ..window_utils import rearm_poll

#: 自动刷新最小间隔（秒）
AUTO_INTERVAL = 300.0
_LEVEL_TAGS = {"expired": "error", "critical": "error", "warn": "warn",
               "ok": "ok"}


class CertWatchPanel(ttk.Frame):
    """证书到期面板。"""

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
        self._v_total = tk.StringVar(value="—")
        self._v_expired = tk.StringVar(value="—")
        self._v_warn = tk.StringVar(value="—")
        self._v_nearest = tk.StringVar(value="—")
        self._build()
        self.refresh()
        self._poll()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text=t("证书到期"),
                  style="Title.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self._status,
                  style="CardDim.TLabel").pack(side="left",
                                             padx=(theme.PAD_MD, 0))
        ttk.Button(bar, text=t("刷新"), command=self.refresh,
                   style="Toolbar.TButton").pack(side="right")

        self._filter_entry = ttk.Entry(bar, textvariable=self._filter_var,
                                       width=22)
        self._filter_entry.pack(side="right", padx=(0, theme.PAD_SM))
        self._filter_entry.bind("<Return>", lambda e: self.refresh())
        ttk.Label(bar, text=t("站点 / 路径过滤"),
                  style="CardDim.TLabel").pack(side="right", padx=(0, 4))

        cards = ttk.Frame(self)
        cards.pack(fill="x", pady=(theme.PAD_MD, 0))
        for title, var in ((t("证书"), self._v_total),
                           (t("已过期"), self._v_expired),
                           (t("30 天内到期"), self._v_warn),
                           (t("最近到期"), self._v_nearest)):
            self._card(cards, title, var)

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, pady=(theme.PAD_MD, 0))
        body = ttk.Frame(wrap)
        body.pack(side="top", fill="both", expand=True)
        heads = (t("站点"), t("到期时间"), t("剩余天数"), t("状态"),
                 t("证书路径"))
        widths = (170, 130, 80, 110, 430)
        self._tree = ttk.Treeview(body, columns=heads, show="headings",
                                  selectmode="browse")
        for col, width in zip(heads, widths):
            self._tree.heading(col, text=col)
            self._tree.column(col, width=width, anchor="w", stretch=False)
        sb = ttk.Scrollbar(body, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._tree.bind("<Double-1>", self._on_open)

        ttk.Label(self, text=t("只读：从 vhost 的 ssl_certificate 读路径，"
                               "用系统 openssl 取到期时间；不续期、不改配置，"
                               "双击打开证书所在目录"),
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
        self._status.set(t("正在读取证书…"))
        threading.Thread(target=self._worker,
                         args=(self._filter_var.get().strip(),),
                         daemon=True).start()

    def _worker(self, only: str) -> None:
        try:
            data = watch.report(self.config, only=only)
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
                    run_log.error("plugin", t("证书列表渲染失败：{err}", err=e))
            else:
                self._busy = False
                self._status.set(t("读取失败"))
                run_log.error("plugin", t("证书读取失败：{err}", err=msg[1]))
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
        tree.tag_configure("ok", foreground=theme.OK)
        tree.tag_configure("warn", foreground=theme.WARN)
        tree.tag_configure("error", foreground=theme.ERR)

    def _render(self) -> None:
        d = self._data
        self._v_total.set(str(d.get("total") or 0))
        self._v_expired.set(str(d.get("expired") or 0))
        self._v_warn.set(str(int(d.get("warn") or 0)
                             + int(d.get("critical") or 0)))
        nearest = d.get("nearest") or {}
        self._v_nearest.set(
            t("{days} 天", days=nearest["days_left"]) if nearest else "—")

        error = str(d.get("error") or "")
        if error:
            self._status.set(t(watch.ERROR_MESSAGES.get(error, "读取失败")))
        else:
            self._status.set(t("{n} 张证书 · 已过期 {expired} · "
                               "30 天内到期 {warn}",
                               n=d.get("total") or 0,
                               expired=d.get("expired") or 0,
                               warn=int(d.get("warn") or 0)
                               + int(d.get("critical") or 0)))

        self._tree.delete(*self._tree.get_children())
        self._rows.clear()
        for i, it in enumerate(d.get("items") or []):
            level = str(it.get("level") or "ok")
            days = it.get("days_left")
            item_id = self._tree.insert(
                "", "end",
                values=(it.get("site") or "—",
                        it.get("not_after_text") or "—",
                        t("{days} 天", days=days),
                        t(watch.LEVEL_MESSAGES.get(level, "正常")),
                        it.get("path") or "—"),
                tags=("odd" if i % 2 else "even",
                      _LEVEL_TAGS.get(level, "ok")))
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
        folder = os.path.dirname(path) or "."
        try:
            pu.open_path(folder)
        except Exception as e:  # noqa: BLE001
            run_log.error("plugin", t("打开证书目录失败：{err}", err=e))
            return
        self.notify(t("已打开证书目录：{path}", path=folder))
