# -*- coding: utf-8 -*-
"""Git 工作区插件：porcelain 解析、单仓库状态、批量汇总与过滤。"""
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from core.plugins.git_status import repos

HAS_GIT = bool(shutil.which("git"))


def _git(root: str, *args: str):
    """在指定目录跑一条 git 命令（测试夹具用，失败不抛）。"""
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace")


def _project(name: str, root: str):
    return SimpleNamespace(name=name, root=root, php="", domains=(), urls=())


class ParseStatusTest(unittest.TestCase):
    """纯解析：不依赖 git 可执行文件，三平台都能跑。"""

    def test_branch_ahead_behind_and_counts(self):
        text = "\n".join([
            "# branch.oid 0123456789abcdef",
            "# branch.head main",
            "# branch.upstream origin/main",
            "# branch.ab +2 -3",
            "1 M. N... 100644 100644 100644 aaaa bbbb src/staged.py",
            "1 .M N... 100644 100644 100644 cccc dddd src/modified.py",
            "? new-file.txt",
            "u UU N... 100644 100644 100644 100644 eeee ffff gggg conflict.txt",
        ])
        info = repos.parse_status(text)
        self.assertEqual(info["branch"], "main")
        self.assertEqual(info["upstream"], "origin/main")
        self.assertEqual(info["ahead"], 2)
        self.assertEqual(info["behind"], 3)
        self.assertEqual(info["staged"], 1)
        self.assertEqual(info["modified"], 1)
        self.assertEqual(info["untracked"], 1)
        self.assertEqual(info["conflict"], 1)
        self.assertIn("new-file.txt", info["changes"])
        self.assertIn("src/staged.py", info["changes"])
        self.assertIn("src/modified.py", info["changes"])
        self.assertIn("conflict.txt", info["changes"])

    def test_rename_keeps_target_path(self):
        # 重命名行的 path 与 origPath 用 TAB 分隔，只取新路径
        line = ("2 R. N... 100644 100644 100644 aaaa bbbb R100 "
                "docs/new name.md\tdocs/old.md")
        info = repos.parse_status("# branch.head main\n" + line)
        self.assertEqual(info["changes"], ["docs/new name.md"])

    def test_detached_head_and_no_upstream(self):
        info = repos.parse_status("# branch.head (detached)")
        self.assertEqual(info["branch"], "(detached)")
        self.assertEqual(info["upstream"], "")
        self.assertEqual(info["ahead"], 0)

    def test_empty_and_garbage_input(self):
        for text in ("", None, "\n\n", "not a porcelain line"):
            info = repos.parse_status(text or "")
            self.assertEqual(info["branch"], "")
            self.assertEqual(info["staged"] + info["modified"]
                             + info["untracked"] + info["conflict"], 0)

    def test_changes_are_capped(self):
        text = "\n".join(f"? f{i}.txt" for i in range(repos.MAX_CHANGES + 20))
        info = repos.parse_status(text)
        self.assertEqual(info["untracked"], repos.MAX_CHANGES + 20)
        self.assertEqual(len(info["changes"]), repos.MAX_CHANGES)


class FormatTimeTest(unittest.TestCase):
    def test_zero_and_invalid(self):
        self.assertEqual(repos.format_time(0), "")
        self.assertEqual(repos.format_time(None), "")
        self.assertEqual(repos.format_time("abc"), "")

    def test_valid_timestamp(self):
        self.assertRegex(repos.format_time(1700000000), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


@unittest.skipUnless(HAS_GIT, "需要 git 可执行文件")
class RepoDescribeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="phpvm-git-")
        self.root = os.path.join(self.tmp, "proj")
        os.makedirs(self.root)
        _git(self.root, "init", "-q")
        self._commit("init")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commit(self, message: str) -> None:
        _git(self.root, "-c", "user.email=test@example.com",
             "-c", "user.name=tester", "commit", "-q", "--allow-empty",
             "-m", message)

    def test_clean_repo(self):
        row = repos.describe(self.root)
        self.assertTrue(row["is_repo"])
        self.assertEqual(row["status"], "clean")
        self.assertEqual(row["dirty"], 0)
        self.assertEqual(row["last_subject"], "init")
        self.assertTrue(row["branch"])
        self.assertTrue(repos.format_time(row["last_ts"]))

    def test_untracked_then_staged(self):
        with open(os.path.join(self.root, "new.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        row = repos.describe(self.root)
        self.assertEqual(row["status"], "dirty")
        self.assertEqual(row["untracked"], 1)
        self.assertEqual(row["staged"], 0)
        self.assertIn("new.txt", row["changes"])

        _git(self.root, "add", "new.txt")
        row = repos.describe(self.root)
        self.assertEqual(row["staged"], 1)
        self.assertEqual(row["untracked"], 0)
        self.assertEqual(row["dirty"], 1)

    def test_subdirectory_falls_back_to_parent_repo(self):
        # 站点 root 常指向 Laravel 的 public 等子目录：回退上一级找 .git
        public = os.path.join(self.root, "public")
        os.makedirs(public)
        with open(os.path.join(public, "index.php"), "w", encoding="utf-8") as f:
            f.write("<?php echo 1;")
        row = repos.describe(public)
        self.assertTrue(row["is_repo"])
        self.assertEqual(row["status"], "dirty")  # public/index.php 未跟踪
        self.assertEqual(row["untracked"], 1)
        self.assertIn("public/", row["changes"])  # 未跟踪目录折叠为一条
        self.assertEqual(
            os.path.normcase(os.path.abspath(row["repo_root"])),
            os.path.normcase(os.path.abspath(self.root)))

    def test_not_a_repo(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        row = repos.describe(plain)
        self.assertFalse(row["is_repo"])
        self.assertEqual(row["status"], "none")
        self.assertEqual(row["error"], "not_repo")

    def test_missing_directory(self):
        row = repos.describe(os.path.join(self.tmp, "nope"))
        self.assertEqual(row["status"], "missing")
        self.assertEqual(row["error"], "missing")

    def test_no_git_executable(self):
        with mock.patch.object(repos, "git_executable", return_value=""):
            row = repos.describe(self.root)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error"], "no_git")

    def test_timeout_is_normalised(self):
        with mock.patch.object(repos, "_run",
                               side_effect=subprocess.TimeoutExpired("git", 1)):
            row = repos.describe(self.root)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error"], "timeout")


@unittest.skipUnless(HAS_GIT, "需要 git 可执行文件")
class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="phpvm-git-")
        self.repo = os.path.join(self.tmp, "alpha")
        self.plain = os.path.join(self.tmp, "beta")
        os.makedirs(self.repo)
        os.makedirs(self.plain)
        _git(self.repo, "init", "-q")
        _git(self.repo, "-c", "user.email=test@example.com",
             "-c", "user.name=tester", "commit", "-q", "--allow-empty",
             "-m", "init")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_aggregate_and_filters(self):
        projects = [_project("alpha", self.repo), _project("beta", self.plain)]
        data = repos.report(projects=projects)
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["repos"], 1)
        self.assertEqual(data["clean"], 1)
        self.assertEqual(data["none"], 1)
        self.assertEqual(data["dirty"], 0)

        only = repos.report(projects=projects, only="alp")
        self.assertEqual(only["total"], 1)
        self.assertEqual(only["items"][0]["name"], "alpha")

        limited = repos.report(projects=projects, limit=1)
        self.assertEqual(limited["total"], 1)

    def test_dirty_counted_in_summary(self):
        with open(os.path.join(self.repo, "a.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        data = repos.report(projects=[_project("alpha", self.repo)])
        self.assertEqual(data["dirty"], 1)
        self.assertEqual(data["clean"], 0)
        self.assertEqual(data["items"][0]["status"], "dirty")

    def test_find_by_name_and_path(self):
        projects = [_project("alpha", self.repo), _project("beta", self.plain)]
        items = repos.report(projects=projects)["items"]
        self.assertEqual(repos.find(items, "ALPHA")["name"], "alpha")
        self.assertEqual(repos.find(items, self.repo)["name"], "alpha")
        self.assertEqual(repos.find(items, "bet")["name"], "beta")
        self.assertIsNone(repos.find(items, "missing"))
        self.assertIsNone(repos.find(items, ""))


if __name__ == "__main__":
    unittest.main()
