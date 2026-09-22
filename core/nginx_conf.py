# -*- coding: utf-8 -*-
"""nginx 配置文本读写：按上下文（main / events / http）读取与改写指令。

只做**文本层面的精确行操作**，不解析 nginx 语义、不读写文件
（文件读写 / 备份 / `nginx -t` 校验统一由 core/tuning.py 负责），因此可单测。

- 上下文由 `{` / `}` 计数判定：main / events / http / http/server …
- 注释行（`#` 开头）不参与匹配，行尾注释在改写时保留；
- 读取：返回首个命中指令的值（限定上下文时按前缀匹配，包含子块继承的值）；
- 写入：命中则只替换该行的值；未命中则插入到目标上下文块首；块缺失时新建块。
"""
import re

MAIN = "main"
EVENTS = "events"
HTTP = "http"

INDENT = "    "

# 块开头：`events {`、`location / {` 等（块名后可带参数）
_BLOCK_OPEN_RE = re.compile(r"^([A-Za-z_][\w-]*)((?:\s+[^{]*)?)\{\s*$")
# 指令行：`keepalive_timeout 30;`（值内不含分号；行尾注释可选）
_DIRECTIVE_RE = re.compile(r"^(\s*)([A-Za-z_][\w-]*)(\s+)(.+?)\s*;\s*(#.*)?$")


def strip_comment(line: str) -> str:
    """去掉行内 `#` 之后的注释（不处理引号内的 #，nginx 配置中极罕见）。"""
    idx = line.find("#")
    return line if idx < 0 else line[:idx]


def context_map(text: str) -> list[str]:
    """逐行返回该行所属上下文，如 'main' / 'events' / 'http' / 'http/server'。"""
    lines = text.splitlines()
    ctx: list[str] = []
    stack: list[str] = []
    for line in lines:
        body = strip_comment(line).strip()
        m = _BLOCK_OPEN_RE.match(body)
        if m:
            ctx.append("/".join(stack) or MAIN)
            stack.append(m.group(1))
            continue
        if body.startswith("}"):
            if stack:
                stack.pop()
            ctx.append("/".join(stack) or MAIN)
            continue
        ctx.append("/".join(stack) or MAIN)
    return ctx


def _is_sub(context: str, wanted: str) -> bool:
    """wanted 是否为该行上下文本身或其祖先（值继承：http 内的 server 继承 http）。"""
    return context == wanted or context.startswith(wanted + "/")


def get_directive(text: str, name: str, context: str | None = None) -> str | None:
    """读取指令当前值；未设置返回 None。context 为 None 时不限制上下文。"""
    lines = text.splitlines()
    ctxs = context_map(text)
    for line, ctx in zip(lines, ctxs):
        if context and not _is_sub(ctx, context):
            continue
        m = _DIRECTIVE_RE.match(strip_comment(line))
        if m and m.group(2) == name:
            return m.group(4).strip()
    return None


def _block_range(text: str, name: str) -> tuple[int, int] | None:
    """返回顶层（或 http 直接层）块的 (开头行号, 结束行号)；不存在返回 None。"""
    lines = text.splitlines()
    depth: list[str] = []
    start = None
    for i, line in enumerate(lines):
        body = strip_comment(line).strip()
        m = _BLOCK_OPEN_RE.match(body)
        if m:
            # 只认顶层块与 http 直接层块：避免把 server 内的同名块（如 location）当成目标
            if start is None and m.group(1) == name and (not depth or depth[-1] == HTTP):
                start = i
            depth.append(m.group(1))
            continue
        if body.startswith("}"):
            if depth:
                depth.pop()
            if start is not None and not depth:
                return start, i
            if not depth:
                start = None
    return None


def _indent_inside(lines: list[str], open_idx: int, close_idx: int) -> str:
    """块内首个非空非注释行的缩进；空块时返回默认缩进。"""
    for line in lines[open_idx + 1:close_idx]:
        body = strip_comment(line).strip()
        if body:
            return line[: len(line) - len(line.lstrip())]
    return INDENT


def _insert_block(lines: list[str], name: str, value_line: str) -> list[str]:
    """块不存在时新建块并追加到文件末尾：events { ... }。"""
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(f"{name} {{")
    lines.append(f"{INDENT}{value_line}")
    lines.append("}")
    return lines


def set_directive(text: str, name: str, value: str, context: str = MAIN) -> tuple[str, str]:
    """写入指令，返回 (新文本, 动作)。动作：replaced / inserted / unchanged。

    - 命中已有行：只替换值（保留缩进、键名与行尾注释）；
    - 未命中：插入到目标上下文块首（main 插到首个顶层指令前），块缺失则新建。
    """
    lines = text.splitlines()
    ctxs = context_map(text)
    target = f"{name} {value};"

    for i, line in enumerate(lines):
        if ctxs[i] != context or line.lstrip().startswith("#"):
            continue
        # 直接匹配原行：正则尾部的 (#.*)? 才能捕获行尾注释并在改写时保留
        m = _DIRECTIVE_RE.match(line)
        if m and m.group(2) == name:
            if m.group(4).strip() == value:
                return text, "unchanged"
            comment = m.group(5) or ""
            tail = f"  {comment.strip()}" if comment.strip() else ""
            lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{value};{tail}"
            return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), "replaced"

    if context == MAIN:
        pos = next((i for i, l in enumerate(lines)
                    if ctxs[i] == MAIN and strip_comment(l).strip()), None)
        pos = len(lines) if pos is None else pos
        lines.insert(pos, target)
        return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), "inserted"

    rng = _block_range(text, context)
    if rng is None:
        lines = _insert_block(lines, context, target)
        return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), "inserted"
    open_idx, close_idx = rng
    indent = _indent_inside(lines, open_idx, close_idx)
    lines.insert(open_idx + 1, f"{indent}{target}")
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), "inserted"


def apply_changes(text: str, changes: list[tuple[str, str, str]]) -> tuple[str, list[dict]]:
    """批量写入 [(指令名, 值, 上下文)]，返回 (新文本, 每项结果)。

    逐项重写（每步基于最新文本重新定位），避免行号漂移。
    """
    results: list[dict] = []
    current = text
    for name, value, context in changes:
        current, action = set_directive(current, name, value, context)
        results.append({"key": name, "value": value, "context": context, "action": action})
    return current, results
