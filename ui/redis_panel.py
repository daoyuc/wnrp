# -*- coding: utf-8 -*-
"""Redis 管理页：实例选择 + 状态卡片/控制按钮/日志 + Redis 命令 + DB 键空间图表。

布局：顶部实例选择行（跨全部功能），主体为三个页签：
- 状态与日志：运行状态卡片、控制按钮、操作日志；
- Redis 命令：选择目标 DB 后执行任意 Redis 命令并查看结果；
- DB 键空间：Canvas 柱状图展示各逻辑库 key 数（忽略空库）。
"""
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

from core import process_utils as pu
from core.redis_manager import RedisInstance, RedisManager  # pyright: ignore[reportImplicitRelativeImport]
from .theme import (
    CARD_BG, ERR, FONT, GRAY, LOG_ACCENT, LOG_BG, LOG_FG, OK,
    PRIMARY, PRIMARY_DARK, TEXT, TEXT_DIM, WARN,
)

# 执行前需要二次确认的命令（首词小写匹配）
_DANGEROUS_CMDS = {
    "flushall", "flushdb", "shutdown", "slaveof", "replicaof", "debug",
}

_DBS = [str(i) for i in range(16)]  # 逻辑库下拉 0..15（databases 默认 16）


class RedisPanel(ttk.Frame):
    # 控件在 _build / *_page 等方法中创建（跨方法链无法被静态跟踪），
    # 提前声明类型以满足 reportUninitializedInstanceVariable 检查。
    dot_label: ttk.Label
    state_label: ttk.Label
    info_vars: dict[str, tk.StringVar]
    log_text: tk.Text
    instance_var: tk.StringVar
    instance_cb: ttk.Combobox
    page_status: ttk.Frame
    page_cmd: ttk.Frame
    page_keyspace: ttk.Frame
    _ctrl_btns: list[ttk.Button]
    db_var: tk.StringVar
    cmd_entry: ttk.Entry
    btn_exec: ttk.Button
    cmd_out: tk.Text
    cmd_state: ttk.Label
    ks_state: ttk.Label
    btn_ks_refresh: ttk.Button
    ks_canvas: tk.Canvas

    def __init__(self, master, redis_mgr: RedisManager, notify):
        super().__init__(master, padding=8)
        self.redis_mgr = redis_mgr
        self.notify = notify

        self._queue: queue.Queue[Any] = queue.Queue()
        self._busy = False              # 启停类操作忙
        self._pending_refresh = False
        self._cmd_busy = False          # 命令执行忙
        self._ks_busy = False           # 键空间统计忙
        self._inst_running = False      # 当前选中实例运行中（键空间页空态判定）
        self._ks_stats: list[tuple[int, int]] | None = None  # 键空间数据
        self._ks_ts = ""                # 上次统计时间

        self._build()
        self.refresh_status()
        self.refresh_keyspace()
        self._start_drain()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        # 顶部：实例选择（全部页签共享）
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="Redis 实例：", font=(FONT, 9, "bold")).pack(side="left")
        self.instance_var = tk.StringVar()
        self.instance_cb = ttk.Combobox(top, textvariable=self.instance_var, state="readonly",
                                        width=24, font=(FONT, 9))
        self.instance_cb.pack(side="left", padx=(4, 10))
        self.instance_cb.bind("<<ComboboxSelected>>", lambda e: self._on_select())
        self.instance_cb["values"] = [i.name for i in self.redis_mgr.instances]
        self.instance_var.set(self._default_name())
        ttk.Button(top, text="刷新状态", command=self.refresh_status).pack(side="left")

        # 主体：三个页签
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True)
        self.page_status = self._build_status_page(nb)
        self.page_cmd = self._build_cmd_page(nb)
        self.page_keyspace = self._build_keyspace_page(nb)
        nb.add(self.page_status, text="  状态与日志  ")
        nb.add(self.page_cmd, text="  Redis 命令  ")
        nb.add(self.page_keyspace, text="  DB 键空间  ")

    # ------------------------------------------------------------------ #
    # 页签 1：状态与日志（原布局迁入）
    # ------------------------------------------------------------------ #
    def _build_status_page(self, master) -> ttk.Frame:
        page = ttk.Frame(master, padding=(2, 6, 2, 2))

        left = ttk.Frame(page)
        left.pack(side="left", fill="y", padx=(0, 10))

        card = ttk.LabelFrame(left, text="运行状态", padding=14)
        card.pack(fill="x")
        self.dot_label = ttk.Label(card, text="●", font=(FONT, 16, "bold"), foreground=GRAY)
        self.dot_label.pack(anchor="w")
        self.state_label = ttk.Label(card, text="检测中…", font=(FONT, 12, "bold"),
                                     foreground=PRIMARY_DARK)
        self.state_label.pack(anchor="w", pady=(4, 8))

        info_grid = ttk.Frame(card)
        info_grid.pack(anchor="w")
        self.info_vars = {}
        for i, (k, _) in enumerate(INFO_ROWS):
            ttk.Label(info_grid, text=f"{k}：", font=(FONT, 9, "bold")).grid(
                row=i, column=0, sticky="e", pady=2
            )
            var = tk.StringVar(value="—")
            self.info_vars[k] = var
            ttk.Label(info_grid, textvariable=var, font=(FONT, 9)).grid(
                row=i, column=1, sticky="w", padx=(6, 0), pady=2
            )

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(10, 0))
        btn_defs = [
            ("btn_start", "启动 Redis", "Accent.TButton", lambda: self._run("start")),
            ("btn_restart", "重启", None, lambda: self._run("restart")),
            ("btn_stop", "停止 Redis", "Danger.TButton", lambda: self._run("stop")),
            ("btn_ping", "测试连接 (PING)", None, lambda: self._run("ping")),
            ("btn_conf", "打开配置文件", None, self._open_conf),
            ("btn_rrefresh", "刷新状态", None, self.refresh_status),
        ]
        self._ctrl_btns: list[ttk.Button] = []
        for idx, (attr, text, style, cmd) in enumerate(btn_defs):
            btn = ttk.Button(btns, text=text, command=cmd)
            if style:
                btn.configure(style=style)
            btn.grid(row=idx // 2, column=idx % 2, sticky="ew", padx=(0, 6), pady=(0, 6))
            setattr(self, attr, btn)
            self._ctrl_btns.append(btn)
        for c in range(2):
            btns.columnconfigure(c, weight=1, uniform="rb")

        ttk.Label(
            left,
            text="提示：Redis 监听端口在各自配置文件中\n（port 项），修改后重启 Redis 生效。",
            foreground=TEXT_DIM,
            font=(FONT, 8),
            justify="left",
        ).pack(anchor="w", pady=(10, 0))

        # 右侧：命令输出日志
        right = ttk.Frame(page)
        right.pack(side="left", fill="both", expand=True)
        ttk.Label(right, text="命令输出", foreground=PRIMARY_DARK,
                  font=(FONT, 9, "bold")).pack(anchor="w", pady=(0, 4))
        log_wrap = ttk.Frame(right)
        log_wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            log_wrap, wrap="word", font=("Consolas", 9),
            background=LOG_BG, foreground=LOG_FG,
            relief="flat", padx=10, pady=8, state="disabled",
        )
        vsb = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.log_text.tag_configure("ok", foreground=OK)
        self.log_text.tag_configure("err", foreground=ERR)
        self.log_text.tag_configure("warn", foreground=WARN)
        self.log_text.tag_configure("info", foreground=LOG_ACCENT)

        self._append_log("== phpvm Redis 管理器 ==", "info")
        if self.redis_mgr.instances:
            for inst in self.redis_mgr.instances:
                self._append_log(
                    f"发现实例 [{inst.name}]：{inst.server}（端口 {inst.port}）", "info"
                )
        else:
            self._append_log("未找到 redis-server 实例（请将 redis 安装目录放入环境根目录下的 Redis* 文件夹）", "warn")
        return page

    # ------------------------------------------------------------------ #
    # 页签 2：Redis 命令
    # ------------------------------------------------------------------ #
    def _build_cmd_page(self, master) -> ttk.Frame:
        page = ttk.Frame(master, padding=(2, 6, 2, 2))

        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="目标 DB：", font=(FONT, 9, "bold")).pack(side="left")
        self.db_var = tk.StringVar(value="0")
        db_cb = ttk.Combobox(bar, textvariable=self.db_var, state="readonly",
                             values=_DBS, width=4, font=(FONT, 9))
        db_cb.pack(side="left", padx=(0, 10))
        ttk.Label(bar, text="命令：", font=(FONT, 9, "bold")).pack(side="left")
        self.cmd_entry = ttk.Entry(bar, font=("Consolas", 9))
        self.cmd_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.cmd_entry.bind("<Return>", self._exec_cmd)
        self.btn_exec = ttk.Button(bar, text="执 行", style="Accent.TButton",
                                   command=self._exec_cmd)
        self.btn_exec.pack(side="left")

        ttk.Label(
            page,
            text="示例：SET k1 hello / GET k1 / INFO / DBSIZE / KEYS *  （FLUSHALL、FLUSHDB、SHUTDOWN 等危险命令执行前需确认）",
            foreground=TEXT_DIM, font=(FONT, 8),
        ).pack(anchor="w", pady=(0, 6))

        wrap = ttk.Frame(page)
        wrap.pack(fill="both", expand=True)
        self.cmd_out = tk.Text(
            wrap, wrap="word", font=("Consolas", 9),
            background=LOG_BG, foreground=LOG_FG,
            relief="flat", padx=10, pady=8, state="disabled",
        )
        cvsb = ttk.Scrollbar(wrap, orient="vertical", command=self.cmd_out.yview)
        self.cmd_out.configure(yscrollcommand=cvsb.set)
        self.cmd_out.pack(side="left", fill="both", expand=True)
        cvsb.pack(side="right", fill="y")
        self.cmd_out.tag_configure("cmd", foreground=LOG_ACCENT,
                                   font=("Consolas", 9, "bold"))
        self.cmd_out.tag_configure("err", foreground=ERR)
        self.cmd_out.tag_configure("info", foreground=LOG_ACCENT)
        self.cmd_state = ttk.Label(page, text="", foreground=TEXT_DIM, font=(FONT, 8))
        self.cmd_state.pack(anchor="w", pady=(6, 0))
        self._append_cmd("输入 Redis 命令后回车执行，输出显示在此区。", "info")
        return page

    # ------------------------------------------------------------------ #
    # 页签 3：DB 键空间图表
    # ------------------------------------------------------------------ #
    def _build_keyspace_page(self, master) -> ttk.Frame:
        page = ttk.Frame(master, padding=(2, 6, 2, 2))

        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="各逻辑库 key 数量统计（忽略空库）",
                  foreground=PRIMARY_DARK, font=(FONT, 9, "bold")).pack(side="left")
        self.ks_state = ttk.Label(bar, text="", foreground=TEXT_DIM, font=(FONT, 8))
        self.ks_state.pack(side="right", padx=(8, 0))
        self.btn_ks_refresh = ttk.Button(bar, text="刷新", command=self.refresh_keyspace)
        self.btn_ks_refresh.pack(side="right")

        self.ks_canvas = tk.Canvas(page, background=CARD_BG, highlightthickness=1,
                                   highlightbackground="#D3DCE8")
        self.ks_canvas.pack(fill="both", expand=True)
        self.ks_canvas.bind("<Configure>", lambda e: self._draw_keyspace())
        self.ks_canvas.bind("<Button-1>", lambda e: self.refresh_keyspace())
        return page

    # ------------------------------------------------------------------ #
    # 实例选择
    # ------------------------------------------------------------------ #
    @staticmethod
    def _version_key(name: str) -> tuple[int, ...]:
        """从实例目录名提取版本号用于排序，如 Redis-8.4.4 → (8,4,4)。"""
        nums = re.findall(r"\d+", name)
        return tuple(int(x) for x in nums) if nums else (0,)

    def _default_name(self) -> str:
        """默认选中实例：版本号最高的目录（Redis-8.4.4 > Redis）。"""
        if not self.redis_mgr.instances:
            return ""
        return max(self.redis_mgr.instances, key=lambda i: self._version_key(i.name)).name

    def _instance(self) -> RedisInstance | None:
        name = self.instance_var.get()
        for inst in self.redis_mgr.instances:
            if inst.name == name:
                return inst
        # 当前值不在实例列表中（如尚未设置）→ 回退默认高版本
        default = self._default_name()
        for inst in self.redis_mgr.instances:
            if inst.name == default:
                return inst
        return self.redis_mgr.instances[0] if self.redis_mgr.instances else None

    def _on_select(self) -> None:
        inst = self._instance()
        if inst is None:
            return
        self._append_log(f"已选择实例 [{inst.name}]（端口 {inst.port}）", "info")
        self.refresh_status()
        self.refresh_keyspace()

    # ------------------------------------------------------------------ #
    # 队列消息分发（常驻 drain：worker 线程只 queue.put，全部 UI 操作
    # 收敛到主线程定时循环，避免跨线程操作 Tkinter）
    # ------------------------------------------------------------------ #
    def _start_drain(self) -> None:
        self.after(150, self._drain)

    def _drain(self) -> None:
        try:
            while True:
                item = self._queue.get_nowait()
                try:
                    self._dispatch(*item)
                except Exception as e:  # noqa: BLE001
                    self._append_log(f"内部错误：{e}", "err")
        except queue.Empty:
            pass
        self.after(150, self._drain)

    def _dispatch(self, kind: str, payload: Any) -> None:
        if kind == "status":
            sel_name, data, ver = payload
            self._set_busy(False)
            names = [i.name for i in self.redis_mgr.instances]
            if not self.instance_var.get() or self.instance_var.get() not in names:
                self.instance_var.set(self._default_name())
            self._render_status(sel_name, data, ver)
            self._draw_keyspace()  # 运行状态变化后重绘键空间图（幂等）
        elif kind == "auto":
            self._pending_refresh = False
            data = payload
            inst = self._instance()
            if inst is not None and inst.name in data:
                self._render_status(inst.name, data, self.info_vars["版本"].get())
                self._draw_keyspace()
        elif kind == "op":
            action, msg = payload
            self._set_busy(False)
            if action == "ping":
                tag = "ok" if "PONG" in msg else "warn"
                self._append_log(f"[PING] {msg}", tag)
            else:
                ok = "成功" in msg and "失败" not in msg
                tag = "ok" if ok else ("warn" if "未在运行" in msg or "已在运行" in msg else "err")
                self._append_log(f"[{action}] {msg}", tag)
            self.notify(msg)
            self.refresh_status()
            if action in ("start", "stop", "restart"):
                self.refresh_keyspace()
        elif kind == "error":
            self._set_busy(False)
            messagebox.showerror("错误", payload, parent=self)
            self._append_log(payload, "err")
        elif kind == "cmd_result":
            cmd_line, res = payload
            self._set_cmd_busy(False)
            self.cmd_state.configure(text="")
            self._render_cmd_result(cmd_line, res)
            self.refresh_keyspace()  # 写入/删除后及时反映键空间变化
        elif kind == "keyspace":
            name, stats = payload
            self._ks_busy = False
            self.btn_ks_refresh.configure(state="normal")
            inst = self._instance()
            if inst is not None and inst.name == name:
                self._ks_stats = stats
                self._ks_ts = time.strftime("%H:%M:%S")
                self._draw_keyspace()

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    def refresh_status(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        inst = self._instance()  # 主线程取实例，worker 线程不碰 Tk
        if inst is None:
            self._set_busy(False)
            return

        def worker():
            try:
                data: dict[str, tuple[bool, list[int]]] = {}
                for i in self.redis_mgr.instances:
                    running, pids = self.redis_mgr.get_status(i)
                    data[i.name] = (running, pids)
                ver = self.redis_mgr.get_version(inst)
                self._queue.put(("status", (inst.name, data, ver)))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", f"状态获取失败：{e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _render_status(self, sel_name: str, data: dict[str, tuple[bool, list[int]]], ver: str) -> None:
        inst = self._instance()
        if inst is None or inst.name not in data:
            return
        running, pids = data[inst.name]
        self._inst_running = running
        if running:
            self.dot_label.configure(text="●", foreground=OK)
            self.state_label.configure(text="运行中", foreground=OK)
            self.info_vars["PID"].set(", ".join(map(str, pids)) if pids else "—")
        else:
            self.dot_label.configure(text="○", foreground=GRAY)
            self.state_label.configure(text="已停止", foreground=GRAY)
            self.info_vars["PID"].set("—")
        self.info_vars["版本"].set(ver)
        self.info_vars["端口"].set(str(inst.port))
        self.info_vars["配置"].set(inst.conf or "—")
        self.info_vars["数据目录"].set(inst.dir)

    def auto_refresh(self) -> None:
        if self._busy or self._pending_refresh or not self.redis_mgr.instances:
            return
        self._pending_refresh = True

        def worker():
            try:
                data = {}
                for i in self.redis_mgr.instances:
                    running, pids = self.redis_mgr.get_status(i)
                    data[i.name] = (running, pids)
                self._queue.put(("auto", data))
            except Exception:  # noqa: BLE001
                self._queue.put(("auto", {}))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    # 启停操作
    # ------------------------------------------------------------------ #
    def _run(self, action: str) -> None:
        if self._busy:
            return
        inst = self._instance()
        if inst is None:
            messagebox.showwarning("提示", "未发现 Redis 实例。", parent=self)
            return
        self._set_busy(True)

        def worker():
            try:
                msg = getattr(self.redis_mgr, action)(inst)
                if action == "ping" and not msg:
                    msg = "未找到 redis-cli 可执行文件，无法测试连接"
                self._queue.put(("op", (action, msg)))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", f"{action} 失败：{e}"))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Redis 命令
    # ------------------------------------------------------------------ #
    def _exec_cmd(self, event=None) -> None:  # noqa: ARG002
        if self._cmd_busy:
            return
        inst = self._instance()
        if inst is None:
            messagebox.showwarning("提示", "未发现 Redis 实例。", parent=self)
            return
        line = self.cmd_entry.get().strip()
        if not line:
            return
        first = line.split()[0].strip('"').lower()
        if first in _DANGEROUS_CMDS:
            sure = messagebox.askyesno(
                "危险命令确认",
                f"命令 {line!r} 会删除数据或影响运行，确定要执行吗？",
                parent=self,
            )
            if not sure:
                return
        try:
            db = int(self.db_var.get())
        except ValueError:
            db = 0
        self._set_cmd_busy(True)
        self.cmd_state.configure(text=f"执行中（实例 {inst.name} · DB{db}）…")

        def worker():
            try:
                res = self.redis_mgr.run_command(inst, db, line)
            except Exception as e:  # noqa: BLE001
                res = f"(error) 执行异常：{e}"
            self._queue.put(("cmd_result", (line, res)))

        threading.Thread(target=worker, daemon=True).start()

    def _render_cmd_result(self, cmd_line: str, res: str) -> None:
        self._append_cmd(f"> DB{self.db_var.get()} {cmd_line}", "cmd")
        if not res:
            self._append_cmd("(空输出)")
        elif res.startswith("(error)") or "could not connect" in res.lower() \
                or "connection refused" in res.lower():
            self._append_cmd(res, "err")
        else:
            self._append_cmd(res)

    # ------------------------------------------------------------------ #
    # DB 键空间
    # ------------------------------------------------------------------ #
    def refresh_keyspace(self) -> None:
        if self._ks_busy:
            return
        inst = self._instance()
        if inst is None:
            return
        self._ks_busy = True
        self.btn_ks_refresh.configure(state="disabled")

        def worker():
            try:
                stats = self.redis_mgr.keyspace_stats(inst)
            except Exception:  # noqa: BLE001
                stats = None
            self._queue.put(("keyspace", (inst.name, stats)))

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _fmt_keys(n: int) -> str:
        """key 数格式化：过万显示中文万，避免柱顶数字重叠。"""
        if n >= 1000000:
            return f"{n / 10000:.0f}万"
        if n >= 10000:
            return f"{n / 10000:.1f}万"
        return str(n)

    def _draw_keyspace(self) -> None:
        c = self.ks_canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 60 or h < 60:
            return
        stats = self._ks_stats
        inst = self._instance()
        title_top = 10
        c.create_text(
            w // 2, title_top, text="键空间分布（DB → key 数）",
            fill=TEXT_DIM, font=(FONT, 9),
        )
        state_text = ""
        if inst is None:
            state_text = "未发现 Redis 实例"
        elif not self._inst_running:
            state_text = "实例未运行，无法统计键空间"
        elif stats is None:
            state_text = "键空间获取失败，请确认实例可连接后重试"
        elif not stats:
            state_text = "无数据：所有逻辑库均为空"
        if state_text or not stats:
            if state_text:
                c.create_text(w // 2, h // 2, text=state_text, fill=GRAY, font=(FONT, 10))
            return

        total = sum(k for _, k in stats)
        c.create_text(
            w // 2, h - 8,
            text=f"共 {total} 个 key · 最后统计 {self._ks_ts} · 点击图表可刷新",
            fill=TEXT_DIM, font=(FONT, 8),
        )

        # 柱状图区（顶部标题 / 底部汇总信息 / 轴标签留白）
        top_pad = 30
        bottom_pad = 34
        base_y = h - bottom_pad
        chart_h = base_y - top_pad
        max_keys = max(k for _, k in stats)
        n = len(stats)
        left, right = 12.0, float(w) - 12.0
        slot = (right - left) / n
        bar_w = min(46.0, slot * 0.6)

        for i, (db, keys) in enumerate(stats):
            cx = left + slot * i + slot / 2
            bh = max(3.0, (keys / max_keys) * (chart_h - 4))
            color = PRIMARY_DARK if keys == max_keys else PRIMARY
            c.create_rectangle(
                cx - bar_w / 2, base_y - bh, cx + bar_w / 2, base_y,
                fill=color, outline="",
            )
            # 数值（keys 极小时放在柱内，避免压到顶部外）
            ny = base_y - bh - 14
            if bh < 26:
                ny = base_y - bh - 2
            c.create_text(cx, max(ny, 6), text=self._fmt_keys(keys),
                          fill=TEXT, font=(FONT, 8))
            c.create_text(cx, h - 18, text=f"db{db}", fill=TEXT_DIM, font=(FONT, 8))

    # ------------------------------------------------------------------ #
    def _open_conf(self) -> None:
        inst = self._instance()
        if inst is None or not inst.conf:
            messagebox.showwarning("提示", "未找到配置文件。", parent=self)
            return
        pu.open_path(inst.conf)

    def _append_log(self, text: str, tag: str = "") -> None:
        self.log_text.configure(state="normal")
        start = self.log_text.index("end-1c")
        self.log_text.insert("end", text + "\n")
        if tag:
            self.log_text.tag_add(tag, start, "end-1c")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _append_cmd(self, text: str, tag: str = "") -> None:
        self.cmd_out.configure(state="normal")
        start = self.cmd_out.index("end-1c")
        self.cmd_out.insert("end", text + "\n")
        if tag:
            self.cmd_out.tag_add(tag, start, "end-1c")
        self.cmd_out.see("end")
        self.cmd_out.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for b in self._ctrl_btns:
            b.configure(state=state)

    def _set_cmd_busy(self, busy: bool) -> None:
        """命令执行忙状态（独立于启停忙，避免两者互相阻塞）。"""
        self._cmd_busy = busy
        self.btn_exec.configure(state="disabled" if busy else "normal")


INFO_ROWS = [("PID", ""), ("版本", ""), ("端口", ""), ("配置", ""), ("数据目录", "")]
