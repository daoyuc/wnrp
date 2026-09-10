# -*- coding: utf-8 -*-
"""软件更新对话框：检查更新 → 查看更新说明 → 下载 → 一键升级重启。

所有网络与文件操作都在后台线程执行，结果经 ``queue`` 回传，
主线程用 ``after`` 轮询刷新（项目异步纪律：禁止跨线程调用 tk）。
"""
import os
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from core import updater
from core.i18n import t
from .theme import CARD_BG, ERR, FONT, GRAY, OK, PRIMARY, TEXT
from .window_utils import fit_window


def format_size(num: int) -> str:
    """字节数 → 人类可读（KB / MB / GB）。"""
    if not num:
        return "-"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{num} B"


class UpdateDialog(tk.Toplevel):
    """软件更新窗口（可手动打开，也可在启动检查发现新版本后打开）。"""

    FIT_SIZE = (760, 580, 640, 460)

    def __init__(self, master, config, on_status=None, auto_check: bool = True):
        super().__init__(master)
        self.config_obj = config
        self.on_status = on_status

        self._queue: queue.Queue = queue.Queue()
        self._progress = (0, 0)      # (已下载, 总大小)，后台线程写入、主线程读取
        self._cancel = False
        self._release: updater.Release | None = None
        self._pkg = ""
        self._busy = False

        self.title(t("软件更新"))
        self.resizable(False, True)
        self.configure(bg=CARD_BG)
        self.transient(master)
        self.grab_set()

        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=t("软件更新"), style="Title.TLabel").pack(anchor="w")
        self._head_var = tk.StringVar(
            value=t("当前版本 {ver}", ver=updater.current_version()))
        ttk.Label(body, textvariable=self._head_var, style="SubTitle.TLabel").pack(
            anchor="w", pady=(2, 10))

        # ---- 更新说明 ----
        ttk.Label(body, text=t("更新说明"), font=(FONT, 9, "bold"),
                  background=CARD_BG).pack(anchor="w")
        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, pady=(4, 10))
        self._notes = tk.Text(
            wrap, height=12, wrap="word", font=(FONT, 9),
            background="#FAFBFD", foreground=TEXT, relief="solid", borderwidth=1,
        )
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self._notes.yview)
        self._notes.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        self._notes.pack(side="left", fill="both", expand=True)

        # ---- 进度 ----
        self._progress_bar = ttk.Progressbar(body, mode="determinate", maximum=100)
        self._progress_bar.pack(fill="x")
        self._status_var = tk.StringVar(value=t("正在检查更新…"))
        self._status_label = ttk.Label(body, textvariable=self._status_var,
                                       style="SubTitle.TLabel")
        self._status_label.pack(anchor="w", pady=(4, 0))

        # ---- 按钮栏（先 pack：窗口被压小时优先保留）----
        btns = ttk.Frame(body)
        btns.pack(fill="x", side="bottom", pady=(14, 0))
        self._btn_update = ttk.Button(btns, text=t("立即升级"), style="Accent.TButton",
                                      command=self._start_download, state="disabled")
        self._btn_update.pack(side="right")
        self._btn_close = ttk.Button(btns, text=t("稍后"), command=self._on_close_btn)
        self._btn_close.pack(side="right", padx=(0, 8))
        self._btn_skip = ttk.Button(btns, text=t("跳过此版本"), command=self._skip,
                                    state="disabled")
        self._btn_skip.pack(side="left")
        self._btn_page = ttk.Button(btns, text=t("前往发布页"), command=self._open_page,
                                    state="disabled")
        self._btn_page.pack(side="left", padx=(8, 0))

        self._center(master)
        self.after(120, self._poll)
        if auto_check:
            self.start_check()
        else:
            self._set_status(t("点击「检查更新」开始"), GRAY)

    # ------------------------------------------------------------------ #
    def _center(self, master) -> None:
        w, h, mw, mh = self.FIT_SIZE
        fit_window(self, master, width=w, height=h, min_width=mw, min_height=mh)

    # ---- 状态辅助 ----
    def _set_status(self, text: str, color: str = TEXT) -> None:
        self._status_var.set(text)
        try:
            self._status_label.configure(foreground=color)
        except tk.TclError:
            pass

    def _set_notes(self, text: str) -> None:
        self._notes.configure(state="normal")
        self._notes.delete("1.0", "end")
        self._notes.insert("1.0", text or t("（该版本未提供更新说明）"))
        self._notes.configure(state="disabled")

    def _set_buttons(self, update=False, skip=False, page=False, busy=False) -> None:
        self._busy = busy
        self._btn_update.configure(state="normal" if update else "disabled")
        self._btn_skip.configure(state="normal" if skip else "disabled")
        self._btn_page.configure(state="normal" if page else "disabled")
        self._btn_close.configure(text=t("取消下载") if busy else t("稍后"))

    def _on_close_btn(self) -> None:
        """下载中 = 取消下载；空闲 = 关闭窗口。"""
        if self._busy:
            self._cancel = True
            self._set_status(t("正在取消下载…"), GRAY)
            return
        self.destroy()

    def _open_page(self) -> None:
        url = (self._release.page_url if self._release else "") or \
            f"https://github.com/{updater.GITHUB_REPO}/releases"
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            messagebox.showinfo(t("前往发布页"), url, parent=self)

    def _open_pkg_dir(self) -> None:
        from core import process_utils as pu

        if self._pkg:
            pu.open_path(os.path.dirname(self._pkg))

    def _skip(self) -> None:
        if self._release:
            self.config_obj.set_setting("skipped_version", self._release.version)
        self.destroy()

    # ------------------------------------------------------------------ #
    # 后台任务
    # ------------------------------------------------------------------ #
    def start_check(self) -> None:
        """后台查询最新版本。"""
        self._set_status(t("正在检查更新…"), GRAY)
        self._progress_bar.configure(mode="indeterminate", value=0)
        self._progress_bar.start(12)
        self._set_buttons(busy=True)

        def worker():
            try:
                rel = updater.check_update()
                self._queue.put(("checked", rel, ""))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", None, str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _start_download(self) -> None:
        """后台下载安装包（带进度与取消）。"""
        if self._release is None:
            return
        self._cancel = False
        self._progress = (0, 0)
        self._progress_bar.configure(mode="determinate", maximum=100, value=0)
        self._set_status(t("正在下载安装包…"), PRIMARY)
        self._set_buttons(busy=True)

        release = self._release

        def on_progress(done, total):
            self._progress = (done, total)

        def worker():
            try:
                path = updater.download(release, on_progress=on_progress,
                                        cancel=lambda: self._cancel)
                self._queue.put(("downloaded", path, ""))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", None, str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _do_apply(self) -> None:
        """执行替换安装并退出 phpvm（升级脚本负责重新拉起）。"""
        if not self._pkg:
            return
        ok, msg = updater.apply_and_restart(self._pkg)
        if not ok:
            messagebox.showerror(t("升级失败"), msg, parent=self)
            return
        if self.on_status:
            self.on_status(msg)
        self._set_status(msg, OK)
        messagebox.showinfo(t("软件更新"), msg, parent=self)
        updater.exit_app()

    # ------------------------------------------------------------------ #
    # 主线程轮询
    # ------------------------------------------------------------------ #
    def _poll(self) -> None:
        try:
            while True:
                kind, payload, extra = self._queue.get_nowait()
                if kind == "checked":
                    self._on_checked(payload)
                elif kind == "downloaded":
                    self._on_downloaded(payload)
                elif kind == "error":
                    self._on_error(extra)
        except queue.Empty:
            pass

        done, total = self._progress
        if self._busy and total:
            self._progress_bar.configure(value=min(100, done * 100 / total))
            self._set_status(
                t("正在下载安装包… {done} / {total}",
                  done=format_size(done), total=format_size(total)), PRIMARY)
        elif self._busy and done:
            self._set_status(t("正在下载安装包… {done}", done=format_size(done)), PRIMARY)

        if self.winfo_exists():
            self.after(120, self._poll)

    def _on_checked(self, rel) -> None:
        self._progress_bar.stop()
        self._progress_bar.configure(mode="determinate", value=0)
        self._release = rel

        if not updater.is_newer_version(rel.version):
            self._head_var.set(t("当前版本 {cur} · 最新版本 {new}",
                                 cur=updater.current_version(), new=rel.version))
            self._set_notes(rel.notes)
            self._set_status(t("已是最新版本"), OK)
            self._set_buttons(update=False, skip=False, page=True, busy=False)
            return

        self._head_var.set(t("当前版本 {cur} → 最新版本 {new}",
                             cur=updater.current_version(), new=rel.version))
        self._set_notes(rel.notes)
        asset = rel.assets.get(updater.platform_key())

        if asset is None:
            self._set_buttons(update=False, skip=True, page=True, busy=False)
            self._set_status(t("该版本未发布当前平台安装包，可前往发布页手动下载"), GRAY)
            return

        size = format_size(asset.size)
        if updater.supports_auto_update():
            self._set_status(t("发现新版本 {ver}（安装包 {size}）",
                               ver=rel.version, size=size), PRIMARY)
            self._btn_update.configure(text=t("立即升级"), command=self._start_download)
        else:
            self._set_status(t("发现新版本 {ver}（{size}）· 当前为源码运行，下载后需手动安装",
                               ver=rel.version, size=size), GRAY)
            self._btn_update.configure(text=t("下载安装包"), command=self._start_download)
        self._set_buttons(update=True, skip=True, page=True, busy=False)

    def _on_downloaded(self, path: str) -> None:
        self._pkg = path
        self._progress_bar.configure(value=100)
        if not updater.supports_auto_update():
            self._set_status(t("安装包已下载：{path}", path=path), OK)
            self._set_buttons(busy=False)
            self._btn_update.configure(text=t("打开安装包目录"), command=self._open_pkg_dir)
            return
        self._set_status(t("安装包已就绪（{path}）", path=path), OK)
        self._set_buttons(busy=False)
        self._btn_update.configure(text=t("立即重启并升级"), command=self._do_apply)
        if messagebox.askyesno(t("软件更新"),
                               t("安装包已下载完成，是否立即重启 phpvm 完成升级？"),
                               parent=self):
            self._do_apply()

    def _on_error(self, message: str) -> None:
        self._progress_bar.stop()
        self._progress_bar.configure(mode="determinate", value=0)
        self._set_status(message, ERR)
        self._set_buttons(busy=False)
        if self.on_status:
            self.on_status(t("检查更新失败：{msg}", msg=message))


class UpdateBanner:
    """启动静默检查：发现新版本时回调 UI（不自动弹窗，只做提示）。"""

    def __init__(self, master, config, on_found):
        self.master = master
        self.config = config
        self.on_found = on_found
        self._queue: queue.Queue = queue.Queue()
        self._done = False

    def start(self, delay_ms: int = 4000) -> None:
        if not self.config.get_setting("check_update_on_start", True):
            return
        self.master.after(delay_ms, self._spawn)

    def _spawn(self) -> None:
        def worker():
            rel = None
            try:
                rel = updater.check_update(timeout=8)
            except Exception:  # noqa: BLE001
                rel = None  # 启动检查静默失败：不打扰用户
            self._queue.put(rel)
            self._done = True

        threading.Thread(target=worker, daemon=True).start()
        self.master.after(1000, self._poll)

    def _poll(self) -> None:
        try:
            rel = self._queue.get_nowait()
        except queue.Empty:
            if not self._done:
                self.master.after(1000, self._poll)
            return
        if rel is not None and updater.is_newer_version(rel.version):
            skipped = str(self.config.get_setting("skipped_version", "") or "")
            if rel.version != skipped:
                self.on_found(rel)
