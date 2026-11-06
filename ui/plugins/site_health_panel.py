# -*- coding: utf-8 -*-
"""「站点巡检」页签：一次性探测 nginx 站点的 HTTP(S) 可用性。

契约（主窗口 / 主题系统）：

- ``auto_refresh()``：主窗口 8 秒 tick 调用，但探测是**真实网络请求**，因此面板内部
  按 :data:`AUTO_INTERVAL` 秒节流，避免 8 秒一轮把本机站点打满；
- ``refresh_theme()``：主题切换时重设 Treeview 斑马纹与状态色。

安全边界：探测地址只由 ``core.plugins.site_health.probe`` 从 vhost ``server_name``
生成（不接受任意 URL），只发 ``GET /``、不跟随重定向、不带 Cookie、不走系统代理；
面板本身不写文件、不落库。双击某行只是用系统浏览器打开该站点地址。
"""
import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import ttk

from core import run_log
from core.i18n import t
from core.plugins import settings as plugin_settings
from core.plugins.site_health import MANIFEST, probe
from .. import theme
from ..window_utils import rearm_poll

#: 自动巡检最小间隔（秒）：主窗口 tick 是 8 秒，这里独立节流
AUTO_INTERVAL = 60.0
_SCHEMES = ("http", "https")
_STATE_TAGS = {"ok": "ok", "warn": "warn", "down": "down"}


def _fmt_bytes(n) -> str:
    try:
        size = float(n or 0)
    except (TypeError, ValueError):
        return "—"
    if size <= 0:
        return "—"
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} MB"


class SiteHealthPanel(ttk.Frame):
    """站点巡检面板。"""

    def __init__(self, master, config, notify):
        super().__init__(master, padding=8)
        self.config = config
        self.notify = notify
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._data: dict = {}
        self._rows: dict[str, dict] = {}
        self._last_probe = 0.0
        settings = plugin_settings.merged(config, MANIFEST)
        self._timeout = max(1.0, min(30.0, float(settings.get("timeout") or 5)))
        self._status = tk.StringVar(value=t("就绪"))
        self._scheme_var = tk.StringVar(
            value="https" if str(settings.get("scheme")) == "https" else "http")
        self._filter_var = tk.StringVar()
        self._v_total = tk.StringVar(value="—")
        self._v_up = tk.StringVar(value="—")
        self._v_bad = tk.StringVar(value="—")
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
        ttk.Label(bar, text=t("站点巡检"),
                  style="Title.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self._status,
                  style="CardDim.TLabel").pack(side="left",
                                             padx=(theme.PAD_MD, 0))
        ttk.Button(bar, text=t("刷新"), command=self.refresh,
                   style="Toolbar.TButton").pack(side="right")

        self._scheme_combo = ttk.Combobox(
            bar, textvariable=self._scheme_var, values=list(_SCHEMES),
            width=7, state="readonly")
        self._scheme_combo.pack(side="right", padx=(0, theme.PAD_SM))
        self._scheme_combo.bind("<<ComboboxSelected>>", self._on_scheme)
        ttk.Label(bar, text=t("协议"),
                  style="CardDim.TLabel").pack(side="right", padx=(0, 4))

        self._filter_entry = ttk.Entry(bar, textvariable=self._filter_var,
                                       width=18)
        self._filter_entry.pack(side="right", padx=(0, theme.PAD_SM))
        self._filter_entry.bind("<Return>", lambda e: self.refresh())
        ttk.Label(bar, text=t("域名过滤"),
                  style="CardDim.TLabel").pack(side="right", padx=(0, 4))

        cards = ttk.Frame(self)
        cards.pack(fill="x", pady=(theme.PAD_MD, 0))
        for title, var in ((t("站点数"), self._v_total),
                           (t("正常"), self._v_up),
                           (t("异常"), self._v_bad),
                           (t("平均耗时"), self._v_avg)):
            self._card(cards, title, var)

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, pady=(theme.PAD_MD, 0))
        body = ttk.Frame(wrap)
        body.pack(side="top", fill="both", expand=True)
        heads = (t("域名"), t("结果"), t("状态码"), t("耗时"), t("大小"),
                 t("证书"), t("PHP"), t("说明"))
        widths = (190, 70, 60, 70, 70, 80, 70, 260)
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

        ttk.Label(self, text=t("只探测 nginx vhost 里的域名：GET /，"
                               "不跟随重定向、不带 Cookie、不经系统代理"),
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
    # 数据刷新（worker + 队列回主线程）
    # ------------------------------------------------------------------ #
    def auto_refresh(self) -> None:
        if self._busy:
            return
        if time.monotonic() - self._last_probe < AUTO_INTERVAL:
            return
        self.refresh()

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._last_probe = time.monotonic()
        self._status.set(t("正在巡检…"))
        threading.Thread(
            target=self._worker,
            args=(self._scheme_var.get(), self._filter_var.get().strip(),
                  self._timeout),
            daemon=True).start()

    def _worker(self, scheme: str, only: str, timeout: float) -> None:
        try:
            data = probe.report(self.config, scheme=scheme, timeout=timeout,
                                only=only)
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
                    run_log.error("plugin", t("站点巡检渲染失败：{err}", err=e))
            else:
                self._busy = False
                self._status.set(t("巡检失败"))
                run_log.error("plugin", t("站点巡检失败：{err}", err=msg[1]))
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
        tree.tag_configure("down", foreground=theme.ERR)

    def _render(self) -> None:
        d = self._data
        total = int(d.get("total") or 0)
        self._v_total.set(str(total))
        self._v_up.set(str(d.get("up") or 0))
        self._v_bad.set(str(int(d.get("warn") or 0) + int(d.get("down") or 0)))
        avg = d.get("avg_ms")
        self._v_avg.set("—" if avg is None else f"{avg} ms")
        self._status.set(t("巡检完成 · {scheme} · 超时 {timeout}s · {n} 个站点",
                           scheme=d.get("scheme") or "http",
                           timeout=self._timeout, n=total))

        self._tree.delete(*self._tree.get_children())
        self._rows.clear()
        for i, it in enumerate(d.get("sites") or []):
            state = str(it.get("state") or "down")
            item_id = self._tree.insert(
                "", "end",
                values=(it.get("domain") or "",
                        t("正常") if state == "ok" else (
                            t("异常") if state == "warn" else t("不可用")),
                        "—" if it.get("status") is None else it["status"],
                        f'{it.get("ms") or 0} ms',
                        _fmt_bytes(it.get("bytes")),
                        self._cert_text(it),
                        it.get("php_version") or "—",
                        self._note_text(it)),
                tags=("odd" if i % 2 else "even", _STATE_TAGS.get(state, "down")))
            self._rows[item_id] = it

    def _cert_text(self, it: dict) -> str:
        if it.get("scheme") != "https":
            return "—"
        if it.get("insecure"):
            return t("自签")
        days = it.get("cert_days")
        if days is None:
            return "—"
        if days < 0:
            return t("已过期")
        return t("{n} 天", n=days)

    def _note_text(self, it: dict) -> str:
        code = str(it.get("error") or "")
        if code:
            return t(probe.ERROR_MESSAGES.get(code, "网络错误"))
        location = str(it.get("location") or "")
        if location:
            return t("跳转到 {url}", url=location)
        return ""

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_scheme(self, _event=None) -> None:
        self.refresh()

    def _on_open(self, _event=None) -> None:
        selection = self._tree.selection()
        if not selection:
            return
        row = self._rows.get(selection[0]) or {}
        url = str(row.get("url") or "")
        if not url.startswith(("http://", "https://")):
            return
        try:
            webbrowser.open(url)
        except Exception as e:  # noqa: BLE001
            run_log.error("plugin", t("打开站点失败：{err}", err=e))
            return
        self.notify(t("已在浏览器打开：{url}", url=url))
