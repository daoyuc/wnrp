# -*- coding: utf-8 -*-
"""自动升级链路自测：模拟 GitHub Releases 走通「检查 → 下载 → 校验」链路。"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import updater  # noqa: E402
from core.version import is_newer  # noqa: E402

DATA = b"phpvm-fake-installer-payload"
GOOD = hashlib.sha256(DATA).hexdigest()
NAME = "phpvm-1.2.0-macos.dmg"

RELEASE_JSON = json.dumps({
    "tag_name": "v1.2.0",
    "body": "- 新增自动升级",
    "html_url": "https://github.com/daoyuc/wnrp/releases/tag/v1.2.0",
    "published_at": "2026-09-10T12:00:00Z",
    "assets": [
        {"name": NAME, "browser_download_url": "https://example.invalid/pkg", "size": len(DATA)},
        {"name": "SHA256SUMS.txt", "browser_download_url": "https://example.invalid/sums", "size": 64},
    ],
})


class FakeResp:
    def __init__(self, data):
        self._buf = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


sums_digest = [GOOD]
updater._http_text = lambda url, timeout=updater.TIMEOUT: (
    RELEASE_JSON if url.endswith("/latest")
    else f"{sums_digest[0]}  {NAME}\n")
updater._open = lambda url, timeout=updater.TIMEOUT: FakeResp(DATA)

tmp = tempfile.mkdtemp(prefix="phpvm_verify_")
progress = []
try:
    rel = updater.check_update()
    assert rel.version == "1.2.0", rel.version
    assert rel.notes == "- 新增自动升级"
    asset = rel.assets["macos"]
    assert asset.name == NAME and asset.sha256 == GOOD, asset
    assert is_newer(rel.version, "1.0.0")
    print("[1] 检查更新：解析正常 →", rel.version, asset.name, "sha256 已获取")

    path = updater.download(rel, dest_dir=tmp, on_progress=lambda d, t: progress.append((d, t)))
    assert open(path, "rb").read() == DATA
    assert os.path.basename(path) == NAME
    assert progress and progress[-1][0] == len(DATA)
    print("[2] 下载 + SHA-256 校验：通过，进度回调", progress[-1], "→", path)

    # 校验和不匹配时必须拒绝并清理临时文件
    sums_digest[0] = "0" * 64
    rel2 = updater.check_update()
    try:
        updater.download(rel2, dest_dir=tmp)
        raise AssertionError("校验和不匹配时未拒绝下载")
    except updater.UpdateError as e:
        leftovers = [n for n in os.listdir(tmp) if n.endswith(".part")]
        assert not leftovers, leftovers
        print("[3] 校验和不匹配：已拒绝并清理临时文件 →", e)

    # 取消下载（首个分块前即中止）
    sums_digest[0] = GOOD
    rel3 = updater.check_update()
    try:
        updater.download(rel3, dest_dir=tmp, cancel=lambda: True)
        raise AssertionError("取消后仍在继续下载")
    except updater.UpdateError as e:
        leftovers = [n for n in os.listdir(tmp) if n.endswith(".part")]
        assert not leftovers, leftovers
        print("[4] 取消下载：正常中断并清理 →", e)

    print("平台:", updater.platform_key(), "| 自动替换支持:", updater.supports_auto_update())
    print("ALL OK")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
