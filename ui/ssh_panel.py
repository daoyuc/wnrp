# -*- coding: utf-8 -*-
"""「SSH 管理」页签：主机清单 + 连接测试 + 命令片段 + 远端执行输出。

契约（主窗口 / 主题系统）：

- ``auto_refresh()``：定时 tick 调用，**只重载本地数据**，不探测主机，
  避免离线或主机不可达时界面卡顿；
- ``refresh_theme()``：主题切换时重设 Treeview 斑马纹配色。

安全边界：网络操作全部在 worker 线程；只有用户点击「测试连接 / 打开终端 /
运行片段」才会发起连接，没有任何自动连接或自动执行；密码只存在内存与
``ssh_hosts.json`` 中，不写日志、不进命令行。
"""
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from core import run_log, ssh_manager, ssh_store
from core.i18n import t
from . import theme
from .window_utils import rearm_poll

_POLL_MS = 120
_BUSY_TIMEOUT = 60.0


class SshPanel(ttk.Frame):
    """SSH 主机与命令片段面板。"""

    def __init__(self, master, config, notify):
        super().__init__(master, padding=8)
        self.config = config
        self.notify = notify
        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._busy_since = 0.0
        self._hosts: list = []
        self._snippets: list = []
        self._selected_host = None
        self._search = tk.StringVar()
        self._tag = tk.StringVar(value="")
        self._status = tk.StringVar(value=t("就绪"))
        self._detail = tk.StringVar(value=t("选择左侧主机查看详情"))
        self._build()
        self.reload()
        self._poll()

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Label(bar, text=t("SSH 管理"), style="Title.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self._status,
                  style="CardDim.TLabel").pack(side="left",
                                               padx=(theme.PAD_MD, 0))
        ttk.Button(bar, text=t("刷新"), command=self.reload,
                   style="Toolbar.TButton").pack(side="right")
        self._tag_box = ttk.Combobox(bar, textvariable=self._tag, width=12,
                                     state="readonly")
        self._tag_box.pack(side="right", padx=(0, theme.PAD_SM))
        self._tag_box.bind("<<ComboboxSelected>>", lambda e: self._render_hosts())
        ttk.Label(bar, text=t("标签"), style="CardDim.TLabel").pack(
            side="right", padx=(0, theme.PAD_XS))
        ttk.Entry(bar, textvariable=self._search, width=20).pack(
            side="right", padx=(0, theme.PAD_SM))
        ttk.Label(bar, text=t("搜索"), style="CardDim.TLabel").pack(
            side="right", padx=(0, theme.PAD_XS))
        self._search.trace_add("write", lambda *_: self._render_hosts())

        if not ssh_manager.available():
            ttk.Label(
                self,
                text=t("未安装依赖 paramiko，连接与执行不可用；可先用 CLI 管理配置：{cmd}",
                       cmd=ssh_manager.install_hint()),
                style="CardDim.TLabel",
            ).pack(anchor="w", pady=(theme.PAD_SM, 0))

        body = ttk.PanedWindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, pady=(theme.PAD_MD, 0))

        left = ttk.Frame(body)
        body.add(left, weight=1)
        head = ttk.Frame(left)
        head.pack(fill="x")
        ttk.Label(head, text=t("主机"), style="Section.TLabel").pack(side="left")
        self._hcount = ttk.Label(head, text="", style="CardDim.TLabel")
        self._hcount.pack(side="right")
        self._htree = ttk.Treeview(left, columns=("addr", "user", "tags"),
                                   show="tree headings", selectmode="browse",
                                   height=12)
        self._htree.column("#0", width=150)
        self._htree.column("addr", width=150)
        self._htree.column("user", width=90)
        self._htree.column("tags", width=110)
        self._htree.pack(fill="both", expand=True, side="left")
        hsb = ttk.Scrollbar(left, orient="vertical",
                            command=self._htree.yview)
        hsb.pack(side="right", fill="y")
        self._htree.configure(yscrollcommand=hsb.set)
        self._htree.bind("<<TreeviewSelect>>", lambda e: self._on_pick())

        hbtns = ttk.Frame(self)
        hbtns.pack(fill="x", pady=(theme.PAD_SM, 0))
        ttk.Button(hbtns, text=t("新增主机"), command=self._on_add_host,
                   style="Accent.TButton").pack(side="left")
        for text, fn in ((t("编辑"), self._on_edit_host),
                         (t("删除"), self._on_remove_host),
                         (t("测试连接"), self._on_test),
                         (t("打开终端"), self._on_terminal)):
            ttk.Button(hbtns, text=text, command=fn).pack(
                side="left", padx=(theme.PAD_SM, 0))

        right = ttk.Frame(body)
        body.add(right, weight=2)
        ttk.Label(right, textvariable=self._detail, style="CardDim.TLabel",
                  justify="left").pack(anchor="w", fill="x")
        srow = ttk.Frame(right)
        srow.pack(fill="x", pady=(theme.PAD_SM, 0))
        ttk.Label(srow, text=t("命令片段"), style="Section.TLabel").pack(side="left")
        ttk.Button(srow, text=t("新增片段"), command=self._on_add_snippet,
                   style="Accent.TButton").pack(side="right")
        self._stree = ttk.Treeview(right, columns=("scope", "command"),
                                   show="tree headings", selectmode="browse",
                                   height=8)
        self._stree.column("#0", width=140)
        self._stree.column("scope", width=90)
        self._stree.column("command", width=260)
        self._stree.pack(fill="both", expand=True, side="left")
        ssb = ttk.Scrollbar(right, orient="vertical",
                            command=self._stree.yview)
        ssb.pack(side="right", fill="y")
        self._stree.configure(yscrollcommand=ssb.set)
        self._stree.bind("<Double-1>", lambda e: self._on_run_snippet())

        sbtns = ttk.Frame(self)
        sbtns.pack(fill="x", pady=(theme.PAD_SM, 0))
        for text, fn in ((t("编辑片段"), self._on_edit_snippet),
                         (t("删除片段"), self._on_remove_snippet),
                         (t("复制命令"), self._on_copy_snippet),
                         (t("在远端执行"), self._on_run_snippet)):
            ttk.Button(sbtns, text=text, command=fn).pack(
                side="left", padx=(0, theme.PAD_SM))

        ttk.Label(self, text=t("输出"), style="Section.TLabel").pack(
            anchor="w", pady=(theme.PAD_SM, 0))
        self._out = ScrolledText(self, height=8, wrap="word",
                                 state="disabled", font=theme.mono(9))
        self._out.pack(fill="both", expand=True)

        self.refresh_theme()

    # ------------------------------------------------------------------ #
    # 数据
    # ------------------------------------------------------------------ #
    def reload(self) -> None:
        self._hosts = ssh_store.load_hosts()
        # 首次进 SSH 页时预置一批常用运维命令（只读探测），只写一次
        seeded = ssh_store.ensure_builtin_snippets(self.config)
        self._snippets = ssh_store.load_snippets(self.config)
        tags = ssh_store.all_tags(self._hosts)
        self._tag_box.configure(values=[""] + tags)
        if self._tag.get() and self._tag.get() not in tags:
            self._tag.set("")
        self._render_hosts()
        if seeded:
            self._status.set(t("已预置 {n} 条常用命令片段，可编辑或删除",
                               n=len(seeded)))

    def auto_refresh(self) -> None:
        self.reload()

    def refresh_theme(self) -> None:
        for tree in (getattr(self, "_htree", None),
                     getattr(self, "_stree", None)):
            if tree is None:
                continue
            tree.tag_configure("odd", background=theme.ROW_ALT)
        out = getattr(self, "_out", None)
        if out is not None:
            try:
                out.configure(background=theme.LOG_BG,
                              foreground=theme.LOG_FG,
                              insertbackground=theme.LOG_FG)
            except (tk.TclError, AttributeError):
                pass

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _render_hosts(self) -> None:
        for item in self._htree.get_children():
            self._htree.delete(item)
        rows = ssh_store.search_hosts(self._hosts, self._search.get(),
                                      self._tag.get())
        keep = str((self._selected_host or {}).get("id", ""))
        for index, host in enumerate(rows):
            self._htree.insert(
                "", "end", iid=str(host["id"]), text=host["name"],
                values=(host["host"], host["user"],
                        ", ".join(host.get("tags") or [])),
                tags=("odd",) if index % 2 else ())
        self._hcount.configure(text=t("{n} 台", n=len(rows)))
        if keep and self._htree.exists(keep):
            self._htree.selection_set(keep)
        else:
            self._selected_host = None
            self._detail.set(t("选择左侧主机查看详情"))
            self._render_snippets()

    def _on_pick(self) -> None:
        sel = self._htree.selection()
        self._selected_host = None
        if sel:
            for host in self._hosts:
                if str(host["id"]) == str(sel[0]):
                    self._selected_host = host
                    break
        self._render_detail()
        self._render_snippets()

    def _render_detail(self) -> None:
        host = self._selected_host
        if host is None:
            self._detail.set(t("选择左侧主机查看详情"))
            return
        auth = t("密码认证") if host["auth"] == "password" else t("私钥认证")
        # 以 missing_secret 为准：私钥认证看的是 key_path，不是口令有没有填
        secret = t("凭据未配置") if ssh_store.missing_secret(host) else t("凭据已配置")
        tags = ", ".join(host.get("tags") or []) or t("无标签")
        text = (f"{host['name']} · {host['user']}@{host['host']}:{host['port']}\n"
                f"{auth} · {secret} · {tags}")
        if host.get("note"):
            text += "\n" + host["note"]
        self._detail.set(text)

    def _render_snippets(self) -> None:
        for item in self._stree.get_children():
            self._stree.delete(item)
        known = {str(h["id"]) for h in self._hosts}
        rows = ssh_store.snippets_for(self.config, host=self._selected_host,
                                      known_host_ids=known)
        self._snippets = rows
        for index, snip in enumerate(rows):
            scope = t("通用") if not snip["host_ids"] \
                else t("{n} 台主机", n=len(snip["host_ids"]))
            lines = snip["command"].strip().splitlines()
            self._stree.insert("", "end", iid=str(snip["id"]), text=snip["name"],
                               values=(scope, lines[0][:80] if lines else ""),
                               tags=("odd",) if index % 2 else ())

    # ------------------------------------------------------------------ #
    # 后台任务
    # ------------------------------------------------------------------ #
    def _append(self, text: str) -> None:
        if not text:
            return
        self._out.configure(state="normal")
        self._out.insert("end", text.rstrip() + "\n")
        self._out.see("end")
        self._out.configure(state="disabled")

    def _require_host(self):
        if self._selected_host is None:
            self.notify(t("请先选择一台主机"), "error")
        return self._selected_host

    def _require_dep(self) -> bool:
        if not ssh_manager.available():
            self.notify(t("缺少依赖 paramiko：{cmd}",
                          cmd=ssh_manager.install_hint()), "error")
            return False
        return True

    def _start_bg(self, kind: str, host: dict, trust: bool = False) -> None:
        if self._busy:
            self.notify(t("上一个操作还未完成，请稍候"), "error")
            return
        self._busy = True
        self._busy_since = time.time()
        command = getattr(self, "_run_command", "")

        def work() -> None:
            try:
                if kind == "test":
                    res = ssh_manager.test_connection(host, trust=trust)
                else:
                    res = ssh_manager.exec_command(host, command, trust=trust)
            except Exception as e:  # noqa: BLE001 - 线程内异常必须转成消息
                res = {"ok": False, "error_kind": "error",
                       "error": f"{type(e).__name__}: {e}"}
            self._queue.put((kind, host, res))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self) -> None:
        while True:
            try:
                kind, host, res = self._queue.get_nowait()
            except queue.Empty:
                break
            self._busy = False
            self._on_result(kind, host, res)
        if self._busy and time.time() - self._busy_since > _BUSY_TIMEOUT:
            self._busy = False
            self._status.set(t("操作超时，已解锁"))
        rearm_poll(self, self._poll, _POLL_MS)

    def _on_result(self, kind: str, host: dict, res: dict) -> None:
        label = ssh_manager.target_label(host)
        if not res.get("ok") and res.get("error_kind") == "host_key":
            fp = res.get("fingerprint", "")
            if messagebox.askyesno(
                    t("信任主机指纹"),
                    t("主机 {target} 的指纹\n{fp}\n\n确认无误并信任吗？",
                      target=label, fp=fp),
                    parent=self.winfo_toplevel()):
                self._status.set(t("正在重新连接…"))
                self._start_bg(kind, host, trust=True)
            else:
                self._status.set(t("已取消：指纹未信任"))
            return
        if kind == "test":
            if res.get("ok"):
                self._status.set(t("连接成功：{target} · {ms} ms",
                                   target=label, ms=res.get("latency_ms", 0)))
                self._append(t("[连接成功] {target} · {ms} ms · {server}",
                               target=label, ms=res.get("latency_ms", 0),
                               server=res.get("banner", "")))
                if res.get("fingerprint"):
                    self._append(t("指纹：{fp}", fp=res["fingerprint"]))
                return
            self._status.set(t("连接失败"))
            self._append(t("[连接失败] {target}：{err}",
                           target=label, err=res.get("error", "")))
            self.notify(t("连接失败"), "error")
            return
        if res.get("stdout"):
            self._append(res["stdout"])
        if res.get("stderr"):
            self._append("[stderr] " + res["stderr"])
        if res.get("truncated"):
            self._append(t("（输出超过上限，已截断）"))
        if res.get("ok"):
            self._status.set(t("执行完成：{target} · 退出码 {code}",
                               target=label, code=res.get("exit_status", -1)))
            return
        self._status.set(t("执行失败"))
        self._append(t("[执行失败] {err}", err=res.get("error", "")))
        self.notify(t("执行失败"), "error")

    # ------------------------------------------------------------------ #
    # 主机动作
    # ------------------------------------------------------------------ #
    def _on_test(self) -> None:
        host = self._require_host()
        if host is None or not self._require_dep():
            return
        self._status.set(t("正在测试 {target}…",
                           target=ssh_manager.target_label(host)))
        self._start_bg("test", host)

    def _on_terminal(self) -> None:
        host = self._require_host()
        if host is None:
            return
        label = ssh_manager.target_label(host)
        if host["auth"] == "password" and not messagebox.askyesno(
                t("打开终端"),
                t("将用系统终端连接 {target}，密码需在终端窗口内输入。继续吗？",
                  target=label),
                parent=self.winfo_toplevel()):
            return
        if ssh_manager.open_terminal(host):
            self._status.set(t("已打开终端：{target}", target=label))
        else:
            self.notify(t("未能打开终端（请确认系统自带 ssh 客户端）"), "error")

    def _on_add_host(self) -> None:
        data = host_dialog(self, None)
        if data is None:
            return
        host, err = ssh_store.add_host(data)
        if err:
            self.notify(err, "error")
            return
        self.reload()
        if self._htree.exists(str(host["id"])):
            self._htree.selection_set(str(host["id"]))
            self._on_pick()
        self._status.set(t("已添加主机：{name}", name=host["name"]))
        self._notice_missing_secret(host)

    def _on_edit_host(self) -> None:
        host = self._require_host()
        if host is None:
            return
        data = host_dialog(self, host)
        if data is None:
            return
        updated, err = ssh_store.update_host(host["id"], data)
        if err:
            self.notify(err, "error")
            return
        self.reload()
        self._status.set(t("已更新主机：{name}", name=updated["name"]))
        self._notice_missing_secret(updated)

    def _notice_missing_secret(self, host: dict) -> None:
        """保存与连通性无关：缺凭据只提示「连接前补上」，不拦保存。"""
        if ssh_store.missing_secret(host):
            self.notify(
                t("主机 {name} 已保存，但还没有密码或私钥，连接前补齐即可",
                  name=host["name"]), "warn")

    def _on_remove_host(self) -> None:
        host = self._require_host()
        if host is None:
            return
        if not messagebox.askyesno(
                t("删除主机"),
                t("确定删除主机 {name}？关联片段会变成通用片段。",
                  name=host["name"]),
                parent=self.winfo_toplevel()):
            return
        if not ssh_store.remove_host(host["id"]):
            self.notify(t("删除失败"), "error")
            return
        self._selected_host = None
        self.reload()
        self._status.set(t("已删除主机"))

    # ------------------------------------------------------------------ #
    # 片段动作
    # ------------------------------------------------------------------ #
    def _selected_snippet(self):
        sel = self._stree.selection()
        if not sel:
            return None
        for snip in self._snippets:
            if str(snip["id"]) == str(sel[0]):
                return snip
        return None

    def _on_add_snippet(self) -> None:
        data = snippet_dialog(self, None, self._hosts, self._selected_host)
        if data is None:
            return
        snip, err = ssh_store.add_snippet(self.config, data)
        if err:
            self.notify(err, "error")
            return
        self.reload()
        self._status.set(t("已添加片段：{name}", name=snip["name"]))

    def _on_edit_snippet(self) -> None:
        snip = self._selected_snippet()
        if snip is None:
            self.notify(t("请先选择一个命令片段"), "error")
            return
        data = snippet_dialog(self, snip, self._hosts, self._selected_host)
        if data is None:
            return
        _snip, err = ssh_store.update_snippet(self.config, snip["id"], data)
        if err:
            self.notify(err, "error")
            return
        self.reload()
        self._status.set(t("已更新片段"))

    def _on_remove_snippet(self) -> None:
        snip = self._selected_snippet()
        if snip is None:
            self.notify(t("请先选择一个命令片段"), "error")
            return
        if not messagebox.askyesno(t("删除片段"),
                                   t("确定删除片段 {name}？", name=snip["name"]),
                                   parent=self.winfo_toplevel()):
            return
        if not ssh_store.remove_snippet(self.config, snip["id"]):
            self.notify(t("删除失败"), "error")
            return
        self.reload()
        self._status.set(t("已删除片段"))

    def _on_copy_snippet(self) -> None:
        snip = self._selected_snippet()
        if snip is None:
            self.notify(t("请先选择一个命令片段"), "error")
            return
        self.clipboard_clear()
        self.clipboard_append(snip["command"])
        self._status.set(t("已复制命令到剪贴板"))

    def _on_run_snippet(self) -> None:
        host = self._require_host()
        if host is None or not self._require_dep():
            return
        snip = self._selected_snippet()
        if snip is None:
            self.notify(t("请先选择一个命令片段"), "error")
            return
        if not messagebox.askyesno(
                t("在远端执行"),
                t("将在 {target} 执行：\n\n{cmd}\n\n确认执行吗？",
                  target=ssh_manager.target_label(host), cmd=snip["command"]),
                parent=self.winfo_toplevel()):
            return
        self._run_command = snip["command"]
        self._append(f"$ {snip['name']}: {snip['command']}")
        self._status.set(t("正在执行…"))
        self._start_bg("run", host)


def _form_row(win, row: int, label: str, var, show=None, width: int = 34):
    """对话框里的一行「标签 + 输入框」。"""
    ttk.Label(win, text=label).grid(row=row, column=0, sticky="w",
                                    padx=(0, theme.PAD_SM), pady=theme.PAD_XS)
    entry = ttk.Entry(win, textvariable=var, width=width, show=show)
    entry.grid(row=row, column=1, sticky="we", pady=theme.PAD_XS)
    return entry


def host_dialog(parent, host: dict | None):
    """主机新增 / 编辑对话框；返回字段字典，取消返回 None。

    编辑时密码与口令留空表示「不修改」——避免每次都要重输一遍。
    """
    host = host or {}
    editing = bool(host)
    win = tk.Toplevel(parent)
    win.title(t("编辑主机") if editing else t("新增主机"))
    win.transient(parent.winfo_toplevel())
    win.resizable(False, False)
    win.columnconfigure(1, weight=1)

    v = {
        "name": tk.StringVar(value=str(host.get("name", ""))),
        "host": tk.StringVar(value=str(host.get("host", ""))),
        "port": tk.StringVar(value=str(host.get("port", 22))),
        "user": tk.StringVar(value=str(host.get("user", ""))),
        "auth": tk.StringVar(value=str(host.get("auth", "password"))),
        "password": tk.StringVar(),
        "key_path": tk.StringVar(value=str(host.get("key_path", ""))),
        "passphrase": tk.StringVar(),
        "tags": tk.StringVar(value=", ".join(host.get("tags") or [])),
        "note": tk.StringVar(value=str(host.get("note", ""))),
    }
    # 私钥留空时先填一个看得见的默认值（本机 ~/.ssh 里确实存在才填，可随时改掉）
    auto = {"path": ""}
    if v["auth"].get() == "key" and not v["key_path"].get().strip():
        auto["path"] = ssh_store.default_key_path()
        if auto["path"]:
            v["key_path"].set(auto["path"])
    _form_row(win, 0, t("名称"), v["name"])
    _form_row(win, 1, t("地址"), v["host"])
    _form_row(win, 2, t("端口"), v["port"], width=10)
    _form_row(win, 3, t("用户名"), v["user"])
    ttk.Label(win, text=t("认证方式")).grid(row=4, column=0, sticky="w",
                                            pady=theme.PAD_XS)
    ttk.Combobox(win, textvariable=v["auth"], values=["password", "key"],
                 state="readonly", width=12).grid(row=4, column=1, sticky="w",
                                                  pady=theme.PAD_XS)
    pw_entry = _form_row(
        win, 5, t("密码"), v["password"], show="*")
    # 新增时明确告诉用户「可以先空着存」：保存不校验连通性，凭据之后补
    hint = t("编辑时留空表示不修改") if editing else t("可留空，连接前再补")
    ttk.Label(win, text=hint, style="CardDim.TLabel").grid(
        row=5, column=2, sticky="w", padx=(theme.PAD_XS, 0))

    key_row = ttk.Frame(win)
    key_row.grid(row=6, column=1, sticky="we", pady=theme.PAD_XS)
    ttk.Label(win, text=t("私钥文件")).grid(row=6, column=0, sticky="w",
                                            pady=theme.PAD_XS)
    key_entry = ttk.Entry(key_row, textvariable=v["key_path"], width=26)
    key_entry.pack(side="left", fill="x", expand=True)
    key_hint = tk.StringVar()
    ttk.Label(win, textvariable=key_hint, style="CardDim.TLabel").grid(
        row=6, column=2, sticky="w", padx=(theme.PAD_XS, 0))

    def browse() -> None:
        path = filedialog.askopenfilename(
            parent=win, title=t("选择私钥文件"),
            filetypes=[(t("私钥文件"), "*"), (t("所有文件"), "*.*")])
        if path:
            v["key_path"].set(path)

    browse_btn = ttk.Button(key_row, text=t("浏览"), command=browse, width=8)
    browse_btn.pack(side="left", padx=(theme.PAD_XS, 0))
    pass_entry = _form_row(win, 7, t("私钥口令"), v["passphrase"], show="*")
    _form_row(win, 8, t("标签"), v["tags"])
    _form_row(win, 9, t("备注"), v["note"])

    def detect_key() -> str:
        """只探测一次，切来切去不反复读盘。"""
        if not auto["path"]:
            auto["path"] = ssh_store.default_key_path()
        return auto["path"]

    def sync_key_hint(*_args) -> None:
        if v["auth"].get() != "key":
            key_hint.set("")
            return
        path = v["key_path"].get().strip()
        if not path:
            # 本机没有可用的默认私钥 vs 用户自己清空了，提示要分开
            key_hint.set(t("已清空私钥路径") if auto["path"]
                         else t("未找到默认私钥，请手动选择"))
        elif path == auto["path"]:
            key_hint.set(t("已自动选用默认私钥"))
        else:
            key_hint.set("")

    def sync_auth(*_args) -> None:
        is_key = v["auth"].get() == "key"
        if is_key and not v["key_path"].get().strip():
            picked = detect_key()
            if picked:
                v["key_path"].set(picked)
        pw_entry.configure(state="disabled" if is_key else "normal")
        for widget in (key_entry, browse_btn, pass_entry):
            widget.configure(state="normal" if is_key else "disabled")
        sync_key_hint()

    v["auth"].trace_add("write", sync_auth)
    v["key_path"].trace_add("write", sync_key_hint)
    sync_auth()

    result: dict = {}

    def on_ok() -> None:
        data = {
            "name": v["name"].get().strip(),
            "host": v["host"].get().strip(),
            "port": v["port"].get().strip(),
            "user": v["user"].get().strip(),
            "auth": v["auth"].get(),
            "key_path": v["key_path"].get().strip(),
            "passphrase": v["passphrase"].get(),
            "tags": v["tags"].get(),
            "note": v["note"].get().strip(),
        }
        password = v["password"].get()
        if password:
            data["password"] = password
        elif not editing:
            data["password"] = ""
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


def snippet_dialog(parent, snip: dict | None, hosts: list, current_host=None):
    """命令片段新增 / 编辑对话框；返回字段字典，取消或校验失败返回 None。"""
    snip = snip or {}
    editing = bool(snip)
    by_id = {str(h["id"]): h["name"] for h in hosts}
    win = tk.Toplevel(parent)
    win.title(t("编辑片段") if editing else t("新增片段"))
    win.transient(parent.winfo_toplevel())
    win.resizable(False, False)
    win.columnconfigure(1, weight=1)

    name = tk.StringVar(value=str(snip.get("name", "")))
    tags = tk.StringVar(value=", ".join(snip.get("tags") or []))
    note = tk.StringVar(value=str(snip.get("note", "")))
    if editing:
        default_refs = ", ".join(by_id.get(i, i) for i in snip.get("host_ids") or [])
    else:
        default_refs = str((current_host or {}).get("name", ""))
    host_refs = tk.StringVar(value=default_refs)

    _form_row(win, 0, t("名称"), name)
    ttk.Label(win, text=t("命令")).grid(row=1, column=0, sticky="nw",
                                        padx=(0, theme.PAD_SM), pady=theme.PAD_XS)
    command = tk.Text(win, width=54, height=7, wrap="word",
                      font=theme.mono(9))
    command.insert("1.0", str(snip.get("command", "")))
    command.grid(row=1, column=1, columnspan=2, sticky="we", pady=theme.PAD_XS)
    _form_row(win, 2, t("关联主机"), host_refs)
    ttk.Label(win, text=t("填主机名，逗号分隔；留空为通用片段"),
              style="CardDim.TLabel").grid(row=3, column=1, sticky="w")
    _form_row(win, 4, t("标签"), tags)
    _form_row(win, 5, t("备注"), note)

    result: dict = {}

    def resolve_host_ids():
        ids: list[str] = []
        text = host_refs.get().replace("，", ",")
        for ref in text.split(","):
            ref = ref.strip()
            if not ref:
                continue
            found = next((h for h in hosts
                          if str(h["id"]) == ref or h["name"] == ref), None)
            if found is None:
                messagebox.showerror(t("关联主机不存在"),
                                     t("未找到主机：{name}", name=ref),
                                     parent=win)
                return None
            if found["id"] not in ids:
                ids.append(found["id"])
        return ids

    def on_ok() -> None:
        ids = resolve_host_ids()
        if ids is None:
            return
        result["data"] = {
            "name": name.get().strip(),
            "command": command.get("1.0", "end").rstrip("\n"),
            "host_ids": ids,
            "tags": tags.get(),
            "note": note.get().strip(),
        }
        win.destroy()

    def on_cancel() -> None:
        result["data"] = None
        win.destroy()

    btns = ttk.Frame(win)
    btns.grid(row=6, column=0, columnspan=3, sticky="e", pady=(theme.PAD_MD, 0))
    ttk.Button(btns, text=t("取消"), command=on_cancel).pack(side="right")
    ttk.Button(btns, text=t("确定"), command=on_ok,
               style="Accent.TButton").pack(side="right", padx=(0, theme.PAD_SM))

    win.grab_set()
    win.wait_window()
    return result.get("data")
