# -*- coding: utf-8 -*-
"""PHP 版本管理页：版本列表（状态灯/版本号/端口/PID/配置）+ 启停/重启/端口/配置操作。

所有耗时操作（扫描、php -v 解析、启停、状态刷新）均在后台线程执行，
通过 queue 回传 UI 线程，避免界面卡死。
"""
import os
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core import crash_watchdog
from core import process_utils as pu
from core import xdebug
from core.config import Config, IS_WIN
from core.health_monitor import HealthMonitor
from core.i18n import t
from core.php_manager import PhpManager, PhpVersion, PortConflictError
from .dialogs import IniDialog, IniEditDialog, PortDialog, SelfCheckDialog
from .download_dialog import DownloadDialog
from .extension_dialog import ExtensionDialog
from .tuning_dialog import TuningDialog
from .xdebug_dialog import XdebugDialog
from . import theme

#: 列表列：状态 / 标识 / 端口进程 + 4 项最常用的 ini 指标 + 扩展数 / 调试开关。
#: 完整指标与路径放在下方「常用指标」栏，避免列过宽挤占比较视图。
COLUMNS = [
    ("status", t("状态"), 64, "center"),
    ("name", t("版本目录"), 105, "w"),
    ("ver", t("PHP 版本"), 85, "center"),
    ("port", t("端口"), 68, "center"),
    ("pid", "PID", 78, "center"),
    ("mem", t("内存上限"), 92, "center"),
    ("upload", t("上传上限"), 92, "center"),
    ("maxtime", t("执行时限"), 92, "center"),
    ("ext", t("扩展"), 68, "center"),
    ("debug", t("调试"), 64, "center"),
    ("ini", t("配置文件"), 170, "w"),
]

#: 详情栏「常用指标」展示的 ini 项（key, 展示名），按 4 列网格排布
METRIC_ITEMS = [
    ("memory_limit", t("内存上限")),
    ("post_max_size", t("提交上限")),
    ("upload_max_filesize", t("上传上限")),
    ("max_file_uploads", t("最大上传文件数")),
    ("max_execution_time", t("执行时限")),
    ("max_input_time", t("输入超时")),
    ("display_errors", t("显示错误")),
    ("error_reporting", t("错误级别")),
    ("date.timezone", t("时区")),
    ("default_charset", t("默认字符集")),
    ("opcache.enable", t("OPcache")),
    ("extension_dir", t("扩展目录")),
]

#: 关键扩展（与「版本自检」的 9 项一致）：详情栏给出命中统计
KEY_EXTS = ("redis", "pdo_mysql", "mysqli", "openssl",
            "curl", "mbstring", "gd", "fileinfo", "zip")

#: 配置文件列前缀：该版本还没有生效的 php.ini（需先「初始化 php.ini」）
_MISSING_MARK = "⚠ "


class PhpPanel(ttk.Frame):
    def __init__(self, master, php_mgr: PhpManager, config: Config, notify):
        super().__init__(master, padding=8)
        self.php_mgr = php_mgr
        self.config = config
        self.notify = notify

        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._versions: list[PhpVersion] = []
        self._name_to_iid: dict[str, str] = {}
        self._pending_row_refresh = False
        self._draining = False  # 队列 drain 是否已启动（唯一消费者）
        #: 常用指标缓存：{版本名: 指标}，按 ini 的 (mtime, size) 失效
        self._metrics: dict[str, dict] = {}

        self._build()
        self.refresh_versions()
        self._start_drain()

    # ------------------------------------------------------------------ #
    # 界面构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        self.btn_start = ttk.Button(bar, text=t("启动"), style="Accent.TButton",
                                    command=lambda: self._operate("start"))
        self.btn_stop = ttk.Button(bar, text=t("停止"), style="Danger.TButton",
                                   command=lambda: self._operate("stop"))
        self.btn_restart = ttk.Button(bar, text=t("重启"), command=lambda: self._operate("restart"))
        self.btn_port = ttk.Button(bar, text=t("编辑端口"), command=self._edit_port)
        self.btn_ini = ttk.Button(bar, text=t("查看配置"), command=self._view_ini)
        self.btn_edit = ttk.Button(bar, text=t("编辑配置"), command=self._edit_ini)
        self.btn_check = ttk.Button(bar, text=t("自检"), command=self._self_check)
        self.btn_ext = ttk.Button(bar, text=t("安装扩展"), command=self._manage_ext)
        self.btn_tune = ttk.Button(bar, text=t("推荐设置"), command=self._recommend_settings)
        # 没有生效 php.ini 的版本：先生成一份，否则「编辑配置 / 推荐设置」无从下手
        self.btn_init_ini = ttk.Button(bar, text=t("初始化 php.ini"), command=self._init_ini)
        self.btn_debug = ttk.Button(bar, text=t("调试"), command=self._open_xdebug)
        self.btn_download = ttk.Button(bar, text=t("下载新版本"), style="Accent.TButton",
                                       command=self._download_version)
        self.btn_terminal = ttk.Button(bar, text=t("打开终端"), command=self._open_terminal)
        self.btn_composer = ttk.Button(bar, text=t("Composer"), command=self._composer)
        self.btn_refresh = ttk.Button(bar, text=t("刷新"), command=self.refresh_versions)
        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_port,
                  self.btn_ini, self.btn_edit, self.btn_check, self.btn_ext,
                  self.btn_tune, self.btn_init_ini, self.btn_debug, self.btn_download,
                  self.btn_terminal, self.btn_composer, self.btn_refresh):
            b.pack(side="left", padx=(0, 6))
        ttk.Label(bar, text=t("选中版本后操作 · 双击行查看配置"),
                  style="SubTitle.TLabel").pack(side="left", padx=(4, 0))

        # 常用指标栏（选中版本）：先按 bottom 占位，表格再 fill 剩余空间
        self._build_metrics_bar().pack(side="bottom", fill="x", pady=(6, 0))

        # 表格
        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            wrap, columns=[c[0] for c in COLUMNS], show="headings", selectmode="browse"
        )
        for col, text, width, anchor in COLUMNS:
            self.tree.heading(col, text=text)
            self.tree.column(
                col, width=width, anchor=anchor,
                stretch=(col == "ini"), minwidth=60,
            )
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        self.tree.tag_configure("dot_run", foreground=theme.OK)
        self.tree.tag_configure("dot_stop", foreground=theme.GRAY)
        self.tree.tag_configure("dot_err", foreground=theme.ERR)
        self.tree.tag_configure("odd", background=theme.ROW_ALT)
        self.tree.tag_configure("even", background=theme.CARD_BG)
        self.tree.bind("<Double-1>", lambda e: self._view_ini())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())

    # ------------------------------------------------------------------ #
    # 数据加载 / 渲染
    # ------------------------------------------------------------------ #
    def refresh_theme(self) -> None:
        """主题切换钩子：重设 Treeview 标记色（ttk 不会自动刷新 tag 配色）。"""
        self.tree.tag_configure("dot_run", foreground=theme.OK)
        self.tree.tag_configure("dot_stop", foreground=theme.GRAY)
        self.tree.tag_configure("dot_err", foreground=theme.ERR)
        self.tree.tag_configure("odd", background=theme.ROW_ALT)
        self.tree.tag_configure("even", background=theme.CARD_BG)

    def refresh_versions(self) -> None:
        """全量扫描 + 版本解析 + 状态刷新（后台线程）。"""
        if self._busy:
            return
        self._set_busy(True)
        self.notify(t("正在扫描 PHP 版本…"))

        def worker():
            try:
                versions = self.php_mgr.scan_versions()
                versions = self.php_mgr.resolve(refresh_status=True, fast=True)
                self._queue.put(("versions", versions))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", ("", t("扫描失败：{err}", err=e))))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    # ------------------------------------------------------------------ #
    # 队列分发：worker 线程只负责 put，所有 UI 操作收敛到主线程的
    # drain 循环（唯一消费者）。此前 scan / 操作 / 自动刷新 / 单行刷新各有
    # 一个 after 轮询抢同一个队列，会互相吞掉消息，导致状态永久刷不出来。
    # ------------------------------------------------------------------ #
    def _start_drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        self.after(80, self._drain)

    def _drain(self) -> None:
        if not self.winfo_exists():
            self._draining = False
            return
        try:
            while True:
                self._dispatch(*self._queue.get_nowait())
        except queue.Empty:
            pass
        # 不可见页签降频：drain 只是空转取消息，不必跟着 80ms 跑
        self.after(80 if self.winfo_ismapped() else 400, self._drain)

    def _dispatch(self, kind: str, payload) -> None:
        if kind == "versions":
            self._set_busy(False)
            self._render(payload)
            self.notify(t("已发现 {count} 个 PHP 版本", count=len(payload)))
        elif kind in ("op", "conflict"):
            self._set_busy(False)
            self._on_op_done(kind, payload)
        elif kind == "error":
            self._set_busy(False)
            name, msg = payload
            self.notify(msg)
            messagebox.showerror(t("操作失败") if name else t("错误"), msg, parent=self)
            if name:
                self._refresh_row(name)
        elif kind == "status":
            self._pending_row_refresh = False
            self._apply_status(payload)
        elif kind == "row":
            self._update_rows()

    def _row(self, v: PhpVersion, i: int) -> tuple[tuple, list[str]]:
        """单行数据 + 标记：指标取自该版本 ini（按 mtime 缓存，心跳刷新只做 stat）。

        配置文件缺失时路径前加 ⚠（该行需先「初始化 php.ini」），指标列显示 —。
        """
        if v.running:
            dot, tag = "●", "dot_run"
        elif v.error:
            dot, tag = "!", "dot_err"  # 二进制跑不起来（如 Homebrew 升级后缺动态库）
        else:
            dot, tag = "○", "dot_stop"
        disp = t("不可用") if v.error else (v.display or "—")
        target = self.php_mgr.ini_target(v)
        cell = self._ini_cell(target)
        m = self._metrics_for(v)
        keys = m.get("keys") or {}
        if self.php_mgr.ini_ready(v):
            ini = cell
            ext_cell = str(len(m.get("exts") or []))
            debug_cell = t("开") if m.get("xdebug") else t("关")
        else:
            ini = _MISSING_MARK + cell
            ext_cell = debug_cell = "—"
        return (
            (dot, v.name, disp, v.port, v.pid if v.pid else "—",
             keys.get("memory_limit") or "—",
             keys.get("upload_max_filesize") or "—",
             keys.get("max_execution_time") or "—",
             ext_cell, debug_cell, ini),
            [tag, "odd" if i % 2 else "even"],
        )

    # ------------------------------------------------------------------ #
    # 常用指标：底部详情栏 + 列表行（按 ini 的 mtime 缓存，避免心跳重复读盘）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _ini_cell(target: str) -> str:
        """配置文件列的紧凑显示：末两级（如 ``8.5/php.ini`` / ``php82/php.ini``）。"""
        parent = os.path.basename(os.path.dirname(target or ""))
        name = os.path.basename(target or "")
        return f"{parent}/{name}" if parent else name

    def _bind_metrics_refresh(self, dlg):
        """配置类对话框关闭后刷新指标（这些对话框没有完成回调，靠 Destroy 事件）。

        ini 的 (mtime, size) 已变 → 缓存失效重算，列表与详情栏随即反映修改结果。
        """
        def on_destroy(event):
            if event.widget is dlg and self.winfo_exists():
                self._update_rows()

        dlg.bind("<Destroy>", on_destroy, add="+")
        return dlg

    def _build_metrics_bar(self) -> ttk.LabelFrame:
        """底部「常用指标」栏：选中版本的常用 ini 值 + 扩展 / 调试 + 关键路径。"""
        box = ttk.LabelFrame(self, text=t("常用指标"), padding=(10, 6))
        grid = ttk.Frame(box, style="Card.TFrame")
        grid.pack(fill="x")
        self._metric_vars: dict[str, tk.StringVar] = {}
        cols = 4
        for idx, (key, label) in enumerate(METRIC_ITEMS):
            row, col = divmod(idx, cols)
            var = tk.StringVar(value="—")
            self._metric_vars[key] = var
            ttk.Label(grid, text=label + "：", style="SubTitle.TLabel").grid(
                row=row, column=col * 2, sticky="e", padx=(0, 2), pady=1)
            ttk.Label(grid, textvariable=var, style="Card.TLabel").grid(
                row=row, column=col * 2 + 1, sticky="w", padx=(0, 14), pady=1)
        r = (len(METRIC_ITEMS) + cols - 1) // cols
        span = cols * 2 - 1
        self._ext_var = tk.StringVar(value="—")
        ttk.Label(grid, text=t("扩展") + "：", style="SubTitle.TLabel").grid(
            row=r, column=0, sticky="e", padx=(0, 2), pady=(4, 1))
        ttk.Label(grid, textvariable=self._ext_var, style="Card.TLabel").grid(
            row=r, column=1, columnspan=span, sticky="w", pady=(4, 1))
        # 运行依赖异常（如缺动态库）：仅在确有原因时显示
        self._dep_var = tk.StringVar(value="")
        self._dep_label = ttk.Label(grid, text=t("运行依赖") + "：", style="SubTitle.TLabel")
        self._dep_value = ttk.Label(grid, textvariable=self._dep_var, style="Card.TLabel",
                                    wraplength=760, justify="left")
        self._dep_label.grid(row=r + 1, column=0, sticky="e", padx=(0, 2), pady=1)
        self._dep_value.grid(row=r + 1, column=1, columnspan=span, sticky="w", pady=1)
        self._set_dep_error("")
        self._path_var = tk.StringVar(value="—")
        ttk.Label(grid, text=t("配置文件") + "：", style="SubTitle.TLabel").grid(
            row=r + 2, column=0, sticky="e", padx=(0, 2), pady=1)
        ttk.Label(grid, textvariable=self._path_var, style="Card.TLabel",
                  wraplength=760, justify="left").grid(
            row=r + 2, column=1, columnspan=span, sticky="w", pady=1)
        self._cgi_var = tk.StringVar(value="—")
        ttk.Label(grid, text="php-cgi：", style="SubTitle.TLabel").grid(
            row=r + 3, column=0, sticky="e", padx=(0, 2), pady=1)
        ttk.Label(grid, textvariable=self._cgi_var, style="Card.TLabel",
                  wraplength=760, justify="left").grid(
            row=r + 3, column=1, columnspan=span, sticky="w", pady=1)
        return box

    def _set_dep_error(self, reason: str) -> None:
        """显示 / 隐藏「运行依赖」行（空原因时整行隐藏，不占视觉噪音）。"""
        self._dep_var.set(reason)
        if reason:
            self._dep_value.configure(foreground=theme.ERR)
            self._dep_label.grid()
            self._dep_value.grid()
        else:
            self._dep_label.grid_remove()
            self._dep_value.grid_remove()

    def _metrics_for(self, v: PhpVersion) -> dict:
        """该版本的常用指标；ini 未变（mtime + size 相同）时复用缓存。"""
        target = self.php_mgr.ini_target(v)
        try:
            st = os.stat(target)
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        cached = self._metrics.get(v.name)
        if cached is not None and cached.get("_sig") == sig:
            return cached
        m = self._compute_metrics(v)
        m["_sig"] = sig
        self._metrics[v.name] = m
        return m

    def _compute_metrics(self, v: PhpVersion) -> dict:
        """读取该版本生效 php.ini 的常用指标（无生效配置时 ``ok=False``）。"""
        info = self.php_mgr.read_key_ini(v)
        if "__error__" in info:
            return {"ok": False, "keys": {}, "exts": [], "xdebug": False}
        keys = {k: val for k, val in info.items() if not str(k).startswith("__")}
        exts = [str(e).strip().strip('"').strip("'")
                for e in info.get("__extensions__", [])]
        try:
            xdebug_on = xdebug.loader_enabled(v)
        except Exception:  # noqa: BLE001 —— 指标展示不应影响面板
            xdebug_on = False
        return {"ok": True, "keys": keys, "exts": exts, "xdebug": xdebug_on}

    def _update_metrics_bar(self) -> None:
        """把选中版本的常用指标刷到详情栏；未选中 / 无配置时给出提示。"""
        v = self._selected()
        if v is None:
            for var in self._metric_vars.values():
                var.set("—")
            self._ext_var.set(t("选中一个版本查看常用指标"))
            self._set_dep_error("")
            self._path_var.set("—")
            self._cgi_var.set("—")
            return
        m = self._metrics_for(v)
        keys = m.get("keys") or {}
        for key, _label in METRIC_ITEMS:
            self._metric_vars[key].set(keys.get(key) or "—")
        if not m.get("ok"):
            self._ext_var.set(t("（无生效配置文件，请先「初始化 php.ini」）"))
        else:
            exts = m.get("exts") or []
            stems = {os.path.splitext(e)[0].lower() for e in exts}
            hit = sum(1 for k in KEY_EXTS if k in stems)
            self._ext_var.set(
                t("共 {n} 个", n=len(exts)) + "  ·  "
                + t("关键扩展 {hit}/{total}", hit=hit, total=len(KEY_EXTS))
                + "  ·  " + t("Xdebug {state}",
                              state=t("开") if m.get("xdebug") else t("关")))
        self._set_dep_error(v.error)
        self._path_var.set(self.php_mgr.ini_target(v))
        self._cgi_var.set(v.cgi or "—")

    def _render(self, versions: list[PhpVersion]) -> None:
        self._versions = versions
        self._name_to_iid.clear()
        self.tree.delete(*self.tree.get_children())
        for i, v in enumerate(versions):
            values, tags = self._row(v, i)
            iid = self.tree.insert("", "end", values=values, tags=tags)
            self._name_to_iid[v.name] = iid
        self._update_buttons()

    # ------------------------------------------------------------------ #
    # 启停 / 重启
    # ------------------------------------------------------------------ #
    def _operate(self, action: str) -> None:
        v = self._selected()
        if v is None:
            messagebox.showinfo(t("提示"), t("请先在列表中选择一个 PHP 版本。"), parent=self)
            return
        if self._busy:
            return
        action_text = {"start": t("启动"), "stop": t("停止"), "restart": t("重启")}[action]
        if action == "stop" and not messagebox.askyesno(
                t("停止 PHP"),
                t("停止 [{name}] 后使用该版本的站点会无法访问（502），确定继续？", name=v.name),
                parent=self):
            return
        self._set_busy(True)
        self.notify(t("正在{action} [{name}] …", action=action_text, name=v.name))

        def worker():
            try:
                if action == "start":
                    msg = self.php_mgr.start(v)
                    # 启动/已在运行 → 纳入守护进程看护（失联自动恢复）
                    crash_watchdog.watch_version(v.name)
                elif action == "stop":
                    msg = self.php_mgr.stop(v)
                    # stop() 未抛异常说明版本已停止或本未运行 → 解除守护进程看护，
                    # 避免失联探测将其重新拉起。依据执行结果判定，不依赖消息文案。
                    crash_watchdog.unwatch(v.name)
                else:
                    msg = self.php_mgr.restart(v)
                    crash_watchdog.watch_version(v.name)
                self._queue.put(("op", (v.name, msg)))
            except PortConflictError as e:
                self._queue.put(("conflict", (v.name, str(e))))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", (v.name, t("{action}失败：{err}",
                                                     action=action_text, err=e))))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    def _on_op_done(self, kind: str, payload: tuple[str, str]) -> None:
        """启停/重启结果（kind 为 op 正常完成、conflict 端口冲突）。"""
        name, msg = payload
        self.notify(msg)
        if kind == "conflict":
            messagebox.showwarning(t("端口冲突"), msg, parent=self)
        else:
            messagebox.showinfo(t("操作完成"), msg, parent=self)
        self._refresh_row(name)

    # ------------------------------------------------------------------ #
    # 轻量状态刷新（定时器）
    # ------------------------------------------------------------------ #
    def auto_refresh(self) -> None:
        if self._busy or not self._versions or self._pending_row_refresh:
            return
        self._pending_row_refresh = True

        def worker():
            # 批量刷新：一次 TCP 快照 + 一次进程快照内完成全部版本状态判定
            try:
                self.php_mgr.refresh_all_status(self._versions, fast=True)
                results = {v.name: (v.running, v.pid) for v in self._versions}
            except Exception:  # noqa: BLE001
                results = {}
            self._queue.put(("status", results))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    def _apply_status(self, results: dict) -> None:
        changed = False
        by_name = {x.name: x for x in self._versions}  # 一次建表，避免逐条线性查找
        for name, (running, pid) in results.items():
            v = by_name.get(name)
            if v and (v.running != running or v.pid != pid):
                v.running, v.pid = running, pid
                changed = True
        if changed:
            self._update_rows()

    def _refresh_row(self, name: str) -> None:
        def worker():
            v = next((x for x in self._versions if x.name == name), None)
            if v is None:
                return
            try:
                v.running, v.pid = self.php_mgr.get_status(v, fast=True)
            except Exception:  # noqa: BLE001
                pass
            self._queue.put(("row", v))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    def _update_rows(self) -> None:
        for i, v in enumerate(self._versions):
            iid = self._name_to_iid.get(v.name)
            if not iid:
                continue
            values, tags = self._row(v, i)
            self.tree.item(iid, values=values, tags=tags)
        self._update_buttons()

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def _selected(self) -> PhpVersion | None:
        sel = self.tree.selection()
        if not sel:
            return None
        for v in self._versions:
            if self._name_to_iid.get(v.name) == sel[0]:
                return v
        return None

    def _update_buttons(self) -> None:
        has_sel = self._selected() is not None and not self._busy
        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_port,
                  self.btn_ini, self.btn_edit, self.btn_check, self.btn_ext,
                  self.btn_tune, self.btn_debug):
            b.configure(state="normal" if has_sel else "disabled")
        # 「初始化 php.ini」只在选中版本确实没有生效配置时可用
        v = self._selected()
        need_ini = v is not None and not self.php_mgr.ini_ready(v)
        self.btn_init_ini.configure(
            state="normal" if (need_ini and not self._busy) else "disabled")
        # 选中项变化 / 状态刷新后，同步底部常用指标
        self._update_metrics_bar()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_refresh.configure(state="disabled" if busy else "normal")
        self._update_buttons()

    # ------------------------------------------------------------------ #
    # 端口 / 配置
    # ------------------------------------------------------------------ #
    def _edit_port(self) -> None:
        v = self._selected()
        if v is None:
            return
        PortDialog(self, v, self.config, on_saved=self.refresh_versions)

    def _view_ini(self) -> None:
        v = self._selected()
        if v is None:
            return
        IniDialog(self, v, self.php_mgr)

    def _edit_ini(self) -> None:
        v = self._selected()
        if v is None:
            return
        if not self.php_mgr.ini_ready(v):
            messagebox.showwarning(
                t("无独立配置文件"),
                t("[{name}] 还没有生效的 php.ini：{path}\n"
                  "请先点「初始化 php.ini」生成配置，再使用该功能。",
                  name=v.name, path=self.php_mgr.ini_target(v)),
                parent=self,
            )
            return
        self._bind_metrics_refresh(IniEditDialog(self, v, self.php_mgr))

    def _recommend_settings(self) -> None:
        """按本机硬件给出 php.ini 的开发环境推荐值，由用户勾选后写入。"""
        v = self._selected()
        if v is None:
            return
        if not self.php_mgr.ini_ready(v):
            messagebox.showwarning(
                t("无独立配置文件"),
                t("[{name}] 还没有生效的 php.ini：{path}\n"
                  "请先点「初始化 php.ini」生成配置，再使用该功能。",
                  name=v.name, path=self.php_mgr.ini_target(v)),
                parent=self,
            )
            return
        from core import tuning

        profile = tuning.detect_machine()
        items = tuning.php_suggestions(v.ini, profile, v.display)
        if not items:
            messagebox.showinfo(
                t("推荐设置"),
                t("[{name}] 的 php.ini 已经符合开发环境推荐值，无需改动。", name=v.name),
                parent=self,
            )
            return
        self._bind_metrics_refresh(TuningDialog(
            self,
            t("PHP 推荐设置 · {name}", name=v.name),
            t("配置文件：{file}", file=v.ini),
            profile, items, tuning.php_notes(),
            lambda picked: self._apply_tuning(v, picked),
        ))

    def _apply_tuning(self, v, items) -> tuple[bool, str]:
        """写入勾选的推荐项；返回 (是否成功, 提示文本)。"""
        from core import tuning

        res = tuning.apply_php(v.ini, items)
        self.notify(t("已按推荐值更新 {count} 项配置（{name}）",
                      count=res.get("changed", 0), name=v.name))
        return True, t("已更新 {count} 项配置，备份保留于：\n{backup}\n\n"
                       "重启 [{name}]（停止后启动）即可生效。",
                       count=res.get("changed", 0), backup=res.get("backup", ""),
                       name=v.name)

    def _init_ini(self) -> None:
        """为没有生效配置的 PHP 版本生成 php.ini（官方模板优先，其次内置骨架）。"""
        v = self._selected()
        if v is None:
            return
        target = self.php_mgr.ini_target(v)
        if os.path.exists(target):
            messagebox.showinfo(
                t("初始化 php.ini"),
                t("[{name}] 已有配置文件：{path}", name=v.name, path=target),
                parent=self,
            )
            self._update_buttons()
            return
        src, src_desc = self.php_mgr.ini_template(v)
        if not messagebox.askyesno(
                t("初始化 php.ini"),
                t("[{name}] 当前没有生效的 php.ini，「编辑配置 / 推荐设置」不可用。\n\n"
                  "将生成：{path}\n来源：{src}\n\n生成后需重启该版本生效。是否继续？",
                  name=v.name, path=target, src=src_desc),
                parent=self):
            return
        ok, msg = self.php_mgr.init_ini(v)
        if not ok:
            self.notify(msg)
            messagebox.showerror(t("初始化 php.ini"), msg, parent=self)
            return
        self.notify(msg)
        messagebox.showinfo(t("初始化 php.ini"), msg, parent=self)
        # v.ini 已同步：直接重绘本行，无需重新扫描
        self._update_rows()

    def _open_xdebug(self) -> None:
        """Xdebug 调试开关（写 php.ini，自动备份 + 自检，失败还原）。"""
        v = self._selected()
        if v is None:
            return
        self._bind_metrics_refresh(
            XdebugDialog(self, v, on_restart=lambda: self._operate("restart"),
                         notify=lambda msg: self.notify(msg)))

    def _self_check(self) -> None:
        v = self._selected()
        if v is None:
            return
        SelfCheckDialog(self, v, HealthMonitor())

    def _download_version(self) -> None:
        """macOS 引导 Homebrew；Windows 打开官方下载安装对话框。"""
        if not IS_WIN:
            messagebox.showinfo(
                t("macOS 安装 PHP 版本"),
                t("macOS 下请用 Homebrew 安装，phpvm 会自动发现已安装的 keg：\n\n"
                  "  brew install php@8.1        # 示例：PHP 8.1\n"
                  "  brew install php@7.4 php@5.6\n\n"
                  "安装完成回到本页点「刷新」即出现新版本。\n"
                  "也可以自行放置官方二进制到 ~/wnrp/phpNN/（含 bin/php-cgi）后刷新。"),
                parent=self,
            )
            return

        def on_installed():
            self.notify(t("新版本已安装，正在刷新列表…"))
            self.refresh_versions()

        DownloadDialog(self, self.php_mgr, self.config, on_installed=on_installed)

    def _manage_ext(self) -> None:
        v = self._selected()
        if v is None:
            return
        if not IS_WIN:
            messagebox.showinfo(
                t("macOS 扩展管理"),
                t("[{name}] 运行于 macOS，扩展不再使用 Windows .dll 安装页：\n\n"
                  "已随 brew 公式编译的扩展（redis/memcached/imap 等）装好即默认加载；\n"
                  "其它 PECL 扩展请在终端安装（默认装到当前 brew 默认 PHP，多版本并存时\n"
                  "建议先切 keg：brew link php@8.1 --force）：\n"
                  "  pecl install redis\n\n"
                  "启用/禁用请点上方「编辑配置」改对应 php.ini：\n"
                  "  extension=redis.so\n"
                  "brew 的 ini 一般在 /opt/homebrew/etc/php/<版本>/php.ini"
                  "（Intel 前缀为 /usr/local）。",
                  name=v.name),
                parent=self,
            )
            return
        self._bind_metrics_refresh(ExtensionDialog(self, v, self.php_mgr))

    def _composer(self) -> None:
        """检测 Composer，并在所选 PHP 版本的 PATH 下打开终端查看版本。"""
        v = self._selected()
        if v is None:
            return
        from core import tool_manager

        info = tool_manager.info()
        if not info["ok"]:
            messagebox.showinfo(t("Composer"), info["message"], parent=self)
            self.notify(t("未检测到 Composer"))
            return
        if tool_manager.open_composer_terminal(php_dir=v.dir):
            self.notify(t("Composer（{ver}）已在终端打开 · PHP 版本：{name}",
                          ver=info["version"], name=v.name))
        else:
            messagebox.showinfo(
                t("Composer"),
                t("已检测到 Composer：{path}\n{ver}\n\n"
                  "无法自动打开终端，可手动执行：\n"
                  "  set PATH={dir};%PATH%\n  composer -V",
                  path=info["path"], ver=info["version"], dir=v.dir),
                parent=self,
            )

    def _open_terminal(self) -> None:
        """新开终端，PATH 前置所选 PHP 版本目录（新窗口生效）。"""
        v = self._selected()
        if v is None:
            return
        if pu.open_terminal(cwd=v.dir, path_prepend=v.dir):
            self.notify(t("已打开终端：{name}（PATH 已前置该版本目录）", name=v.name))
        else:
            messagebox.showwarning(
                t("无法打开终端"),
                t("未找到可用的终端程序。可手动执行：\n  set PATH={dir};%PATH%", dir=v.dir),
                parent=self,
            )
