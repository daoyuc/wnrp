# -*- coding: utf-8 -*-
"""文档体系守护：链接可达、引用的代码路径真实存在、CLI 参考与代码同步、表格结构完整。

这些断言都很廉价（只读仓库内 Markdown），但能挡住最常见的文档腐烂：
改了命令忘了重生成 CLI 参考、重构删了模块而文档还在指它、手写文档时把表格写歪。
规则本身见 `docs/README.md`「维护规则」。
"""
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = sorted(glob.glob(os.path.join(ROOT, "docs", "*.md")))
ROOT_DOCS = [os.path.join(ROOT, "README.md"), os.path.join(ROOT, "AGENTS.md")]
ALL_DOCS = ROOT_DOCS + DOCS

#: 文档里引用的代码文件（`core/xxx.py` 形式）
CODE_REF = re.compile(r"\b((?:core|ui|tests|packaging)/[A-Za-z_]+\.py)\b")
#: Markdown 行内链接
LINK = re.compile(r"\]\(([^)\s]+?)\)")


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class DocLinkTest(unittest.TestCase):
    def test_local_links_resolve(self):
        """文档里的相对链接必须指向真实存在的文件/目录。"""
        broken = []
        for path in ALL_DOCS:
            base = os.path.dirname(path)
            for link in LINK.findall(read(path)):
                if link.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                target = os.path.normpath(os.path.join(base, link.rstrip("/")))
                if not os.path.exists(target):
                    broken.append(f"{os.path.relpath(path, ROOT)} -> {link}")
        self.assertEqual(broken, [], f"失效链接：{broken}")

    def test_cited_code_paths_exist(self):
        """文档提到的 core/ui/tests/packaging 模块必须存在（防止重构后文档悬空）。"""
        missing = []
        for path in DOCS:  # 只查 docs/：README 的目录树同理，但那里逐个列举、噪音更大
            for ref in sorted(set(CODE_REF.findall(read(path)))):
                if not os.path.exists(os.path.join(ROOT, ref)):
                    missing.append(f"{os.path.relpath(path, ROOT)} -> {ref}")
        self.assertEqual(missing, [], f"引用了不存在的模块：{missing}")


class DocStructureTest(unittest.TestCase):
    def test_docs_index_covers_every_doc(self):
        """docs/README.md 的文档地图必须覆盖 docs/ 下的每一份文档（不留孤儿文档）。"""
        index = read(os.path.join(ROOT, "docs", "README.md"))
        missing = [os.path.basename(p) for p in DOCS
                   if os.path.basename(p) != "README.md"
                   and f"{os.path.basename(p)}]" not in index
                   and os.path.basename(p) not in index]
        self.assertEqual(missing, [], f"未在 docs/README.md 登记：{missing}")

    def test_tables_have_consistent_columns(self):
        """表格每行的列数必须与表头一致（单元格里混入换行最容易撑坏表格）。

        规则：跳过 ``` 代码块；连续的 `|` 开头行视作一个表格块；列数按未转义竖线数判断。
        """
        problems = []
        for path in ALL_DOCS:
            rel = os.path.relpath(path, ROOT)
            in_fence = False
            table: list[tuple[int, int, str]] = []

            def flush():
                if not table:
                    return
                head = table[0][1]
                bad = [(no, txt) for no, cells, txt in table if cells != head]
                if bad:
                    detail = "; ".join(f"{rel}:{no} {txt[:100]}" for no, txt in bad[:3])
                    problems.append(f"{rel} 第 {table[0][0]} 行起的表格应为 {head} 列（异常行：{detail}）")
                table.clear()

            for no, line in enumerate(read(path).splitlines(), start=1):
                if line.lstrip().startswith("```"):
                    in_fence = not in_fence
                    flush()
                    continue
                if in_fence or not line.startswith("|"):
                    flush()
                    continue
                # 转义竖线（\|）不算分隔符
                cells = len(re.split(r"(?<!\\)\|", line)) - 2
                table.append((no, cells, line))
            flush()
        self.assertEqual(problems, [], "; ".join(problems))


class CliReferenceTest(unittest.TestCase):
    def test_cli_reference_is_up_to_date(self):
        """docs/CLI.md 必须与 cli.py 的 argparse 结构一致（改命令后跑 _docs_cli.py 重生成）。"""
        import _docs_cli

        expected = _docs_cli.build_markdown()
        actual = read(os.path.join(ROOT, "docs", "CLI.md"))
        self.assertEqual(
            actual, expected,
            "docs/CLI.md 已过期：请运行 python3 _docs_cli.py 重新生成")


if __name__ == "__main__":
    unittest.main()
