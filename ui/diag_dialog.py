# -*- coding: utf-8 -*-
"""一键体检对话框：对选中站点（或全站）跑只读诊断，分级列出 502 成因，
并对可安全修复项（补 include / 写 hosts / 启动 PHP / 重新启用 / 启动 Nginx）提供一键修复。"""
import os
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from core import hosts_manager
from core.diag import level_label
from core.i18n import t
from core.php_manager import PhpManager
from core.vhost_manager import VhostManager
from . import theme
from .window_utils import fit_window

_PAD = theme.PAD_MD


class DiagDialog(tk.Toplevel):
    def __init__(self, master, entry, vhost_mgr: VhostManager, notify=None):
        super().__init__(master)
        self.entry = entry  # VhostEntry | None（None = 全站）
        self.vhost_mgr = vhost_mgr
        self.cfg = vhost_mgr.config
        self.notify = notify or (lambda m: None)
        self.deps: dict = {}
        self._busy = False

        self.title(t("一键体检") + (f" · {entry.server_name}" if entry else t(" · 全部站点")))
        self.resizable(True, True)
        self.configure(bg=theme.CARD_BG)
        self.transient(master)
        self.grab_set()
        self._build()
        self._start()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        body = ttk.Frame(self, padding=theme.PAD_LG)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=t("一键体检"), style="Title.TLabel").pack(anchor="w")
        self._status = ttk.Label(body, text=t("正在体检…"), style="SubTitle.TLabel")
        self._status.pack(anchor="w", pady=(0, _PAD))

        # 滚动结果区
        box = ttk.Frame(body)
        box.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(box, bg=theme.CARD_BG, highlightthickness=0)
        self._scroll = ttk.Scrollbar(box, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=self._scroll.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        self._scroll.pack(side="right", fill="y")
        self._inner = ttk.Frame(self._canvas)
        self._win = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._inner.bind("<Configure>",
                         lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfig(self._win, width=e.width))

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(_PAD, 0))
        self.btn_rerun = ttk.Button(bar, text=t("重新体检"), command=self._start)
        self.btn_rerun.pack(side="right")
        ttk.Button(bar, text=t("关闭"), command=self.destroy).pack(side="right", padx=(0, _PAD))

        fit_window(self, master)

    # ------------------------------------------------------------------ #
    def _start(self) -> None:
        if self._busy:
            return
        self._busy = True
        self.btn_rerun.configure(state="disabled")
        self._status.configure(text=t("正在体检…"))
        self._clear()

        def worker():
            try:
                pm = PhpManager(self.cfg)
                pm.scan_versions()
                pm.resolve(refresh_status=True, fast=False)
                nginx = self.vhost_mgr.nginx
                nginx_running = nginx.get_status()[0]
                logs_dir = nginx.logs_dir
                hosts_map: dict = {}
                try:
                    with open(hosts_manager.hosts_path(), "r",
                              encoding="utf-8", errors="replace") as f:
                        hosts_map = hosts_manager.parse_mapping(f.read())
                except OSError:
                    pass
                entries = [self.entry] if self.entry else self.vhost_mgr.scan(include_disabled=True)
                results = []
                for e in entries:
                    items = self._diagnose(e, pm.versions, nginx_running, logs_dir, hosts_map)
                    results.append((e, items))
                self.deps = {"php_versions": pm.versions, "nginx": nginx,
                             "logs_dir": logs_dir, "hosts_map": hosts_map}
                self.after(0, lambda: self._render(results))
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda: self._status.configure(
                    text=t("体检失败：{err}", err=e)))
            finally:
                self._busy = False
                self.after(0, lambda: self.btn_rerun.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _diagnose(entry, php_versions, nginx_running, logs_dir, hosts_map):
        from core import diag

        return diag.diagnose_site(
            entry, config=None, vhost_mgr=None,
            php_versions=php_versions, nginx_running=nginx_running,
            logs_dir=logs_dir, hosts_map=hosts_map,
        )

    # ------------------------------------------------------------------ #
    def _clear(self) -> None:
        for w in list(self._inner.children.values()):
            w.destroy()

    def _render(self, results) -> None:
        self._clear()
        if not results:
            ttk.Label(self._inner, text=t("未发现任何站点配置")).pack(anchor="w")
            self._status.configure(text=t("无站点"))
            return
        for entry, items in results:
            self._render_entry(entry, items)
        # 汇总
        worst = "ok"
        order = {"ok": 0, "skip": 0, "warn": 1, "err": 2}
        for _, items in results:
            for it in items:
                if order.get(it.level, 0) > order.get(worst, 0):
                    worst = it.level
        self._status.configure(
            text=t("体检完成：{n} 个站点，总体 {level}", n=len(results), level=_badge(worst)),
            fg=self._color(worst))

    def _render_entry(self, entry, items) -> None:
        from core import diag

        sec = ttk.Frame(self._inner, style="Card.TFrame")
        sec.pack(fill="x", pady=(0, _PAD), padx=2)
        pad = ttk.Frame(sec, padding=theme.PAD_MD)
        pad.pack(fill="x")

        summary = diag.summarize(items)
        head = ttk.Frame(pad)
        head.pack(fill="x")
        ttk.Label(head, text=entry.server_name, style="CardBold.TLabel").pack(side="left")
        ttk.Label(head, text=_badge(summary), foreground=self._color(summary),
                  style="CardBold.TLabel").pack(side="left", padx=(6, 0))
        ttk.Label(head, text=entry.file_rel, style="SubTitle.TLabel").pack(
            side="left", padx=(6, 0))

        for it in items:
            row = ttk.Frame(pad)
            row.pack(fill="x", pady=(2, 0))
            mark = ttk.Label(row, text=level_label(it.level),
                            foreground=self._color(it.level),
                            font=("", 11, "bold"))
            mark.pack(side="left")
            txt = ttk.Label(row, text=it.name + (f"：{it.detail}" if it.detail else ""),
                            style="Card.TLabel", wraplength=640, justify="left")
            txt.pack(side="left", fill="x", expand=True, padx=(4, 0))
            if it.fix_key and it.level in ("err", "warn"):
                ttk.Button(row, text=t("修复"),
                           command=lambda k=it.fix_key, e=entry: self._fix(k, e)).pack(
                    side="right", padx=(4, 0))
            elif it.fix:
                ttk.Label(row, text=t("修复：{fix}", fix=it.fix),
                          style="SubTitle.TLabel", wraplength=640, justify="left").pack(
                    side="left", padx=(8, 0), fill="x", expand=True)

    # ------------------------------------------------------------------ #
    def _fix(self, fix_key: str, entry) -> None:
        if self._busy:
            return
        self._busy = True
        self.btn_rerun.configure(state="disabled")
        self._status.configure(text=t("正在修复…"))

        def worker():
            try:
                msg = self._apply_fix(fix_key, entry)
            except Exception as e:  # noqa: BLE001
                msg = t("修复失败：{err}", err=e)
            self.after(0, lambda: self.notify(msg))
            self.after(0, lambda: self._status.configure(text=msg))
            self.after(0, self._start)  # 修复后重新体检

        threading.Thread(target=worker, daemon=True).start()

    def _apply_fix(self, fix_key: str, entry) -> str:
        if fix_key == "include":
            res = self.vhost_mgr.ensure_include()
            out = res.get("message", "")
            if res.get("ok") and self.deps.get("nginx").get_status()[0]:
                out += "\n" + self.vhost_mgr.nginx.reload()
            return out or t("已补 include")
        if fix_key == "enable":
            res = self.vhost_mgr.set_site_enabled(entry.file, True)
            return res.get("message", t("已重新启用站点"))
        if fix_key == "hosts":
            doms = [d for d in entry.server_name.split() if not d.startswith("*.")]
            res = hosts_manager.ensure_entries(doms, ip="127.0.0.1")
            return res.get("message", t("已写入 hosts"))
        if fix_key == "php":
            ver = (entry.php_version or "").split(",")[0].strip()
            v = next((x for x in self.deps.get("php_versions", [])
                      if x.name == ver), None)
            if v is None:
                return t("未找到 PHP 版本 {ver}，无法启动", ver=ver)
            return PhpManager(self.cfg).start(v)
        if fix_key == "nginx":
            return self.vhost_mgr.nginx.start()
        return t("暂不支持自动修复该项")

    # ------------------------------------------------------------------ #
    def _color(self, level: str) -> str:
        return {"ok": theme.ok, "warn": theme.warn, "err": theme.err,
                "skip": theme.gray}.get(level, theme.text)


def _badge(level: str) -> str:
    return {
        "ok": t("正常"), "warn": t("有警告"), "err": t("有错误"), "skip": t("不适用"),
    }.get(level, level)
