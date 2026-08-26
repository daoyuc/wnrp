# -*- coding: utf-8 -*-
"""ini 关键配置项表单编辑：元数据 / 读取 / 校验 / 写回（备份 + 精确行替换）。

- 覆盖 php_manager.KEY_INI_ITEMS 中适合表单编辑的常用项；
- 类型区分 int / size（K/M/G 后缀）/ onoff / timezone / enum / str；
- 写前备份 <ini>.bak，写失败自动还原；
- 以二进制 + latin-1 无损读写：只替换命中的行，其余字节原样保留，
  避免 errors="replace" 污染非 UTF-8 注释。
"""
import os
import re
import shutil
import zoneinfo

# 键名（匹配行首，大小写不敏感）+ 类型 + 中文标签/提示
INI_ITEMS_META = [
    {"key": "memory_limit", "type": "size", "label": "内存上限",
     "hint": "如 128M / 512M / -1(不限)"},
    {"key": "post_max_size", "type": "size", "label": "POST 上限",
     "hint": "如 8M / 64M（需大于 upload_max_filesize）"},
    {"key": "upload_max_filesize", "type": "size", "label": "上传文件上限",
     "hint": "如 2M / 100M"},
    {"key": "max_file_uploads", "type": "int", "label": "单次最大上传数",
     "hint": "正整数，如 20"},
    {"key": "max_execution_time", "type": "int", "label": "最大执行秒数",
     "hint": "正整数；0 为不限（CLI）"},
    {"key": "max_input_time", "type": "int", "label": "输入超时秒数",
     "hint": "正整数；-1 为不限"},
    {"key": "display_errors", "type": "onoff", "label": "显示错误",
     "hint": "On / Off（生产建议 Off）"},
    {"key": "error_reporting", "type": "enum", "label": "错误报告级别",
     "hint": "常见级别见下拉",
     "options": ["E_ALL", "E_ALL & ~E_DEPRECATED & ~E_STRICT",
                 "E_ALL & ~E_NOTICE", "E_ERROR | E_PARSE | E_CORE_ERROR"]},
    {"key": "date.timezone", "type": "timezone", "label": "默认时区",
     "hint": "如 Asia/Shanghai / UTC"},
    {"key": "default_charset", "type": "str", "label": "默认字符集",
     "hint": "如 UTF-8"},
    {"key": "opcache.enable", "type": "onoff", "label": "Opcache 开关",
     "hint": "On / Off（FastCGI 生效）"},
]

_INT_RE = re.compile(r"^-?\d+$")
_SIZE_RE = re.compile(r"^-?\d+\s*[KMG]?$", re.IGNORECASE)


def get_meta(key: str) -> dict | None:
    """按键名返回元数据；未知键返回 None。"""
    for m in INI_ITEMS_META:
        if m["key"] == key:
            return m
    return None


def validate_value(meta: dict, value: str) -> str | None:
    """校验表单值，返回错误信息；合法返回 None。"""
    value = (value or "").strip()
    t = meta.get("type", "str")
    if t == "int":
        if not _INT_RE.match(value):
            return "必须是整数"
        if int(value) < -1:
            return "必须为 -1 或正整数"
    elif t == "size":
        if not _SIZE_RE.match(value):
            return "必须是数字，可带 K/M/G 后缀（如 128M）"
    elif t == "onoff":
        if value.lower() not in ("on", "off", "1", "0"):
            return "必须是 On 或 Off"
    elif t == "timezone":
        if not value:
            return "不能为空"
        if value not in zoneinfo.available_timezones():
            return "无效时区（如 Asia/Shanghai）"
    elif t == "enum":
        options = meta.get("options") or []
        if options and value not in options:
            return f"只能从 {', '.join(options)} 中选择"
    elif t == "str":
        if not value:
            return "不能为空"
    return None


# --------------------------------------------------------------------- #
# 读写（二进制安全）
# --------------------------------------------------------------------- #
def _read_lines(path: str) -> list[str]:
    with open(path, "rb") as f:
        raw = f.read()
    return [ln.decode("latin-1") for ln in raw.splitlines(keepends=True)]


def _write_lines(path: str, lines: list[str]) -> None:
    with open(path, "wb") as f:
        for ln in lines:
            f.write(ln.encode("latin-1"))


def load_values(path: str) -> dict[str, str]:
    """读取 ini 中各项当前值（仅非注释行，首个命中）；文件不可读返回空 dict。"""
    values: dict[str, str] = {}
    try:
        lines = _read_lines(path)
    except OSError:
        return {}
    keys = [m["key"] for m in INI_ITEMS_META]
    for line in lines:
        if line.lstrip().startswith(";"):
            continue
        stripped = line.strip()
        if not stripped:
            continue
        # 匹配 key 开头（后随 = 或空白 =），避免误匹配 key_cli 等长键
        for key in keys:
            if re.match(r"^" + re.escape(key) + r"(?:\s*=|=)", stripped, re.IGNORECASE):
                values[key] = stripped.split("=", 1)[1].strip()
                break
    return values


def save_values(path: str, changes: dict[str, str]) -> tuple[int, str]:
    """写回多个键值。返回 (修改数, 备份路径)。

    行匹配：非注释行且键名精确匹配（行首 + 等号）；未找到的行追加到文件尾部。
    写前备份 <path>.bak；写入失败自动还原备份并抛出 OSError。
    """
    lines = _read_lines(path)
    backup = path + ".bak"
    shutil.copy2(path, backup)

    new_lines: list[str] = []
    # 逐行替换命中键（键名大小写不敏感但保留原键名）
    by_lower = {k.lower(): k for k in changes}
    matched_keys: set[str] = set()
    for line in lines:
        if line.lstrip().startswith(";"):
            new_lines.append(line)
            continue
        match = re.match(r"^(\s*)([A-Za-z0-9_.\-]+)(\s*=\s*)(.*)$", line)
        if match:
            raw_key = match.group(2)
            target_key = by_lower.get(raw_key.lower())
            if target_key:
                new_line = f"{match.group(1)}{raw_key}{match.group(3)}{changes[target_key]}\n"
                new_lines.append(new_line)
                matched_keys.add(target_key)
                continue
        new_lines.append(line)

    # 追加未找到的键
    for key, value in changes.items():
        if key not in matched_keys:
            new_lines.append(f"{key} = {value}\n")

    changed = len(changes)
    try:
        _write_lines(path, new_lines)
    except OSError:
        shutil.copy2(backup, path)
        raise
    return changed, backup
