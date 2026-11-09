# -*- coding: utf-8 -*-
"""「Git 工作区」页签：一屏看完全部项目的分支、未提交与待推送情况。

契约（主窗口 / 主题系统）：

- ``auto_refresh()``：主窗口 8 秒 tick 调用，但这里每条记录都要起一个 git 子进程，
  因此按 :data:`AUTO_INTERVAL` 秒节流，避免把磁盘和 CPU 拖满；
- ``refresh_theme()``：主题切换时重设 Treeview 斑马纹与状态色。

安全边界（ADR 21）：只读 ``git status`` / ``git log``，不 fetch、不 pull、不提交；
双击 / 右键只打开终端或目录，不做任何仓库写操作。
"""
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from core import process_utils as pu
from core import run_log
from core.i18n import t
from core.plugins import settings as plugin_settings
from core.plugins.git_status import MANIFEST, repos
from .. import theme
from ..window_utils import rearm_poll

#: 自动刷新最小间隔（秒）：一次刷新 = N 个项目 × 2 条 git 子进程
AUTO_INTERVAL = 60.0
_STATE_TAGS = {"clean": "ok", "dirty": "warn", "ahead": "warn",
               "none": "even", "missing": "err", "error": "err"}


class GitStatusPanel(ttk.Frame):
    """Git 工作区看板。"""

    def __init__(self, master, config, notify):
        super().__init__(master, padding=8)
        self.config = config
        self.notify = notify
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._data: dict = {}
        self._rows: dict[str, dict] = {}
        self._last_scan = 0.0
        settings = plugin_settings.merged(config, MANIFEST)
        try:
            self._timeout = max(1.0, min(60.0, float(settings.get("timeout") or 6)))
        except (TypeError, ValueError):
            self._timeout = 6.0
        self._status = tk.StringVar(value=t("就绪"))
        self._filter_var = tk.StringVar()
        self._v_total = tk.StringVar(value="—")
        self._v_dirty = tk.StringVar(value="—")
        self._v_ahead = tk.StringVar(value="—")
        self._v_behind = tk.StringVar(value="—")
        self._v_none = tk.StringVar(value="—")
        self._build()
        self.refresh()
        self._poll()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text=t("Git 工作区"),
                  style="Title.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self._status,
                  style="CardDim.TLabel").pack(side="left",
                                             padx=(theme.PAD_MD, 0))
        ttk.Button(bar, text=t("刷新"), command=self.refresh,
                   style="Toolbar.TButton").pack(side="right")

        self._filter_entry = ttk.Entry(bar, textvariable=self._filter_var,
                                       width=18)
        self._filter_entry.pack(side="right", padx=(0, theme.PAD_SM))
        self._filter_entry.bind("<Return>", lambda e: self.refresh())
        ttk.Label(bar, text=t("项目过滤"),
                  style="CardDim.TLabel").pack(side="right", padx=(0, 4))

        cards = ttk.Frame(self)
        cards.pack(fill="x", pady=(theme.PAD_MD, 0))
        for title, var in ((t("项目数"), self._v_total),
                           (t("未提交"), self._v_dirty),
                           (t("待推送"), self._v_ahead),
                           (t("待拉取"), self._v_behind),
                           (t("非仓库"), self._v_none)):
            self._card(cards, title, var)

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, pady=(theme.PAD_MD, 0))
        body = ttk.Frame(wrap)
        body.pack(side="top", fill="both", expand=True)
        heads = (t("项目"), t("分支"), t("变更"), t("领先"), t("落后"),
                 t("最后提交"), t("说明"))
        widths = (180, 140, 70, 60, 60, 130, 300)
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

        self._tree.bind("<Double-1>", self._on_open_terminal)
        self._tree.bind("<Button-3>", self._on_menu)
        self._menu = tk.Menu(self, tearoff=False)

        ttk.Label(self, text=t("只读查询：git status / git log，"
                               "不 fetch、不 pull、不提交"),
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
        if time.monotonic() - self._last_scan < AUTO_INTERVAL:
            return
        self.refresh()

    def refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._last_scan = time.monotonic()
        self._status.set(t("正在查询…"))
        threading.Thread(target=self._worker,
                         args=(self._filter_var.get().strip(),
                               self._timeout), daemon=True).start()

    def _worker(self, only: str, timeout: float) -> None:
        try:
            data = repos.report(self.config, only=only, timeout=timeout)
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
                    run_log.error("plugin", t("Git 工作区渲染失败：{err}", err=e))
            else:
                self._busy = False
                self._status.set(t("查询失败"))
                run_log.error("plugin", t("Git 工作区查询失败：{err}", err=msg[1]))
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
        tree.tag_configure("err", foreground=theme.ERR)

    def _render(self) -> None:
        d = self._data
        self._v_total.set(str(d.get("total") or 0))
        self._v_dirty.set(str(d.get("dirty") or 0))
        self._v_ahead.set(str(d.get("ahead") or 0))
        self._v_behind.set(str(d.get("behind") or 0))
        self._v_none.set(str(d.get("none") or 0))

        if not d.get("git"):
            self._status.set(t("未安装 git"))
        else:
            self._status.set(t("查询完成 · {n} 个项目 · {repos} 个仓库",
                               n=d.get("total") or 0,
                               repos=d.get("repos") or 0))

        self._tree.delete(*self._tree.get_children())
        self._rows.clear()
        for i, it in enumerate(d.get("items") or []):
            state = str(it.get("status") or "none")
            item_id = self._tree.insert(
                "", "end",
                values=(it.get("name") or "",
                        self._branch_text(it),
                        self._change_text(it),
                        self._count_text(it.get("ahead"), "↑"),
                        self._count_text(it.get("behind"), "↓"),
                        it.get("last_time") or "—",
                        self._note_text(it)),
                tags=("odd" if i % 2 else "even",
                      _STATE_TAGS.get(state, "even")))
            self._rows[item_id] = it

    def _branch_text(self, it: dict) -> str:
        branch = str(it.get("branch") or "")
        if not branch:
            return "—"
        if not it.get("upstream"):
            return t("{branch}（无上游）", branch=branch)
        return branch

    def _change_text(self, it: dict) -> str:
        state = str(it.get("status") or "")
        if state == "missing":
            return t(repos.STATE_MESSAGES["missing"])
        if not it.get("is_repo"):
            return t(repos.STATE_MESSAGES["none"])
        dirty = int(it.get("dirty") or 0)
        return t("干净") if dirty == 0 else str(dirty)

    def _count_text(self, value, arrow: str) -> str:
        try:
            n = int(value or 0)
        except (TypeError, ValueError):
            n = 0
        return "—" if n <= 0 else f"{arrow}{n}"

    def _note_text(self, it: dict) -> str:
        code = str(it.get("error") or "")
        # 非仓库 / 目录缺失已由「变更」列表达，这里只补充真正的错误
        if code and code not in ("not_repo", "missing"):
            return t(repos.ERROR_MESSAGES.get(code, "查询失败"))
        return str(it.get("last_subject") or "")

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _selected(self) -> dict:
        selection = self._tree.selection()
        if not selection:
            return {}
        return self._rows.get(selection[0]) or {}

    def _on_open_terminal(self, _event=None) -> None:
        row = self._selected()
        root = str(row.get("root") or "")
        if not root:
            return
        if not pu.open_terminal(cwd=root):
            messagebox.showwarning(t("打开终端失败"), t("请检查系统终端是否可用"),
                                   parent=self)
            return
        self.notify(t("已打开终端：{path}", path=root))

    def _on_menu(self, event) -> None:
        row_id = self._tree.identify_row(event.y)
        if not row_id:
            return
        self._tree.selection_set(row_id)
        row = self._rows.get(row_id) or {}
        root = str(row.get("root") or "")
        if not root:
            return
        self._menu.delete(0, "end")
        self._menu.add_command(label=t("打开终端"),
                               command=self._on_open_terminal)
        self._menu.add_command(label=t("打开目录"),
                               command=lambda: pu.open_path(root))
        self._menu.add_command(label=t("复制路径"),
                               command=lambda: self._copy(root))
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _copy(self, text: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        self.notify(t("已复制：{text}", text=text))
