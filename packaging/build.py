#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phpvm 安装包构建脚本（仅用标准库，可在 macOS / Linux / Windows 运行）。

用法::

    python3 packaging/build.py macos                 # dist/phpvm-<ver>-macos.dmg
    python3 packaging/build.py windows               # dist/phpvm-<ver>-windows(.zip)
    python3 packaging/build.py all                   # 两个平台
    python3 packaging/build.py sums                  # 仅重建 SHA256SUMS.txt
    python3 packaging/build.py macos --version 1.2.0

产物（默认写入 ``dist/``）::

    phpvm-<ver>-macos/phpvm.app          macOS 应用包（未压缩）
    phpvm-<ver>-macos.dmg                macOS 安装镜像（自动升级下载此文件）
    phpvm-<ver>-windows/                 Windows 免安装目录
    phpvm-<ver>-windows.zip              Windows 免安装压缩包
    phpvm-<ver>-windows-setup.exe        Windows 安装程序（需 Windows + Inno Setup）
    SHA256SUMS.txt                       校验和清单（自动升级用它校验安装包）

版本号优先级：``--version`` > 环境变量 ``PHPVM_BUILD_VERSION`` > core/version.py。

说明：安装包**不含** ``config.json``。首次运行时程序会在可写数据目录
（源码/绿色版 = 包目录；.app 或只读安装目录 = ``~/.phpvm``）自动生成，
因此升级覆盖程序文件不会丢失端口映射与用户设置。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")
sys.path.insert(0, ROOT)

from core.version import APP_ID, APP_NAME, APP_VERSION, SUMS_ASSET, asset_name  # noqa: E402

# 运行所需源码
ENTRY_FILES = ["main.py"]
PACKAGES = ["core", "ui", "i18n"]
POSIX_EXTRA = ["phpvm.command", "README.md"]
WIN_EXTRA = ["phpvm.bat", "README.md"]

# 复制时一律排除（缓存 / 开发工具产物 / 开发机运行时状态）
EXCLUDE_NAMES = {
    "__pycache__", ".DS_Store", ".gitignore", ".git", ".codebuddy",
    "_keys.json", "config.json", "crash_watchdog.json", "crash_watchdog.lock",
    "crash_watchdog.log", "recover_history.json", "autostart_services.log",
    "phpvm.ico", "phpvm.icns", "build", "dist",
}


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    print(f"[build] {msg}")


def _should_skip(name: str) -> bool:
    return (name in EXCLUDE_NAMES or name.endswith(".pyc") or name.endswith(".pyo"))


def _copy_sources(dest: str, extra: list[str]) -> None:
    """把运行所需源码复制到 dest（保持目录结构）。"""
    os.makedirs(dest, exist_ok=True)
    for name in ENTRY_FILES + extra:
        src = os.path.join(ROOT, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, name))
        else:
            _log(f"警告：缺少 {name}，已跳过")
    for pkg in PACKAGES:
        src = os.path.join(ROOT, pkg)
        if not os.path.isdir(src):
            continue
        shutil.copytree(
            src, os.path.join(dest, pkg), dirs_exist_ok=True,
            ignore=lambda _d, names: [n for n in names if _should_skip(n)],
        )


def _inject_version(dest: str, ver: str) -> None:
    """把版本号写进构建副本的 core/version.py（不改动仓库源码）。"""
    path = os.path.join(dest, "core", "version.py")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    new = re.sub(r'^APP_VERSION = ".*?"', f'APP_VERSION = "{ver}"',
                 text, count=1, flags=re.M)
    with open(path, "w", encoding="utf-8") as f:
        f.write(new)


def _default_config_text() -> str:
    """全新安装用的配置模板（与 core/config.py 的内置默认值一致）。"""
    from core.config import DEFAULT_PORTS, DEFAULT_SETTINGS

    return json.dumps({"ports": DEFAULT_PORTS, "settings": DEFAULT_SETTINGS},
                      ensure_ascii=False, indent=2) + "\n"


def _reset_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)


# --------------------------------------------------------------------------- #
# macOS
# --------------------------------------------------------------------------- #
LAUNCHER = """#!/bin/sh
# phpvm macOS 启动器：自动挑选带 tkinter 的 Python 3（Homebrew 优先）
HERE="$(cd "$(dirname "$0")/../Resources/phpvm" && pwd)"
for cand in \\
    /opt/homebrew/opt/python@3.13/bin/python3.13 \\
    /opt/homebrew/opt/python@3.12/bin/python3.12 \\
    /opt/homebrew/bin/python3 \\
    /usr/local/bin/python3 \\
    /usr/bin/python3 \\
    python3 ; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import tkinter" >/dev/null 2>&1; then
        exec "$cand" "$HERE/main.py" "$@"
    fi
done
MSG="phpvm 需要带 tkinter 的 Python 3（推荐：brew install python-tk@3.13）"
osascript -e "display alert \\"phpvm\\" message \\"$MSG\\"" >/dev/null 2>&1 || echo "$MSG" >&2
exit 1
"""

INFO_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>{name}</string>
    <key>CFBundleDisplayName</key><string>{name}</string>
    <key>CFBundleIdentifier</key><string>{app_id}</string>
    <key>CFBundleVersion</key><string>{version}</string>
    <key>CFBundleShortVersionString</key><string>{version}</string>
    <key>CFBundleExecutable</key><string>{name}</string>
    <key>CFBundleIconFile</key><string>{name}</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>LSMinimumSystemVersion</key><string>10.13</string>
    <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
"""


def _app_png(size: int) -> bytes:
    """生成应用图标 PNG（蓝底圆 + 白色 P，与 core/icon.py 的图案一致）。"""
    bg = (0x2B, 0x57, 0x9A, 0xFF)     # 主题蓝
    fg = (0xFF, 0xFF, 0xFF, 0xFF)     # 白
    clear = (0, 0, 0, 0)
    raw = bytearray()
    cx = cy = (size - 1) / 2.0
    radius = size * 0.47
    for y in range(size):
        raw.append(0)  # PNG 行过滤器：None
        for x in range(size):
            dx, dy = x - cx, y - cy
            if dx * dx + dy * dy > radius * radius:
                raw += bytes(clear)
                continue
            sx, sy = (x + 0.5) / size, (y + 0.5) / size
            is_p = ((0.34 <= sx <= 0.45 and 0.26 <= sy <= 0.74)
                    or (0.47 <= sx <= 0.68 and 0.26 <= sy <= 0.48))
            raw += bytes(fg if is_p else bg)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def _make_icns(stage: str) -> str | None:
    """用 iconutil 生成 phpvm.icns（非 macOS 或缺工具时返回 None）。"""
    if not shutil.which("iconutil"):
        return None
    iconset = os.path.join(stage, "phpvm.iconset")
    _reset_dir(iconset)
    specs = [(16, "16x16"), (32, "16x16@2x"), (32, "32x32"), (64, "32x32@2x"),
             (128, "128x128"), (256, "128x128@2x"), (256, "256x256"),
             (512, "256x256@2x"), (512, "512x512"), (1024, "512x512@2x")]
    for px, name in specs:
        with open(os.path.join(iconset, f"icon_{name}.png"), "wb") as f:
            f.write(_app_png(px))
    icns = os.path.join(stage, f"{APP_NAME}.icns")
    result = subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns],
                            capture_output=True, check=False)
    shutil.rmtree(iconset, ignore_errors=True)
    if result.returncode != 0 or not os.path.exists(icns):
        _log("iconutil 生成 icns 失败，跳过图标")
        return None
    return icns


def build_macos(ver: str, out_dir: str, with_dmg: bool = True) -> list[str]:
    """构建 phpvm.app（必要时打包为 dmg），返回产物路径列表。"""
    if sys.platform != "darwin":
        _log("警告：非 macOS 平台无法生成 dmg（仍会构建 .app 目录结构）")

    stage = os.path.join(BUILD, "macos")
    _reset_dir(stage)
    app = os.path.join(stage, f"{APP_NAME}.app")
    contents = os.path.join(app, "Contents")
    res_pkg = os.path.join(contents, "Resources", APP_NAME)
    os.makedirs(os.path.join(contents, "MacOS"), exist_ok=True)
    os.makedirs(os.path.join(contents, "Resources"), exist_ok=True)

    _copy_sources(res_pkg, POSIX_EXTRA)
    _inject_version(res_pkg, ver)

    launcher = os.path.join(contents, "MacOS", APP_NAME)
    with open(launcher, "w", encoding="utf-8") as f:
        f.write(LAUNCHER)
    os.chmod(launcher, 0o755)

    with open(os.path.join(contents, "Info.plist"), "w", encoding="utf-8") as f:
        f.write(INFO_PLIST.format(name=APP_NAME, app_id=APP_ID, version=ver))

    icns = _make_icns(stage)
    if icns:
        shutil.copy2(icns, os.path.join(contents, "Resources", f"{APP_NAME}.icns"))

    os.makedirs(out_dir, exist_ok=True)
    out_app_dir = os.path.join(out_dir, f"{APP_NAME}-{ver}-macos")
    shutil.rmtree(out_app_dir, ignore_errors=True)
    os.makedirs(out_app_dir, exist_ok=True)
    shutil.copytree(app, os.path.join(out_app_dir, f"{APP_NAME}.app"), symlinks=True)
    outputs = [os.path.join(out_app_dir, f"{APP_NAME}.app")]
    _log(f"已构建 {out_app_dir}/{APP_NAME}.app")

    if not with_dmg or not shutil.which("hdiutil"):
        if with_dmg:
            _log("未找到 hdiutil，跳过 dmg（需在 macOS 上执行）")
        return outputs

    # dmg 暂存目录：附上 /Applications 快捷方式，便于拖拽安装
    dmg_stage = os.path.join(stage, "dmg")
    _reset_dir(dmg_stage)
    shutil.copytree(app, os.path.join(dmg_stage, f"{APP_NAME}.app"), symlinks=True)
    link = os.path.join(dmg_stage, "Applications")
    try:
        os.symlink("/Applications", link)
    except OSError:
        pass

    dmg = os.path.join(out_dir, asset_name("macos", ver))
    if os.path.exists(dmg):
        os.remove(dmg)
    result = subprocess.run(
        ["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", dmg_stage,
         "-ov", "-format", "UDZO", dmg],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        _log("hdiutil 生成 dmg 失败：" + result.stderr.decode("utf-8", "replace").strip())
        return outputs
    outputs.append(dmg)
    _log(f"已构建 {dmg}")
    return outputs


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #
def build_windows(ver: str, out_dir: str) -> list[str]:
    """构建 Windows 免安装目录与 zip；若装了 Inno Setup 则编译安装程序。"""
    stage = os.path.join(BUILD, "windows")
    _reset_dir(stage)
    _copy_sources(stage, WIN_EXTRA)
    _inject_version(stage, ver)
    # 全新安装的配置模板（升级时由 Inno Setup 的 onlyifdoesntexist 保护）
    with open(os.path.join(stage, "config.json"), "w", encoding="utf-8") as f:
        f.write(_default_config_text())

    os.makedirs(out_dir, exist_ok=True)
    out_win_dir = os.path.join(out_dir, f"{APP_NAME}-{ver}-windows")
    shutil.rmtree(out_win_dir, ignore_errors=True)
    shutil.copytree(stage, out_win_dir, symlinks=True)
    _log(f"已构建 {out_win_dir}")

    outputs = [out_win_dir]
    zip_path = shutil.make_archive(out_win_dir, "zip", root_dir=out_dir,
                                   base_dir=f"{APP_NAME}-{ver}-windows")
    outputs.append(zip_path)
    _log(f"已构建 {zip_path}")

    iscc = shutil.which("iscc") or shutil.which("ISCC")
    if not iscc:
        _log("未找到 Inno Setup 编译器（iscc）：已跳过 setup.exe，"
             "请在 Windows 上安装 Inno Setup 6 后重新执行本命令")
        return outputs

    iss = os.path.join(ROOT, "packaging", f"{APP_NAME}.iss")
    result = subprocess.run(
        [iscc, f"/DMyAppVersion={ver}", f"/DSourceDir={out_win_dir}",
         f"/DOutputDir={out_dir}", iss],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        _log("iscc 编译失败：" + result.stderr.decode("utf-8", "replace").strip())
        return outputs
    setup = os.path.join(out_dir, asset_name("windows", ver))
    if os.path.exists(setup):
        outputs.append(setup)
        _log(f"已构建 {setup}")
    return outputs


# --------------------------------------------------------------------------- #
# 校验和清单
# --------------------------------------------------------------------------- #
def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_sums(out_dir: str) -> str:
    """为 dist 下的安装包产物生成 SHA256SUMS.txt（自动升级据此校验）。"""
    names = []
    for name in sorted(os.listdir(out_dir)):
        path = os.path.join(out_dir, name)
        if not os.path.isfile(path):
            continue
        if name == SUMS_ASSET or not name.startswith(APP_NAME + "-"):
            continue
        if name.endswith((".dmg", ".exe", ".zip", ".tar.gz")):
            names.append(name)
    lines = [f"{_sha256(os.path.join(out_dir, n))}  {n}" for n in names]
    target = os.path.join(out_dir, SUMS_ASSET)
    with open(target, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    _log(f"已写入 {target}（{len(lines)} 项）")
    return target


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build.py", description="phpvm 安装包构建（macOS / Windows）")
    parser.add_argument("target", choices=["macos", "windows", "all", "sums"],
                        help="构建目标")
    parser.add_argument("--version", default=os.environ.get("PHPVM_BUILD_VERSION")
                        or APP_VERSION, help="版本号（默认取 core/version.py）")
    parser.add_argument("--dist", default=DIST, help="产物输出目录")
    parser.add_argument("--no-dmg", action="store_true", help="macOS 不生成 dmg")
    args = parser.parse_args(argv)

    ver = args.version.strip().lstrip("v")
    out_dir = os.path.abspath(args.dist)
    os.makedirs(out_dir, exist_ok=True)
    _log(f"版本 {ver}，产物目录 {out_dir}")

    if args.target in ("macos", "all"):
        build_macos(ver, out_dir, with_dmg=not args.no_dmg)
    if args.target in ("windows", "all"):
        build_windows(ver, out_dir)
    write_sums(out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
