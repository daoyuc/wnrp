# -*- coding: utf-8 -*-
"""i18n 词条扫描 / 覆盖检查（内部开发工具，不入 git）。

用法：
  python3 _i18n_scan.py            # 扫描所有 t("…") msgid，输出 i18n/_keys.json
  python3 _i18n_scan.py --report   # 统计 + 各语言缺失词条清单（覆盖度）
"""
import ast
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
I18N = os.path.join(ROOT, "i18n")
SRC_DIRS = ["ui", "core"]
RESOURCE_LANGS = ["en", "zh_TW", "ja", "ko"]


def iter_sources():
    for name in ("main.py",):
        yield os.path.join(ROOT, name)
    for d in SRC_DIRS:
        for root, _dirs, files in os.walk(os.path.join(ROOT, d)):
            for f in sorted(files):
                if f.endswith(".py"):
                    yield os.path.join(root, f)


def collect():
    keys: dict[str, dict] = {}
    for path in iter_sources():
        try:
            src = open(path, "r", encoding="utf-8").read()
            tree = ast.parse(src, filename=path)
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "t"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                msgid = node.args[0].value
                if not msgid.strip():
                    continue
                info = keys.setdefault(msgid, {"count": 0, "files": []})
                info["count"] += 1
                rel = os.path.relpath(path, ROOT)
                if rel not in info["files"]:
                    info["files"].append(rel)
    return keys


def load_table(lang):
    table = {}
    d = os.path.join(I18N, lang)
    if not os.path.isdir(d):
        f = os.path.join(I18N, lang + ".json")
        if os.path.exists(f):
            try:
                table.update(json.load(open(f, "r", encoding="utf-8")))
            except Exception:
                pass
        return table
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            try:
                data = json.load(open(os.path.join(d, name), "r", encoding="utf-8"))
                if isinstance(data, dict):
                    table.update(data)
            except Exception:
                continue
    return table


def main():
    keys = collect()
    _keys_path = os.path.join(I18N, "_keys.json")
    os.makedirs(I18N, exist_ok=True)
    json.dump(keys, open(_keys_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, sort_keys=True)
    print(f"msgid 总数：{len(keys)}")
    if "--report" not in sys.argv:
        return
    for lang in RESOURCE_LANGS:
        table = load_table(lang)
        missing = [k for k in keys if k not in table]
        print(f"[{lang}] 已译 {len(table)} / 缺失 {len(missing)}")
        if missing:
            print("  示例缺失：", " | ".join(missing[:8]))


if __name__ == "__main__":
    main()
