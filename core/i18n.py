# -*- coding: utf-8 -*-
"""国际化（i18n）核心：语言探测 / 词条加载 / 翻译函数 t()。

约定
----
- 源码里的字符串字面量即 msgid（当前为简体中文原文，见各调用处 `t("…")`），
  zh_CN 不需要词条表（直接命中原文）；
  其余语言在 i18n/<lang>/*.json 提供 msgid -> msgstr 映射。
- 词条值支持 {kwarg} 占位：调用 `t("端口 {port} 已被占用", port=9000)`。
- 语言持久化在 config.json settings.lang；为空/None 时按系统 locale 探测：
  zh/zh_CN -> zh_CN，zh_TW/zh_HK/zh_MO -> zh_TW，ja -> ja，ko -> ko，其余 -> en。
- 运行中切换语言后需重启生效（切换只写入配置，不热更新）。
"""
import glob
import json
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
# i18n 资源根：<repo>/i18n（core/i18n.py 的上级即仓库根）
RESOURCE_DIR = os.path.join(os.path.dirname(_THIS_DIR), "i18n")

IS_WIN = sys.platform.startswith("win")

# 支持的语言：code -> 以该语言自述的名称（用于语言选择菜单）
LANGS: dict[str, str] = {
    "en": "English",
    "zh_CN": "简体中文",
    "zh_TW": "繁體中文",
    "ja": "日本語",
    "ko": "한국어",
}
# 词条表在 zh_CN 语言直接回落原文，故只有以下语言需要资源文件
RESOURCE_LANGS = tuple(k for k in LANGS if k != "zh_CN")

# 运行时语言（由 set_language 覆盖；导入期先用系统探测）
_current: str = "zh_CN"
_tables: dict[str, dict[str, str]] = {}
# 调试：记录“非 zh 语言下未命中词条”的 msgid，供 coverage 检测
_missing: set[str] = set()


def detect_language() -> str:
    """按系统 locale 返回受支持的语言 code；未知一律回落到 en。"""
    raw = ""
    if IS_WIN:
        try:
            import ctypes

            # GetUserDefaultUILanguage 返回 LCID，映射常见语言
            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            table = {
                0x0404: "zh_TW", 0x0409: "en", 0x0411: "ja",
                0x0412: "ko", 0x0804: "zh_CN", 0x0C04: "zh_HK",
                0x1004: "zh_SG", 0x1404: "zh_MO",
            }
            code = table.get(lcid, "")
            if code:
                return _normalize(code)
        except Exception:
            pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var, "")
        if val:
            raw = val
            break
    if not raw:
        try:
            import locale

            raw = locale.getdefaultlocale()[0] or ""
        except Exception:
            raw = ""
    return _normalize(raw)


def _normalize(raw: str) -> str:
    """把 locale 字符串映射到 LANGS 中的 code。"""
    if not raw:
        return "en"
    tag = raw.replace("-", "_").lower().split(".")[0].split("@")[0]
    base = tag.split("_")[0]
    if base == "zh":
        region = tag.split("_")[1] if "_" in tag else ""
        if region in ("tw", "hk", "mo"):
            return "zh_TW"
        return "zh_CN"
    if base == "ja":
        return "ja"
    if base == "ko":
        return "ko"
    if base == "en":
        return "en"
    return "en"


def _resource_files(lang: str) -> list[str]:
    """返回 lang 的词条资源文件：目录 i18n/<lang>/ 下全部 *.json，或单文件。"""
    d = os.path.join(RESOURCE_DIR, lang)
    if os.path.isdir(d):
        return sorted(glob.glob(os.path.join(d, "*.json")))
    f = os.path.join(RESOURCE_DIR, lang + ".json")
    return [f] if os.path.exists(f) else []


def _load_table(lang: str) -> dict[str, str]:
    """加载并合并某语言全部词条文件；文件缺失/损坏时返回空表（不抛错）。"""
    merged: dict[str, str] = {}
    for f in _resource_files(lang):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for k, v in data.items():
                    if isinstance(v, str):
                        merged[str(k)] = v
        except (OSError, json.JSONDecodeError):
            continue
    return merged


def load_language(lang: str) -> None:
    """把某语言的词条表载入缓存（语言本身合法即设当前语言）。"""
    global _current
    if lang not in LANGS:
        lang = "en"
    _current = lang
    if lang in _tables:
        return
    _tables[lang] = _load_table(lang) if lang != "zh_CN" else {}


def current_language() -> str:
    return _current


def set_language(lang: str) -> None:
    """程序启动时调用：按用户配置或系统探测设置语言。"""
    load_language(lang or detect_language())


def translate(msgid: str, **kwargs) -> str:
    """翻译 msgid；命中词条则按 kwargs 格式化；未命中回退原文（并记录缺失）。"""
    text = msgid
    if _current != "zh_CN":
        text = _tables.get(_current, {}).get(msgid)
        if text is None:
            _missing.add(msgid)
            text = msgid
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return text


# 便捷别名：core/ui 各模块统一 `from .i18n import t`
t = translate


def missing_keys() -> set[str]:
    """返回当前语言下未命中词条的 msgid 集合（供覆盖度检测）。"""
    return set(_missing)


def languages_available() -> dict[str, str]:
    return dict(LANGS)


def format_bool(value: bool, true_text: str, false_text: str) -> str:
    return true_text if value else false_text


# 模块导入期即确定语言（进程级，便于命令行/后台线程使用）
set_language("")
