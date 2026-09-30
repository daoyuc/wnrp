# -*- coding: utf-8 -*-
"""界面布局辅助：可纵向滚动的内容区 + 随宽度自动换行的长文本。

为什么需要它
------------
「关于」这类内容高度不可控的页面（环境信息 + 模块开关 + 一堆设置项）如果直接 pack
进页签，超出一屏的部分只能靠用户手动拉大窗口才看得到；而 Tk 的 Label 默认**不换行**，
长文本会横向撑破布局、被页签边界裁掉（即「看不全 + 有遮挡」）。

这里提供两个原语：

- :func:`scrolled_frame`：Canvas + 只在需要时出现的纵向滚动条 + 鼠标滚轮，
  内容高度不再决定窗口尺寸，宽度始终跟随容器（不横向滚动）；
- :func:`auto_wrap`：按**基准容器**宽度算 ``wraplength``，让长文本在窗口变窄时
  自动折行，而不是把父容器越撑越宽。

用法::

    sc = scrolled_frame(page, padding=theme.PAD_XL, style="Card.TFrame")
    ttk.Label(sc.content, text=...).pack(anchor="w")
    sc.bind_wheel()          # 内容全部建完后调用一次
"""
import sys
import tkinter as tk
from tkinter import ttk

from . import theme


class ScrollCanvas(tk.Canvas):
    """滚动载体：底色跟随 Card 角色，并在换主题时由 ``refresh_theme()`` 校正。

    为什么需要这个钩子：``ui.theme.apply_mode`` 是按「旧色值 → 新色值」批量改写控件颜色的，
    同一个色值在色板里可能对应多个角色，Canvas 很容易被刷成另一个角色的颜色
    （实测浅色 #FFFFFF → 深色 #303439，而 Card 应该是 #24272C），
    卡片区域就会露出一块深浅不一的底。钩子在映射之后直接写回正确的角色值。
    """

    def __init__(self, master, background=None, **kw):
        super().__init__(master, **kw)
        self._bg = background  # 调用方显式指定底色时不参与主题刷新
        self.configure(background=background or theme.CARD_BG)

    def refresh_theme(self) -> None:
        if self._bg is None:
            self.configure(background=theme.CARD_BG)


class ScrolledFrame:
    """可纵向滚动的内容区（内容超高时出现滚动条，否则自动隐藏）。

    属性：
    - ``outer``：放进页签 / 对话框的容器（直接 pack(fill="both", expand=True)）；
    - ``canvas``：滚动载体，同时可作为 :func:`auto_wrap` 的基准容器；
    - ``content``：真正的内容帧，宽度由 canvas 决定（横向不滚动）。
    """

    def __init__(self, master, *, padding=0, style=None, background=None,
                 pack=True):
        self.outer = ttk.Frame(master)
        if pack:  # 绝大多数场景都是「占满宿主」，默认直接铺开（要自定义就传 pack=False）
            self.outer.pack(fill="both", expand=True)
        self.canvas = ScrollCanvas(self.outer, background=background,
                                   highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self.outer, orient="vertical",
                                       command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_view_change)
        self.content = ttk.Frame(self.canvas, padding=padding, style=style)
        self._inner_id = self.canvas.create_window((0, 0), window=self.content,
                                                   anchor="nw")
        self._job = None      # after_idle 合并后的同步任务 id
        self._region = None   # 上一次写入的 scrollregion（相同则不写，见 _sync）
        self._syncing = False
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.content.bind("<Configure>", lambda e: self._schedule_sync())

    # ------------------------------------------------------------------ #
    def _on_canvas_resize(self, event) -> None:
        # 内容帧宽度跟随 canvas：窗口变窄时内容一起收窄（配合 auto_wrap 折行）
        self.canvas.itemconfigure(self._inner_id, width=event.width)
        self._schedule_sync()

    def _on_view_change(self, first, last) -> None:
        self.scrollbar.set(first, last)
        self._schedule_sync()

    def _schedule_sync(self) -> None:
        """合并连续的配置变化（统一放到 after_idle 里算），避免 pack/unpack 抖动。"""
        if self._job is not None:
            return
        try:
            self._job = self.canvas.after_idle(self._sync)
        except tk.TclError:  # 窗口已销毁
            self._job = None

    def _sync(self) -> None:
        self._job = None
        if self._syncing:  # 自己引发的 Configure / yscrollcommand 不再排队
            return
        self._syncing = True
        try:
            # scrollregion 只在真正变化时写：写它本身会触发 yscrollcommand，
            # 而无条件重排会让 update_idletasks 陷入「空闲任务永远做不完」的死循环
            region = self.canvas.bbox("all") or (0, 0, 1, 1)
            if region != self._region:
                self.canvas.configure(scrollregion=region)
                self._region = region
            first, last = self.canvas.yview()
            needed = float(first) > 0.001 or float(last) < 0.999
            shown = bool(self.scrollbar.winfo_ismapped())
            if needed and not shown:
                self.scrollbar.pack(side="right", fill="y")
            elif not needed and shown:
                self.scrollbar.pack_forget()
        except tk.TclError:  # pragma: no cover - 销毁竞态
            pass
        finally:
            self._syncing = False

    # ------------------------------------------------------------------ #
    def bind_wheel(self) -> None:
        """内容全部建完后调用一次：canvas 及其所有子孙都可用滚轮滚动。

        为什么不能只绑 canvas：Tk 把事件投递给**指针所在的那一个控件**，
        而 canvas 内嵌窗口（create_window）的子控件并不在 canvas 的 bindtags 里，
        只绑 canvas 会出现「鼠标停在文本/勾选框上时滚轮失效」。
        """
        bind_wheel_tree(self.canvas, self.content)


def scrolled_frame(master, *, padding=0, style=None, background=None,
                   pack=True) -> ScrolledFrame:
    """在 ``master`` 内建一个可纵向滚动的内容区，返回 :class:`ScrolledFrame`。"""
    return ScrolledFrame(master, padding=padding, style=style,
                         background=background, pack=pack)


def bind_wheel_tree(canvas: tk.Canvas, root: tk.Misc) -> None:
    """给 ``canvas`` 与 ``root`` 的整棵子树绑滚轮（Windows/macOS 与 X11 都覆盖）。"""

    def scroll(units: int) -> None:
        try:
            canvas.yview_scroll(units, "units")
        except tk.TclError:  # pragma: no cover - 销毁竞态
            pass

    def on_wheel(event) -> str:
        delta = getattr(event, "delta", 0) or 0
        if sys.platform == "darwin":
            units = -int(delta)
        else:
            units = int(-delta / 120)
            if units == 0:
                units = -1 if delta > 0 else 1
        scroll(units)
        return "break"

    def on_button(event) -> str:
        scroll(-1 if getattr(event, "num", 0) == 4 else 1)
        return "break"

    targets: list = [canvas]
    stack = [root]
    while stack:  # 迭代而非递归：面板控件多时不担心栈深
        widget = stack.pop()
        targets.append(widget)
        stack.extend(widget.winfo_children())
    for widget in targets:
        widget.bind("<MouseWheel>", on_wheel, add="+")
        widget.bind("<Button-4>", on_button, add="+")
        widget.bind("<Button-5>", on_button, add="+")


def auto_wrap(widget, base, *, fraction: float = 1.0, pad: int = 0,
              min_width: int = 160):
    """让长文本控件随 ``base`` 宽度自动换行，返回该控件（便于链式 ``.pack()``）。

    ``wraplength = max(min_width, base 宽度 × fraction - pad)``。

    **为什么按 base 而不是父容器测量**：两列布局里父容器宽度由子控件的请求宽度反推，
    拿它算 ``wraplength`` 会形成「越算越宽」的正反馈；``base``（页面 canvas）的宽度
    只由窗口大小决定，用它当基准不会抖动。``fraction`` / ``pad`` 用于把「整页宽」
    换算成控件所在那一列的可用宽度。
    """

    def apply(_event=None) -> None:
        try:
            width = int(base.winfo_width() * fraction) - pad
            widget.configure(wraplength=max(min_width, width))
        except tk.TclError:  # pragma: no cover - 销毁竞态
            return

    apply()
    base.bind("<Configure>", apply, add="+")
    return widget
