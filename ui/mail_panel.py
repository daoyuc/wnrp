# -*- coding: utf-8 -*-
"""邮件页（F11）：查看 PHP ``mail()`` 捕获到的邮件，并按版本开关捕获。

实现要点：
- 捕获由 ``core.mail_catcher`` 落盘为 ``.eml``（``sendmail_path`` 指向垫片脚本），
  本页只做列表 / 详情 / 开关，不引入任何常驻进程或外部二进制；
- 耗时操作（列目录、解析邮件、写 php.ini）都在后台线程执行，经 queue 回主线程；
- ``sendmail_path`` 仅 Unix 有效（Windows 的 mail() 走 SMTP），面板会直接说明。
"""
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from core import mail_catcher
from core import process_utils as pu
from core import run_log
from core.config import Config
from core.i18n import t
from core.php_manager import PhpManager
from . import theme

COLUMNS = [
    ("time", t("时间"), 130, "w"),
    ("from", t("发件人"), 200, "w"),
    ("to", t("收件人"), 170, "w"),
    ("subject", t("主题"), 300, "w"),
    ("size", t("大小"), 70, "e"),
]


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


class MailPanel(ttk.Frame):
    """邮件页签。"""

    def __init__(self, master, notify, php_mgr: PhpManager, config: Config):
        super().__init__(master, padding=8)
        self.notify = notify
        self.php_mgr = php_mgr
        self.config = config

        self._queue: queue.Queue = queue.Queue()
        self._mails: list[dict] = []
        self._iid_to_mail: dict[str, dict] = {}
        self._busy = False
        self._draining = False

        self._build()
        self.refresh()
        self._start_drain()

    # ------------------------------------------------------------------ #
    # 界面构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text=t("PHP 版本："), style="CardBold.TLabel").pack(side="left")
        self._ver_box = ttk.Combobox(bar, state="readonly", width=14, values=[])
        self._ver_box.pack(side="left", padx=(theme.PAD_XS, theme.PAD_SM))
        self._ver_box.bind("<<ComboboxSelected>>", lambda e: self._on_version_changed())
        self.btn_enable = ttk.Button(bar, text=t("开启捕获"), style="Accent.TButton",
                                     command=lambda: self._toggle(True))
        self.btn_disable = ttk.Button(bar, text=t("关闭捕获"), command=lambda: self._toggle(False))
        self.btn_refresh = ttk.Button(bar, text=t("刷新"), command=self.refresh)
        self.btn_open = ttk.Button(bar, text=t("打开邮件目录"), command=self._open_dir)
        self.btn_clear = ttk.Button(bar, text=t("清空"), style="Danger.TButton",
                                    command=self._clear)
        for b in (self.btn_enable, self.btn_disable, self.btn_refresh,
                  self.btn_open, self.btn_clear):
            b.pack(side="left", padx=(0, 6))
        self._state = ttk.Label(bar, text=t("正在读取…"), style="SubTitle.TLabel")
        self._state.pack(side="left", padx=(4, 0))

        self._hint = ttk.Label(self, text="", style="SubTitle.TLabel")
        self._hint.pack(anchor="w", pady=(0, 6))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            wrap, columns=[c[0] for c in COLUMNS], show="headings", selectmode="browse")
        for col, text, width, anchor in COLUMNS:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor,
                             stretch=(col == "subject"), minwidth=60)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("odd", background=theme.ROW_ALT)
        self.tree.tag_configure("even", background=theme.CARD_BG)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._show_mail())

        detail = ttk.LabelFrame(self, text=t("邮件原文（.eml）"), padding=6)
        detail.pack(fill="both", expand=True, pady=(6, 0))
        self.text = tk.Text(detail, height=12, wrap="none", state="disabled",
                            background=theme.LOG_BG, foreground=theme.LOG_FG,
                            font=theme.mono(), relief="flat", padx=8, pady=6)
        tsb = ttk.Scrollbar(detail, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=tsb.set)
        self.text.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")

    def refresh_theme(self) -> None:
        """主题切换钩子：重设 Treeview 标记色。"""
        self.tree.tag_configure("odd", background=theme.ROW_ALT)
        self.tree.tag_configure("even", background=theme.CARD_BG)

    # ------------------------------------------------------------------ #
    # 后台刷新（队列 + drain，与其它面板一致）
    # ------------------------------------------------------------------ #
    def _start_drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        self.after(80, self._drain)

    def _drain(self) -> None:
        # 整个循环包在 try 里：窗口销毁 / after 失败时必须停摆，不能让异常冒出
        # Tk 回调 —— 否则末尾的重排不执行，drain 链断裂、_draining 永为 True，
        # 队列消息被永久静默丢弃（busy 不复位、自动刷新停摆）
        try:
            if not self.winfo_exists():
                self._draining = False
                return
            while True:
                try:
                    self._dispatch(*self._queue.get_nowait())
                except queue.Empty:
                    break
                except Exception as e:  # noqa: BLE001 - 单条消息失败只丢这一条
                    run_log.error("ui", t("内部错误：{err}", err=e))
            self.after(80 if self.winfo_ismapped() else 400, self._drain)
        except tk.TclError:  # 窗口已销毁：排不了定时器，永久停摆
            self._draining = False

    def _dispatch(self, kind: str, payload) -> None:
        if kind == "data":
            self._set_busy(False)
            names, mails, status, name = payload
            self._render_versions(names, name)
            self._render(mails)
            self._render_state(status, name)
        elif kind == "op":
            self._set_busy(False)
            ok, msg = payload
            self.notify(msg)
            if ok:
                self.refresh()
            else:
                messagebox.showerror(t("操作失败"), msg, parent=self)
        elif kind == "sink":
            # 自动拉起 SMTP sink 的结果（Windows）：只提示，不打断
            self.notify(payload)
        else:  # error
            self._set_busy(False)
            self.notify(str(payload))
            messagebox.showerror(t("操作失败"), str(payload), parent=self)

    def refresh(self) -> None:
        """后台列出邮件 + 读取所选版本的捕获状态。"""
        if self._busy:
            return
        self._set_busy(True)
        name = self._current_name()

        def worker():
            try:
                if not self.php_mgr.versions:
                    self.php_mgr.scan_versions()
                names = [v.name for v in self.php_mgr.versions]
                # 首次进入时下拉框还是空的：直接定下默认版本，避免首屏状态与
                # 下拉框不一致（要等下一次心跳才恢复）
                chosen = name if name in names else (names[0] if names else "")
                v = next((x for x in self.php_mgr.versions if x.name == chosen), None)
                # Windows：有版本开着捕获但 sink 没在跑（如 phpvm 重启过）→ 自动拉起
                started = mail_catcher.ensure_sink(self.php_mgr.versions)
                status = mail_catcher.status(v) if v is not None else None
                mails = mail_catcher.list_mails(limit=500)
                self._queue.put(("data", (names, mails, status, chosen)))
                if started:
                    self._queue.put(("sink", started[1]))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", t("读取邮件失败：{err}", err=e)))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    def auto_refresh(self) -> None:
        """跟随主窗口心跳：重列邮件与状态（``refresh`` 自带忙判定，不会叠加）。"""
        self.refresh()

    def _render_versions(self, names: list[str], name: str) -> None:
        if list(self._ver_box["values"]) != names:
            self._ver_box["values"] = names
        if name and name in names and self._ver_box.get() != name:
            self._ver_box.current(names.index(name))
            self.config.set_setting("mail_php", name)

    def _render(self, mails: list[dict]) -> None:
        self._mails = mails
        self.tree.delete(*self.tree.get_children())
        self._iid_to_mail.clear()
        for i, m in enumerate(mails):
            values = (
                time.strftime("%m-%d %H:%M:%S", time.localtime(m["mtime"])),
                m["from"] or "—", m["to"] or "—",
                m["subject"] or t("（无主题）"), _human_size(m["size"]),
            )
            iid = self.tree.insert("", "end", values=values,
                                   tags=["odd" if i % 2 else "even"])
            self._iid_to_mail[iid] = m
        if not mails:
            self._set_text(t("暂无捕获到的邮件。\n\n"
                             "在「PHP 版本管理」页或本页选择版本后点「开启捕获」，"
                             "PHP 的 mail() 就会把邮件写到邮件目录。"))
        self.notify(t("已捕获 {n} 封邮件", n=len(mails)))

    def _render_state(self, status: dict | None, name: str = "") -> None:
        if status is None:
            self._state.configure(text=t("请先在上方选择一个 PHP 版本"), foreground=theme.GRAY)
            self._hint.configure(text="")
            self._set_buttons(False, False)
            return
        smtp = status.get("mode") == "smtp"
        if smtp:
            # Windows：mail() 走 SMTP，由本机 sink 收信；sink 随 phpvm 进程运行
            if status["enabled"] and not status["sink_running"]:
                self._hint.configure(
                    text=t("捕获服务未运行：phpvm 关闭期间 PHP 发信会失败，"
                           "点「开启捕获」可立即拉起（127.0.0.1:{port}）。",
                           port=status.get("sink_port", 0)),
                    foreground=theme.WARN)
            else:
                self._hint.configure(
                    text=t("Windows 的 mail() 走 SMTP：phpvm 在 127.0.0.1:{port} 收信并落盘 .eml"
                           "（捕获服务随 phpvm 运行）。", port=status.get("sink_port", 0)),
                    foreground=theme.TEXT)
        else:
            self._hint.configure(text="", foreground=theme.TEXT)
        state = t("已开启") if status["enabled"] else t("未开启")
        line = t("[{name}] 捕获：{state}", name=name or self._current_name(), state=state)
        if smtp:
            line += " · " + t("SMTP {state}",
                              state=t("运行中") if status["sink_running"] else t("未运行"))
        line += " · " + t("目录：{dir}（{n} 封）", dir=status["mail_dir"], n=status["count"])
        self._state.configure(text=line,
                              foreground=theme.OK if status["enabled"] else theme.GRAY)
        self._set_buttons(status["supported"] and not status["enabled"],
                          status["supported"] and status["enabled"])

    def _set_buttons(self, can_enable: bool, can_disable: bool) -> None:
        busy = self._busy
        self.btn_enable.configure(state="normal" if (can_enable and not busy) else "disabled")
        self.btn_disable.configure(state="normal" if (can_disable and not busy) else "disabled")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.btn_refresh.configure(state="disabled" if busy else "normal")
        self._set_buttons(not busy, not busy)

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _current_name(self) -> str:
        return str(self._ver_box.get()).strip() or str(
            self.config.get_setting("mail_php", "") or "")

    def _on_version_changed(self) -> None:
        self.config.set_setting("mail_php", self._current_name())
        self.refresh()

    def _selected(self) -> dict | None:
        sel = self.tree.selection()
        return self._iid_to_mail.get(sel[0]) if sel else None

    def _show_mail(self) -> None:
        m = self._selected()
        if m is None:
            return
        self._set_text(mail_catcher.read_mail(m["file"]))

    def _set_text(self, content: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content or "")
        self.text.configure(state="disabled")

    def _toggle(self, on: bool) -> None:
        name = self._current_name()
        v = next((x for x in self.php_mgr.versions if x.name == name), None)
        if v is None:
            messagebox.showinfo(t("提示"), t("请先在上方选择一个 PHP 版本。"), parent=self)
            return
        if on and not messagebox.askyesno(
                t("开启邮件捕获"),
                t("将把 [{name}] 的 sendmail_path 指向 phpvm 的捕获脚本（改前自动备份，"
                  "关闭时还原）。\nPHP 的 mail() 邮件会写入：\n{dir}\n\n确定继续？",
                  name=name, dir=mail_catcher.mail_dir()),
                parent=self):
            return
        self._set_busy(True)
        self.notify(t("正在{act}邮件捕获…", act=t("开启") if on else t("关闭")))

        def worker():
            try:
                ok, msg, _backup = (mail_catcher.enable(v) if on else mail_catcher.disable(v))
            except Exception as e:  # noqa: BLE001
                ok, msg = False, t("操作失败：{err}", err=e)
            self._queue.put(("op", (ok, msg)))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    def _clear(self) -> None:
        if not self._mails:
            messagebox.showinfo(t("清空"), t("当前没有捕获到的邮件。"), parent=self)
            return
        if not messagebox.askyesno(
                t("清空"),
                t("将删除邮件目录下的全部 .eml（{n} 封）：\n{dir}\n\n确定继续？",
                  n=len(self._mails), dir=mail_catcher.mail_dir()),
                parent=self):
            return
        n = mail_catcher.clear_mails()
        self.notify(t("已清空 {n} 封邮件", n=n))
        self._set_text("")
        self.refresh()

    def _open_dir(self) -> None:
        target = mail_catcher.mail_dir()
        if not os.path.isdir(target):
            messagebox.showinfo(t("目录不存在"), t("邮件目录不存在：{path}", path=target),
                                parent=self)
            return
        pu.open_path(target)
