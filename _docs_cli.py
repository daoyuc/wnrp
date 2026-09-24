# -*- coding: utf-8 -*-
"""CLI 参考文档生成器（内部开发工具，与 `_i18n_scan.py` 同类）。

用法：
  python3 _docs_cli.py            # 依据 cli.py 的 argparse 结构生成 docs/CLI.md
  python3 _docs_cli.py --check    # 只校验 docs/CLI.md 是否与代码一致（不一致退出码 1）

为什么不手写：命令与参数会漂移。契约的唯一来源是 `cli.py` 的 argparse 结构
（与 `python3 cli.py schema --json` 输出同源），本脚本只负责渲染成 Markdown。
因此**改完命令后必须重跑本脚本**（`tests/test_docs.py` 会在一致性被破坏时失败）。
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DOC_PATH = os.path.join(ROOT, "docs", "CLI.md")

#: 每个动作都继承的全局参数（COMMON），统一写在「通用约定」里，避免每个动作表格重复四行
GLOBAL_FLAGS = {"--json", "--lang", "--quiet", "-q", "--debug"}

HEADER = (
    "<!-- 由 _docs_cli.py 自动生成，请勿手改；改命令后运行 python3 _docs_cli.py -->\n"
)


def _subparsers(parser: argparse.ArgumentParser) -> dict:
    """{子命令名: 子解析器}（argparse 私有属性，仅结构读取）。"""
    for act in parser._actions:  # noqa: SLF001
        if isinstance(act, argparse._SubParsersAction):  # noqa: SLF001
            return dict(act.choices)
    return {}


def _choices_help(parser: argparse.ArgumentParser) -> dict:
    """{子命令名: help 文案}。

    argparse 把 `add_parser(help=...)` 存在父解析器的 _choices_actions 上（而不是子解析器
    的 description），`cli.py schema` 的 _describe_parser 只看 description，因此这里补一次。
    """
    out: dict[str, str] = {}
    for act in parser._actions:  # noqa: SLF001
        if isinstance(act, argparse._SubParsersAction):  # noqa: SLF001
            for ca in getattr(act, "_choices_actions", []):
                out[ca.dest] = (ca.help or "").strip()
    return out


def _cell(text: str) -> str:
    """表格单元格转义：竖线转义、换行压平（否则会撑坏 Markdown 表格）。"""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _option_rows(options: list[dict], *, skip_global: bool = True) -> list[tuple]:
    """把 _describe_parser 的 options 转成表格行：(参数, 必填, 默认, 说明)。"""
    rows: list[tuple] = []
    for opt in options:
        flags = list(opt.get("flags") or [])
        if skip_global and flags and set(flags) <= GLOBAL_FLAGS:
            continue
        name = " ".join(flags) if flags else f"<{opt.get('dest', '')}>"
        required = "是" if (opt.get("required") or not flags) else ""
        default = opt.get("default", "")
        if isinstance(default, (list, tuple)):
            default = ", ".join(str(d) for d in default)
        elif isinstance(default, bool):
            default = "true" if default else "false"
        rows.append((name, required, str(default), _cell(opt.get("help", ""))))
    return rows


def _option_table(options: list[dict], *, skip_global: bool = True) -> list[str]:
    rows = _option_rows(options, skip_global=skip_global)
    if not rows:
        return []
    out = ["| 参数 | 必填 | 默认 | 说明 |", "|---|---|---|---|"]
    out += [f"| `{name}` | {req} | {default} | {help_} |"
            for name, req, default, help_ in rows]
    return out


def build_markdown() -> str:
    """按当前 cli.py 结构渲染 docs/CLI.md 的完整内容。"""
    import cli  # noqa: PLC0415 —— 延迟导入：调用方只需本模块的函数

    parser = cli.build_parser()
    desc = cli._describe_parser(parser)  # noqa: SLF001 —— 与 schema 命令同源
    group_parsers = _subparsers(parser)
    group_help = _choices_help(parser)
    conventions = {
        "json": "加 `--json`：stdout **只**输出一个 `{\"ok\",\"command\",\"data\",\"warnings\"}` 文档；"
                "字段名与枚举值一律英文（稳定契约），说明性文本按 `--lang` 输出",
        "exit_codes": "`0` 成功 / `1` 业务失败（`ok=false`，含 `data.error`）/ "
                      "`2` 参数用法错误 / `130` 被中断",
        "non_interactive": "永不弹窗、不等待输入；写 hosts 会在无权限时触发系统授权框（需显式 `--hosts`）",
        "safety": "写操作支持 `--dry-run`（只报告将做什么）；删除类需 `--yes`；"
                  "Redis 高危命令（FLUSHALL 等）需 `--force`；改动前自动备份 `.bak`",
        "recommended_flow": "`schema --json` → `env --json` → `site create … --dry-run` → `site create …`",
    }

    lines = [
        HEADER.rstrip("\n"),
        "",
        "# CLI 参考（自动生成）",
        "",
        "> 本文由 `python3 _docs_cli.py` 从 `cli.py` 的 argparse 结构生成，**请勿手改**。",
        "> 一致性校验：`python3 _docs_cli.py --check`（`tests/test_docs.py` 亦会校验）。",
        "> 机器可读契约（AI 优先读这个）：`python3 cli.py schema --json`。",
        "",
        "## 通用约定",
        "",
        "| 项 | 说明 |",
        "|---|---|",
    ]
    lines += [f"| {name} | {_cell(text)} |" for name, text in conventions.items()]
    lines += [
        "",
        "以下全局参数对**所有动作**可用（各动作表格里不再重复）：",
        "",
    ]
    lines += _option_table(desc.get("options", []), skip_global=False)
    lines += ["", "## 命令总览", "", "| 分组 | 说明 | 动作 |", "|---|---|---|"]

    groups = desc.get("subcommands", {})
    for group in sorted(groups):
        actions = groups[group].get("subcommands", {})
        names = " · ".join(f"`{a}`" for a in sorted(actions)) or "—"
        lines.append(f"| `{group}` | {_cell(group_help.get(group, ''))} | {names} |")

    lines += ["", "## 分组与动作", ""]
    for group in sorted(groups):
        g = groups[group]
        help_ = _cell(group_help.get(group, ""))
        lines += [f"## `{group}`" + (f" — {help_}" if help_ else ""), ""]
        action_helps = _choices_help(group_parsers[group]) if group in group_parsers else {}
        for action in sorted(g.get("subcommands", {})):
            h = _cell(action_helps.get(action, ""))
            lines += [f"### `{group} {action}`" + (f" — {h}" if h else ""), ""]
            table = _option_table(g["subcommands"][action].get("options", []))
            lines += table + [""] if table else ["（无参数）", ""]

    text = "\n".join(lines).rstrip() + "\n"
    return text


def main() -> int:
    check = "--check" in sys.argv[1:]
    text = build_markdown()
    if check:
        try:
            with open(DOC_PATH, "r", encoding="utf-8") as f:
                current = f.read()
        except OSError:
            print(f"缺失：{DOC_PATH}（运行 python3 _docs_cli.py 生成）")
            return 1
        if current != text:
            print("docs/CLI.md 与 cli.py 结构不一致：请运行 python3 _docs_cli.py 重新生成")
            return 1
        print("docs/CLI.md 与 cli.py 结构一致")
        return 0
    os.makedirs(os.path.dirname(DOC_PATH), exist_ok=True)
    with open(DOC_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"已生成 {DOC_PATH}（{len(text.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
