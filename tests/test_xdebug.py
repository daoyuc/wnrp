# -*- coding: utf-8 -*-
"""Xdebug 调试开关：状态判定、写入指令、关闭、自检失败回滚。

用假的 `pu.run_cmd` 模拟 `php -c <ini> -r` 与 `php -m`：按 ini 中是否有
**未注释**的 xdebug 加载行来决定「是否已加载」，从而覆盖成功与回滚两条路径。
"""
import os
import re
import tempfile
import unittest

from core import xdebug
from core.php_manager import CLI_NAME, PhpVersion

_LOADER = re.compile(r"^\s*zend_extension\s*=\s*.*xdebug", re.IGNORECASE | re.MULTILINE)


def _fake_run_cmd(args, timeout=None):
    """模拟 php：能加载 ini 就回 OK，并按 ini 内容回显已加载扩展。"""
    ini = ""
    if "-c" in args:
        idx = args.index("-c")
        if idx + 1 < len(args):
            ini = args[idx + 1]
    text = ""
    if ini and os.path.exists(ini):
        with open(ini, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    out = "OK\n"
    if _LOADER.search(text):
        out += "xdebug\n"
    return 0, out, ""


class XdebugTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        os.makedirs(os.path.join(self.dir, "ext"), exist_ok=True)
        # 假的 php 可执行文件：verify() 只要求它存在，输出由 run_cmd 替身给出
        with open(os.path.join(self.dir, CLI_NAME), "wb") as f:
            f.write(b"")
        self.ini = os.path.join(self.dir, "php.ini")
        with open(self.ini, "w", encoding="utf-8") as f:
            f.write("memory_limit = 128M\n;extension=openssl\n")
        self.v = PhpVersion(name="php82", display="8.2.0", dir=self.dir,
                            cgi=os.path.join(self.dir, "php-cgi"), ini=self.ini,
                            port=9082)
        self._real_run_cmd = xdebug.pu.run_cmd
        xdebug.pu.run_cmd = _fake_run_cmd

    def tearDown(self):
        xdebug.pu.run_cmd = self._real_run_cmd
        self.tmp.cleanup()

    def _add_ext(self) -> None:
        with open(os.path.join(self.dir, "ext", "php_xdebug.dll"), "wb") as f:
            f.write(b"")

    def _read(self) -> str:
        with open(self.ini, "r", encoding="utf-8") as f:
            return f.read()

    # ---------------------------------------------------------------- #
    def test_status_not_installed(self):
        info = xdebug.status(self.v)
        self.assertFalse(info["installed"])
        self.assertFalse(info["enabled"])
        self.assertTrue(info["ini_exists"])
        self.assertEqual(info["port"], xdebug.DEFAULT_PORT)

    def test_enable_requires_extension(self):
        ok, msg, _ = xdebug.enable(self.v)
        self.assertFalse(ok)
        self.assertIn("Xdebug", msg)
        self.assertNotIn("zend_extension", self._read(), "未装扩展时不应改 ini")

    def test_enable_requires_ini(self):
        self._add_ext()
        self.v.ini = os.path.join(self.dir, "missing.ini")
        ok, _msg, _ = xdebug.enable(self.v)
        self.assertFalse(ok)

    def test_enable_writes_loader_and_directives(self):
        self._add_ext()
        ok, msg, backup = xdebug.enable(self.v, port=9000)
        self.assertTrue(ok, msg)
        self.assertTrue(os.path.exists(backup))
        body = self._read()
        self.assertTrue(_LOADER.search(body), "应写入未注释的加载行")
        self.assertIn("php_xdebug.dll", body)
        self.assertIn("xdebug.mode = debug", body)
        self.assertIn("xdebug.client_port = 9000", body)
        self.assertIn("xdebug.start_with_request = yes", body)
        self.assertIn("memory_limit = 128M", body, "原有配置必须保留")

    def test_enable_is_idempotent(self):
        """重复开启不应产生第二条加载行。"""
        self._add_ext()
        xdebug.enable(self.v)
        xdebug.enable(self.v)
        self.assertEqual(len(_LOADER.findall(self._read())), 1)

    def test_disable_comments_loader_and_keeps_directives(self):
        self._add_ext()
        xdebug.enable(self.v)
        ok, msg, _ = xdebug.disable(self.v)
        self.assertTrue(ok, msg)
        body = self._read()
        self.assertFalse(_LOADER.search(body), "加载行应被注释")
        self.assertIn(";zend_extension=", body)
        self.assertIn("xdebug.mode = debug", body, "调试指令保留，便于再次开启")

    def test_disable_without_loader_is_noop(self):
        ok, _msg, _ = xdebug.disable(self.v)
        self.assertFalse(ok)
        self.assertEqual(self._read().count("zend_extension"), 0)

    def test_status_after_enable(self):
        self._add_ext()
        xdebug.enable(self.v, port=9001)
        info = xdebug.status(self.v)
        self.assertTrue(info["installed"])
        self.assertTrue(info["enabled"])
        self.assertTrue(info["debugging"])
        self.assertEqual(info["port"], 9001)
        self.assertEqual(info["mode"], "debug")

    def test_rolls_back_when_verify_fails(self):
        """自检不通过（PHP 未加载 xdebug）时，ini 必须还原成原样。"""
        self._add_ext()
        original = self._read()
        xdebug.pu.run_cmd = lambda args, timeout=None: (0, "OK\n", "")
        ok, msg, _backup = xdebug.enable(self.v)
        self.assertFalse(ok)
        # 应走「自检失败」分支（详情含 Xdebug 专名），而非「写入失败」分支
        self.assertIn("Xdebug", msg)
        self.assertEqual(self._read(), original, "失败后应完整还原 ini")

    def test_rolls_back_when_php_fails(self):
        self._add_ext()
        original = self._read()
        xdebug.pu.run_cmd = lambda args, timeout=None: (1, "", "PHP Startup: boom")
        ok, _msg, _ = xdebug.enable(self.v)
        self.assertFalse(ok)
        self.assertEqual(self._read(), original)


if __name__ == "__main__":
    unittest.main()
