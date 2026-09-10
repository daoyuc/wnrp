# -*- coding: utf-8 -*-
"""SQLite 数据库页：选择数据库文件 → 查看表 / 视图与结构 → 执行只读查询。

当前只提供「基础查询」能力（只读，不做增删改）：

- 顶部：数据库文件下拉（自动发现环境根与站点目录下的 *.db/*.sqlite/*.sqlite3）
  + 打开 / 浏览… / 刷新 / 关闭；上次打开的库下次启动自动带回；
- 左侧：表 / 视图清单（单击看结构、双击生成 SELECT）+ 结构（列名 / 类型 / 约束 / 默认值）；
- 右侧：SQL 编辑框（Ctrl/Cmd+Enter 执行）+ 结果表格（最多 500 行，超时自动中断）；
- 底部：状态 + 「执 行」「清空」按钮（固定底部，窗口压小也可见）。

所有数据库操作在工作线程执行，经 queue 回主线程渲染，避免卡界面。
"""
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.i18n import t
from core.sqlite_manager import QUERY_LIMIT, SqliteManager, format_value, quote_ident
from .theme import CARD_BG, ERR, FONT, OK, TEXT, TEXT_DIM


class SqlitePanel(ttk.Frame):
    """SQLite 只读查询面板。"""

    def __init__(self, master, sqlite_mgr: SqliteManager, vhost_mgr, notify):
        super().__init__(master, padding=8)
        self.mgr = sqlite_mgr
        self.vhost_mgr = vhost_mgr
        self.notify = notify
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._busy = False
        self._files: list = []
        self._tables: list = []
        self._pending_open = ""
        self._deferred = None  # 忙时挂起的操作（job, tag），当前任务结束后自动接续
        self._last_sql = ""    # 最近一次查询 SQL（分页与导出用）
        self._offset = 0       # 当前页起始行
        self._has_more = False # 是否还有下一页
        self._build()
        self.refresh_files(open_last=True)

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        # 底部状态 + 操作按钮：先 pack 到底部，窗口被压小时按钮仍可见
        bar = ttk.Frame(self)
        bar.pack(side="bottom", fill="x", pady=(6, 0))
        self.btn_exec = ttk.Button(bar, text=t("执 行"), style="Accent.TButton",
                                   command=self.execute)
        self.btn_exec.pack(side="right")
        ttk.Button(bar, text=t("清空"), command=self.clear_all).pack(
            side="right", padx=(0, 6))
        ttk.Button(bar, text=t("导出 CSV"), command=self.export_csv).pack(
            side="right", padx=(0, 6))
        # 结果分页（每页 QUERY_LIMIT 行）
        self.btn_prev = ttk.Button(bar, text=t("◀ 上一页"), state="disabled",
                                   command=lambda: self._goto_page(-1))
        self.btn_prev.pack(side="right", padx=(0, 6))
        self.btn_next = ttk.Button(bar, text=t("下一页 ▶"), state="disabled",
                                   command=lambda: self._goto_page(1))
        self.btn_next.pack(side="right", padx=(0, 6))
        self.state_label = ttk.Label(bar, text=t("未选择数据库文件。"),
                                     foreground=TEXT_DIM, font=(FONT, 8))
        self.state_label.pack(side="left")

        # 顶部：数据库文件选择
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 4))
        ttk.Label(top, text=t("数据库文件："), style="Section.TLabel").pack(side="left")
        self.file_var = tk.StringVar()
        self.file_cb = ttk.Combobox(top, textvariable=self.file_var, width=46)
        self.file_cb.pack(side="left", padx=(0, 6))
        self.file_cb.bind("<Return>", lambda e: self.open_db())
        self.file_cb.bind("<<ComboboxSelected>>", lambda e: self.open_db())
        ttk.Button(top, text=t("打开"), command=self.open_db).pack(side="left", padx=(0, 4))
        ttk.Button(top, text=t("浏览…"), command=self._browse).pack(side="left", padx=(0, 4))
        ttk.Button(top, text=t("刷新"), command=self.refresh_files).pack(
            side="left", padx=(0, 4))
        ttk.Button(top, text=t("关闭"), command=self.close_db).pack(side="left")

        ttk.Label(
            self,
            text=t("提示：只读查询模式，仅支持 SELECT / PRAGMA / EXPLAIN / WITH 等语句；"
                   "双击左侧表名可生成查询语句。"),
            foreground=TEXT_DIM, font=(FONT, 8), wraplength=900, justify="left",
        ).pack(anchor="w", pady=(0, 6))

        main = ttk.Frame(self)
        main.pack(fill="both", expand=True)

        # 左侧：表 / 视图 + 结构
        left = ttk.Frame(main, width=340)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        self.table_box = ttk.LabelFrame(left, text=t("表 / 视图（{n}）", n=0), padding=6)
        self.table_box.pack(fill="both", expand=True)
        self.table_tree = ttk.Treeview(self.table_box, columns=("name", "kind"),
                                       show="headings", selectmode="browse", height=8)
        self.table_tree.heading("name", text=t("名称"))
        self.table_tree.heading("kind", text=t("类型"))
        self.table_tree.column("name", width=205, anchor="w", minwidth=90)
        self.table_tree.column("kind", width=80, anchor="center", minwidth=50, stretch=False)
        tsb = ttk.Scrollbar(self.table_box, orient="vertical", command=self.table_tree.yview)
        self.table_tree.configure(yscrollcommand=tsb.set)
        self.table_tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.table_tree.bind("<<TreeviewSelect>>", lambda e: self._on_table_select())
        self.table_tree.bind("<Double-1>", lambda e: self._gen_select())

        self.schema_box = ttk.LabelFrame(left, text=t("结构"), padding=6)
        self.schema_box.pack(fill="both", expand=True, pady=(6, 0))
        self.schema_info = ttk.Label(self.schema_box, text="", foreground=TEXT_DIM,
                                     font=(FONT, 8))
        self.schema_info.pack(anchor="w", pady=(0, 4))
        self.schema_tree = ttk.Treeview(
            self.schema_box, columns=("col", "type", "key", "dflt"),
            show="headings", selectmode="browse", height=6)
        for cid, text, width, anchor, stretch in (
            ("col", t("列名"), 130, "w", True),
            ("type", t("类型"), 90, "w", False),
            ("key", t("约束"), 80, "center", False),
            ("dflt", t("默认值"), 90, "w", False),
        ):
            self.schema_tree.heading(cid, text=text)
            self.schema_tree.column(cid, width=width, anchor=anchor,
                                    minwidth=50, stretch=stretch)
        ssb = ttk.Scrollbar(self.schema_box, orient="vertical",
                            command=self.schema_tree.yview)
        self.schema_tree.configure(yscrollcommand=ssb.set)
        self.schema_tree.pack(side="left", fill="both", expand=True)
        ssb.pack(side="right", fill="y")

        # 右侧：SQL 编辑 + 结果
        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))

        sql_box = ttk.LabelFrame(right, text=t("SQL 查询"), padding=6)
        sql_box.pack(fill="x")
        self.sql_text = tk.Text(sql_box, height=5, wrap="none", font=("Consolas", 9),
                                background=CARD_BG, foreground=TEXT, relief="flat",
                                padx=8, pady=6, insertbackground=TEXT)
        ssvb = ttk.Scrollbar(sql_box, orient="vertical", command=self.sql_text.yview)
        self.sql_text.configure(yscrollcommand=ssvb.set)
        self.sql_text.pack(side="left", fill="both", expand=True)
        ssvb.pack(side="right", fill="y")
        for seq in ("<Control-Return>", "<Command-Return>"):
            self.sql_text.bind(seq, lambda e: self._exec_shortcut())

        res_box = ttk.LabelFrame(right, text=t("结果"), padding=6)
        res_box.pack(fill="both", expand=True, pady=(6, 0))
        wrap = ttk.Frame(res_box)
        wrap.pack(fill="both", expand=True)
        self.result_tree = ttk.Treeview(wrap, show="headings", selectmode="browse")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.result_tree.yview)
        hsb = ttk.Scrollbar(res_box, orient="horizontal", command=self.result_tree.xview)
        self.result_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.result_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        hsb.pack(fill="x")

    # ------------------------------------------------------------------ #
    # 数据库文件
    def refresh_files(self, open_last: bool = False) -> None:
        """扫描环境根 + 站点目录下的数据库文件；open_last 时自动打开上次的库。"""
        if self._busy:
            return
        if open_last:
            self._pending_open = self.mgr.last_path()
        self._set_state(t("正在扫描数据库文件…"))

        def job():
            return self.mgr.discover(list(self.mgr.roots) + self._site_roots())

        self._run_async(job, "files")

    def _site_roots(self) -> list:
        """站点根目录（vhost 配置中的 root）：站点自带的 SQLite 库多在此。"""
        try:
            return [e.root for e in self.vhost_mgr.scan()
                    if getattr(e, "root", "") and os.path.isdir(e.root)]
        except Exception:  # noqa: BLE001
            return []

    def open_db(self, path: str | None = None) -> None:
        """打开选中的数据库文件（只读），随后刷新表清单。"""
        if self._busy:
            return
        target = (path if path is not None else self.file_var.get()).strip()
        if not target:
            self._set_state(t("未选择数据库文件。"), ERR)
            return
        self._set_state(t("正在打开…"))

        def job():
            self.mgr.open(target)
            return self.mgr.path, self.mgr.tables()

        self._run_async(job, "opened")

    def close_db(self) -> None:
        """关闭当前数据库并清空视图。"""
        self.mgr.close()
        self._render_tables([])
        self._clear_result()
        self._set_state(t("已关闭数据库"), TEXT_DIM)

    def _browse(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title=t("选择 SQLite 数据库文件"),
            filetypes=[(t("SQLite 文件"), "*.db *.sqlite *.sqlite3 *.db3"),
                       (t("全部文件"), "*.*")],
        )
        if path:
            self.file_var.set(path)
            self.open_db(path)

    # ------------------------------------------------------------------ #
    # 表 / 结构
    def _on_table_select(self) -> None:
        """选中表 → 后台读取列定义与行数。"""
        sel = self.table_tree.selection()
        if not sel or self._busy or not self.mgr.is_open:
            return
        name = sel[0]

        def job():
            return name, self.mgr.columns(name), self.mgr.count_rows(name)

        self._run_async(job, "schema")

    def _gen_select(self) -> None:
        """双击表名：生成 SELECT 语句并立即执行。"""
        sel = self.table_tree.selection()
        if not sel or self._busy:
            return
        self.sql_text.delete("1.0", "end")
        self.sql_text.insert("1.0", f"SELECT * FROM {quote_ident(sel[0])} LIMIT 200;")
        self.sql_text.focus_set()
        self.execute()

    # ------------------------------------------------------------------ #
    # 查询
    def execute(self) -> None:
        """执行 SQL 编辑框中的查询语句（从第一页开始）。"""
        if self._busy:
            return
        sql = self.sql_text.get("1.0", "end").strip()
        if not sql:
            self._set_state(t("请输入 SQL 语句后执行"), ERR)
            return
        self._last_sql = sql
        self._offset = 0
        self._set_state(t("正在执行…"))

        def job():
            return self.mgr.query(sql, limit=QUERY_LIMIT, offset=0)

        self._run_async(job, "query")

    def _goto_page(self, delta: int) -> None:
        """翻页：delta = +1 下一页 / -1 上一页（重跑同一条 SQL + 新 offset）。"""
        if self._busy or not self._last_sql:
            return
        new_offset = max(0, self._offset + delta * QUERY_LIMIT)
        if delta < 0 and self._offset == 0:
            return
        self._offset = new_offset
        self._set_state(t("正在执行…"))
        sql, offset = self._last_sql, self._offset

        def job():
            return self.mgr.query(sql, limit=QUERY_LIMIT, offset=offset)

        self._run_async(job, "query")

    def export_csv(self) -> None:
        """把当前结果导出为 CSV（UTF-8 BOM，便于 Excel 直接打开）。"""
        cols = self._current_columns()
        rows = self._current_rows()
        if not cols:
            messagebox.showinfo(t("导出 CSV"), t("当前没有可导出的结果。"), parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title=t("导出 CSV"), defaultextension=".csv",
            initialfile="phpvm_query_result.csv",
            filetypes=[("CSV", "*.csv"), (t("所有文件"), "*.*")],
        )
        if not path:
            return
        try:
            import csv

            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(cols)
                writer.writerows(rows)
        except OSError as e:
            messagebox.showerror(t("导出失败"), str(e), parent=self)
            return
        self._set_state(t("已导出 {n} 行到 {path}", n=len(rows), path=path), OK)
        self.notify(t("已导出 CSV：{path}", path=path))

    def _exec_shortcut(self) -> str:
        self.execute()
        return "break"

    def clear_all(self) -> None:
        """清空 SQL 编辑框、结果与结构视图。"""
        self.sql_text.delete("1.0", "end")
        self._clear_result()
        self._render_schema([], None)
        if self.mgr.is_open:
            self._set_state(t("已打开：{path}", path=self.mgr.path), TEXT_DIM)
        else:
            self._set_state(t("未选择数据库文件。"), TEXT_DIM)

    # ------------------------------------------------------------------ #
    # 后台任务与渲染
    def _run_async(self, job, tag: str) -> None:
        if self._busy:
            # 例如扫描文件期间点了「打开」：挂起，等当前任务回包后再执行
            self._deferred = (job, tag)
            return
        self._set_busy(True)

        def worker():
            try:
                self._queue.put((tag, job()))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self._poll()

    def _poll(self) -> None:
        try:
            tag, payload = self._queue.get_nowait()
        except queue.Empty:
            self.after(80, self._poll)
            return
        self._set_busy(False)
        handler = {
            "files": self._on_files,
            "opened": self._on_opened,
            "schema": self._on_schema,
            "query": self._on_query,
            "error": self._on_error,
        }.get(tag)
        if handler is not None:
            handler(payload)
        self._resume_deferred()

    def _resume_deferred(self) -> None:
        """执行被挂起的操作（若有）。"""
        if self._deferred is None or self._busy:
            return
        job, tag = self._deferred
        self._deferred = None
        self._run_async(job, tag)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_exec.configure(state="disabled" if busy else "normal")

    def _set_state(self, text: str, color: str = TEXT_DIM) -> None:
        self.state_label.configure(text=text, foreground=color)

    # ---- 渲染 ----
    def _on_files(self, paths: list) -> None:
        self._files = paths
        cur = self.file_var.get().strip()
        self.file_cb.configure(values=paths)
        if cur:
            self.file_var.set(cur)
        elif paths:
            self.file_var.set(paths[0])
        if not paths:
            self._set_state(t("未发现数据库文件（点击「浏览…」选择其它文件）"), TEXT_DIM)
        else:
            self._set_state(t("已发现 {n} 个数据库文件", n=len(paths)), TEXT_DIM)
        if self._pending_open:
            path, self._pending_open = self._pending_open, ""
            if os.path.isfile(path):
                self.file_var.set(path)
                self.open_db(path)

    def _on_opened(self, payload) -> None:
        path, tables = payload
        self.file_var.set(path)
        self._render_tables(tables)
        self._clear_result()
        self._set_state(t("已打开：{path}", path=path), OK)
        self.notify(t("已打开：{path}", path=path))

    def _on_schema(self, payload) -> None:
        _name, cols, count = payload
        self._render_schema(cols, count)

    def _on_query(self, res) -> None:
        self._render_result(res)
        self._has_more = bool(res.truncated)
        self._update_pager()
        if not res.columns:
            self._set_state(t("无结果"), TEXT_DIM)
            return
        ms = f"{res.elapsed:.0f}"
        page = self._offset // QUERY_LIMIT + 1
        if res.truncated:
            self._set_state(
                t("第 {page} 页 · 本页 {n} 行 · 耗时 {ms} ms（还有下一页）",
                  page=page, n=len(res.rows), ms=ms), OK)
        else:
            self._set_state(
                t("第 {page} 页 · 共 {n} 行 · 耗时 {ms} ms{more}",
                  page=page, n=len(res.rows), ms=ms,
                  more="" if self._offset == 0 else t("（已翻页）")), OK)

    def _update_pager(self) -> None:
        """按当前 offset 与是否还有下一页刷新翻页按钮可用性。"""
        self.btn_prev.configure(state="normal" if self._offset > 0 else "disabled")
        self.btn_next.configure(state="normal" if self._has_more else "disabled")

    def _current_columns(self) -> list[str]:
        """当前结果表的列名列表。"""
        cols = []
        for cid in self.result_tree["columns"]:
            head = self.result_tree.heading(cid, "text")
            if head:
                cols.append(head)
        return cols

    def _current_rows(self) -> list[list]:
        """当前结果表的行数据（显示值）。"""
        return [list(self.result_tree.item(iid, "values"))
                for iid in self.result_tree.get_children()]

    def _on_error(self, msg: str) -> None:
        self._set_state(msg, ERR)
        self.notify(msg)

    def _render_tables(self, tables: list) -> None:
        self._tables = tables
        self.table_tree.delete(*self.table_tree.get_children())
        for tb in tables:
            self.table_tree.insert("", "end", iid=tb.name,
                                   values=(tb.name, t("表") if tb.kind == "table" else t("视图")))
        self.table_box.configure(text=t("表 / 视图（{n}）", n=len(tables)))
        self._render_schema([], None)

    def _render_schema(self, cols: list, count) -> None:
        self.schema_tree.delete(*self.schema_tree.get_children())
        for c in cols:
            marks = []
            if c.pk:
                marks.append("PK")
            if c.notnull:
                marks.append("NOT NULL")
            self.schema_tree.insert("", "end", values=(c.name, c.type,
                                                       ", ".join(marks), c.default))
        self.schema_info.configure(
            text=t("行数：{n}", n="—" if count is None else count) if cols else "")

    def _render_result(self, res) -> None:
        """把查询结果铺进表格：列名去重（JOIN 可能出现重名列），宽度按内容估算。"""
        self._clear_result()
        if not res.columns:
            return
        ids: list = []
        headings: list = []
        used: dict = {}
        for col in res.columns:
            base = str(col) or "col"
            seen = used.get(base, 0)
            used[base] = seen + 1
            ids.append(base if not seen else f"{base}#{seen}")
            headings.append(base)
        self.result_tree.configure(columns=ids, show="headings")
        widths = [max(len(h), 6) for h in headings]
        for row in res.rows:
            values = [format_value(v) for v in row]
            for i, v in enumerate(values[:len(ids)]):
                widths[i] = min(40, max(widths[i], len(v)))
            self.result_tree.insert("", "end", values=values[:len(ids)])
        for cid, head, width in zip(ids, headings, widths):
            self.result_tree.heading(cid, text=head)
            self.result_tree.column(cid, width=max(80, min(320, width * 9)),
                                    anchor="w", minwidth=60, stretch=True)

    def _clear_result(self) -> None:
        self.result_tree.delete(*self.result_tree.get_children())
        self.result_tree.configure(columns=())
