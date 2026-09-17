# -*- coding: utf-8 -*-
"""安装扩展对话框：本地扩展启用/禁用 + 在线下载安装第三方扩展。

- 「本地扩展」页：扫描 <php_dir>/ext/*.dll，勾选即启用（写回 ini，备份 .bak）；
- 「在线安装」页：Redis / Xdebug / ImageMagick / Swoole / Memcached，
  按本机 PHP 版本 + NTS/TS + 架构自动匹配 PECL / xdebug.org 最新构建。
"""
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core import php_extension as ext_mod
from core.i18n import t
from core.php_manager import PhpManager, PhpVersion
from . import theme
from .window_utils import fit_window


class ExtensionDialog(tk.Toplevel):
    """管理/安装某 PHP 版本的扩展。"""

    def __init__(self, master, version: PhpVersion, php_mgr: PhpManager):
        super().__init__(master)
        self.version = version
        self.php_mgr = php_mgr
        self._queue: queue.Queue = queue.Queue()
        self._infos: list[ext_mod.ExtInfo] = []
        self._vars: dict[str, tk.BooleanVar] = {}
        self._runtime: ext_mod.RuntimeInfo | None = None
        self._busy = False
        self._draining = False  # 队列 drain 是否已启动（唯一消费者）

        ver_txt = f"PHP {version.display}" if version.display else t("PHP 版本未知")
        title = t("安装扩展 · {name} ({ver})", name=version.name, ver=ver_txt)
        self.title(title)
        self.configure(bg=theme.CARD_BG)
        self.transient(master)

        header = ttk.Frame(self, padding=(16, 14, 16, 4))
        header.pack(fill="x")
        ttk.Label(header, text=title,
                  style="Title.TLabel").pack(anchor="w")
        self.sub_label = ttk.Label(header, text=t("正在加载…"), style="SubTitle.TLabel")
        self.sub_label.pack(anchor="w", pady=(4, 0))

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=16, pady=(8, 4))
        self.local_tab = ttk.Frame(nb, padding=8)
        self.online_tab = ttk.Frame(nb, padding=8)
        nb.add(self.local_tab, text=f"  {t('本地扩展')}  ")
        nb.add(self.online_tab, text=f"  {t('在线安装')}  ")

        self._build_local_tab()
        self._build_online_tab()

        btns = ttk.Frame(self, padding=(16, 0, 16, 14))
        # 先于页签内容分配空间（side=bottom），保证右下角按钮始终可见
        btns.pack(side="bottom", fill="x", before=nb)
        self.progress_label = ttk.Label(btns, text="", style="SubTitle.TLabel")
        self.progress_label.pack(side="left")
        ttk.Button(btns, text=t("关闭"), command=self.destroy).pack(side="right")
        self.btn_save = ttk.Button(btns, text=t("保存扩展启用状态"),
                                   style="Accent.TButton", command=self._save)
        self.btn_save.pack(side="right", padx=(0, 8))

        self._center()
        self._load()

    # ------------------------------------------------------------------ #
    # UI 构建
    # ------------------------------------------------------------------ #
    def _build_local_tab(self) -> None:
        self.local_canvas = tk.Canvas(self.local_tab, background=theme.CARD_BG, highlightthickness=0)
        vsb = ttk.Scrollbar(self.local_tab, orient="vertical", command=self.local_canvas.yview)
        self.local_canvas.configure(yscrollcommand=vsb.set)
        self.local_inner = ttk.Frame(self.local_canvas)
        inner_id = self.local_canvas.create_window((0, 0), window=self.local_inner, anchor="nw")
        self.local_inner.bind(
            "<Configure>", lambda e: self.local_canvas.configure(scrollregion=self.local_canvas.bbox("all"))
        )
        self.local_canvas.bind(
            "<Configure>", lambda e: self.local_canvas.itemconfigure(inner_id, width=e.width)
        )
        self.local_canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        # 只接管本区域滚轮：bind_all 是全应用级，常驻会劫持其它窗口的滚动，
        # 且对话框销毁后回调仍可能被触发。canvas 内部控件是它的子 widget，
        # 事件冒泡会经过 canvas，无需 bind_all。
        self.local_canvas.bind("<MouseWheel>", self._on_wheel)

    def _build_online_tab(self) -> None:
        hint = ttk.Label(
            self.online_tab,
            text=t("在线扩展会按本机 PHP 版本 / NTS-TS / 架构自动匹配官方构建，"
                   "安装后自动启用（重启 FastCGI 生效）。"),
            style="SubTitle.TLabel", wraplength=760,
        )
        hint.pack(anchor="w", pady=(0, 6))
        self.online_tree = ttk.Treeview(
            self.online_tab, columns=("ext", "name", "desc", "status"),
            show="headings", selectmode="browse", height=9,
        )
        for cid, text, w in (("ext", t("扩展"), 90), ("name", t("名称"), 120),
                             ("desc", t("说明"), 380), ("status", t("状态"), 150)):
            self.online_tree.heading(cid, text=text)
            self.online_tree.column(cid, width=w, anchor="w", stretch=(cid == "desc"))
        ovsb = ttk.Scrollbar(self.online_tab, orient="vertical", command=self.online_tree.yview)
        self.online_tree.configure(yscrollcommand=ovsb.set)
        self.online_tree.pack(side="left", fill="both", expand=True, pady=(0, 4))
        ovsb.pack(side="right", fill="y", pady=(0, 4))
        self.online_tree.tag_configure("ok", foreground=theme.OK)
        self.online_tree.tag_configure("warn", foreground=theme.WARN)
        self.online_tree.tag_configure("err", foreground=theme.ERR)
        self.online_tree.bind("<<TreeviewSelect>>", lambda e: self._update_online_btn())

        bar = ttk.Frame(self.online_tab)
        bar.pack(fill="x", pady=(4, 0))
        self.online_status = ttk.Label(bar, text="", style="SubTitle.TLabel")
        self.online_status.pack(side="left")
        self.btn_install = ttk.Button(bar, text=t("下载并安装"), style="Accent.TButton",
                                      state="disabled", command=self._install_online)
        self.btn_install.pack(side="right")

    # ------------------------------------------------------------------ #
    # 加载 / 渲染
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        def worker():
            try:
                infos = ext_mod.scan_ext_dir(self.version.dir)
                enabled = ext_mod.read_enabled_exts(self.version.ini)
                for info in infos:
                    info.enabled = info.key in enabled
                rt = ext_mod.detect_runtime(self.version.dir)
                self._queue.put(("loaded", (infos, rt)))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    # ------------------------------------------------------------------ #
    # 队列分发：drain 是唯一消费者（加载 / 保存 / 安装各自一个 after 轮询
    # 抢同一队列会互相吞消息，导致保存与安装结果永远显示不出来）
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
        self.after(80 if self.winfo_ismapped() else 400, self._drain)

    def _dispatch(self, kind: str, payload) -> None:
        if kind == "error":
            self.sub_label.configure(text=t("加载失败：{err}", err=payload))
        elif kind == "loaded":
            self._on_loaded(payload)
        elif kind == "prog":
            self.progress_label.configure(text=payload)
        elif kind in ("saved", "save_err"):
            self._on_saved(kind, payload)
        elif kind == "installed":
            self._on_installed(payload)

    def _on_loaded(self, payload: tuple) -> None:
        infos, rt = payload
        self._infos = infos
        self._runtime = rt
        self._render_local(infos)
        self._render_online(infos, rt)

        ext_dir = f"{self.version.dir}\\ext"
        n_enabled = ext_mod.read_enabled_exts(self.version.ini).__len__()
        rt_txt = f"{rt.series} · {rt.ts.upper()} · {rt.arch}" if rt.series else t("版本信息未知")
        self.sub_label.configure(
            text=t("ext：{dir}  |  当前 {rt}  |  {n} 个扩展",
                   dir=ext_dir, rt=rt_txt, n=len(infos))
            + "  ·  " + t("{n} 个已启用", n=n_enabled)
        )

    def _render_local(self, infos: list[ext_mod.ExtInfo]) -> None:
        for w in self.local_inner.winfo_children():
            w.destroy()
        self._vars.clear()
        if not infos:
            ttk.Label(self.local_inner, text=t("ext 目录下没有扩展 dll。"), background=theme.CARD_BG,
                      foreground=theme.GRAY).pack(anchor="w", padx=6, pady=8)
            return
        for info in infos:
            row = ttk.Frame(self.local_inner)
            row.pack(fill="x", pady=2, padx=4)
            var = tk.BooleanVar(value=info.enabled)
            self._vars[info.key] = var
            ttk.Checkbutton(row, variable=var, command=self._update_dirty).pack(side="left")
            ttk.Label(row, text=info.dll, font=(theme.FONT, 9, "bold"),
                      background=theme.CARD_BG, width=24, anchor="w").pack(side="left", padx=(2, 8))
            ttk.Label(row, text=t(info.desc), style="SubTitle.TLabel", background=theme.CARD_BG,
                      width=26, anchor="w").pack(side="left")
            status = t("已启用") if info.enabled else t("未启用")
            ttk.Label(row, text=status, foreground=theme.OK if info.enabled else theme.GRAY,
                      background=theme.CARD_BG, font=(theme.FONT, 9, "bold")).pack(side="right", padx=8)

    def _render_online(self, infos: list[ext_mod.ExtInfo], rt: ext_mod.RuntimeInfo) -> None:
        self.online_tree.delete(*self.online_tree.get_children())
        installed_keys = {i.key for i in infos}
        enabled_keys = ext_mod.read_enabled_exts(self.version.ini)
        if not rt.series:
            self.online_status.configure(text=t("无法探测 PHP 版本信息，在线安装不可用。"), foreground=theme.ERR)
        else:
            self.online_status.configure(
                text=t("匹配目标：PHP {series} · {ts} · {arch} · {compiler}",
                       series=rt.series, ts=rt.ts.upper(), arch=rt.arch,
                       compiler=rt.compiler or "—")
            )
        for item in ext_mod.EXT_CATALOG:
            key = item["key"]
            if key in enabled_keys:
                status, tag = t("已启用"), "ok"
            elif key in installed_keys:
                status, tag = t("已下载（未启用）"), "warn"
            else:
                status, tag = t("可安装"), ""
            self.online_tree.insert("", "end",
                                    values=(key, t(item["name"]), t(item["desc"]), status),
                                    tags=(tag,) if tag else ())

    def _update_dirty(self) -> None:
        changes = self._diff()
        self.btn_save.configure(
            text=t("保存扩展启用状态") if not changes else t("保存（{n} 项变更）", n=len(changes))
        )

    def _diff(self) -> set[str]:
        enabled = {i.key for i in self._infos if i.enabled}
        wanted = {k for k, v in self._vars.items() if v.get()}
        return wanted ^ enabled

    # ------------------------------------------------------------------ #
    # 保存
    # ------------------------------------------------------------------ #
    def _save(self) -> None:
        if self._busy:
            return
        enabled = {i.key for i in self._infos if i.enabled}
        wanted = {k for k, v in self._vars.items() if v.get()}
        enable = wanted - enabled
        disable = enabled - wanted
        if not enable and not disable:
            messagebox.showinfo(t("提示"), t("没有需要保存的变更。"), parent=self)
            return
        self._busy = True
        self.btn_save.configure(state="disabled")

        def worker():
            try:
                count, backup = ext_mod.apply_extensions(self.version.ini, enable, disable)
                self._queue.put(("saved", (count, backup)))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("save_err", str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    def _on_saved(self, kind: str, payload) -> None:
        self._busy = False
        self.btn_save.configure(state="normal")
        if kind == "save_err":
            messagebox.showerror(t("保存失败"), str(payload), parent=self)
            return
        count, backup = payload
        self._reload_local_state()
        self._render_online(self._infos, self._runtime)
        self._update_dirty()
        messagebox.showinfo(
            t("已保存"),
            t("共更新 {count} 个扩展的启用状态。\n备份：{backup}\n\n"
              "重启 [{name}]（停止后启动）即可生效。",
              count=count, backup=backup, name=self.version.name),
            parent=self,
        )

    def _reload_local_state(self) -> None:
        enabled = ext_mod.read_enabled_exts(self.version.ini)
        for info in self._infos:
            info.enabled = info.key in enabled

    # ------------------------------------------------------------------ #
    # 在线安装
    # ------------------------------------------------------------------ #
    def _selected_online(self) -> dict | None:
        sel = self.online_tree.selection()
        if not sel:
            return None
        key = self.online_tree.item(sel[0], "values")[0]
        for item in ext_mod.EXT_CATALOG:
            if item["key"] == key:
                return item
        return None

    def _update_online_btn(self) -> None:
        sel = self._selected_online()
        state = "normal" if (sel and not self._busy and self._runtime
                             and self._runtime.series) else "disabled"
        self.btn_install.configure(state=state)

    def _install_online(self) -> None:
        if self._busy:
            return
        item = self._selected_online()
        if item is None or self._runtime is None or not self._runtime.series:
            return
        self._busy = True
        self.btn_install.configure(state="disabled")
        self.progress_label.configure(text=t("正在安装 {name} …", name=item["name"]))

        def worker():
            try:
                def progress(msg):
                    self._queue.put(("prog", msg))

                ok, msg, dll = ext_mod.install_online(item, self.version.dir, self._runtime, progress)
                self._queue.put(("installed", (item["key"], ok, msg, dll)))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("installed", (item["key"], False, str(e), "")))

        threading.Thread(target=worker, daemon=True).start()
        self._start_drain()

    def _on_installed(self, payload: tuple) -> None:
        key, ok, msg, dll = payload
        self._busy = False
        self.progress_label.configure(text="")
        self._update_online_btn()
        if not ok:
            messagebox.showerror(t("安装失败"), msg, parent=self)
            return
        # 自动启用新装的扩展
        try:
            ext_mod.apply_extensions(self.version.ini, {key}, set())
        except Exception:  # noqa: BLE001
            pass
        self._reload_local_state()
        self._render_local(self._infos)
        self._render_online(self._infos, self._runtime)
        self._update_dirty()
        messagebox.showinfo(
            t("安装成功"),
            t("{msg}\n\n已自动写入 php.ini 启用。\n重启 [{name}]（停止后启动）即可生效。",
              msg=msg, name=self.version.name),
            parent=self,
        )

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def _on_wheel(self, event) -> None:
        if not self.winfo_exists():
            return
        try:
            self.local_canvas.yview_scroll(int(-event.delta / 120), "units")
        except tk.TclError:
            pass

    def _center(self) -> None:
        """按屏幕可用工作区收敛窗口大小并居中，保证右下角按钮可见。"""
        fit_window(self, self.master, width=780, height=700,
                   min_width=680, min_height=560)

    def destroy(self) -> None:
        try:
            self.local_canvas.unbind("<MouseWheel>")
        except Exception:  # noqa: BLE001
            pass
        super().destroy()
