# -*- coding: utf-8 -*-
"""Nginx 日志面板：查看 logs 目录下的 access/error 日志。

- 文件下拉选择 logs 目录内 *.log*
- 首次加载读取文件尾部（最多 256KB / 2000 行），之后增量追加
- 文件轮转（大小变小）自动重置为尾部读取
- 「自动跟随」勾选时随主窗口 tick 增量刷新
- 过滤框：关键字实时过滤显示（不区分大小写），匹配处高亮
- 行级统计：error / warn 行计数展示在状态栏
- 行内高亮：error 红、warn 橙、时间戳蓝、HTTP 4xx/5xx 高亮
"""
import os
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk

from .theme import ERR, LOG_ACCENT, LOG_BG, LOG_FG, WARN

NGINX_LOGS_DIR = r"C:\wnrp\nginx\logs"
TAIL_BYTES = 256 * 1024
MAX_LINES = 2000
_DEFAULT_PREFER = ("error.log", "access.log")

_TIME_RE = re.compile(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}")
_CODE_RE = re.compile(r"\s([45]\d{2})\s")
_ERR_WORDS = ("[error]", "[crit]", "[alert]", "[emerg]")
_WARN_WORDS = ("[warn]", "[notice]")


class NginxLogPanel(ttk.Frame):
    def __init__(self, master, notify):
        super().__init__(master, padding=8)
        self.notify = notify
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._busy = False
        self._offset = 0  # 已读到的文件偏移
        self._files: list[str] = []
        self._lines: list[str] = []  # 已读行缓存（用于过滤重绘）
        self._stats = {"err": 0, "warn": 0, "other": 0}
        self._size = 0
        self._mtime = 0.0
        self._build()
        self.refresh_file_list()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 6))

        ttk.Label(top, text="日志文件：", style="Section.TLabel").pack(side="left")
        self.file_var = tk.StringVar()
        self.file_cb = ttk.Combobox(top, textvariable=self.file_var, state="readonly", width=22)
        self.file_cb.pack(side="left", padx=(0, 8))
        self.file_cb.bind("<<ComboboxSelected>>", lambda e: self._reset_and_reload())

        self.follow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="自动跟随", variable=self.follow_var).pack(side="left", padx=(0, 8))

        ttk.Button(top, text="刷新", command=self.reload).pack(side="left", padx=(0, 4))
        ttk.Button(top, text="清屏", command=self.clear_view).pack(side="left", padx=(0, 4))
        ttk.Button(top, text="打开目录", command=self._open_dir).pack(side="left", padx=(0, 10))

        ttk.Label(top, text="过滤：", style="Section.TLabel").pack(side="left")
        self.filter_var = tk.StringVar()
        filter_entry = ttk.Entry(top, textvariable=self.filter_var, width=16)
        filter_entry.pack(side="left", padx=(0, 8))
        filter_entry.bind("<KeyRelease>", lambda e: self._render_filtered())
        filter_entry.bind("<Return>", lambda e: self._render_filtered())

        self.info_var = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.info_var, style="SubTitle.TLabel").pack(side="left", padx=(10, 0))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.text = tk.Text(
            wrap, wrap="char", font=("Consolas", 9),
            background=LOG_BG, foreground=LOG_FG, relief="flat", padx=10, pady=8,
            state="disabled",
        )
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=vsb.set)
        self.text.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.text.tag_configure("err", foreground=ERR)
        self.text.tag_configure("warn", foreground=WARN)
        self.text.tag_configure("ts", foreground=LOG_ACCENT)
        self.text.tag_configure("code4", foreground=WARN)
        self.text.tag_configure("code5", foreground=ERR)
        self.text.tag_configure("hl", background="#3B2F00")

    # ------------------------------------------------------------------ #
    def refresh_file_list(self) -> None:
        try:
            files = sorted(
                f for f in os.listdir(NGINX_LOGS_DIR)
                if f.endswith(".log") or f.endswith((".log.1", ".log.2"))
            )
        except OSError:
            files = []
        self._files = files
        self.file_cb.configure(values=files)
        if files:
            if self.file_var.get() not in files:
                prefer = next((f for f in _DEFAULT_PREFER if f in files), files[0])
                self.file_var.set(prefer)
            self._reset_and_reload()
        else:
            self.info_var.set("logs 目录无 .log 文件")

    def _current_path(self) -> str | None:
        name = self.file_var.get()
        return os.path.join(NGINX_LOGS_DIR, name) if name else None

    def _reset_and_reload(self) -> None:
        """切换文件：清空显示/缓存并重置偏移后重新加载。"""
        self._offset = 0
        self._lines = []
        self._stats = {"err": 0, "warn": 0, "other": 0}
        self._clear_text()
        self.reload()

    def clear_view(self) -> None:
        """清屏：仅清显示区，行缓存保留（后续增量/过滤仍基于缓存）。"""
        self._clear_text()

    def _clear_text(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def _open_dir(self) -> None:
        try:
            os.startfile(NGINX_LOGS_DIR)
        except OSError as e:
            self.notify(f"打开目录失败：{e}")

    # ------------------------------------------------------------------ #
    def reload(self) -> None:
        if self._busy:
            return
        path = self._current_path()
        if not path or not os.path.exists(path):
            self.info_var.set("文件不存在")
            return
        self._busy = True

        def worker():
            try:
                size = os.path.getsize(path)
                mtime = os.path.getmtime(path)
                # 文件轮转或首次加载 → 重新读尾部
                if self._offset == 0 or size < self._offset:
                    content = self._tail(path)
                    kind = "full"
                else:
                    with open(path, "rb") as f:
                        f.seek(self._offset)
                        content = f.read(size - self._offset).decode("utf-8", errors="replace")
                    kind = "incr"
                self._offset = size
                self._queue.put(("data", (kind, content, size, mtime)))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", f"{type(e).__name__}：{e}"))

        threading.Thread(target=worker, daemon=True).start()
        self._poll()

    def _tail(self, path: str) -> str:
        """读取文件尾部（最多 TAIL_BYTES / MAX_LINES 行）。"""
        size = os.path.getsize(path)
        read_len = min(size, TAIL_BYTES)
        with open(path, "rb") as f:
            f.seek(size - read_len)
            data = f.read(read_len)
        text = data.decode("utf-8", errors="replace")
        if read_len < size:
            idx = text.find("\n")
            if idx >= 0:
                text = text[idx + 1:]  # 丢弃首行截断的半行
        lines = text.splitlines()
        return "\n".join(lines[-MAX_LINES:])

    def _poll(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            self.after(60, self._poll)
            return
        self._busy = False
        if kind == "error":
            self.info_var.set(payload)
            return
        kind2, content, size, mtime = payload
        self._size, self._mtime = size, mtime
        if kind2 == "full":
            self._clear_text()
            self._lines = []
            self._stats = {"err": 0, "warn": 0, "other": 0}
        self._append_lines(content.splitlines())
        self._update_info()

    def _append_lines(self, lines: list[str]) -> None:
        """新增行：更新行缓存与统计，按过滤条件插入显示。"""
        if not lines:
            self._update_info()
            return
        self._lines.extend(lines)
        if len(self._lines) > MAX_LINES:
            self._lines = self._lines[-MAX_LINES:]
        for line in lines:
            cls = self._classify(line)
            self._stats[cls or "other"] += 1
        kw = self.filter_var.get().strip()
        self.text.configure(state="normal")
        for line in lines:
            if kw and kw.lower() not in line.lower():
                continue
            self._insert_line(line)
        self.text.see("end")
        self.text.configure(state="disabled")
        self._update_info()

    def _render_filtered(self) -> None:
        """过滤关键字变化：基于行缓存全量重绘。"""
        kw = self.filter_var.get().strip()
        self._clear_text()
        self.text.configure(state="normal")
        for line in self._lines:
            if kw and kw.lower() not in line.lower():
                continue
            self._insert_line(line)
        self.text.see("end")
        self.text.configure(state="disabled")
        self._update_info()

    # ------------------------------------------------------------------ #
    def _classify(self, line: str) -> str:
        """返回 err / warn / ''（普通行）。error 日志按级别词，access 按状态码。"""
        low = line.lower()
        if any(w in low for w in _ERR_WORDS):
            return "err"
        if any(w in low for w in _WARN_WORDS):
            return "warn"
        m = _CODE_RE.search(line)
        if m:
            return "err" if m.group(1)[0] == "5" else "warn"
        return ""

    def _insert_line(self, line: str) -> None:
        self.text.insert("end", line + "\n")
        start = self.text.index("end-1c")
        self._tag_line(start, line)

    def _tag_line(self, start: str, line: str) -> None:
        """单行着色：级别 / 时间戳 / HTTP 状态码 / 关键字高亮。"""
        cls = self._classify(line)
        if cls:
            self.text.tag_add(cls, start, "end-1c")
        m = _TIME_RE.search(line)
        if m:
            self.text.tag_add("ts", f"{start}+{m.start()}c", f"{start}+{m.end()}c")
        m = _CODE_RE.search(line)
        if m:
            code = m.group(1)
            tag = "code5" if code[0] == "5" else "code4"
            self.text.tag_add(tag, f"{start}+{m.start(1)}c", f"{start}+{m.end(1)}c")
        kw = self.filter_var.get().strip()
        if kw:
            low, k = line.lower(), kw.lower()
            pos, step = 0, len(k)
            while True:
                i = low.find(k, pos)
                if i < 0:
                    break
                self.text.tag_add("hl", f"{start}+{i}c", f"{start}+{i + step}c")
                pos = i + step

    # ------------------------------------------------------------------ #
    def _update_info(self) -> None:
        parts = []
        size_txt = (
            f"{self._size / 1024:.0f} KB" if self._size < 1024 * 1024
            else f"{self._size / 1024 / 1024:.1f} MB"
        )
        from datetime import datetime
        mt = datetime.fromtimestamp(self._mtime).strftime("%H:%M:%S")
        total = len(self._lines)
        parts.append(f"{size_txt} · {mt} · {total} 行")
        kw = self.filter_var.get().strip()
        if kw:
            shown = sum(1 for ln in self._lines if kw.lower() in ln.lower())
            parts.append(f"显示 {shown}")
        if self._stats["err"]:
            parts.append(f"错误 {self._stats['err']}")
        if self._stats["warn"]:
            parts.append(f"警告 {self._stats['warn']}")
        self.info_var.set(" · ".join(parts))

    # ------------------------------------------------------------------ #
    def auto_refresh(self) -> None:
        """主窗口 tick 调用：仅「自动跟随」勾选时增量刷新。"""
        if self.follow_var.get():
            self.reload()
