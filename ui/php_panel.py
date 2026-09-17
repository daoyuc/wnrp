# -*- coding: utf-8 -*-
"""PHP 版本管理页：版本列表（状态灯/版本号/端口/PID/配置）+ 启停/重启/端口/配置操作。

所有耗时操作（扫描、php -v 解析、启停、状态刷新）均在后台线程执行，
通过 queue 回传 UI 线程，避免界面卡死。
"""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core import crash_watchdog
from core import process_utils as pu
from core.config import Config, IS_WIN
from core.health_monitor import HealthMonitor
from core.i18n import t
from core.php_manager import PhpManager, PhpVersion, PortConflictError
from .dialogs import IniDialog, IniEditDialog, PortDialog, SelfCheckDialog
from .download_dialog import DownloadDialog
from .extension_dialog import ExtensionDialog
from . import theme

COLUMNS = [
    ("status", t("状态"), 70, "center"),
    ("name", t("版本目录"), 110, "w"),
    ("ver", t("PHP 版本"), 90, "center"),
    ("port", t("端口"), 80, "center"),
    ("pid", "PID", 90, "center"),
    ("ini", t("配置文件"), 300, "w"),
]


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
        self.btn_download = ttk.Button(bar, text=t("下载新版本"), style="Accent.TButton",
                                       command=self._download_version)
        self.btn_terminal = ttk.Button(bar, text=t("打开终端"), command=self._open_terminal)
        self.btn_composer = ttk.Button(bar, text=t("Composer"), command=self._composer)
        self.btn_refresh = ttk.Button(bar, text=t("刷新"), command=self.refresh_versions)
        for b in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_port,
                  self.btn_ini, self.btn_edit, self.btn_check, self.btn_ext,
                  self.btn_download, self.btn_terminal, self.btn_composer,
                  self.btn_refresh):
            b.pack(side="left", padx=(0, 6))
        ttk.Label(bar, text=t("选中版本后操作 · 双击行查看配置"),
                  style="SubTitle.TLabel").pack(side="left", padx=(4, 0))

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
        self.after(80, self._drain)

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

    def _render(self, versions: list[PhpVersion]) -> None:
        self._versions = versions
        self._name_to_iid.clear()
        self.tree.delete(*self.tree.get_children())
        for i, v in enumerate(versions):
            running, pid = v.running, v.pid
            dot, tag = ("●", "dot_run") if running else ("○", "dot_stop")
            tags = [tag, "odd" if i % 2 else "even"]
            iid = self.tree.insert(
                "", "end",
                values=(
                    dot, v.name, v.display, v.port,
                    pid if pid else "—",
                    v.ini,
                ),
                tags=tags,
            )
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
        for name, (running, pid) in results.items():
            v = next((x for x in self._versions if x.name == name), None)
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
            running, pid = v.running, v.pid
            dot, tag = ("●", "dot_run") if running else ("○", "dot_stop")
            tags = [tag, "odd" if i % 2 else "even"]
            self.tree.item(
                iid,
                values=(dot, v.name, v.display, v.port, pid if pid else "—", v.ini),
                tags=tags,
            )
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
                  self.btn_ini, self.btn_edit, self.btn_check, self.btn_ext):
            b.configure(state="normal" if has_sel else "disabled")

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
        if not v.ini:
            messagebox.showwarning(
                t("无独立配置文件"),
                t("[{name}] 未使用独立 php.ini（读取 PHP 编译默认配置）。\n"
                  "如需按版本定制配置，请在版本目录中放置 php.ini 后重新刷新。",
                  name=v.name),
                parent=self,
            )
            return
        IniEditDialog(self, v, self.php_mgr)

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
        ExtensionDialog(self, v, self.php_mgr)

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
