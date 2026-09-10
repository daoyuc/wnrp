# -*- coding: utf-8 -*-
"""「新建站点」可视化向导：分 4 步配置一个 nginx 虚拟站点。

步骤：
  1. 基本信息 —— 域名、项目目录、PHP 版本（决定 fastcgi_pass 端口）
  2. 应用模板 —— Laravel / WordPress / ThinkPHP / 通用 PHP / 静态 / SPA，实时预览配置
  3. hosts 映射 —— 一键把域名写入 hosts 指向 127.0.0.1（无权限时按平台弹窗提权）
  4. 确认创建 —— 落盘 vhost 文件 → 自动补 include → nginx -t 校验 → 写入 hosts → 重载

创建全程后台执行，结果经 queue 回传；校验失败自动还原本次改动并保留备份提示。
"""
import os
import queue
import re
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core import hosts_manager, process_utils as pu
from core.config import Config, IS_WIN, WNRP_ROOT
from core.i18n import t
from core.nginx_manager import NginxManager
from core.php_manager import PhpManager
from core.site_templates import TEMPLATES, TEMPLATE_MAP, render_config
from core.vhost_manager import VhostManager
from .theme import CARD_BG, ERR, FONT, OK, PRIMARY, TEXT
from .window_utils import fit_window

STEP_TITLES = ["基本信息", "应用模板", "hosts 映射", "确认创建"]

# 域名：允许单标签(localhost/foo)、FQDN、以及开头的通配符 *.xxx
_DOMAIN_RE = re.compile(
    r"^(?:\*\.)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?$"
)


def _valid_domain(name: str) -> bool:
    return bool(name and _DOMAIN_RE.match(name) and "--" not in name)


def _nginx_path(p: str) -> str:
    return p.replace("\\", "/")


def _safe_conf_base(domain: str) -> str:
    """由域名得到安全文件名主体（*.local → _local.conf）。"""
    s = re.sub(r"[^A-Za-z0-9._-]", "_", domain).strip("._-")
    return s or "site"


class SiteWizardDialog(tk.Toplevel):
    """新建站点向导。"""

    def __init__(self, master, config: Config, on_done=None):
        super().__init__(master)
        self.config = config
        self.on_done = on_done
        self.vhost_mgr = VhostManager(config)
        self._queue: queue.Queue = queue.Queue()
        self._step = 0
        self._busy = False
        self._finished = False
        self._php_map: list[dict] = []   # {label, name, port}
        self._tpl_key = TEMPLATES[0]["key"]
        self._changed = {"vhost": False, "include": False}
        self._backups: list[tuple[str, str]] = []   # (path, backup)
        self.conf_path: str = ""
        self._fname: str = ""

        self.title(t("新建站点 · 可视化向导"))
        self.configure(bg=CARD_BG)
        self.transient(master)

        # 变量
        self.v_domain = tk.StringVar()
        self.v_root = tk.StringVar()
        self.v_php = tk.StringVar(value="")
        self.v_template = tk.StringVar(value=t(TEMPLATES[0]["name"]))
        self.v_filename = tk.StringVar()
        self.v_hosts = tk.BooleanVar(value=True)

        self._build()
        self._render_steps()
        self._show_step(0)
        self._load_php_versions()
        self._center(master)

    # ------------------------------------------------------------------ #
    # UI 骨架
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        header = ttk.Frame(self, padding=(18, 14, 18, 4))
        header.pack(fill="x")
        ttk.Label(header, text=t("新建 Nginx 站点"), style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text=t("按步骤填写信息并选择应用模板，向导将自动生成 vhost 配置、"
                   "写入 hosts 映射并执行 nginx -t 校验。"),
            style="SubTitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        # 步骤指示条
        self.step_row = ttk.Frame(self, padding=(12, 2, 12, 4))
        self.step_row.pack(fill="x")
        self.step_labels: list[tk.Label] = []
        for i, name in enumerate(STEP_TITLES):
            lab = tk.Label(
                self.step_row, text=f"  {i + 1}. {t(name)}  ", font=(FONT, 9, "bold"),
                background="#DDE3EC", foreground=TEXT, padx=8, pady=4,
            )
            lab.pack(side="left", padx=(0, 8))
            self.step_labels.append(lab)

        # 步骤内容区
        self.content = ttk.Frame(self)
        self.content.pack(fill="both", expand=True, padx=14, pady=4)

        # 底部导航：先于步骤内容分配空间，窗口被压小时按钮仍优先可见
        nav = ttk.Frame(self, padding=(14, 6, 14, 14))
        nav.pack(side="bottom", fill="x", before=self.content)
        self.btn_cancel = ttk.Button(nav, text=t("取消"), command=self._cancel)
        self.btn_cancel.pack(side="left")
        self.hint_label = ttk.Label(nav, text="", style="SubTitle.TLabel")
        self.hint_label.pack(side="left", padx=(12, 0))
        self.btn_next = ttk.Button(nav, text=t("下一步"), style="Accent.TButton", command=self._on_next)
        self.btn_next.pack(side="right")
        self.btn_prev = ttk.Button(nav, text=t("上一步"), command=self._on_prev)
        self.btn_prev.pack(side="right", padx=(0, 8))

        # 分步 frame（先建好，切换显示）
        self._frame_step1 = self._build_step1(self.content)
        self._frame_step2 = self._build_step2(self.content)
        self._frame_step3 = self._build_step3(self.content)
        self._frame_step4 = self._build_step4(self.content)

    # --------------------- 步骤 1：基本信息 --------------------- #
    def _build_step1(self, master) -> ttk.Frame:
        fr = ttk.Frame(master, padding=8)
        card = ttk.LabelFrame(fr, text=t("站点信息"), padding=14)
        card.pack(fill="x")
        grid = ttk.Frame(card)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        def add_label_row(row, text):
            ttk.Label(grid, text=text, font=(FONT, 9, "bold"),
                      background=CARD_BG).grid(row=row, column=0, sticky="ne", pady=5, padx=(0, 8))

        # 域名
        add_label_row(0, t("域名："))
        self.entry_domain = ttk.Entry(grid, textvariable=self.v_domain, font=(FONT, 11))
        self.entry_domain.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(
            grid,
            text=t("示例 myapp.test 或 www.example.com；多个域名用空格分隔；\n"
                   "支持 *.dev 泛解析（通配项不会写入 hosts）。"),
            style="SubTitle.TLabel",
        ).grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.entry_domain.bind("<KeyRelease>", lambda e: self._on_input_changed())

        # 项目目录
        add_label_row(1, t("项目目录："))
        row1 = ttk.Frame(grid)
        row1.grid(row=1, column=1, sticky="ew", pady=4)
        row1.columnconfigure(0, weight=1)
        self.entry_root = ttk.Entry(row1, textvariable=self.v_root, font=(FONT, 10))
        self.entry_root.grid(row=0, column=0, sticky="ew")
        self.entry_root.bind("<KeyRelease>", lambda e: self._on_input_changed())
        self.entry_root.bind("<<FocusOut>>", lambda e: self._on_input_changed())
        ttk.Button(row1, text=t("浏览…"), command=self._pick_root).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(row1, text=t("推荐目录"), command=self._fill_default_root).grid(row=0, column=2, padx=(6, 0))
        ttk.Label(
            grid,
            text=t("选择要绑定到该域名的项目目录（Laravel/ThinkPHP 等会自动拼 /public）。"),
            style="SubTitle.TLabel",
        ).grid(row=1, column=2, sticky="w", padx=(8, 0))

        # PHP 版本
        add_label_row(2, t("PHP 版本："))
        self.cmb_php = ttk.Combobox(grid, textvariable=self.v_php, state="readonly", width=44)
        self.cmb_php.grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(grid, text=t("决定 nginx 的 fastcgi_pass 端口。"), style="SubTitle.TLabel").grid(
            row=2, column=2, sticky="w", padx=(8, 0))

        info = ttk.LabelFrame(fr, text=t("将自动完成"), padding=12)
        info.pack(fill="x", pady=(12, 0))
        auto_lines = [
            t("· 生成独立站点配置文件（{dir}/<域名>.conf）", dir=self.vhost_mgr.vhost_dir),
            t("· 如生效 nginx.conf 尚未 include 站点目录，会自动补一行 include（备份 .bak）"),
            t("· 写入 hosts 把域名指向 127.0.0.1，nginx -t 校验通过后平滑重载"),
        ]
        ttk.Label(
            info,
            text="\n".join(auto_lines),
            style="SubTitle.TLabel", justify="left", background=CARD_BG,
        ).pack(anchor="w")
        return fr

    # --------------------- 步骤 2：应用模板 --------------------- #
    def _build_step2(self, master) -> ttk.Frame:
        fr = ttk.Frame(master, padding=8)
        top = ttk.Frame(fr)
        top.pack(fill="x")

        left = ttk.Frame(top)
        left.pack(side="left", fill="y")
        ttk.Label(left, text=t("应用模板："), font=(FONT, 9, "bold"), background=CARD_BG).pack(anchor="w")
        tpl_names = [t(x["name"]) for x in TEMPLATES]
        self.cmb_tpl = ttk.Combobox(left, textvariable=self.v_template, values=tpl_names,
                                    state="readonly", width=28)
        self.cmb_tpl.pack(anchor="w", pady=(4, 0))
        self.cmb_tpl.bind("<<ComboboxSelected>>", lambda e: self._on_template_change())
        self.cmb_tpl.current(0)

        ttk.Label(left, text=t("配置文件名："), font=(FONT, 9, "bold"), background=CARD_BG).pack(
            anchor="w", pady=(12, 0))
        self.entry_fn = ttk.Entry(left, textvariable=self.v_filename, width=28)
        self.entry_fn.pack(anchor="w", pady=(4, 0))
        ttk.Label(left, text=t("将生成到 {dir} 目录下", dir=self.vhost_mgr.vhost_dir),
                  style="SubTitle.TLabel", wraplength=280, justify="left").pack(
            anchor="w", pady=(2, 0))

        right = ttk.Frame(top)
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self.tpl_summary = tk.Text(
            right, height=6, wrap="word", font=(FONT, 9), relief="flat",
            background="#F4F6FA", foreground=TEXT, padx=10, pady=8, state="disabled",
        )
        self.tpl_summary.pack(fill="x")
        self.docroot_label = ttk.Label(right, text="", style="SubTitle.TLabel")
        self.docroot_label.pack(anchor="w", pady=(4, 0))

        ttk.Label(fr, text=t("配置预览（只读，可稍后手动微调文件）"), style="Section.TLabel").pack(
            anchor="w", pady=(10, 4))
        wrap = ttk.Frame(fr)
        wrap.pack(fill="both", expand=True)
        self.preview = tk.Text(
            wrap, wrap="none", font=("Menlo" if not IS_WIN else "Consolas", 9),
            background="#1E1E1E", foreground="#C8C8C8", relief="flat", padx=10, pady=8,
            state="disabled",
        )
        hs = ttk.Scrollbar(wrap, orient="horizontal", command=self.preview.xview)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.preview.yview)
        self.preview.configure(xscrollcommand=hs.set, yscrollcommand=vs.set)
        self.preview.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")
        return fr

    # --------------------- 步骤 3：hosts --------------------- #
    def _build_step3(self, master) -> ttk.Frame:
        fr = ttk.Frame(master, padding=8)
        card = ttk.LabelFrame(fr, text=t("hosts 映射（域名 → 本机）"), padding=14)
        card.pack(fill="x")
        self.chk_hosts = ttk.Checkbutton(
            card, text=t("自动写入 hosts，使下列域名指向本机 127.0.0.1"),
            variable=self.v_hosts,
        )
        self.chk_hosts.pack(anchor="w")
        ttk.Label(
            card,
            text=t("写入需要系统管理员权限：macOS 会弹出系统授权框，Windows 会弹出 UAC 确认。\n"
                   "已指向 127.0.0.1 的域名自动跳过；指向其它 IP 的域名不覆盖，仅提示。"),
            style="SubTitle.TLabel", justify="left", background=CARD_BG,
        ).pack(anchor="w", pady=(4, 6))

        self.hosts_status = tk.Text(
            card, height=7, wrap="word", font=(FONT, 9), relief="flat",
            background="#F4F6FA", foreground=TEXT, padx=8, pady=6, state="disabled",
        )
        self.hosts_status.pack(fill="x")

        btns = ttk.Frame(card)
        btns.pack(anchor="w", pady=(8, 0))
        ttk.Button(btns, text=t("打开 hosts 文件"), command=self._open_hosts).pack(side="left")
        ttk.Button(btns, text=t("刷新状态"), command=self._refresh_hosts_status).pack(
            side="left", padx=(8, 0))
        ttk.Label(
            btns, text=t("hosts 路径：{path}", path=hosts_manager.hosts_path()),
            style="SubTitle.TLabel",
        ).pack(side="left", padx=(10, 0))
        return fr

    # --------------------- 步骤 4：确认创建 --------------------- #
    def _build_step4(self, master) -> ttk.Frame:
        fr = ttk.Frame(master, padding=8)
        card = ttk.LabelFrame(fr, text=t("创建前确认"), padding=14)
        card.pack(fill="x")
        self.summary = tk.Text(
            card, height=10, wrap="word", font=(FONT, 9), relief="flat",
            background="#F4F6FA", foreground=TEXT, padx=10, pady=8, state="disabled",
        )
        self.summary.pack(fill="x")

        ttk.Label(fr, text=t("执行日志"), style="Section.TLabel").pack(anchor="w", pady=(10, 4))
        wrap = ttk.Frame(fr)
        wrap.pack(fill="both", expand=True)
        self.log = tk.Text(
            wrap, wrap="word", font=("Menlo" if not IS_WIN else "Consolas", 9),
            background="#1E1E1E", foreground="#C8C8C8", relief="flat", padx=10, pady=8,
            state="disabled",
        )
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=vs.set)
        self.log.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.log.tag_configure("ok", foreground=OK)
        self.log.tag_configure("err", foreground=ERR)
        self.log.tag_configure("info", foreground=PRIMARY)
        return fr

    # ------------------------------------------------------------------ #
    # 步骤切换
    # ------------------------------------------------------------------ #
    def _render_steps(self) -> None:
        for i, lab in enumerate(self.step_labels):
            bg = "#2B579A" if i <= self._step else "#DDE3EC"
            fg = "#FFFFFF" if i <= self._step else "#555555"
            lab.configure(bg=bg, fg=fg)

    def _show_step(self, idx: int) -> None:
        self._step = idx
        frames = (self._frame_step1, self._frame_step2, self._frame_step3, self._frame_step4)
        for f in frames:
            f.pack_forget()
        frames[idx].pack(fill="both", expand=True)
        self.btn_prev.configure(state="disabled" if idx == 0 or self._finished else "normal")
        if not self._finished:
            self.btn_next.configure(text=t("创建站点") if idx == 3 else t("下一步"), state="normal")
        self.hint_label.configure(text="")
        if idx == 1:
            self._refresh_preview()
        elif idx == 2:
            self._refresh_hosts_status()
        elif idx == 3:
            self._refresh_summary()
        self._render_steps()

    def _on_prev(self) -> None:
        if self._busy or self._step == 0:
            return
        self._show_step(self._step - 1)

    def _on_next(self) -> None:
        if self._busy:
            return
        if self._step == 0:
            if not self._validate_step1():
                return
            self._show_step(1)
        elif self._step == 1:
            self._show_step(2)
        elif self._step == 2:
            self._show_step(3)
        else:
            self._do_create()

    # ------------------------------------------------------------------ #
    # 校验 & 取值
    # ------------------------------------------------------------------ #
    def _domains(self) -> list[str]:
        return [d.strip().lower() for d in self.v_domain.get().split() if d.strip()]

    def _validate_step1(self) -> bool:
        doms = self._domains()
        if not doms:
            messagebox.showwarning(t("缺少域名"), t("请先填写站点域名。"), parent=self)
            return False
        bad = [d for d in doms if not _valid_domain(d)]
        if bad:
            messagebox.showwarning(
                t("域名不合法"),
                t("以下域名格式不正确：\n{names}\n\n请使用 字母/数字/中划线/点 组成的合法域名。",
                  names="\n".join(bad)),
                parent=self,
            )
            return False
        root = self.v_root.get().strip()
        if not root:
            messagebox.showwarning(t("缺少目录"), t("请选择站点项目目录。"), parent=self)
            return False
        if not os.path.isdir(root):
            messagebox.showwarning(t("目录不存在"), t("项目目录不存在或不是文件夹：\n{root}", root=root), parent=self)
            return False
        return True

    @property
    def _php_selected(self) -> dict | None:
        label = self.v_php.get()
        for m in self._php_map:
            if m["label"] == label:
                return m
        return None

    @property
    def _tpl(self) -> dict:
        name = self.v_template.get()
        for tpl in TEMPLATES:
            if t(tpl["name"]) == name:
                return tpl
        return TEMPLATES[0]

    def _docroot(self) -> str:
        root = _nginx_path(self.v_root.get().strip()).rstrip("/")
        return root + self._tpl.get("root_suffix", "")

    def _selected_port(self) -> int | None:
        sel = self._php_selected
        return sel["port"] if sel else None

    # ------------------------------------------------------------------ #
    # 数据刷新
    # ------------------------------------------------------------------ #
    def _pick_root(self) -> None:
        d = filedialog.askdirectory(parent=self, title=t("选择站点项目目录"),
                                    initialdir=self.v_root.get() or None)
        if d:
            self.v_root.set(d)
            self._refresh_filename()

    def _fill_default_root(self) -> None:
        doms = self._domains()
        label = _safe_conf_base(doms[0]) if doms else "site"
        d = os.path.join(WNRP_ROOT, "www", label)
        self.v_root.set(d)
        self._refresh_filename()
        if not os.path.isdir(d):
            messagebox.showinfo(
                t("推荐目录"),
                t("已填入推荐目录：\n{d}\n\n该目录尚不存在，请先在系统中创建"
                  "（创建站点前向导会再次校验目录存在）。", d=d),
                parent=self,
            )

    def _refresh_filename(self) -> None:
        if self._finished:
            return
        doms = self._domains()
        if not doms:
            return
        base = _safe_conf_base(doms[0])
        cur = self.v_filename.get().strip()
        if not cur or cur == base + ".conf":
            self.v_filename.set(base + ".conf")

    def _on_input_changed(self, *_args) -> None:
        if self._finished:
            return
        self._refresh_filename()
        if self._step == 1:  # 模板预览页
            self._refresh_preview()

    def _load_php_versions(self) -> None:
        """后台扫描本机 PHP 版本并填入下拉框。"""
        self.v_php.set(t("正在检测 PHP 版本…"))

        def worker():
            try:
                pm = PhpManager(self.config)
                versions = pm.scan_versions()
                pm.resolve(refresh_status=True, fast=True)
                self._queue.put(("phps", versions))
            except Exception as e:  # noqa: BLE001
                self._queue.put(("phps_err", str(e)))

        threading.Thread(target=worker, daemon=True).start()
        self._poll()

    def _poll(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll)
            return
        if kind == "phps":
            self._render_phps(payload)
        elif kind == "phps_err":
            self._render_phps([])
        elif kind == "done":
            self._on_done(payload)
        else:
            self._render_phps([])

    def _render_phps(self, versions) -> None:
        self._php_map = []
        labels: list[str] = []
        default = None
        running_default = None
        for v in versions or []:
            disp = v.display or ""
            label = f"{v.name} · " + t("PHP {disp}（FastCGI 端口 {port}）", disp=disp, port=v.port)
            self._php_map.append({"label": label, "name": v.name, "port": v.port})
            labels.append(label)
            if default is None:
                default = label
            if getattr(v, "running", False) and running_default is None:
                running_default = label
        none_label = t("不使用 PHP（静态 / 纯前端）")
        self._php_map.append({"label": none_label, "name": None, "port": None})
        labels.append(none_label)

        if not versions:
            messagebox.showinfo(
                t("未发现 PHP 版本"),
                t("未扫描到可用 PHP 版本（可先到「PHP 版本管理」页确认）。\n"
                  "Laravel / WordPress / ThinkPHP 等模板需要 PHP，可先选「静态站点 / SPA」模板。"),
                parent=self,
            )
        self.cmb_php.configure(values=labels)
        target = running_default or default or none_label
        if self.v_php.get() in labels:
            target = self.v_php.get()
        self.v_php.set(target)
        if self._step == 1:  # 刷新预览中的 fastcgi 端口
            self._refresh_preview()

    def _on_template_change(self) -> None:
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        tpl = self._tpl
        self._tpl_key = tpl["key"]
        self.tpl_summary.configure(state="normal")
        self.tpl_summary.delete("1.0", "end")
        self.tpl_summary.insert(
            "1.0",
            f"[{t(tpl['name'])}]\n{t(tpl['summary'])}\n\n{t(tpl['hint'])}"
        )
        self.tpl_summary.configure(state="disabled")

        root = self.v_root.get().strip()
        doc = self._docroot() if root else t("（未选择项目目录）")
        self.docroot_label.configure(text=t("文档根(root)：{doc}", doc=doc))

        text = render_config(tpl["key"], server_name=" ".join(self._domains()),
                             docroot=doc if root else "", port=self._selected_port())
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _refresh_hosts_status(self) -> None:
        doms = self._domains()
        real = [d for d in doms if not d.startswith("*.")]
        mapping = hosts_manager.mapping_for_domains(real)
        lines: list[str] = []
        for d in doms:
            if d.startswith("*."):
                lines.append(t("· {d} → 泛解析通配，跳过 hosts（不写入）", d=d))
                continue
            ip = mapping.get(d)
            if ip is None:
                lines.append(t("· {d} → 未映射，将写入 127.0.0.1", d=d))
            elif ip == "127.0.0.1":
                lines.append(t("· {d} → 已指向 127.0.0.1（自动跳过）", d=d))
            else:
                lines.append(t("· {d} → 当前指向 {ip}（不覆盖，仅提示）", d=d, ip=ip))
        if not doms:
            lines.append(t("（请先在第 1 步填写域名）"))
        self.hosts_status.configure(state="normal")
        self.hosts_status.delete("1.0", "end")
        self.hosts_status.insert("1.0", "\n".join(lines))
        self.hosts_status.configure(state="disabled")
        self.v_hosts.set(bool(real))

    def _open_hosts(self) -> None:
        pu.open_path(hosts_manager.hosts_path())

    # ------------------------------------------------------------------ #
    # 确认摘要 & 创建
    # ------------------------------------------------------------------ #
    def _refresh_summary(self) -> None:
        doms = self._domains()
        tpl = self._tpl
        sel = self._php_selected
        base = _safe_conf_base(doms[0]) if doms else "site"
        fname = self.v_filename.get().strip() or base + ".conf"
        root = self.v_root.get().strip()
        doc = self._docroot() if root else ""
        file_path = os.path.join(self.vhost_mgr.vhost_dir, fname)
        self.conf_path = file_path
        self._fname = fname
        php_txt = (t("{name}（FastCGI 端口 {port}）", name=sel["name"], port=sel["port"])
                   if sel and sel["name"] else t("不使用 PHP"))
        lines = [
            t("域名：{v}", v=" ".join(doms) or t("（未填写）")),
            t("配置文件名：{v}", v=fname),
            t("配置文件：{v}", v=file_path),
            t("应用模板：{v}", v=t(tpl["name"])),
            t("项目目录：{v}", v=root or t("（未选择）")),
            t("文档根(root)：{v}", v=doc or t("（未选择）")),
            t("PHP 版本：{v}", v=php_txt),
            t("写入 hosts：{v}",
              v=t("是（指向 127.0.0.1）") if self.v_hosts.get() else t("否")),
        ]
        if os.path.exists(file_path):
            lines.append(t("\n注意：同名配置文件已存在，创建时将覆盖（原文件自动备份为 .bak）"))
        if tpl["needs_php"] and not (sel and sel["port"]):
            lines.append(t("\n[错误] 当前模板需要 PHP，但未选择任何 PHP 版本 —— 请返回第 1 步选择。"))
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", "\n".join(lines))
        self.summary.configure(state="disabled")

    def _do_create(self) -> None:
        doms = self._domains()
        if not self._validate_step1():
            self._show_step(0)
            return
        tpl = self._tpl
        sel = self._php_selected
        if tpl["needs_php"] and not (sel and sel["port"]):
            messagebox.showwarning(
                t("缺少 PHP 版本"),
                t("模板 [{tpl}] 需要 PHP 解析，但未选择任何 PHP 版本。\n"
                  "请返回第 1 步选择一个已安装的 PHP 版本。", tpl=t(tpl["name"])),
                parent=self,
            )
            self._show_step(0)
            return
        fname = self.v_filename.get().strip() or _safe_conf_base(doms[0]) + ".conf"
        if not fname.endswith(".conf"):
            fname += ".conf"
        self._fname = fname
        self.conf_path = os.path.join(self.vhost_mgr.vhost_dir, fname)

        # 主线程快照全部入参（后台线程不得触碰 Tk 变量）
        snapshot = {
            "tpl_key": self._tpl["key"],
            "domains": list(doms),
            "docroot": self._docroot(),
            "port": self._selected_port(),
            "hosts_wanted": bool(self.v_hosts.get()),
        }
        content = render_config(
            snapshot["tpl_key"],
            server_name=" ".join(snapshot["domains"]),
            docroot=snapshot["docroot"],
            port=snapshot["port"],
        )

        self._set_busy(True)
        self._append_log(t("== 开始创建站点 =="), "info")

        def worker():
            steps = []
            steps.append(("file", self._step_create_file(content)))
            steps.append(("inc", self._step_ensure_include()))
            steps.append(("test", self._step_test_config()))
            test_payload = steps[2][1]
            if test_payload.get("ok"):
                if snapshot["hosts_wanted"]:
                    steps.append(("hosts", self._step_hosts(snapshot["domains"])))
                steps.append(("reload", self._step_reload()))
            elif test_payload.get("skip") and snapshot["hosts_wanted"]:
                # nginx 缺失：配置与 hosts 照常完成，仅跳过校验/重载
                steps.append(("hosts", self._step_hosts(snapshot["domains"])))
            self._queue.put(("done", steps))

        threading.Thread(target=worker, daemon=True).start()
        self._poll()

    # ---- 各执行步骤（后台线程）---- #
    def _step_create_file(self, content: str) -> dict:
        res = self.vhost_mgr.write_vhost(self._fname, content)
        if res["ok"]:
            self._changed["vhost"] = True
            if res["existed"] and res["backup"]:
                self._backups.append((res["path"], res["backup"]))
            res["message"] = (t("配置文件已写入：{path}", path=res["path"])
                              + (t("（覆盖原文件，备份 .bak）") if res["existed"] else ""))
        return res

    def _step_ensure_include(self) -> dict:
        res = self.vhost_mgr.ensure_include()
        if res["changed"]:
            self._changed["include"] = True
            self._backups.append((res["backup"].rsplit(".bak", 1)[0], res["backup"]))
        if not os.path.exists(self.vhost_mgr.main_conf):
            # 工具环境还没有生效的 nginx 主配置：不视为失败，仅提示
            res["skip"] = True
            res["message"] = (res["message"] or t("未找到生效主配置，跳过 include 自动补全"))
            res["ok"] = False
        return res

    def _step_test_config(self) -> dict:
        nginx = NginxManager()
        if not os.path.exists(nginx.exe):
            return {"ok": False, "skip": True,
                    "message": t("未找到 nginx（{path}），已跳过配置校验；"
                                 "请配置好 nginx 后手动执行「配置检查」。", path=nginx.exe),
                    "output": ""}
        output = nginx.test_config()
        ok = "successful" in output.lower() and "failed" not in output.lower()
        return {"ok": ok, "output": output,
                "message": t("配置检查通过") if ok else t("nginx -t 校验失败（详见上方输出）")}

    def _step_hosts(self, domains: list[str]) -> dict:
        doms = [d for d in domains if not d.startswith("*.")]
        if not doms:
            return {"ok": True, "message": t("没有可写入 hosts 的域名（通配项已跳过）")}
        return hosts_manager.ensure_entries(doms)

    def _step_reload(self) -> dict:
        nginx = NginxManager()
        running, _ = nginx.get_status()
        if not running:
            return {"ok": True, "message": t("nginx 未运行，启动后会自动加载新站点")}
        msg = nginx.reload()
        return {"ok": True, "message": msg}

    # ------------------------------------------------------------------ #
    # 结果回传
    # ------------------------------------------------------------------ #
    def _on_done(self, steps) -> None:
        self._set_busy(False)
        hard = []       # file/inc/test：决定成败与回滚
        soft_fail = []  # hosts/reload：仅提示，不影响站点配置完成
        skipped = []    # 关键步骤中被跳过的（如未找到 nginx）
        for tag, res in steps:
            self._append_result_log(tag, res)
            if tag in ("file", "inc", "test"):
                hard.append(res)
                if res.get("skip"):
                    skipped.append(res)
            elif tag in ("hosts", "reload") and not res.get("ok"):
                soft_fail.append(res)

        fatal = [r for r in hard if not r.get("ok") and not r.get("skip")]
        if fatal:
            keep = messagebox.askyesno(
                t("配置校验失败"),
                t("nginx -t 校验未通过（或写入失败），通常是模板与项目结构不完全匹配。\n\n"
                  "是否保留已生成的文件以便手动修改？\n"
                  "（选择「否」将自动还原本次改动）"),
                parent=self,
            )
            if not keep:
                self._rollback()
            else:
                self._append_log(t("已保留生成文件，请手动修正后用「配置检查 + 平滑重载」验证。"), "err")
            self.btn_next.configure(text=t("重新创建"), state="normal")
            self.btn_cancel.configure(state="normal")
            return

        # 关键步骤均完成（含「未找到 nginx」等跳过场景）
        self._finished = True
        self._append_log(t("== 站点创建完成 =="), "ok")
        self.btn_next.configure(text=t("关闭"), state="normal")
        notes = []
        for s in skipped:
            m = s.get("message")
            if m and m not in notes:
                notes.append(m)
        for sf in soft_fail:
            notes.append(t("注意：{msg}", msg=sf.get("message", t("hosts 或重载未完成。"))))
        msg = t("站点配置已就绪：\n{path}", path=self.conf_path)
        if notes:
            msg += "\n\n" + "\n".join(notes)
        messagebox.showinfo(t("站点创建成功"), msg, parent=self)
        self.hint_label.configure(text=t("完成，可关闭本向导"), foreground=OK)
        if self.on_done:
            self.on_done()
        self.destroy()

    def _append_result_log(self, tag: str, res: dict) -> None:
        titles = {"file": t("① 写入 vhost 配置"), "inc": t("② nginx.conf include 检查"),
                  "test": t("③ nginx -t 配置校验"), "hosts": t("④ 写入 hosts 映射"),
                  "reload": t("⑤ 平滑重载 Nginx")}
        self._append_log(titles.get(tag, tag), "info")
        if tag == "test" and res.get("output"):
            self._append_log(res["output"].strip(), "ok" if res.get("ok") else "err")
        color = "ok" if res.get("ok") else ("info" if res.get("skip") else "err")
        self._append_log(res.get("message", ""), color)

    def _rollback(self) -> None:
        """还原本次改动：vhost 文件 + nginx.conf。"""
        for path, backup in reversed(self._backups):
            if backup and os.path.exists(backup):
                try:
                    shutil.copy2(backup, path)
                    os.remove(backup)
                except OSError:
                    pass
            elif os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._backups.clear()
        self._changed = {"vhost": False, "include": False}
        self._append_log(t("已还原本次改动（vhost 文件与 nginx.conf 均恢复）。"), "err")

    # ------------------------------------------------------------------ #
    def _append_log(self, text: str, tag: str = "") -> None:
        self.log.configure(state="normal")
        start = self.log.index("end-1c")
        self.log.insert("end", text + "\n")
        if tag:
            self.log.tag_add(tag, start, "end-1c")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_next.configure(state=state)
        self.btn_cancel.configure(state=state)
        self.btn_prev.configure(state=state)

    def _cancel(self) -> None:
        if self._busy:
            return
        if self._finished:
            self.destroy()
            return
        changed = bool(self._backups) or self._changed["vhost"] or self._changed["include"]
        if not changed:
            self.destroy()
            return
        if messagebox.askyesno(
            t("关闭向导"),
            t("本次操作已写入部分文件。\n\n是否在关闭前还原（回滚）已写入的内容？\n"
              "（选择「否」将保留已写入的文件）"),
            parent=self,
        ):
            self._rollback()
        self.destroy()

    def _center(self, master) -> None:
        """按屏幕可用工作区收敛尺寸并定位，保证底部（右下角）按钮始终可见。"""
        fit_window(self, master, width=900, height=690, min_width=820, min_height=600)
