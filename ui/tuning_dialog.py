# -*- coding: utf-8 -*-
"""「推荐设置」对话框：列出按本机硬件算出的 PHP / Nginx 开发配置建议，用户勾选后写入。

对话框本身不碰文件：它只负责展示与勾选，实际写入由调用方传入的 ``on_apply``
完成（PHP 面板写 php.ini、Nginx 面板写 nginx.conf 并跑 `nginx -t`）。
"""
import tkinter as tk
from tkinter import messagebox, ttk

from core.i18n import t
from core.tuning import MachineProfile, Suggestion
from . import theme
from .window_utils import fit_window

COLUMNS = [
    ("use", t("采用"), 60, "center"),
    ("key", t("配置项"), 210, "w"),
    ("current", t("当前值"), 130, "w"),
    ("value", t("建议值"), 130, "w"),
    ("reason", t("理由"), 430, "w"),
]

CHECKED = "☑"
UNCHECKED = "☐"


class TuningDialog(tk.Toplevel):
    """展示建议并让用户决定是否采用。"""

    FIT_SIZE = (980, 620, 760, 440)

    def __init__(self, master, title: str, subtitle: str, profile: MachineProfile,
                 items: list[Suggestion], notes: list[str], on_apply):
        super().__init__(master)
        self.items = items
        self.on_apply = on_apply
        self._checked: dict[str, bool] = {i.key: True for i in items}
        self._iid_to_key: dict[str, str] = {}

        self.title(title)
        self.configure(bg=theme.CARD_BG)
        self.transient(master)

        header = ttk.Frame(self, padding=(16, 14, 16, 4))
        header.pack(fill="x")
        ttk.Label(header, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text=subtitle, style="SubTitle.TLabel").pack(anchor="w", pady=(4, 0))
        ttk.Label(header, text=profile.summary(), style="SubTitle.TLabel").pack(
            anchor="w", pady=(2, 0))
        for note in notes:
            ttk.Label(header, text="· " + note, style="SubTitle.TLabel",
                      wraplength=880, justify="left").pack(anchor="w", pady=(2, 0))

        wrap = ttk.Frame(self)
        wrap.pack(fill="both", expand=True, padx=16, pady=(10, 0))
        self.tree = ttk.Treeview(
            wrap, columns=[c[0] for c in COLUMNS], show="headings", selectmode="none"
        )
        for col, text, width, anchor in COLUMNS:
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor,
                             stretch=(col == "reason"), minwidth=60)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree.tag_configure("odd", background=theme.ROW_ALT)
        self.tree.tag_configure("even", background=theme.CARD_BG)
        self.tree.bind("<Button-1>", self._on_click)

        btns = ttk.Frame(self, padding=(16, 10, 16, 14))
        btns.pack(side="bottom", fill="x", before=wrap)
        ttk.Button(btns, text=t("关闭"), command=self.destroy).pack(side="right")
        self.btn_apply = ttk.Button(btns, text=t("应用选中"), style="Accent.TButton",
                                    command=self._apply)
        self.btn_apply.pack(side="right", padx=(0, 8))
        ttk.Button(btns, text=t("全不选"), command=lambda: self._set_all(False)).pack(side="left")
        ttk.Button(btns, text=t("全选"), command=lambda: self._set_all(True)).pack(
            side="left", padx=(0, 8))
        self.count_label = ttk.Label(btns, text="", style="SubTitle.TLabel")
        self.count_label.pack(side="left", padx=(12, 0))

        self._render()
        self._center(master)

    # ------------------------------------------------------------------ #
    def _render(self) -> None:
        for i, item in enumerate(self.items):
            iid = self.tree.insert(
                "", "end",
                values=(CHECKED, item.key, item.current or t("未设置"),
                        item.value, item.reason),
                tags=("odd" if i % 2 else "even",),
            )
            self._iid_to_key[iid] = item.key
        self._update_count()
        if not self.items:
            self.btn_apply.configure(state="disabled")

    def _on_click(self, event) -> None:
        """只有点第一列（采用）才切换，避免在长文本行上误触。"""
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        key = self._iid_to_key.get(iid)
        if not key:
            return
        self._checked[key] = not self._checked[key]
        self.tree.set(iid, "use", CHECKED if self._checked[key] else UNCHECKED)
        self._update_count()

    def _set_all(self, value: bool) -> None:
        for iid, key in self._iid_to_key.items():
            self._checked[key] = value
            self.tree.set(iid, "use", CHECKED if value else UNCHECKED)
        self._update_count()

    def _update_count(self) -> None:
        picked = sum(1 for v in self._checked.values() if v)
        self.count_label.configure(
            text=t("已选 {picked} / {total} 项", picked=picked, total=len(self.items)))

    def _apply(self) -> None:
        picked = [i for i in self.items if self._checked.get(i.key)]
        if not picked:
            messagebox.showinfo(t("提示"), t("请先勾选要采用的建议项。"), parent=self)
            return
        if not messagebox.askyesno(
                t("应用推荐设置"),
                t("将写入 {count} 项配置（改动前自动备份原文件），确定继续？", count=len(picked)),
                parent=self):
            return
        self.btn_apply.configure(state="disabled")
        self.update_idletasks()
        try:
            ok, message = self.on_apply(picked)
        except Exception as e:  # noqa: BLE001
            ok, message = False, str(e)
        self.btn_apply.configure(state="normal")
        if ok:
            messagebox.showinfo(t("应用完成"), message, parent=self)
            self.destroy()
        else:
            messagebox.showerror(t("应用失败"), message, parent=self)

    def _center(self, master) -> None:
        w, h, min_w, min_h = self.FIT_SIZE
        fit_window(self, master, width=w, height=h, min_width=min_w, min_height=min_h)
