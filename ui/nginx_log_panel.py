# -*- coding: utf-8 -*-
"""Nginx 日志面板：查看 logs 目录下的 access/error 日志。

- 文件下拉选择 logs 目录内 *.log*
- 首次加载读取文件尾部（最多 256KB / 2000 行），之后增量追加
- 文件轮转（大小变小）自动重置为尾部读取
- 「自动跟随」勾选时随主窗口 tick 增量刷新
"""
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk

from .theme import ERR, FONT, GRAY, LOG_BG, LOG_FG, OK, TEXT_DIM

NGINX_LOGS_DIR = r"C:\wnrp\nginx\logs"
TAIL_BYTES = 256 * 1024
MAX_LINES = 2000
_DEFAULT_PREFER = ("error.log", "access.log")


class NginxLogPanel(ttk.Frame):
    def __init__(self, master, notify):
        super().__init__(master, padding=8)
        self.notify = notify
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._offset = 0  # 已读到的文件偏移
        self._files: list[str] = []
        self._build()
        self.refresh_file_list()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 6))

        ttk.Label(top, text="日志文件：", style="Section.TLabel").pack(side="left")
        self.file_var = tk.StringVar()
        self.file_cb = ttk.Combobox(top, textvariable=self.file_var, state="readonly", width=28)
        self.file_cb.pack(side="left", padx=(0, 8))
        self.file_cb.bind("<<ComboboxSelected>>", lambda e: self._reset_and_reload())

        self.follow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="自动跟随", variable=self.follow_var).pack(side="left", padx=(0, 8))

        ttk.Button(top, text="刷新", command=self.reload).pack(side="left", padx=(0, 4))
        ttk.Button(top, text="清屏", command=self.clear_view).pack(side="left", padx=(0, 4))
        ttk.Button(top, text="打开目录", command=self._open_dir).pack(side="left")

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
        self.text.tag_configure("warn", foreground="#FFD24A")

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
        """切换文件：清空显示并重置偏移后重新加载。"""
        self._offset = 0
        self._clear_text()
        self.reload()

    def clear_view(self) -> None:
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
        size_txt = f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"
        from datetime import datetime
        mt = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
        self.info_var.set(f"{size_txt} · 修改 {mt}")
        if kind2 == "full":
            self._clear_text()
            self._insert(content, "warn")
        elif content:
            self._insert(content, "err" if self._is_error_log() else "")

    def _is_error_log(self) -> bool:
        return "error" in self.file_var.get().lower()

    def _insert(self, content: str, tag: str) -> None:
        self.text.configure(state="normal")
        start = self.text.index("end-1c")
        self.text.insert("end", content)
        if not content.endswith("\n"):
            self.text.insert("end", "\n")
        if tag:
            self.text.tag_add(tag, start, "end-1c")
        self.text.see("end")
        self.text.configure(state="disabled")

    # ------------------------------------------------------------------ #
    def auto_refresh(self) -> None:
        """主窗口 tick 调用：仅「自动跟随」勾选时增量刷新。"""
        if self.follow_var.get():
            self.reload()
