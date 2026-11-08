# -*- coding: utf-8 -*-
"""左侧竖向导航（替代横向页签）。

为什么需要它
------------
主界面原来用 ``ttk.Notebook`` 横排页签。模块与插件全开时页面多达 14 个
（总览 / PHP / Nginx / Redis / MySQL / 站点映射 / SQLite / 日志 / 邮件 /
运行日志 / 项目启动器 / 键鼠统计 / 访问日志聚合 / 关于），在 1080px 宽的默认
窗口里页签条会溢出到窗口外，右侧页面**没有任何滚动或折叠入口**，等于直接不可达。
竖向导航把「标题长度」换成「行数」：一列 184px 的窄栏即可装下全部页面，
页面继续增多时也只需滚动，不再挤占宽度。

设计要点
--------
- 分组是 ``Treeview`` 的父节点（服务 / 站点 / 诊断 / 其他），页面是子节点；
  分组行**不是页面**：点它只会把选中项还原到当前页（见 ``_on_select``）。
- 页面切换只做 ``pack`` / ``pack_forget``（不重建面板）。这样
  ``main_window._panel_visible()`` 依赖的 ``winfo_ismapped()`` 语义与 Notebook
  完全一致：不可见页面未映射 → 停掉它的自动刷新。
- 状态圆点用 ``PhotoImage`` 逐像素画（8px），三态缓存；``put`` 不可用时退化为
  文本前缀「●」，不影响功能。主题切换由 ``refresh_theme()`` 重建图片。
- API 刻意与 Notebook 的子集同名（``add/insert/tabs/index/select``），
  主窗口与既有冒烟测试只需替换对象名。
"""
import tkinter as tk
from tkinter import ttk

from core import modules
from core.i18n import t
from . import theme

# 侧栏宽度：够放「SQLite 数据库」「访问日志聚合」这类最长标题，又不过分挤压内容
NAV_WIDTH = 184
# 行高与圆点尺寸（与 theme.py 的 Nav.Treeview rowheight 保持一致）
NAV_ROW_H = 28
DOT_SIZE = 8

# 圆点状态 → 调色板角色（"" = 无状态，不画点）
_STATE_ROLES = {"on": "OK", "off": "GRAY", "warn": "WARN"}


def group_title(group: str) -> str:
    """分组标题的译文（调用时机在渲染时，语言热切换后重建界面即可生效）。

    这里刻意把 msgid 写成 ``t("服务")`` 字面量而不是 ``t(modules.GROUP_NAMES[...])``：
    ``_i18n_scan.py`` 只静态识别字面量参数，走变量会让这四个文案扫不到、
    在四语 JSON 里被当成死键清掉。
    """
    if group == "service":
        return t("服务")
    if group == "site":
        return t("站点")
    if group == "diag":
        return t("诊断")
    if group == "other":
        return t("其他")
    return t(modules.GROUP_NAMES.get(group, group))


class SideNav(ttk.Frame):
    """左侧竖向分组导航 + 右侧页面宿主。

    用法::

        nav = SideNav(self)
        nav.pack(fill="both", expand=True)
        php = PhpPanel(nav.content, ...)     # 面板的 master 是 nav.content
        nav.add(php, text=t("PHP 版本管理"), key="php", group="service")
        nav.bind("<<NavChanged>>", lambda e: nav.current_key())
        nav.restore("php")                   # 未知 key 自动回落第一个页面
    """

    def __init__(self, master, *, width: int = NAV_WIDTH, **kw):
        super().__init__(master, **kw)
        # 侧栏宽度固定（pack_propagate(False)）：内容再长也不撑宽导航栏
        self.sidebar = ttk.Frame(self, style="Nav.TFrame", width=width)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        theme.divider(self, orient="vertical").pack(side="left", fill="y")

        self.content = ttk.Frame(self)
        self.content.pack(side="left", fill="both", expand=True)

        self.tree = ttk.Treeview(self.sidebar, show="tree", selectmode="browse",
                                 style="Nav.Treeview")
        self._scrollbar = ttk.Scrollbar(self.sidebar, orient="vertical",
                                        command=self.tree.yview)
        self.tree.configure(yscrollcommand=self._on_yscroll)
        self.tree.pack(side="left", fill="both", expand=True,
                       padx=(theme.PAD_SM, 0), pady=theme.PAD_SM)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # 注册表：order 决定显示顺序，pages/texts/group_of 供查询与重建
        self._order: list[str] = []
        self._pages: dict[str, tk.Misc] = {}
        self._texts: dict[str, str] = {}
        self._group_of: dict[str, str] = {}
        self._group_iids: dict[str, str] = {}
        self._states: dict[str, str] = {}
        self._current: str = ""
        self._shown: tk.Misc | None = None
        self._suppress = False   # 程序化改选中时抑制 <<TreeviewSelect>> 递归
        self._dots: dict[str, tk.PhotoImage] = {}
        self._dot_ok = True      # PhotoImage 不可用时退化为文本圆点
        self._build_dots()

    # ------------------------------------------------------------------ #
    # 页面登记
    # ------------------------------------------------------------------ #
    def add(self, page, text: str, key: str = "", group: str = "") -> None:
        """追加一个页面（顺序即显示顺序）。"""
        self.insert(len(self._order), page, text, key=key, group=group)

    def insert(self, index: int, page, text: str, key: str = "",
               group: str = "") -> None:
        """在指定位置插入页面。

        ``key`` 是记忆上次页面用的稳定标识（模块 key），缺省时退回页面控件名；
        ``group`` 留空时按 ``modules.group_of(key)`` 自动归类（模块注册表是唯一来源），
        显式传入则用于非模块页面（运行日志 / 关于等），未知值归入 other。
        """
        key = str(key or text)
        if key in self._pages:  # 重复 key 会破坏「记忆页面」的映射，直接拒绝
            raise ValueError(f"duplicate nav key: {key}")
        self._pages[key] = page
        self._texts[key] = text
        group = group or modules.group_of(key)
        self._group_of[key] = group if group in modules.GROUP_NAMES else "other"
        index = max(0, min(int(index), len(self._order)))
        self._order.insert(index, key)
        self._rebuild_tree()

    def tabs(self) -> list:
        """按显示顺序返回页面控件（与 Notebook.tabs() 同义，便于迁移）。"""
        return [self._pages[k] for k in self._order]

    def index(self, page) -> int:
        """页面控件的显示序号；不在导航里则抛 ValueError（与 Notebook 一致）。"""
        for i, key in enumerate(self._order):
            if self._pages[key] is page:
                return i
        raise ValueError("page is not in this SideNav")

    # ------------------------------------------------------------------ #
    # 选中与切页
    # ------------------------------------------------------------------ #
    def select(self, page_or_index) -> None:
        """选中页面（控件或序号）；越界/未知项静默忽略。"""
        if isinstance(page_or_index, int):
            if 0 <= page_or_index < len(self._order):
                self._select_key(self._order[page_or_index])
            return
        for key in self._order:
            if self._pages[key] is page_or_index:
                self._select_key(key)
                return

    def select_key(self, key: str) -> bool:
        """按 key 选中页面；key 不存在返回 False（不切页）。"""
        if key not in self._pages:
            return False
        self._select_key(str(key))
        return True

    def restore(self, key: str) -> None:
        """恢复上次页面：key 已失效（模块被停用）时回落到第一个页面。"""
        if key in self._pages:
            self._select_key(str(key))
        elif self._order:
            self._select_key(self._order[0])

    def current_key(self) -> str:
        return self._current

    def current_page(self):
        return self._pages.get(self._current)

    def _select_key(self, key: str, *, fire: bool = True) -> None:
        if key not in self._pages:
            return
        changed = key != self._current
        self._current = key
        self._show(key)
        self._suppress = True
        try:
            self.tree.selection_set(key)
            self.tree.focus(key)
            parent = self.tree.parent(key)
            if parent:  # 分组被用户折叠过 → 先展开，否则 see() 看不到当前项
                self.tree.item(parent, open=True)
            self.tree.see(key)
        except tk.TclError:  # pragma: no cover - 销毁竞态
            pass
        finally:
            self._suppress = False
        if fire and changed:
            self.event_generate("<<NavChanged>>")

    def _show(self, key: str) -> None:
        """只 pack 当前页面：其余 pack_forget → winfo_ismapped() 语义与页签一致。"""
        page = self._pages.get(key)
        if page is None:
            return
        if self._shown is not None and self._shown is not page:
            try:
                self._shown.pack_forget()
            except tk.TclError:  # pragma: no cover
                pass
        try:
            page.pack(fill="both", expand=True)
        except tk.TclError:  # pragma: no cover - 页面已被销毁
            return
        self._shown = page

    def _on_select(self, _event=None) -> None:
        if self._suppress:
            return
        sel = self.tree.selection()
        if not sel:
            return
        iid = str(sel[0])
        if iid.startswith("g:"):
            # 分组行不是页面：还原到当前页，避免「点了分组看起来没反应」
            if self._current:
                self._select_key(self._current, fire=False)
            return
        if iid == self._current:
            # 已是当前页：必须早退。否则「selection_set → <<TreeviewSelect>> →
            # 再 selection_set」会自激成死循环（Tk 的事件是异步投递的，
            # _select_key 里的 _suppress 早被复位，拦不住这次回调）。
            return
        self._select_key(iid)

    # ------------------------------------------------------------------ #
    # 状态圆点
    # ------------------------------------------------------------------ #
    def set_state(self, key: str, state: str) -> None:
        """设置导航项状态点：``"on"`` / ``"off"`` / ``"warn"`` / ``""``（无点）。

        状态未变化时不碰 UI：``_tick`` 每 8 秒调用一次，无变化重绘会让导航闪。
        """
        state = state if state in _STATE_ROLES or state == "" else ""
        if self._states.get(key) == state:
            return
        self._states[key] = state
        if key in self._pages:
            self._apply_state(key)

    def _apply_state(self, key: str) -> None:
        state = self._states.get(key, "")
        text = self._texts.get(key, "")
        try:
            if not self._dot_ok:
                prefix = {"on": "● ", "off": "○ ", "warn": "▲ "}.get(state, "")
                self.tree.item(key, text=" " + prefix + t(text))
                return
            image = self._dots.get(state) if state else ""
            self.tree.item(key, image=image or "", text=" " + t(text))
        except tk.TclError:  # pragma: no cover - 页面已移除
            pass

    def _build_dots(self) -> None:
        """按当前主题生成三态圆点图片（失败则退化为文本前缀）。"""
        self._dots = {}
        try:
            for state, role in _STATE_ROLES.items():
                self._dots[state] = self._make_dot(getattr(theme, role))
            self._dot_ok = True
        except tk.TclError:  # pragma: no cover - 极简 Tk 构建缺 PhotoImage
            self._dot_ok = False

    def _make_dot(self, color: str) -> tk.PhotoImage:
        """画一个抗锯齿无关的实心圆：先填底色再逐像素画圆（8px，成本可忽略）。"""
        img = tk.PhotoImage(master=self, width=DOT_SIZE, height=DOT_SIZE)
        img.put(theme.PANEL_ALT, to=(0, 0, DOT_SIZE, DOT_SIZE))
        center = (DOT_SIZE - 1) / 2
        radius = DOT_SIZE / 2 - 0.5
        for y in range(DOT_SIZE):
            for x in range(DOT_SIZE):
                if (x - center) ** 2 + (y - center) ** 2 <= radius ** 2:
                    img.put(color, to=(x, y, x + 1, y + 1))
        return img

    # ------------------------------------------------------------------ #
    # 内部树重建 / 滚动条 / 主题
    # ------------------------------------------------------------------ #
    def _rebuild_tree(self) -> None:
        """按 order 重建整棵树（14 项量级，重建比增量插入更简单可靠）。"""
        current = self._current
        self._suppress = True
        try:
            for iid in self.tree.get_children(""):
                self.tree.delete(iid)
            self._group_iids.clear()
            for group in modules.GROUP_KEYS:
                keys = [k for k in self._order if self._group_of[k] == group]
                if not keys:
                    continue
                gid = f"g:{group}"
                self._group_iids[group] = gid
                self.tree.insert("", "end", iid=gid, open=True,
                                 text=" " + group_title(group),
                                 tags=("group",))
                for key in keys:
                    self.tree.insert(gid, "end", iid=key,
                                     text=" " + t(self._texts[key]))
                    self._apply_state(key)
            self._style_tags()
            if current in self._pages:
                self.tree.selection_set(current)
                self.tree.focus(current)
                self.tree.see(current)
        finally:
            self._suppress = False
        self._update_scrollbar()

    def _style_tags(self) -> None:
        """分组行的弱化样式（tag 配色不受 apply_mode 颜色映射覆盖，故自行刷新）。"""
        try:
            self.tree.tag_configure("group", foreground=theme.TEXT_DIM,
                                    font=theme.font(theme.FS_SMALL, True))
        except tk.TclError:  # pragma: no cover
            pass

    def _on_yscroll(self, first, last) -> None:
        self._scrollbar.set(first, last)
        if float(first) > 0.001 or float(last) < 0.999:
            if not self._scrollbar.winfo_ismapped():
                self._scrollbar.pack(side="right", fill="y")
        elif self._scrollbar.winfo_ismapped():
            self._scrollbar.pack_forget()

    def _update_scrollbar(self) -> None:
        try:
            first, last = self.tree.yview()
        except tk.TclError:  # pragma: no cover
            return
        self._on_yscroll(first, last)

    def refresh_theme(self) -> None:
        """主题切换钩子：重建圆点图片 + 分组 tag 配色（由 apply_mode 递归调用）。"""
        self._build_dots()
        self._style_tags()
        for key in self._order:
            self._apply_state(key)
