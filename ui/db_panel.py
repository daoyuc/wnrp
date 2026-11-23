# -*- coding: utf-8 -*-
"""「数据库」页：连接清单 + 只读浏览（表 / 结构 / 数据 / 审计）。

契约（主窗口 / 主题系统）：

- ``auto_refresh()``：只重载本地连接清单，不探测远端库；
- ``refresh_theme()``：主题切换时重设 Treeview 斑马纹。

安全边界：库操作全部在 worker 线程；密码只经对话框内存传入 facade，
不写日志、不进命令行；写路径（DML/DDL）本面板未开放（ADR 28）。
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core.db import facade
from core.db.base import DriverError
from core.i18n import t
from . import theme
from . import widgets as w
from .window_utils import rearm_poll

_POLL_MS = 120
_BUSY_TIMEOUT = 60.0
_PAGE = 100


class DbPanel(ttk.Frame):
    """数据库连接与只读浏览面板。"""

    def __init__(self, master, notify):
        super().__init__(master, padding=8)
        self.notify = notify
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._busy_since = 0.0
        self._connections: list = []
        self._selected: dict | None = None
        self._tables: list = []
        self._database = tk.StringVar(value="")
        self._filter = tk.StringVar(value="")
        self._status = tk.StringVar(value=t("就绪"))
        self._offset = 0
        self._has_more = False
        self._current_table = ""
        self._build()
        self.reload()
        self._poll()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        header = w.PageHeader(
            self, title=t("数据库"),
            subtitle=t("连接清单与只读浏览（MySQL / SQLite）"),
        )
        ttk.Label(header.actions, textvariable=self._status,
                  style="PageSub.TLabel").pack(side="left",
                                               padx=(0, theme.PAD_SM))
        tb = w.Toolbar(
            self,
            primary=[{"text": t("新增连接"), "command": self._on_add}],
            secondary=[
                {"text": t("编辑"), "command": self._on_edit},
                {"text": t("测试连接"), "command": self._on_test},
                {"text": t("刷新"), "command": self.reload},
            ],
            overflow=[{"text": t("删除"), "command": self._on_remove}],
        )
        (self.btn_add, self.btn_edit, self.btn_test,
         self.btn_reload) = tb.buttons

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, pady=(theme.PAD_MD, 0))

        left = ttk.Frame(body)
        body.add(left, weight=1)
        head = ttk.Frame(left)
        head.pack(fill="x")
        ttk.Label(head, text=t("连接"), style="Section.TLabel").pack(side="left")
        self._ccount = ttk.Label(head, text="", style="CardDim.TLabel")
        self._ccount.pack(side="right")
        self._ctree = ttk.Treeview(
            left, columns=("engine", "target"),
            show="tree headings", selectmode="browse", height=14,
        )
        self._ctree.heading("#0", text=t("名称"))
        self._ctree.heading("engine", text=t("引擎"))
        self._ctree.heading("target", text=t("目标"))
        self._ctree.column("#0", width=120)
        self._ctree.column("engine", width=70, stretch=False)
        self._ctree.column("target", width=160)
        self._ctree.pack(fill="both", expand=True, side="left")
        csb = ttk.Scrollbar(left, orient="vertical", command=self._ctree.yview)
        csb.pack(side="right", fill="y")
        self._ctree.configure(yscrollcommand=csb.set)
        self._ctree.bind("<<TreeviewSelect>>", lambda e: self._on_pick_conn())

        right = ttk.Frame(body)
        body.add(right, weight=3)

        dbrow = ttk.Frame(right)
        dbrow.pack(fill="x")
        ttk.Label(dbrow, text=t("数据库"), style="PageSub.TLabel").pack(side="left")
        self._db_cb = ttk.Combobox(dbrow, textvariable=self._database, width=24)
        self._db_cb.pack(side="left", padx=(theme.PAD_XS, theme.PAD_SM))
        self._db_cb.bind("<<ComboboxSelected>>", lambda e: self._load_tables())
        ttk.Button(dbrow, text=t("加载表"), command=self._load_tables,
                   style="Toolbar.TButton").pack(side="left")

        mid = ttk.PanedWindow(right, orient="horizontal")
        mid.pack(fill="both", expand=True, pady=(theme.PAD_SM, 0))

        tblf = ttk.Frame(mid)
        mid.add(tblf, weight=1)
        ttk.Label(tblf, text=t("表 / 视图"), style="Section.TLabel").pack(anchor="w")
        self._ttree = ttk.Treeview(
            tblf, columns=("kind", "rows"),
            show="tree headings", selectmode="browse", height=12,
        )
        self._ttree.heading("#0", text=t("名称"))
        self._ttree.heading("kind", text=t("类型"))
        self._ttree.heading("rows", text=t("约行数"))
        self._ttree.column("#0", width=140)
        self._ttree.column("kind", width=60, stretch=False)
        self._ttree.column("rows", width=70, stretch=False)
        self._ttree.pack(fill="both", expand=True, side="left")
        tsb = ttk.Scrollbar(tblf, orient="vertical", command=self._ttree.yview)
        tsb.pack(side="right", fill="y")
        self._ttree.configure(yscrollcommand=tsb.set)
        self._ttree.bind("<<TreeviewSelect>>", lambda e: self._on_pick_table())

        detail = ttk.Frame(mid)
        mid.add(detail, weight=3)
        self._nb = ttk.Notebook(detail)
        self._nb.pack(fill="both", expand=True)

        data_tab = ttk.Frame(self._nb, padding=4)
        self._nb.add(data_tab, text=t("数据"))
        frow = ttk.Frame(data_tab)
        frow.pack(fill="x")
        ttk.Label(frow, text=t("筛选"), style="PageSub.TLabel").pack(side="left")
        ttk.Entry(frow, textvariable=self._filter, width=36).pack(
            side="left", padx=(theme.PAD_XS, theme.PAD_SM), fill="x", expand=True)
        ttk.Button(frow, text=t("查询"), command=self._load_rows,
                   style="Accent.TButton").pack(side="left")
        dwrap = ttk.Frame(data_tab)
        dwrap.pack(fill="both", expand=True, pady=(theme.PAD_XS, 0))
        self._dtree = ttk.Treeview(dwrap, show="headings", height=12)
        self._dtree.pack(side="left", fill="both", expand=True)
        dsb = ttk.Scrollbar(dwrap, orient="vertical", command=self._dtree.yview)
        dsb.pack(side="right", fill="y")
        self._dtree.configure(yscrollcommand=dsb.set)
        pager = ttk.Frame(data_tab)
        pager.pack(fill="x", pady=(theme.PAD_XS, 0))
        self.btn_prev = ttk.Button(pager, text=t("◀ 上一页"), state="disabled",
                                   command=lambda: self._page(-1))
        self.btn_prev.pack(side="right", padx=(0, theme.PAD_XS))
        self.btn_next = ttk.Button(pager, text=t("下一页 ▶"), state="disabled",
                                   command=lambda: self._page(1))
        self.btn_next.pack(side="right")

        struct_tab = ttk.Frame(self._nb, padding=4)
        self._nb.add(struct_tab, text=t("结构"))
        self._coltree = ttk.Treeview(
            struct_tab,
            columns=("type", "null", "pk", "default", "extra"),
            show="tree headings", height=14,
        )
        self._coltree.heading("#0", text=t("列名"))
        self._coltree.heading("type", text=t("类型"))
        self._coltree.heading("null", text=t("可空"))
        self._coltree.heading("pk", text="PK")
        self._coltree.heading("default", text=t("默认"))
        self._coltree.heading("extra", text=t("额外"))
        self._coltree.column("#0", width=120)
        self._coltree.column("type", width=120)
        self._coltree.column("null", width=50, stretch=False)
        self._coltree.column("pk", width=40, stretch=False)
        self._coltree.column("default", width=100)
        self._coltree.column("extra", width=80)
        self._coltree.pack(fill="both", expand=True)

        audit_tab = ttk.Frame(self._nb, padding=4)
        self._nb.add(audit_tab, text=t("审计"))
        self._atree = ttk.Treeview(
            audit_tab,
            columns=("kind", "conn", "result", "preview"),
            show="tree headings", height=14,
        )
        self._atree.heading("#0", text=t("时间"))
        self._atree.heading("kind", text=t("类型"))
        self._atree.heading("conn", text=t("连接"))
        self._atree.heading("result", text=t("结果"))
        self._atree.heading("preview", text=t("摘要"))
        self._atree.column("#0", width=140)
        self._atree.column("kind", width=100)
        self._atree.column("conn", width=100)
        self._atree.column("result", width=60, stretch=False)
        self._atree.column("preview", width=200)
        self._atree.pack(fill="both", expand=True)
        ttk.Button(audit_tab, text=t("刷新审计"), command=self._load_audit,
                   style="Toolbar.TButton").pack(anchor="e", pady=(theme.PAD_XS, 0))

        self.refresh_theme()

    # ------------------------------------------------------------------ #
    def auto_refresh(self) -> None:
        self.reload()

    def refresh_theme(self) -> None:
        for tree in (getattr(self, "_ctree", None),
                     getattr(self, "_ttree", None),
                     getattr(self, "_dtree", None),
                     getattr(self, "_coltree", None),
                     getattr(self, "_atree", None)):
            if tree is None:
                continue
            tree.tag_configure("odd", background=theme.ROW_ALT)

    def reload(self) -> None:
        """重载本地连接清单（不探测）。"""
        try:
            out = facade.list_connections()
        except Exception as e:  # noqa: BLE001
            self.notify(str(e), "error")
            return
        self._connections = list(out.get("connections") or [])
        self._render_connections()
        self._status.set(t("已加载 {n} 个连接", n=len(self._connections)))

    # ------------------------------------------------------------------ #
    def _render_connections(self) -> None:
        sel = self._ctree.selection()
        keep = sel[0] if sel else ""
        self._ctree.delete(*self._ctree.get_children())
        for i, c in enumerate(self._connections):
            if c.get("engine") == "sqlite":
                target = c.get("path") or "—"
            else:
                target = f"{c.get('user') or '?'}@{c.get('host')}:{c.get('port')}"
                if c.get("database"):
                    target += f"/{c['database']}"
            tag = ("odd",) if i % 2 else ()
            self._ctree.insert(
                "", "end", iid=str(c["id"]), text=c.get("name") or c["id"],
                values=(c.get("engine") or "", target), tags=tag,
            )
        self._ccount.configure(text=str(len(self._connections)))
        if keep and self._ctree.exists(keep):
            self._ctree.selection_set(keep)
        elif self._connections:
            first = str(self._connections[0]["id"])
            self._ctree.selection_set(first)
            self._on_pick_conn()

    def _require_conn(self) -> dict | None:
        if self._selected is None:
            self.notify(t("请先选择一个连接"), "error")
            return None
        return self._selected

    def _on_pick_conn(self) -> None:
        sel = self._ctree.selection()
        if not sel:
            self._selected = None
            return
        cid = sel[0]
        self._selected = next(
            (c for c in self._connections if str(c["id"]) == cid), None)
        self._database.set("")
        self._db_cb["values"] = []
        self._clear_tables()
        self._clear_grid()
        self._clear_columns()
        if self._selected is None:
            return
        if self._selected.get("engine") == "sqlite":
            self._database.set("main")
            self._db_cb["values"] = ["main"]
            self._load_tables()
        else:
            # MySQL：若连接已带默认库，直接用；否则拉库列表
            if self._selected.get("database"):
                self._database.set(self._selected["database"])
                self._db_cb["values"] = [self._selected["database"]]
                self._load_tables()
            else:
                self._start_bg("databases")

    # ------------------------------------------------------------------ #
    # 连接 CRUD
    # ------------------------------------------------------------------ #
    def _on_add(self) -> None:
        data = connection_dialog(self, None)
        if data is None:
            return
        password = data.pop("password", "")
        try:
            out = facade.add_connection(data, password=password)
        except (ValueError, KeyError) as e:
            self.notify(str(e), "error")
            return
        self.reload()
        cid = str(out["connection"]["id"])
        if self._ctree.exists(cid):
            self._ctree.selection_set(cid)
            self._on_pick_conn()
        self._status.set(t("已新增连接：{name}", name=out["connection"]["name"]))

    def _on_edit(self) -> None:
        conn = self._require_conn()
        if conn is None:
            return
        # 编辑需要完整字段（含 path 等）；public 视图已够用
        data = connection_dialog(self, conn)
        if data is None:
            return
        password = data.pop("password", None)
        # 对话框：空密码表示不改 → 传 None；显式填了才更新
        try:
            out = facade.update_connection(
                conn["id"], data,
                password=password if password is not None else None,
            )
        except (ValueError, KeyError) as e:
            self.notify(str(e), "error")
            return
        self.reload()
        self._status.set(t("已更新连接：{name}", name=out["connection"]["name"]))

    def _on_remove(self) -> None:
        conn = self._require_conn()
        if conn is None:
            return
        if not messagebox.askyesno(
                t("删除连接"),
                t("确定删除连接 {name}？", name=conn.get("name")),
                parent=self.winfo_toplevel()):
            return
        try:
            facade.remove_connection(conn["id"], yes=True)
        except (ValueError, KeyError) as e:
            self.notify(str(e), "error")
            return
        self._selected = None
        self.reload()
        self._clear_tables()
        self._clear_grid()
        self._status.set(t("已删除连接"))

    def _on_test(self) -> None:
        if self._require_conn() is None:
            return
        self._status.set(t("正在测试连接…"))
        self._start_bg("test")

    # ------------------------------------------------------------------ #
    # 对象 / 数据
    # ------------------------------------------------------------------ #
    def _load_tables(self) -> None:
        if self._require_conn() is None:
            return
        self._start_bg("tables")

    def _on_pick_table(self) -> None:
        sel = self._ttree.selection()
        if not sel:
            self._current_table = ""
            return
        self._current_table = sel[0]
        self._offset = 0
        self._start_bg("columns")
        self._load_rows()

    def _load_rows(self) -> None:
        if self._require_conn() is None or not self._current_table:
            return
        self._start_bg("rows")

    def _page(self, delta: int) -> None:
        nxt = self._offset + delta * _PAGE
        if nxt < 0:
            nxt = 0
        if delta > 0 and not self._has_more:
            return
        self._offset = nxt
        self._load_rows()

    def _load_audit(self) -> None:
        self._start_bg("audit")

    # ------------------------------------------------------------------ #
    # worker
    # ------------------------------------------------------------------ #
    def _start_bg(self, action: str) -> None:
        if self._busy:
            self.notify(t("请等待当前操作完成"), "warn")
            return
        conn = self._selected
        if conn is None and action != "audit":
            return
        self._busy = True
        self._busy_since = time.monotonic()
        cid = conn["id"] if conn else ""
        database = self._database.get().strip()
        table = self._current_table
        filt = self._filter.get().strip()
        offset = self._offset

        def worker() -> None:
            try:
                if action == "test":
                    payload = facade.test_connection(cid)
                elif action == "databases":
                    payload = facade.list_databases(cid)
                elif action == "tables":
                    payload = facade.list_tables(cid, database=database)
                elif action == "columns":
                    payload = facade.list_columns(
                        cid, table, database=database)
                elif action == "rows":
                    payload = facade.select_rows(
                        cid, table, database=database,
                        filters=filt, limit=_PAGE, offset=offset,
                    )
                elif action == "audit":
                    payload = facade.list_audit(limit=100)
                else:
                    payload = {"error": f"unknown action {action}"}
                self._queue.put((action, payload, None))
            except (DriverError, KeyError, ValueError, OSError) as e:
                kind = getattr(e, "kind", "failed")
                self._queue.put((action, {"error": str(e), "error_kind": kind}, e))
            except Exception as e:  # noqa: BLE001
                self._queue.put((action, {"error": str(e)}, e))

        threading.Thread(target=worker, daemon=True, name=f"db-{action}").start()

    def _poll(self) -> None:
        try:
            while True:
                action, payload, _err = self._queue.get_nowait()
                self._busy = False
                self._apply(action, payload)
        except queue.Empty:
            pass
        if self._busy and time.monotonic() - self._busy_since > _BUSY_TIMEOUT:
            self._busy = False
            self._status.set(t("操作超时"))
        rearm_poll(self, self._poll, _POLL_MS)

    def _apply(self, action: str, payload: dict) -> None:
        if payload.get("error") and action != "test":
            self._status.set(payload["error"])
            self.notify(payload["error"], "error")
            return
        if action == "test":
            if not payload.get("ok"):
                msg = payload.get("error") or t("连接测试失败")
                self._status.set(msg)
                self.notify(msg, "error")
                return
            ms = payload.get("elapsed_ms")
            extra = payload.get("server") or payload.get("path") or ""
            self._status.set(t("连接成功 · {ms} ms · {extra}",
                               ms=ms, extra=extra))
            self.notify(t("连接成功"), "ok")
            return
        if action == "databases":
            names = payload.get("databases") or []
            self._db_cb["values"] = names
            if names and not self._database.get():
                self._database.set(names[0])
            self._status.set(t("已加载 {n} 个数据库", n=len(names)))
            if self._database.get():
                self._load_tables()
            return
        if action == "tables":
            self._tables = payload.get("tables") or []
            self._ttree.delete(*self._ttree.get_children())
            for i, tbl in enumerate(self._tables):
                tag = ("odd",) if i % 2 else ()
                rows = tbl.get("rows_approx")
                self._ttree.insert(
                    "", "end", iid=tbl["name"], text=tbl["name"],
                    values=(tbl.get("kind") or "table",
                            "" if rows is None else rows),
                    tags=tag,
                )
            self._status.set(t("已加载 {n} 张表", n=len(self._tables)))
            return
        if action == "columns":
            cols = payload.get("columns") or []
            self._coltree.delete(*self._coltree.get_children())
            for i, col in enumerate(cols):
                tag = ("odd",) if i % 2 else ()
                self._coltree.insert(
                    "", "end", text=col.get("name") or "",
                    values=(
                        col.get("type") or "",
                        t("是") if col.get("nullable", True) else t("否"),
                        "✓" if col.get("primary_key") else "",
                        "" if col.get("default") is None else str(col["default"]),
                        col.get("extra") or "",
                    ),
                    tags=tag,
                )
            return
        if action == "rows":
            cols = payload.get("columns") or []
            rows = payload.get("rows") or []
            self._dtree.delete(*self._dtree.get_children())
            self._dtree["columns"] = cols
            for c in cols:
                self._dtree.heading(c, text=c)
                self._dtree.column(c, width=110, anchor="w")
            for i, row in enumerate(rows):
                tag = ("odd",) if i % 2 else ()
                vals = ["" if v is None else str(v) for v in row]
                self._dtree.insert("", "end", values=vals, tags=tag)
            self._has_more = bool(payload.get("truncated"))
            self.btn_prev.configure(
                state="normal" if self._offset > 0 else "disabled")
            self.btn_next.configure(
                state="normal" if self._has_more else "disabled")
            self._status.set(
                t("{n} 行 · {ms} ms",
                  n=len(rows), ms=payload.get("elapsed_ms") or 0)
                + (t("（已截断）") if self._has_more else "")
            )
            return
        if action == "audit":
            entries = payload.get("entries") or []
            self._atree.delete(*self._atree.get_children())
            for i, e in enumerate(entries):
                tag = ("odd",) if i % 2 else ()
                self._atree.insert(
                    "", "end", text=e.get("ts") or "",
                    values=(
                        e.get("kind") or "",
                        e.get("conn_name") or e.get("conn_id") or "",
                        e.get("result") or "",
                        (e.get("preview") or e.get("sql") or "")[:80],
                    ),
                    tags=tag,
                )
            self._status.set(t("审计 {n} 条", n=len(entries)))

    def _clear_tables(self) -> None:
        self._tables = []
        self._ttree.delete(*self._ttree.get_children())
        self._current_table = ""

    def _clear_grid(self) -> None:
        self._dtree.delete(*self._dtree.get_children())
        self._dtree["columns"] = ()
        self._offset = 0
        self._has_more = False
        self.btn_prev.configure(state="disabled")
        self.btn_next.configure(state="disabled")

    def _clear_columns(self) -> None:
        self._coltree.delete(*self._coltree.get_children())


# ---------------------------------------------------------------------- #
def _form_row(win, row: int, label: str, var, show=None, width: int = 36):
    ttk.Label(win, text=label).grid(row=row, column=0, sticky="w",
                                    padx=(0, theme.PAD_SM), pady=theme.PAD_XS)
    entry = ttk.Entry(win, textvariable=var, width=width, show=show)
    entry.grid(row=row, column=1, sticky="we", pady=theme.PAD_XS)
    return entry


def connection_dialog(parent, conn: dict | None):
    """连接新增 / 编辑对话框；取消返回 None。

    编辑时密码留空表示不修改；返回的 ``password`` 键：
    - 新增：始终有（可为空串）；
    - 编辑：仅用户填写时带上，否则不出现（调用方传 ``None`` 给 facade）。
    """
    conn = conn or {}
    editing = bool(conn)
    win = tk.Toplevel(parent)
    win.title(t("编辑连接") if editing else t("新增连接"))
    win.transient(parent.winfo_toplevel())
    win.resizable(False, False)
    win.columnconfigure(1, weight=1)

    v = {
        "name": tk.StringVar(value=str(conn.get("name", ""))),
        "engine": tk.StringVar(value=str(conn.get("engine", "mysql"))),
        "host": tk.StringVar(value=str(conn.get("host", "127.0.0.1"))),
        "port": tk.StringVar(value=str(conn.get("port") or 3306)),
        "user": tk.StringVar(value=str(conn.get("user", "root"))),
        "password": tk.StringVar(),
        "database": tk.StringVar(value=str(conn.get("database", ""))),
        "socket": tk.StringVar(value=str(conn.get("socket", ""))),
        "path": tk.StringVar(value=str(conn.get("path", ""))),
        "mysql_cli": tk.StringVar(value=str(conn.get("mysql_cli", ""))),
    }

    _form_row(win, 0, t("名称"), v["name"])
    ttk.Label(win, text=t("引擎")).grid(row=1, column=0, sticky="w",
                                        pady=theme.PAD_XS)
    eng = ttk.Combobox(win, textvariable=v["engine"],
                       values=["mysql", "sqlite"], state="readonly", width=12)
    eng.grid(row=1, column=1, sticky="w", pady=theme.PAD_XS)

    host_e = _form_row(win, 2, t("主机"), v["host"])
    port_e = _form_row(win, 3, t("端口"), v["port"], width=10)
    user_e = _form_row(win, 4, t("用户名"), v["user"])
    pw_e = _form_row(win, 5, t("密码"), v["password"], show="*")
    hint = t("编辑时留空表示不修改") if editing else t("可留空，连接前再补")
    ttk.Label(win, text=hint, style="CardDim.TLabel").grid(
        row=5, column=2, sticky="w", padx=(theme.PAD_XS, 0))
    db_e = _form_row(win, 6, t("数据库"), v["database"])
    sock_e = _form_row(win, 7, t("Socket"), v["socket"])
    cli_e = _form_row(win, 8, t("mysql 客户端"), v["mysql_cli"])

    path_row = ttk.Frame(win)
    path_row.grid(row=9, column=1, sticky="we", pady=theme.PAD_XS)
    ttk.Label(win, text=t("SQLite 文件")).grid(row=9, column=0, sticky="w",
                                               pady=theme.PAD_XS)
    path_e = ttk.Entry(path_row, textvariable=v["path"], width=28)
    path_e.pack(side="left", fill="x", expand=True)

    def browse() -> None:
        path = filedialog.askopenfilename(
            parent=win, title=t("选择数据库文件"),
            filetypes=[
                (t("SQLite 数据库"), "*.sqlite *.sqlite3 *.db"),
                (t("所有文件"), "*.*"),
            ])
        if path:
            v["path"].set(path)

    browse_btn = ttk.Button(path_row, text=t("浏览"), command=browse, width=8)
    browse_btn.pack(side="left", padx=(theme.PAD_XS, 0))

    mysql_widgets = (host_e, port_e, user_e, pw_e, db_e, sock_e, cli_e)
    sqlite_widgets = (path_e, browse_btn)

    def sync_engine(*_a) -> None:
        is_sqlite = v["engine"].get() == "sqlite"
        for wdg in mysql_widgets:
            wdg.configure(state="disabled" if is_sqlite else "normal")
        for wdg in sqlite_widgets:
            wdg.configure(state="normal" if is_sqlite else "disabled")

    v["engine"].trace_add("write", sync_engine)
    sync_engine()

    result: dict = {}

    def on_ok() -> None:
        engine = v["engine"].get().strip()
        data = {
            "name": v["name"].get().strip(),
            "engine": engine,
        }
        if engine == "sqlite":
            data["path"] = v["path"].get().strip()
        else:
            data["host"] = v["host"].get().strip() or "127.0.0.1"
            try:
                data["port"] = int(v["port"].get().strip() or "3306")
            except ValueError:
                data["port"] = 3306
            data["user"] = v["user"].get().strip() or "root"
            data["database"] = v["database"].get().strip()
            data["socket"] = v["socket"].get().strip()
            data["mysql_cli"] = v["mysql_cli"].get().strip()
        password = v["password"].get()
        if editing:
            if password:
                data["password"] = password
        else:
            data["password"] = password
        result["data"] = data
        win.destroy()

    def on_cancel() -> None:
        result["data"] = None
        win.destroy()

    btns = ttk.Frame(win)
    btns.grid(row=10, column=0, columnspan=3, sticky="e", pady=(theme.PAD_MD, 0))
    ttk.Button(btns, text=t("取消"), command=on_cancel).pack(side="right")
    ttk.Button(btns, text=t("确定"), command=on_ok,
               style="Accent.TButton").pack(side="right", padx=(0, theme.PAD_SM))

    win.grab_set()
    win.wait_window()
    return result.get("data")
