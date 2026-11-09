# -*- coding: utf-8 -*-
"""core/php_extension.py 在线安装的下载链路回归测试。

守「扩展下载没有任何进度条/速度反馈，也不复用 php_downloader 的续传重试」：
- _download 必须委托 php_downloader.download_with_progress（并行分块 / 续传 /
  有界重试 / 进度回调 / stop_event 全部透传）；
- install_online 对已存在的扩展直接短路返回，不发起任何下载。
"""
import os
import tempfile
import threading
import unittest
from unittest import mock

from core import php_extension as ext_mod


class DownloadDelegateTest(unittest.TestCase):
    def test_download_delegates_to_php_downloader(self):
        """_download 透传进度回调与取消事件给 download_with_progress。"""
        calls = {}

        def fake_dl(url, dest, progress_cb=None, stop_event=None, retries=4):
            calls["url"] = url
            calls["dest"] = dest
            calls["progress_cb"] = progress_cb
            calls["stop_event"] = stop_event
            calls["retries"] = retries

        stop = threading.Event()
        cb = lambda done, total: None
        with mock.patch.object(ext_mod, "download_with_progress", fake_dl):
            ext_mod._download("http://127.0.0.1/x.zip", "C:/tmp/x.zip",
                              cb, stop)
        self.assertEqual(calls["url"], "http://127.0.0.1/x.zip")
        self.assertEqual(calls["dest"], "C:/tmp/x.zip")
        self.assertIs(calls["progress_cb"], cb)
        self.assertIs(calls["stop_event"], stop)

    def test_install_online_short_circuits_when_dll_exists(self):
        """ext/ 已有同名扩展时直接返回，不发起下载。"""
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = os.path.join(tmp, "ext")
            os.makedirs(ext_dir)
            dll = os.path.join(ext_dir, "php_redis.dll")
            with open(dll, "w", encoding="utf-8") as f:
                f.write("dummy")
            with mock.patch.object(ext_mod, "download_with_progress") as dl:
                ok, msg, name = ext_mod.install_online(
                    {"key": "redis", "name": "Redis", "source": "pecl"},
                    tmp, ext_mod.RuntimeInfo("8.5", "nts", "x64", "vs17"))
            self.assertTrue(ok)
            self.assertEqual(name, "php_redis.dll")
            self.assertIn("已存在", msg)
            dl.assert_not_called()  # 短路路径绝不触发下载

    def test_install_online_passes_progress_and_stop(self):
        """install_online 把 dl_progress / stop_event 透传到下载调用。"""
        seen = {}

        def fake_dl(url, dest, progress_cb=None, stop_event=None, retries=4):
            seen["progress_cb"] = progress_cb
            seen["stop_event"] = stop_event
            # 模拟下载了一个 zip：内含目标 dll，走解压落盘路径
            import io
            import zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("php_redis-6.0.2-8.5-nts-vs17-x64.dll", "dll")
            with open(dest, "wb") as f:
                f.write(buf.getvalue())
            if progress_cb:
                progress_cb(1, 1)

        item = {"key": "redis", "name": "Redis", "source": "pecl"}
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(ext_mod, "_pick_pecl",
                                   return_value=("http://x/php_redis.zip",
                                                 "php_redis-6.0.2.zip")), \
                    mock.patch.object(ext_mod, "download_with_progress", fake_dl):
                ok, msg, name = ext_mod.install_online(
                    item, tmp, ext_mod.RuntimeInfo("8.5", "nts", "x64", "vs17"),
                    progress=lambda m: None, dl_progress=lambda d, t: None,
                    stop_event=stop)
            self.assertTrue(ok, msg)
            self.assertEqual(name, "php_redis-6.0.2-8.5-nts-vs17-x64.dll")
            self.assertTrue(os.path.isfile(os.path.join(tmp, "ext", name)))
        self.assertIs(seen["stop_event"], stop)
        self.assertIsNotNone(seen["progress_cb"])


if __name__ == "__main__":
    unittest.main()
