# -*- coding: utf-8 -*-
"""macOS 启动器契约：vendor wheel 的 ABI 必须与所选解释器小版本一致。

不真跑 build_macos（需要 macOS + dmg 工具），只校验 launcher 文本与
build.py 里「构建时注入小版本」这一段是否仍然存在。
"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_build():
    path = os.path.join(ROOT, "packaging", "build.py")
    spec = importlib.util.spec_from_file_location("wnrp_build", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_build()

    def test_pytag_is_injected_at_build_time(self):
        script = self.mod.LAUNCHER.format(py="3.13")
        self.assertIn('PYTAG="3.13"', script)

    def test_first_round_requires_matching_abi(self):
        script = self.mod.LAUNCHER.format(py="3.13")
        self.assertIn('[ "$ver" = "$PYTAG" ] || continue', script)
        self.assertIn('import sys;print("%d.%d" % sys.version_info[:2])', script)

    def test_second_round_still_starts_app_with_warning(self):
        script = self.mod.LAUNCHER.format(py="3.13")
        self.assertIn("SSH 功能可能不可用", script)
        self.assertEqual(script.count('exec "$cand" "$HERE/main.py" "$@"'), 2)

    def test_build_injects_current_python_tag(self):
        with open(os.path.join(ROOT, "packaging", "build.py"),
                  encoding="utf-8") as f:
            source = f.read()
        self.assertIn("LAUNCHER.format(py=pytag)", source)
        self.assertIn('pytag = f"{sys.version_info.major}.{sys.version_info.minor}"',
                      source)


if __name__ == "__main__":
    unittest.main()
