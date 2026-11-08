# -*- coding: utf-8 -*-
"""`ui/nav.SideNav` 的单元测试（无显示环境自动跳过）。

要点：
- 顺序 API 与 ``ttk.Notebook`` 子集对齐（``add/insert/tabs/index/select``），
  主窗口与冒烟测试才能平滑迁移；
- 切页只映射当前页（``_panel_visible()`` 依赖这个语义）；
- 分组行不是页面（点到分组不能把内容区清空）；
- ``restore`` 对失效 key 回落首页（模块被停用后不能出现空白内容区）；
- ``set_state`` 幂等（每 8 秒 tick 调用一次，状态不变不能重绘）。
"""
import os
import subprocess
import sys
import unittest

try:
    import tkinter as tk
    from tkinter import ttk

    from core import modules
    from core.i18n import t
    from ui import theme
    from ui.nav import SideNav

    _TK_IMPORT_ERROR = ""
except Exception as e:  # noqa: BLE001 - pragma: no cover
    _TK_IMPORT_ERROR = str(e)
    SideNav = None  # type: ignore[assignment]


def _has_display() -> bool:
    """是否有可用 Tk 显示环境（只做环境判断，不创建窗口）。"""
    if SideNav is None:
        return False
    if sys.platform == "win32":
        return True
    if sys.platform == "darwin":
        try:
            return subprocess.run(["pgrep", "-qx", "WindowServer"],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL).returncode == 0
        except OSError:  # pragma: no cover
            return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


@unittest.skipUnless(_has_display(), "无可用 Tk 显示环境（或未安装 tkinter）")
class SideNavTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 整个测试类共用一个 Tk 解释器：macOS + Tk 9 上同一进程反复 Tk()
        # / destroy 会 SIGSEGV（见 test_main_window_smoke._has_display 的说明）
        cls.root = tk.Tk()
        # 不能 withdraw()：退到后台的 root 里子控件永远不会 map，
        # winfo_ismapped() 恒为 0，就测不出「只有当前页被映射」这个关键语义
        cls.root.geometry("900x600+40+40")
        theme.setup_style(cls.root)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def setUp(self):
        self.root = type(self).root
        self.nav = SideNav(self.root)
        self.nav.pack(fill="both", expand=True)
        self.addCleanup(self.nav.destroy)
        self.pages = {}
        for key, group in (("php", "service"), ("redis", "service"),
                           ("vhost", "site"), ("log", "diag")):
            page = ttk.Frame(self.nav.content)
            self.pages[key] = page
            self.nav.add(page, text=key, key=key, group=group)

    def test_tabs_and_index_follow_registration_order(self):
        self.assertEqual([p for p in self.nav.tabs()],
                         [self.pages[k] for k in ("php", "redis", "vhost", "log")])
        self.assertEqual(self.nav.index(self.pages["vhost"]), 2)
        with self.assertRaises(ValueError):
            self.nav.index(ttk.Frame(self.root))

    def test_insert_places_page_at_position(self):
        page = ttk.Frame(self.nav.content)
        self.nav.insert(1, page, text="overview", key="overview", group="service")
        self.assertEqual(self.nav.index(page), 1)
        self.assertEqual([k for k in ("php",) if self.nav.index(self.pages["php"]) == 0], ["php"])

    def test_duplicate_key_is_rejected(self):
        with self.assertRaises(ValueError):
            self.nav.add(ttk.Frame(self.nav.content), text="dup", key="php")

    def test_select_shows_only_one_page_and_fires_once(self):
        events = []
        self.nav.bind("<<NavChanged>>", lambda e: events.append(self.nav.current_key()))
        self.nav.select(self.pages["vhost"])
        for _ in range(3):
            self.root.update()
        self.assertEqual(self.nav.current_key(), "vhost")
        mapped = [k for k, p in self.pages.items() if p.winfo_ismapped()]
        self.assertEqual(mapped, ["vhost"])
        self.assertEqual(events, ["vhost"], "同一次切页只能触发一次 <<NavChanged>>")

        # 重复选中当前页：不应再发事件（避免重复刷新面板）
        self.nav.select(self.pages["vhost"])
        for _ in range(3):
            self.root.update()
        self.assertEqual(events, ["vhost"])

    def test_select_by_index_and_key(self):
        self.nav.select(2)
        self.root.update()
        self.assertEqual(self.nav.current_key(), "vhost")
        self.assertTrue(self.nav.select_key("log"))
        self.root.update()
        self.assertEqual(self.nav.current_key(), "log")
        self.assertFalse(self.nav.select_key("nope"), "未知 key 不应切页")
        self.assertEqual(self.nav.current_key(), "log")

    def test_group_rows_are_not_pages(self):
        """点分组行不能把内容区清空，应还原到当前页。"""
        self.nav.select_key("redis")
        self.root.update()
        group_iid = self.nav._group_iids["service"]
        self.assertIn(group_iid, self.nav.tree.get_children(""))
        self.nav.tree.selection_set(group_iid)
        for _ in range(3):
            self.root.update()
        self.assertEqual(self.nav.current_key(), "redis")
        self.assertTrue(self.pages["redis"].winfo_ismapped())

    def test_restore_falls_back_to_first_page(self):
        self.nav.restore("disabled-page")
        self.root.update()
        self.assertEqual(self.nav.current_key(), "php")
        self.nav.restore("log")
        self.root.update()
        self.assertEqual(self.nav.current_key(), "log")

    def test_set_state_is_idempotent(self):
        self.nav.set_state("php", "on")
        item = self.nav.tree.item("php")
        first_image = item["image"]
        self.assertTrue(first_image, "开启状态应挂上圆点图片")
        self.nav.set_state("php", "on")   # 状态未变：不应重设
        self.assertEqual(self.nav.tree.item("php")["image"], first_image)
        self.nav.set_state("php", "")
        self.assertFalse(self.nav.tree.item("php")["image"], "空状态应清掉圆点")

    def test_theme_refresh_keeps_dots(self):
        self.nav.set_state("nginx", "warn") if "nginx" in self.nav._pages else None
        self.nav.set_state("redis", "off")
        self.root.update()
        theme.apply_mode(self.root, "dark")
        self.root.update()
        self.assertTrue(self.nav._dots, "主题切换后圆点图片应重建")
        if self.nav._dot_ok:
            self.assertTrue(self.nav.tree.item("redis")["image"],
                            "深色主题下圆点应仍然存在")

    def test_group_titles_are_translated(self):
        """分组标题走 t()，保证四语 JSON 里能扫到（漏包裹会显示中文原文）。"""
        service_iid = self.nav._group_iids["service"]
        self.assertIn(t(modules.GROUP_NAMES["service"]),
                      str(self.nav.tree.item(service_iid)["text"]))


if __name__ == "__main__":
    unittest.main()
