# -*- coding: utf-8 -*-
"""core/php_installer.py 版本内升级回归测试。

守「php85 已装 8.5.9，下载 8.5.12 提示目标目录已存在无法升级」的缺陷：
- 已存在的 PHP 版本目录 → 走升级：整体替换文件、保留用户自定义 php.ini；
- 正在运行（端口有存活 PID）→ 拒绝升级，旧目录原样不动；
- 已存在但不是 PHP 目录 → 拒绝覆盖；
- 升级中途失败 → 旧版本原样恢复。
全程沙箱：WNRP_ROOT / TMP_ROOT 打桩到临时目录，php -v 与端口探测打桩。
"""
import hashlib
import http.server
import io
import os
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

from core import php_installer as pi
from core.config import Config
from core.php_downloader import PhpPackage

NAME = "php85"


def _zip_bytes() -> bytes:
    """构造一个最小 PHP 包：php.exe / php-cgi.exe / ini 模板 / 新版扩展。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("php.exe", "new")
        zf.writestr("php-cgi.exe", "new")
        zf.writestr("php.ini-development", "extension_dir = \"ext\"\n")
        zf.writestr("new_ext/php_new.dll", "dll")
    return buf.getvalue()


def _make_pkg(payload: bytes, url: str) -> PhpPackage:
    return PhpPackage(version="8.5.12", series="8.5", arch="x64", ts_mode="nts",
                      compiler="vs17", url=url, size="1MB",
                      sha256=hashlib.sha256(payload).hexdigest(), source="releases")


def _serve(payload: bytes):
    """一次性 HTTP 服务（不支持 Range → 下载器走单流路径，同样覆盖到）。"""
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(self._tmp.name, "wnrp")
        self.tmproot = os.path.join(self._tmp.name, "tmp")
        os.makedirs(self.root)
        os.makedirs(self.tmproot)
        self.cfg = Config(os.path.join(self._tmp.name, "config.json"))
        for target, value in (("WNRP_ROOT", self.root), ("TMP_ROOT", self.tmproot)):
            p = mock.patch.object(pi, target, value)
            p.start()
            self.addCleanup(p.stop)
        # php -v 验证返回成功文本；无端口监听；运行日志不打扰真实文件
        p = mock.patch.object(pi.pu, "run_cmd",
                              return_value=(0, "PHP 8.5.12 (cli) (built: x)", ""))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(pi.pu, "port_to_pid_fast", return_value=[])
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(pi.run_log, "log", return_value=None)
        p.start()
        self.addCleanup(p.stop)
        self._payload = _zip_bytes()
        self._srv = _serve(self._payload)
        self.addCleanup(self._srv.server_close)
        self.addCleanup(self._srv.shutdown)
        self._pkg = _make_pkg(
            self._payload,
            f"http://127.0.0.1:{self._srv.server_address[1]}/php.zip")

    def _mk_old_install(self):
        """造一个已安装的 php85：旧扩展 + 用户自定义 php.ini。"""
        target = os.path.join(self.root, NAME)
        os.makedirs(os.path.join(target, "old_ext"))
        with open(os.path.join(target, "php.exe"), "w", encoding="utf-8") as f:
            f.write("old")
        with open(os.path.join(target, "php.ini"), "w", encoding="utf-8") as f:
            f.write("; my custom ini\ndate.timezone = Asia/Shanghai\n")
        with open(os.path.join(target, "old_ext", "old.dll"), "w",
                  encoding="utf-8") as f:
            f.write("old")
        return target

    def test_upgrade_replaces_files_and_keeps_ini(self):
        target = self._mk_old_install()
        r = pi.install(self._pkg, self.cfg)
        self.assertTrue(r.ok, r.message)
        self.assertEqual(r.name, NAME)
        # 新版本文件就位
        self.assertTrue(os.path.isfile(
            os.path.join(target, "new_ext", "php_new.dll")))
        # 用户自定义 php.ini 原样保留（没有被模板覆盖）
        with open(os.path.join(target, "php.ini"), encoding="utf-8") as f:
            self.assertIn("my custom ini", f.read())
        # 旧文件整体替换、临时区无残留备份
        self.assertFalse(os.path.exists(os.path.join(target, "old_ext")))
        self.assertFalse(os.path.exists(os.path.join(self.tmproot, f"{NAME}_old")))
        # 端口按约定落库
        self.assertEqual(self.cfg.get_port(NAME),
                         pi.default_port_for(NAME, self.cfg))

    def test_upgrade_refused_while_running(self):
        target = self._mk_old_install()
        with mock.patch.object(pi.pu, "port_to_pid_fast", return_value=[4321]), \
                mock.patch.object(pi.pu, "is_pid_alive_fast", return_value=True):
            r = pi.install(self._pkg, self.cfg)
        self.assertFalse(r.ok)
        self.assertIn("4321", r.message)
        # 旧目录原样不动
        self.assertTrue(os.path.isfile(os.path.join(target, "old_ext", "old.dll")))
        self.assertFalse(os.path.exists(os.path.join(target, "new_ext")))

    def test_refuse_overwrite_non_php_dir(self):
        target = os.path.join(self.root, NAME)
        os.makedirs(target)
        with open(os.path.join(target, "keep.txt"), "w", encoding="utf-8") as f:
            f.write("keep")
        r = pi.install(self._pkg, self.cfg)
        self.assertFalse(r.ok)
        with open(os.path.join(target, "keep.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "keep")

    def test_upgrade_failure_restores_old_version(self):
        """落盘阶段失败（新版包缺 php.exe/php-cgi.exe）时旧版本必须原样恢复。"""
        target = self._mk_old_install()
        bad_buf = io.BytesIO()
        with zipfile.ZipFile(bad_buf, "w") as zf:
            zf.writestr("readme.txt", "not a php package")
        bad_payload = bad_buf.getvalue()

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(bad_payload)))
                self.end_headers()
                self.wfile.write(bad_payload)

        srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        bad_pkg = _make_pkg(
            bad_payload, f"http://127.0.0.1:{srv.server_address[1]}/php.zip")

        r = pi.install(bad_pkg, self.cfg)
        self.assertFalse(r.ok)  # 解压内容异常：未找到 php.exe / php-cgi.exe
        # 旧版本被原样搬回
        self.assertTrue(os.path.isfile(os.path.join(target, "old_ext", "old.dll")))
        self.assertTrue(os.path.isfile(os.path.join(target, "php.exe")))
        with open(os.path.join(target, "php.ini"), encoding="utf-8") as f:
            self.assertIn("my custom ini", f.read())
        self.assertFalse(os.path.exists(os.path.join(self.tmproot, f"{NAME}_old")))


if __name__ == "__main__":
    unittest.main()
