# -*- coding: utf-8 -*-
"""「端口占用」页签：列出本机 TCP 监听端口与占用进程（只读）。

契约（主窗口 / 主题系统）：

- ``auto_refresh()``：主窗口 8 秒 tick 调用，但采集要跑 ``netstat`` / ``lsof``
  子进程，因此内部按 :data:`AUTO_INTERVAL` 秒节流；
- ``refresh_theme()``：主题切换时重设 Treeview 斑马纹与归属色。

安全边界：面板只读展示，**不提供杀进程入口**；双击某行仅把端口号复制到剪贴板。
"""
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from core import run_log
from core.i18n import t
from core.plugins import settings as plugin_settings
from core.plugins.port_scan import MANIFEST, scan
from .. import theme
from ..window_utils import rearm_poll

#: 自动刷新最小间隔（秒）：每次刷新要起 1–2 个子进程，不能跟 8s tick 同频
AUTO_INTERVAL = 60.0
_KIND_TAGS = {"nginx": "svc", "php": "svc", "redis": "svc", "mysql": "svc"}


class PortScanPanel(ttk.Frame):
    """端口占用面板。"""

    def __init__(self, master, config, notify):
        super().__init__(master, padding=8)
        self.config = config
        self.notify = notify
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._data: dict = {}
        self._rows: dict[str, dict] = {}
        self._last_scan = 0.0
        plugin_settings.merged(config, MANIFEST)
        self._timeout = float(scan.DEFAULT_TIMEOUT)
        self._status = tk.StringVar(value=t("就绪"))
        self._filter_var = tk.StringVar()
        self._kind_var = tk.StringVar(value=t("全部归属"))
        self._v_ports = tk.StringVar(value="—")
        self._v_services = tk.StringVar(value="—")
        self._v_other = tk.StringVar(value="—")
        self._v_conflict = tk.StringVar(value="—")
        self._kind_labels = {t("全部归属"): ""}
        for key in scan.KINDS:
            self._kind_labels[t(scan.KIND_MESSAGES[key])] = key
        self._build()
        self.refresh()
        self._poll()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text=t("端口占用"), style="Title.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self._status,
                  style="CardDim.TLabel").pack(side="left",
                                             padx=(theme.PAD_MD, 0))
        ttk.Button(bar, text=t("刷新"), command=self.refresh,
                   style="Toolbar.TButton").pack(side="right")

        self._kind_combo = ttk.Combobox(
            bar, textvariable=self._kind_var, values=list(self._kind_labels),
            width=10, state="readonly")
        self._kind_combo.pack(side="right", padx=(0, theme.PAD_SM))
        self._kind_combo.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Label(bar, text=t("归属"),
                  style="CardDim.TLabel").pack(side="right", padx=(0, 4))

        self._filter_entry = ttk.Entry(bar, textvariable=self._filter_var,
                                       width=18)
        self._filter_entry.pack(side="right", padx=(0, theme.PAD_SM))
        self._filter_entry.bind("<Return>", lambda e: self.refresh())
        ttk.Label(bar, text=t("端口 / 进程过滤"),
                  style="CardDim.TLabel").pack(side="right", padx=(0, 4))

        cards = ttk.Frame(self)
        cards.pack(fill="x", pady=(theme.PAD_MD, 0))
        for title, var in ((t("监听端口"), self._v_ports),
                           (t("WNMP 服务"), self._v_services),
                           (t("其他程序"), self._v_other),
                           (t("端口冲突"), self._v_conflict)):
            self._card(cards, title, var)

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, pady=(theme.PAD_MD, 0))
        body = ttk.Frame(wrap)
        body.pack(side="top", fill="both", expand=True)
        heads = (t("端口"), t("进程"), t("PID"), t("归属"),
                 t("监听地址"), t("说明"))
        widths = (70, 180, 70, 90, 220, 220)
        self._tree = ttk.Treeview(body, columns=heads, show="headings",
                                  selectmode="browse")
        for col, width in zip(heads, widths):
            self._tree.heading(col, text=col)
            self._tree.column(col, width=width, anchor="w", stretch=False)
        sb = ttk.Scrollbar(body, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._tree.bind("<Double-1>", self._on_copy)

        ttk.Label(self, text=t("只读列表：归属按进程名推断；双击一行复制端口号，"
                               "本页不杀进程"),
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
    # 采集（worker + 队列回主线程）
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
        self._status.set(t("正在扫描端口…"))
        threading.Thread(target=self._worker,
                         args=(self._filter_var.get().strip(),
                               self._kind_labels.get(self._kind_var.get(), "")),
                         daemon=True).start()

    def _worker(self, only: str, kind: str) -> None:
        try:
            data = scan.report(only=only, kind=kind)
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
                except Exception as e:  # noqa: BLE001 - 单次渲染失败不中断轮询
                    run_log.error("plugin", t("端口占用渲染失败：{err}", err=e))
            else:
                self._busy = False
                self._status.set(t("扫描失败"))
                run_log.error("plugin", t("端口占用扫描失败：{err}", err=msg[1]))
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
        tree.tag_configure("svc", foreground=theme.OK)
        tree.tag_configure("conflict", foreground=theme.WARN)

    def _render(self) -> None:
        d = self._data
        error = str(d.get("error") or "")
        self._v_ports.set(str(d.get("ports") or 0))
        self._v_services.set(str(d.get("services") or 0))
        self._v_other.set(str(d.get("other") or 0))
        self._v_conflict.set(str(len(d.get("conflicts") or [])))
        if error:
            self._status.set(t(scan.ERROR_MESSAGES.get(error, "端口查询失败")))
        else:
            self._status.set(t("扫描完成 · {ports} 个端口 · {n} 条记录",
                               ports=d.get("ports") or 0,
                               n=d.get("total") or 0))

        self._tree.delete(*self._tree.get_children())
        self._rows.clear()
        conflicts = set(d.get("conflicts") or [])
        for i, it in enumerate(d.get("items") or []):
            kind = str(it.get("kind") or "other")
            port = int(it.get("port") or 0)
            tags = ["odd" if i % 2 else "even"]
            tags.append(_KIND_TAGS.get(kind, "conflict" if port in conflicts
                                       else "even"))
            item_id = self._tree.insert(
                "", "end",
                values=(port,
                        it.get("process") or "—",
                        it.get("pid") or "—",
                        t(scan.KIND_MESSAGES.get(kind, "其他程序")),
                        it.get("addr") or "—",
                        t("同端口被多个进程监听") if port in conflicts else ""),
                tags=tuple(tags))
            self._rows[item_id] = it

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_copy(self, _event=None) -> None:
        selection = self._tree.selection()
        if not selection:
            return
        row = self._rows.get(selection[0]) or {}
        port = str(row.get("port") or "")
        if not port:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(port)
        except tk.TclError:
            return
        self.notify(t("已复制端口号：{port}", port=port))
