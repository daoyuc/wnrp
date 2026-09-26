# -*- coding: utf-8 -*-
"""邮件捕获（F11）：把 PHP ``mail()`` 发出的邮件落盘为 ``.eml``，供「邮件」页查看。

实现取舍（与规划一致）：**不托管 Mailpit 之类的外部二进制**，改用 PHP 的
``sendmail_path`` 指向一个只依赖 POSIX shell 的脚本 —— 脚本从 stdin 读取邮件原文，
写成 ``<数据目录>/mail/<时间戳>-<pid>.eml``。零外部依赖、零常驻进程、零端口占用。

平台边界（重要）：
- 官方 php.ini 明确 ``sendmail_path`` **For Unix only**；Windows 下 ``mail()`` 走
  SMTP（``SMTP`` / ``smtp_port``），本方案不适用 —— 相关操作会直接拒绝并说明原因。
- 改动 php.ini 走「备份 → 改 → 保留原值」，:func:`disable` 可完整还原用户原有设置。
"""
import email
import os
import re
from email import policy

from . import app_paths, file_backup
from .config import IS_WIN
from .i18n import t

MAIL_DIR_NAME = "mail"
SHIM_NAME = "phpvm-sendmail.sh"

#: 我们写入 ini 的标记行（成对出现：标记 + sendmail_path）
MARK = "; >>> phpvm mail catcher >>>"
#: 记录用户原有 sendmail_path，便于 disable 还原
ORIG_MARK = "; phpvm-mail-catcher-original: "

_RE_SENDMAIL = re.compile(r"^(\s*)(;?)\s*sendmail_path\s*=\s*(.*)$", re.IGNORECASE)

#: 从 stdin 读邮件并落盘的最小脚本（占位符 __PHPVM_MAIL_DIR__ 在安装时替换）
_SHIM = """#!/bin/sh
# phpvm 邮件捕获（F11）：PHP mail() 经 sendmail_path 调用本脚本，
# 把邮件原文（头部 + 正文）落盘为 .eml，供 phpvm「邮件」页查看。
# 仅用 POSIX shell，无外部依赖；Windows 不适用（mail() 走 SMTP）。
dir="__PHPVM_MAIL_DIR__"
mkdir -p "$dir" 2>/dev/null
umask 022
f="$dir/$(date +%Y%m%d-%H%M%S)-$$.eml"
cat > "$f"
"""


def supported() -> bool:
    """当前平台是否可用（``sendmail_path`` 仅 Unix 有效）。"""
    return not IS_WIN


def mail_dir() -> str:
    """捕获目录（``<数据目录>/mail``，必要时创建）。"""
    base = app_paths.data_file(MAIL_DIR_NAME)
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        pass
    return base


def shim_path() -> str:
    """sendmail 垫片脚本路径。"""
    return app_paths.data_file(SHIM_NAME)


def _sh_escape(path: str) -> str:
    """转义为可直接放进双引号 shell 字符串的形式。"""
    for ch in ("\\", '"', "`", "$"):
        path = path.replace(ch, "\\" + ch)
    return path


def install_shim() -> tuple[bool, str]:
    """写入 / 覆盖垫片脚本并置可执行位。返回 ``(ok, 路径或错误信息)``。"""
    if not supported():
        return False, t("sendmail_path 仅 Unix 有效（Windows 的 mail() 走 SMTP），暂不支持。")
    path = shim_path()
    body = _SHIM.replace("__PHPVM_MAIL_DIR__", _sh_escape(mail_dir()))
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        os.chmod(path, 0o755)
    except OSError as e:
        return False, t("写入失败：{err}", err=e)
    return True, path


def is_installed() -> bool:
    return os.path.exists(shim_path())


def _shim_value(shim: str) -> str:
    """ini 里的取值：路径含空格时加引号（PHP 会去掉外层引号）。"""
    return f'"{shim}"' if " " in shim else shim


def _norm(value: str) -> str:
    v = (value or "").strip().strip('"').strip("'")
    return os.path.normcase(os.path.abspath(v)) if v else ""


def active_sendmail(ini: str) -> str:
    """ini 中**生效**的 sendmail_path 取值（未设置 / 仅注释时返回空串）。"""
    try:
        with open(ini, "rb") as f:
            text = f.read().decode("latin-1")
    except OSError:
        return ""
    for line in text.splitlines():
        m = _RE_SENDMAIL.match(line.strip())
        if m and not m.group(2):
            return m.group(3).strip()
    return ""


def status(v) -> dict:
    """该版本的邮件捕获状态（只读）。"""
    ini = getattr(v, "ini", "") or ""
    cur = active_sendmail(ini) if ini and os.path.exists(ini) else ""
    return {
        "supported": supported(),
        "installed": is_installed(),
        "shim": shim_path(),
        "mail_dir": mail_dir(),
        "ini": ini,
        "ini_exists": bool(ini) and os.path.exists(ini),
        "current": cur,
        "enabled": bool(_norm(cur)) and _norm(cur) == _norm(shim_path()),
        "count": len(list_mails(limit=100000)),
    }


def _rewrite(ini: str, out_lines: list[str]) -> tuple[bool, str, str]:
    """备份后写回 ini。返回 ``(ok, 消息, 备份路径)``。"""
    try:
        with open(ini, "rb") as f:
            raw = f.read()
    except OSError as e:
        return False, t("读取失败：{err}", err=e), ""
    eol = "\r\n" if b"\r\n" in raw else "\n"
    text = eol.join(out_lines) + eol
    try:
        backup = file_backup.backup(ini)
    except OSError as e:
        return False, t("备份失败，未做修改：{err}", err=e), ""
    try:
        with open(ini, "wb") as f:
            f.write(text.encode("latin-1"))
    except OSError as e:
        return False, t("写入失败：{err}", err=e), backup
    return True, "", backup


def _transform(ini: str, shim: str, on: bool) -> tuple[bool, str, str]:
    """重写 ini 的 sendmail_path（保留其它行原样，并记住 / 还原用户原值）。

    返回 ``(ok, 消息, 备份路径)``：成功时消息为空。
    """
    try:
        with open(ini, "rb") as f:
            text = f.read().decode("latin-1")
    except OSError as e:
        return False, t("读取失败：{err}", err=e), ""
    out: list[str] = []
    in_mark = False
    original = None
    for line in text.splitlines():
        s = line.strip()
        if in_mark:                       # 标记行的下一行就是我们写的取值
            in_mark = False
            continue
        if s == MARK:
            in_mark = True
            continue
        if s.startswith(ORIG_MARK):
            original = s[len(ORIG_MARK):].strip()
            continue
        m = _RE_SENDMAIL.match(s)
        if m and not m.group(2):          # 生效中的 sendmail_path：先摘掉
            if original is None:
                original = m.group(3).strip()
            continue
        out.append(line)
    if on:
        if original is not None:
            out.append(ORIG_MARK + original)
        out.append(MARK)
        out.append(f"sendmail_path = {_shim_value(shim)}")
    elif original is not None:
        out.append(f"sendmail_path = {original}")
    return _rewrite(ini, out)


def enable(v) -> tuple[bool, str, str]:
    """开启该版本的邮件捕获。返回 ``(ok, 消息, 备份路径)``。"""
    if not supported():
        return False, t("sendmail_path 仅 Unix 有效（Windows 的 mail() 走 SMTP），暂不支持。"), ""
    ini = getattr(v, "ini", "") or ""
    if not ini or not os.path.exists(ini):
        return False, t("[{name}] 还没有生效的 php.ini，请先生成后再开启邮件捕获。",
                        name=getattr(v, "name", "?")), ""
    ok, msg = install_shim()
    if not ok:
        return False, msg, ""
    ok, msg, backup = _transform(ini, shim_path(), True)
    if not ok:
        return False, msg, ""
    return True, t("已开启邮件捕获：邮件将写入 {dir}\n（重启 [{name}] 后生效）",
                   dir=mail_dir(), name=getattr(v, "name", "?")), backup


def disable(v) -> tuple[bool, str, str]:
    """关闭该版本的邮件捕获并还原原有 sendmail_path。返回 ``(ok, 消息, 备份路径)``。"""
    ini = getattr(v, "ini", "") or ""
    if not ini or not os.path.exists(ini):
        return False, t("配置文件不存在：{path}", path=ini or "—"), ""
    if _norm(active_sendmail(ini)) != _norm(shim_path()):
        return True, t("该版本未开启邮件捕获，无需操作。"), ""
    ok, msg, backup = _transform(ini, shim_path(), False)
    if not ok:
        return False, msg, ""
    return True, t("已关闭邮件捕获并还原原有设置（重启生效）。"), backup


# --------------------------------------------------------------------------- #
# 读取捕获到的邮件
# --------------------------------------------------------------------------- #
def list_mails(limit: int = 200) -> list[dict]:
    """按时间倒序列出捕获到的邮件（解析常用头部；坏文件跳过不报错）。"""
    base = mail_dir()
    try:
        names = [n for n in os.listdir(base) if n.endswith(".eml")]
    except OSError:
        return []
    out: list[dict] = []
    for name in names:
        p = os.path.join(base, name)
        try:
            st = os.stat(p)
            with open(p, "rb") as f:
                msg = email.message_from_binary_file(f, policy=policy.default)
        except Exception:  # noqa: BLE001 —— 单个坏文件不应影响列表
            continue
        out.append({
            "file": p,
            "name": name,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "from": str(msg.get("From", "")),
            "to": str(msg.get("To", "")),
            "subject": str(msg.get("Subject", "")),
            "date": str(msg.get("Date", "")),
        })
    out.sort(key=lambda m: m["mtime"], reverse=True)
    return out[:limit]


def read_mail(path: str) -> str:
    """读取单封邮件的原文（.eml 内容，供详情区展示）。"""
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", errors="replace")
    except OSError as e:
        return t("读取失败：{err}", err=e)


def clear_mails() -> int:
    """删除全部捕获到的邮件，返回删除数量。"""
    base = mail_dir()
    n = 0
    try:
        names = list(os.listdir(base))
    except OSError:
        return 0
    for name in names:
        if not name.endswith(".eml"):
            continue
        try:
            os.remove(os.path.join(base, name))
            n += 1
        except OSError:
            pass
    return n
