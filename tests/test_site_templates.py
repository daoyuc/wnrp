# -*- coding: utf-8 -*-
"""站点配置渲染：文档根必须落成 nginx 正斜杠路径。

Windows 调用方可能直接传 os.path.join 的结果（`C:\\wnrp\\www\\app`）；
若原样写进配置，nginx 会把 `\\w` / `\\a` 当转义解析，站点直接打不开
（F9 Adminer 曾因此生成坏配置）。
"""
import unittest

from core.site_templates import TEMPLATE_MAP, render_config

WIN_PATH = r"C:\wnrp\www\app\public"


class RenderConfigPathTest(unittest.TestCase):
    def test_windows_docroot_normalized_for_every_template(self):
        for key in TEMPLATE_MAP:
            with self.subTest(template=key):
                text = render_config(key, server_name="app.test",
                                     docroot=WIN_PATH, port=9085)
                # 只查 root 指令行：nginx 配置里的 location 正则（\.php$）本就含反斜杠
                roots = [ln for ln in text.splitlines() if ln.strip().startswith("root ")]
                self.assertTrue(roots, "模板 %s 应生成 root 指令" % key)
                for line in roots:
                    self.assertNotIn("\\", line, "root 指令不应带反斜杠：%s" % line)
                self.assertIn('root "C:/wnrp/www/app/public"', text)

    def test_trailing_slash_stripped(self):
        text = render_config("php", server_name="app.test",
                             docroot="D:/www9/app/public/", port=9085)
        self.assertIn('root "D:/www9/app/public"', text)

    def test_placeholders_replaced(self):
        text = render_config("php", server_name="app.test other.test",
                             docroot="D:/www9/app/public", port=9085)
        self.assertNotIn("{{", text, "不应残留未替换的占位符")
        # 模板里 server_name 后是多个空格对齐，故用正则
        self.assertRegex(text, r"server_name\s+app\.test other\.test;")
        self.assertIn("fastcgi_pass 127.0.0.1:9085;", text)


if __name__ == "__main__":
    unittest.main()
