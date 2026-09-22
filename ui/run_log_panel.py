# -*- coding: utf-8 -*-
"""运行日志面板：全局运行日志（应用启停 / 服务启停结果 / 异常告警）实时查看。

数据源 ``core.run_log``（内存环形缓冲 + 落盘 ``run_log.log``）：
- 打开即回填最近若干条，之后由订阅回调（sink）实时追加；
- sink 在写入线程内触发，因此只做入队，UI 操作统一收敛到主线程 drain 循环
  （与其它面板相同的异步规范：唯一消费者 + ``winfo_exists`` 守卫 + 不可见降频）；
- 级别过滤 + 关键字过滤，选中行在下方详情区显示完整消息（多行不截断）；
- 支持复制单行、导出当前视图、用系统默认应用打开落盘日志文件。
"""
import os
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core import process_utils as pu
from core import run_log
from core.i18n import t

from . import theme

MAX_VIEW = 2000  # 视图最多保留条数（与 run_log 内存上限一致）
FILTER_ALL = "全部"
LEVEL_TEXT = {"info": "信息", "ok": "成功", "warn": "警告", "error": "错误"}


class RunLogPanel(ttk.Frame):
    """「运行日志」页签。"""

    def __init__(self, master, notify=None):
        super().__init__(master, padding=8)
        self.notify = notify or (lambda msg: None)
        self._queue: queue.Queue = queue.Queue()
        self._entries: list = []
        self._draining = False
        self._render_key: tuple[str, str] | None = None  # 上次全量重绘的过滤条件
        self._build()
        self._entries = run_log.snapshot(MAX_VIEW)
        self._render()
        run_log.add_sink(self._on_log_written)
        self._start_drain()

    # ------------------------------------------------------------------ #
    # UI 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 6))

        ttk.Label(top, text=t("级别："), style="Section.TLabel").pack(side="left")
        self.level_var = tk.StringVar(value=t(FILTER_ALL))
        self.level_cb = ttk.Combobox(top, textvariable=self.level_var,
                                     state="readonly", width=8)
        self.level_cb["values"] = [t(FILTER_ALL)] + [t(v) for v in LEVEL_TEXT.values()]
        self.level_cb.pack(side="left", padx=(0, 10))
        self.level_cb.bind("<<ComboboxSelected>>", lambda e: self._render())

        ttk.Label(top, text=t("搜索："), style="Section.TLabel").pack(side="left")
        self.filter_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.filter_var, width=18)
        entry.pack(side="left", padx=(0, 10))
        entry.bind("<KeyRelease>", lambda e: self._render())

        self.follow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text=t("自动跟随"), variable=self.follow_var).pack(
            side="left", padx=(0, 10))

        ttk.Button(top, text=t("刷新"), command=self.reload).pack(side="left", padx=(0, 4))
        ttk.Button(top, text=t("清空"), command=self.clear).pack(side="left", padx=(0, 4))
        ttk.Button(top, text=t("复制"), command=self._copy_selected).pack(side="left", padx=(0, 4))
        ttk.Button(top, text=t("导出…"), command=self._export).pack(side="left", padx=(0, 4))
        ttk.Button(top, text=t("打开日志文件"), command=self._open_file).pack(side="left")

        self.info_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.info_var, style="SubTitle.TLabel").pack(
            anchor="w", pady=(0, 4))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=("time", "level", "scope", "message"),
                                 show="headings", selectmode="browse")
        for key, text, width, stretch in (
            ("time", t("时间"), 145, False),
            ("level", t("级别"), 60, False),
            ("scope", t("来源"), 90, False),
            ("message", t("内容"), 520, True),
        ):
            self.tree.heading(key, text=text)
            self.tree.column(key, width=width, stretch=stretch, anchor="w")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_detail())
        self.tree.bind("<Double-1>", lambda e: self._copy_selected())

        detail_box = ttk.LabelFrame(self, text=t("详情"), padding=6)
        detail_box.pack(fill="x", pady=(6, 0))
        self.detail = tk.Text(detail_box, height=5, wrap="word", font=theme.mono(),
                              background=theme.log_bg, foreground=theme.log_fg,
                              relief="flat", padx=8, pady=6, state="disabled")
        dsb = ttk.Scrollbar(detail_box, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=dsb.set)
        self.detail.pack(side="left", fill="both", expand=True)
        dsb.pack(side="right", fill="y")
        self._apply_tags()

    def _apply_tags(self) -> None:
        """Treeview 行的级别配色（tag 无法随主题自动刷新，切换时由本方法重设）。"""
        self.tree.tag_configure("info", foreground=theme.text)
        self.tree.tag_configure("ok", foreground=theme.ok)
        self.tree.tag_configure("warn", foreground=theme.warn)
        self.tree.tag_configure("error", foreground=theme.err)
        self.tree.tag_configure("odd", background=theme.row_alt)
        self.tree.tag_configure("even", background=theme.card_bg)
        try:
            self.detail.configure(background=theme.log_bg, foreground=theme.log_fg)
        except tk.TclError:
            pass

    def refresh_theme(self) -> None:
        self._apply_tags()

    def destroy(self) -> None:
        run_log.remove_sink(self._on_log_written)
        super().destroy()

    # ------------------------------------------------------------------ #
    # 数据同步
    # ------------------------------------------------------------------ #
    def _on_log_written(self, entry) -> None:
        """sink 回调（运行日志写入线程）：只入队，UI 操作留给主线程 drain。"""
        self._queue.put(("entry", entry))

    def _start_drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        self.after(80, self._drain)

    def _drain(self) -> None:
        if not self.winfo_exists():  # 面板已销毁：停止轮询
            self._draining = False
            return
        added: list = []
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "entry":
                    added.append(payload)
        except queue.Empty:
            pass
        if added:
            self._entries.extend(added)
            trimmed = len(self._entries) > MAX_VIEW
            if trimmed:
                self._entries = self._entries[-MAX_VIEW:]
            # 过滤条件未变且未截断 → 只追加新行；否则全量重绘
            if not trimmed and self._filter_key() == self._render_key:
                self._append_rows(added)
            else:
                self._render()
        # 不可见页签降频：drain 只是空转取消息，不必跟着 80ms 跑
        self.after(80 if self.winfo_ismapped() else 400, self._drain)

    def reload(self) -> None:
        """重新回填内存日志（面板订阅期间漏掉的消息也一并补齐）。"""
        self._entries = run_log.snapshot(MAX_VIEW)
        self._render()

    def clear(self) -> None:
        """清空内存日志（落盘文件保留，供事后排查）。"""
        if not messagebox.askyesno(
                t("清空运行日志"),
                t("确定清空内存中的运行日志？（落盘文件会保留）"), parent=self):
            return
        run_log.clear()
        self._entries = []
        self._render()

    # ------------------------------------------------------------------ #
    # 渲染 / 过滤
    # ------------------------------------------------------------------ #
    def _level_code(self) -> str:
        """当前级别下拉对应的 level（「全部」返回空串）。"""
        name = self.level_var.get()
        for code, label in LEVEL_TEXT.items():
            if t(label) == name:
                return code
        return ""

    def _matches(self, entry, level: str, keyword: str) -> bool:
        if level and entry.level != level:
            return False
        if keyword and keyword not in (entry.message + " " + entry.scope).lower():
            return False
        return True

    def _filter_key(self) -> tuple[str, str]:
        """当前过滤条件（级别, 关键字）；变化时必须全量重绘。"""
        return self._level_code(), self.filter_var.get().strip().lower()

    def _visible_entries(self) -> list:
        level, keyword = self._filter_key()
        return [e for e in self._entries if self._matches(e, level, keyword)]

    def _insert_rows(self, rows: list, start_index: int) -> None:
        """按序插入行；奇偶底色从 start_index 起算，增量追加时与全量口径一致。"""
        for offset, entry in enumerate(rows):
            idx = start_index + offset
            self.tree.insert(
                "", "end", iid=str(entry.seq),
                values=(entry.time_text, t(LEVEL_TEXT[entry.level]), entry.scope,
                        entry.message.replace("\n", " ⏎ ")),
                tags=(entry.level, "odd" if idx % 2 else "even"))

    def _update_info(self, shown: list) -> None:
        """刷新顶部统计（条数 / 错误 / 警告 / 落盘路径）。"""
        errs = sum(1 for e in shown if e.level == "error")
        warns = sum(1 for e in shown if e.level == "warn")
        parts = [t("共 {n} 条", n=len(shown))]
        if errs:
            parts.append(t("错误 {n}", n=errs))
        if warns:
            parts.append(t("警告 {n}", n=warns))
        parts.append(t("日志文件：{path}", path=run_log.log_path()))
        self.info_var.set(" · ".join(parts))
        if not shown:
            self._set_detail("")

    def _render(self) -> None:
        """按级别 + 关键字过滤全量重绘列表与统计信息。"""
        level, keyword = self._filter_key()
        self._render_key = (level, keyword)
        shown = [e for e in self._entries if self._matches(e, level, keyword)]
        self.tree.delete(*self.tree.get_children())
        self._insert_rows(shown, 1)
        if self.follow_var.get() and shown:
            self.tree.see(str(shown[-1].seq))
        self._update_info(shown)

    def _append_rows(self, entries: list) -> None:
        """增量追加新到条目（过滤条件未变时），只插入命中过滤条件的行。"""
        level, keyword = self._render_key or self._filter_key()
        new_rows = [e for e in entries if self._matches(e, level, keyword)]
        if new_rows:
            self._insert_rows(new_rows, len(self.tree.get_children()) + 1)
            if self.follow_var.get():
                self.tree.see(str(new_rows[-1].seq))
        self._update_info(self._visible_entries())

    def _selected_entry(self):
        sel = self.tree.selection()
        if not sel:
            return None
        seq = sel[0]
        return next((e for e in self._entries if str(e.seq) == seq), None)

    def _set_detail(self, text: str) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if text:
            self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def _show_detail(self) -> None:
        entry = self._selected_entry()
        self._set_detail(entry.line if entry is not None else "")

    # ------------------------------------------------------------------ #
    # 操作
    # ------------------------------------------------------------------ #
    def _copy_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.notify(t("请先选择一条日志"))
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(entry.line)
        except tk.TclError:
            return
        self.notify(t("已复制到剪贴板"))

    def _export(self) -> None:
        """导出当前过滤视图为文本文件。"""
        path = filedialog.asksaveasfilename(
            parent=self, title=t("导出日志"), defaultextension=".log",
            initialfile="phpvm_run_log.log",
            filetypes=[(t("日志文件"), "*.log"), (t("所有文件"), "*.*")],
        )
        if not path:
            return
        text = "\n".join(e.line for e in self._visible_entries())
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError as e:
            messagebox.showerror(t("导出失败"), t("导出失败：{msg}", msg=e), parent=self)
            return
        self.notify(t("已导出到 {path}", path=path))

    def _open_file(self) -> None:
        """用系统默认应用打开落盘日志文件（不存在时先创建空文件）。"""
        path = run_log.log_path()
        if not os.path.exists(path):
            try:
                open(path, "a", encoding="utf-8").close()
            except OSError as e:
                messagebox.showerror(t("打开日志文件"), str(e), parent=self)
                return
        pu.open_path(path)
