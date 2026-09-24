# -*- coding: utf-8 -*-
"""Xdebug 调试开关对话框：显示状态 + 改端口 + 一键开启 / 关闭。"""
import tkinter as tk
from tkinter import messagebox, ttk

from core import xdebug
from core.i18n import t
from core.php_manager import PhpVersion
from . import theme
from .window_utils import fit_window


class XdebugDialog(tk.Toplevel):
    """某一 PHP 版本的 Xdebug 调试开关。"""

    def __init__(self, master, version: PhpVersion, on_restart=None, notify=None):
        super().__init__(master)
        self.version = version
        self.on_restart = on_restart
        self.notify = notify or (lambda msg: None)
        self.state_info = xdebug.status(version)

        self.title(t("Xdebug 调试 · {name}", name=version.name))
        self.resizable(False, False)
        self.configure(bg=theme.CARD_BG)
        self.transient(master)
        self.grab_set()

        body = ttk.Frame(self, padding=theme.PAD_XL)
        body.pack()

        ttk.Label(
            body,
            text=t("[{name}]  PHP {display}", name=version.name, display=version.display),
            style="Title.TLabel",
        ).pack(anchor="w")

        self.var_state = tk.StringVar(value=self._state_text())
        ttk.Label(body, textvariable=self.var_state, style="CardBold.TLabel").pack(
            anchor="w", pady=(theme.PAD_MD, theme.PAD_SM)
        )

        row = ttk.Frame(body, style="Card.TFrame")
        row.pack(fill="x", pady=(0, theme.PAD_SM))
        ttk.Label(row, text=t("调试端口："), style="CardBold.TLabel").pack(side="left")
        self.var_port = tk.StringVar(value=str(self.state_info.get("port", xdebug.DEFAULT_PORT)))
        entry = ttk.Entry(row, textvariable=self.var_port, width=8)
        entry.pack(side="left", padx=(theme.PAD_SM, 0))
        entry.bind("<Return>", lambda e: self._enable())

        if not self.state_info.get("installed"):
            ttk.Label(
                body,
                text=t("未检测到 Xdebug 扩展：请先点 PHP 页的「安装扩展」安装 Xdebug。"),
                style="SubTitle.TLabel", wraplength=460, justify="left",
            ).pack(anchor="w", pady=(0, theme.PAD_SM))

        ttk.Label(
            body,
            text=t("IDE 配置要点：{ide}", ide=t(
                "PhpStorm 在「设置 → PHP → Debug → Xdebug」把调试端口改为上面的端口；"
                "VS Code 在 launch.json 的 \"Listen for Xdebug\" 里填同一端口。")),
            style="SubTitle.TLabel", wraplength=460, justify="left",
        ).pack(anchor="w", pady=(0, theme.PAD_SM))
        ttk.Label(
            body,
            text=t("说明：开启 / 关闭都只改 php.ini（自动备份 .bak 并自检，失败自动还原），"
                   "重启该 PHP 版本后生效。"),
            style="SubTitle.TLabel", wraplength=460, justify="left",
        ).pack(anchor="w")

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(theme.PAD_LG, 0))
        self.btn_disable = ttk.Button(btns, text=t("关闭调试"), command=self._disable)
        self.btn_disable.pack(side="right")
        self.btn_enable = ttk.Button(btns, text=t("开启调试"), style="Accent.TButton",
                                     command=self._enable)
        self.btn_enable.pack(side="right", padx=(0, theme.PAD_SM))
        ttk.Button(btns, text=t("关闭"), command=self.destroy).pack(side="left")
        self._sync_buttons()

        fit_window(self, master)

    # ------------------------------------------------------------------ #
    def _state_text(self) -> str:
        info = self.state_info
        if not info.get("ini_exists"):
            return t("状态：未找到生效的 php.ini（请先「初始化 php.ini」）")
        if not info.get("installed"):
            return t("状态：未检测到 Xdebug 扩展")
        if info.get("enabled"):
            return t("状态：已开启 · 端口 {port} · mode={mode}",
                     port=info.get("port"), mode=info.get("mode") or "-")
        return t("状态：未开启（扩展已就绪）")

    def _sync_buttons(self) -> None:
        info = self.state_info
        ready = bool(info.get("ini_exists")) and bool(info.get("installed"))
        self.btn_enable.configure(state="normal" if ready else "disabled")
        self.btn_disable.configure(
            state="normal" if (ready and info.get("enabled")) else "disabled")

    def _port(self) -> int:
        raw = (self.var_port.get() or "").strip()
        try:
            port = int(raw)
        except ValueError:
            raise ValueError(t("调试端口必须是整数"))
        if not 1 <= port <= 65535:
            raise ValueError(t("调试端口必须在 1-65535 之间"))
        return port

    def _after_change(self, ok: bool, msg: str) -> None:
        if not ok:
            messagebox.showerror(t("Xdebug 调试"), msg, parent=self)
            self.notify(msg)
            return
        self.state_info = xdebug.status(self.version)
        self.var_state.set(self._state_text())
        self._sync_buttons()
        self.notify(msg)
        if self.version.running and self.on_restart is not None and messagebox.askyesno(
                t("Xdebug 调试"),
                t("{msg}\n\n该版本正在运行，是否立即重启使其生效？", msg=msg),
                parent=self):
            self.destroy()
            self.on_restart()
            return
        messagebox.showinfo(t("Xdebug 调试"), msg, parent=self)

    def _enable(self) -> None:
        try:
            port = self._port()
        except ValueError as e:
            messagebox.showerror(t("调试端口不合法"), str(e), parent=self)
            return
        ok, msg, _backup = xdebug.enable(self.version, port=port)
        self._after_change(ok, msg)

    def _disable(self) -> None:
        if not messagebox.askyesno(
                t("关闭调试"),
                t("将注释 [{name}] 的 php.ini 中的 Xdebug 加载行（原文件自动备份为 .bak）。\n"
                  "确定继续？", name=self.version.name),
                parent=self):
            return
        ok, msg, _backup = xdebug.disable(self.version)
        self._after_change(ok, msg)
