# -*- coding: utf-8 -*-
"""core/php_downloader.py 下载器回归测试。

守护「下载 PHP 版本卡住」的三类根因（都用真实本地 HTTP 服务复现，不 mock urlopen）：
1. 服务端不给 Content-Length 时，进度回调仍必须按已下载字节持续上报，
   否则界面只能显示一个不动的满格进度条，看起来就是卡死；
2. 连接中途断开（声明了长度但只发一半）→ 带 Range 断点续传重试，
   最终文件必须与源完全一致；
3. 用户取消（stop_event 置位）→ 立刻抛 DownloadCancelled，
   且不能重试、不能把半成品当成下载成功。
"""
import hashlib
import http.server
import os
import tempfile
import threading
import unittest

from core import php_downloader


def _make_server(payload: bytes, *, send_length: bool = True,
                 truncate_first: bool = False):
    """起一个一次性 HTTP 服务：支持 Range、可选择性省略 Content-Length / 首包截断。

    返回 (server, state)：state 记录请求次数与每次的 Range 头。
    """
    state = {"hits": 0, "ranges": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *args):  # 静音测试输出
            pass

        def do_GET(self):
            state["hits"] += 1
            rng = self.headers.get("Range")
            state["ranges"].append(rng)
            start = 0
            if rng and rng.startswith("bytes="):
                start = int(rng[6:].split("-")[0])
            body = payload[start:]
            self.send_response(206 if start else 200)
            if start:
                self.send_header(
                    "Content-Range",
                    f"bytes {start}-{len(payload) - 1}/{len(payload)}")
            if send_length:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if truncate_first and state["hits"] == 1:
                # 声明完整长度却只发一半就断开，模拟网络中断
                self.wfile.write(body[: max(1, len(body) // 2)])
                self.wfile.flush()
                self.close_connection = True
                return
            self.wfile.write(body)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    return server, state


class DownloadRobustnessTest(unittest.TestCase):
    def setUp(self):
        self._servers = []
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def tearDown(self):
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()

    def _serve(self, payload: bytes, **kwargs):
        srv, state = _make_server(payload, **kwargs)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._servers.append(srv)
        url = f"http://127.0.0.1:{srv.server_address[1]}/php.zip"
        return url, state

    def _dest(self, name: str = "php.zip") -> str:
        return os.path.join(self._dir.name, name)

    def test_progress_reports_bytes_without_content_length(self):
        """无 Content-Length 时也要有字节进度，而不是「不动」。"""
        payload = os.urandom(256 * 1024)
        url, _ = self._serve(payload, send_length=False)
        seen = []
        php_downloader.download_with_progress(
            url, self._dest(), progress_cb=lambda done, total: seen.append((done, total)))
        self.assertGreaterEqual(len(seen), 2)
        # 最后一次回调必须收口到「已完成」（total 回落为已写入字节数）
        self.assertEqual(seen[-1][0], len(payload))
        self.assertEqual(seen[-1][1], len(payload))
        with open(self._dest(), "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_resume_after_truncated_connection(self):
        """首包被截断后带 Range 续传，最终内容完整且不多发重下。"""
        payload = os.urandom(320 * 1024)
        url, state = self._serve(payload, truncate_first=True)
        php_downloader.download_with_progress(url, self._dest())
        with open(self._dest(), "rb") as f:
            got = f.read()
        self.assertEqual(hashlib.sha256(got).hexdigest(),
                         hashlib.sha256(payload).hexdigest())
        self.assertEqual(state["hits"], 2)
        self.assertIsNone(state["ranges"][0])
        self.assertTrue(state["ranges"][1].startswith("bytes="))

    def test_cancel_raises_immediately(self):
        """stop_event 已置位时立刻取消，不触碰磁盘、不重试。"""
        payload = b"x" * 1024
        url, state = self._serve(payload)
        stop = threading.Event()
        stop.set()
        with self.assertRaises(php_downloader.DownloadCancelled):
            php_downloader.download_with_progress(
                url, self._dest(), stop_event=stop)
        self.assertEqual(state["hits"], 0)
        self.assertFalse(os.path.exists(self._dest()))

    def test_gives_up_after_retries(self):
        """服务器始终失败时按重试次数收口报错，绝不无限等待。"""
        payload = os.urandom(64 * 1024)
        url, state = self._serve(payload, truncate_first=True)
        with self.assertRaises(RuntimeError):
            php_downloader.download_with_progress(url, self._dest(), retries=0)
        self.assertEqual(state["hits"], 1)


if __name__ == "__main__":
    unittest.main()
