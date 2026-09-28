# -*- coding: utf-8 -*-
"""邮件捕获（F11）：把 PHP ``mail()`` 发出的邮件落盘为 ``.eml``，供「邮件」页查看。

两种落地方式（同一捕获目录、同一阅读页）：

- **Unix（``sendmail`` 模式）**：``sendmail_path`` 指向一个只依赖 POSIX shell 的脚本，
  脚本从 stdin 读邮件原文落盘。零常驻进程、零端口占用。
- **Windows（``smtp`` 模式）**：官方 php.ini 明确 ``sendmail_path`` **For Unix only**，
  ``mail()`` 只能走 SMTP，因此把 ``SMTP`` / ``smtp_port`` 指向
  :mod:`core.mail_sink`（127.0.0.1 上的极简 sink，随 phpvm 运行）。

共同约定：改动 php.ini 一律「备份 → 改 → 记住原值」，:func:`disable` 可完整还原。
"""
import email
import os
import re
from email import policy

from . import app_paths, file_backup, mail_sink
from .config import IS_WIN
from .i18n import t

MAIL_DIR_NAME = "mail"
SHIM_NAME = "phpvm-sendmail.sh"

#: 我们写入 ini 的标记行（成对出现：标记 + sendmail_path）
MARK = "; >>> phpvm mail catcher >>>"
#: Windows（SMTP 模式）的标记块：带结束标记，便于整块摘除
SMTP_MARK = "; >>> phpvm mail catcher (SMTP) >>>"
SMTP_END_MARK = "; <<< phpvm mail catcher (SMTP) <<<"
#: 记录用户原有取值，便于 disable 还原（sendmail 模式：原 sendmail_path；SMTP 模式：key=value）
ORIG_MARK = "; phpvm-mail-catcher-original: "

_RE_SENDMAIL = re.compile(r"^(\s*)(;?)\s*sendmail_path\s*=\s*(.*)$", re.IGNORECASE)
_RE_SMTP = re.compile(r"^(\s*)(;?)\s*SMTP\s*=\s*(.*)$", re.IGNORECASE)
_RE_SMTP_PORT = re.compile(r"^(\s*)(;?)\s*smtp_port\s*=\s*(.*)$", re.IGNORECASE)
_RE_FROM = re.compile(r"^(\s*)(;?)\s*sendmail_from\s*=\s*(.*)$", re.IGNORECASE)
#: SMTP 模式要写入 / 还原的键（顺序即写回顺序）
_SMTP_KEYS = ("SMTP", "smtp_port", "sendmail_from")
#: Windows 的 mail() 需要一个默认发件人，否则报 "Bad Message Return Path"
_SMTP_FROM = "dev@phpvm.local"

_cfg_cache = None

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
    """当前平台是否可用。

    Unix 用 ``sendmail_path`` 垫片，Windows 用 :mod:`core.mail_sink`（SMTP），
    两条路径都可用，故恒为 True；具体模式见 :func:`mode`。
    """
    return True


def mode() -> str:
    """当前生效的捕获方式：``sendmail``（Unix）/ ``smtp``（Windows）。"""
    return "smtp" if IS_WIN else "sendmail"


def smtp_port() -> int:
    """SMTP sink 端口（``settings.mail_sink_port``，默认 1025）。"""
    global _cfg_cache
    if _cfg_cache is None:
        try:
            from .config import Config

            _cfg_cache = Config()
        except Exception:  # noqa: BLE001 - 读不到配置就退回默认端口
            _cfg_cache = False
    if _cfg_cache:
        try:
            return int(_cfg_cache.get_setting("mail_sink_port", mail_sink.DEFAULT_PORT))
        except (TypeError, ValueError):
            pass
    return mail_sink.DEFAULT_PORT


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
    """写入 / 覆盖垫片脚本并置可执行位。返回 ``(ok, 路径或错误信息)``。

    仅 ``sendmail`` 模式（Unix）适用；Windows 走 SMTP sink，不需要垫片。
    """
    if mode() != "sendmail":
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


def active_smtp(ini: str) -> tuple[str, str]:
    """ini 中**生效**的 ``(SMTP, smtp_port)``（未设置 / 仅注释时为空串）。"""
    host = port = ""
    try:
        with open(ini, "rb") as f:
            text = f.read().decode("latin-1")
    except OSError:
        return "", ""
    for line in text.splitlines():
        s = line.strip()
        if not host:
            m = _RE_SMTP.match(s)
            if m and not m.group(2):
                host = m.group(3).strip()
                continue
        if not port:
            m = _RE_SMTP_PORT.match(s)
            if m and not m.group(2):
                port = m.group(3).strip()
    return host, port


def status(v) -> dict:
    """该版本的邮件捕获状态（只读）。"""
    ini = getattr(v, "ini", "") or ""
    exists = bool(ini) and os.path.exists(ini)
    cur = active_sendmail(ini) if exists else ""
    host, port = active_smtp(ini) if exists else ("", "")
    if mode() == "smtp":
        enabled = host.strip().lower() in ("127.0.0.1", "localhost") \
            and port.strip() == str(smtp_port())
        current = t("SMTP {host}:{port}", host=host, port=port) if host or port else ""
    else:
        enabled = bool(_norm(cur)) and _norm(cur) == _norm(shim_path())
        current = cur
    return {
        "supported": supported(),
        "mode": mode(),
        "installed": is_installed(),
        "shim": shim_path(),
        "mail_dir": mail_dir(),
        "ini": ini,
        "ini_exists": exists,
        "current": current,
        "enabled": enabled,
        "sink_running": mail_sink.running() if mode() == "smtp" else False,
        "sink_port": smtp_port() if mode() == "smtp" else 0,
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


def _transform_smtp(ini: str, on: bool) -> tuple[bool, str, str]:
    """Windows：重写 ini 的 ``SMTP`` / ``smtp_port`` 指向本机 sink。

    与 sendmail 版同样保留用户原值（写成 ``; phpvm-mail-catcher-original: KEY=VALUE``），
    :func:`disable` 时原样还原。返回 ``(ok, 消息, 备份路径)``。
    """
    try:
        with open(ini, "rb") as f:
            text = f.read().decode("latin-1")
    except OSError as e:
        return False, t("读取失败：{err}", err=e), ""
    out: list[str] = []
    originals: dict[str, str] = {}
    in_block = False
    for line in text.splitlines():
        s = line.strip()
        if in_block:                      # 标记块内全部丢弃，直到结束标记
            if s == SMTP_END_MARK:
                in_block = False
            continue
        if s == SMTP_MARK:
            in_block = True
            continue
        if s.startswith(ORIG_MARK):
            key, _, value = s[len(ORIG_MARK):].strip().partition("=")
            if key.strip() in _SMTP_KEYS:
                originals[key.strip()] = value.strip()
                continue
            out.append(line)
            continue
        for key, rx in (("SMTP", _RE_SMTP), ("smtp_port", _RE_SMTP_PORT),
                        ("sendmail_from", _RE_FROM)):
            m = rx.match(s)
            if m and not m.group(2):      # 生效中的同名键：摘掉并记住原值
                originals.setdefault(key, m.group(3).strip())
                break
        else:
            out.append(line)
    if on:
        for key in _SMTP_KEYS:
            if key in originals:
                out.append(f"{ORIG_MARK}{key}={originals[key]}")
        out.append(SMTP_MARK)
        out.append("SMTP = 127.0.0.1")
        out.append(f"smtp_port = {smtp_port()}")
        out.append(f"sendmail_from = {_SMTP_FROM}")
        out.append(SMTP_END_MARK)
    else:
        for key in _SMTP_KEYS:
            if key in originals:
                out.append(f"{key} = {originals[key]}")
    return _rewrite(ini, out)


def ensure_sink(versions) -> tuple[bool, str] | None:
    """Windows：任一版本已开启捕获而 sink 未运行时自动拉起它。

    返回 ``None`` 表示无需动作（非 Windows / 已在运行 / 没有任何版本开启）。
    """
    if mode() != "smtp" or mail_sink.running():
        return None
    try:
        need = any(status(v)["enabled"] for v in (versions or []))
    except Exception:  # noqa: BLE001 - 状态探测失败不应阻塞界面
        return None
    if not need:
        return None
    return mail_sink.start(smtp_port())


def enable(v) -> tuple[bool, str, str]:
    """开启该版本的邮件捕获。返回 ``(ok, 消息, 备份路径)``。"""
    ini = getattr(v, "ini", "") or ""
    if not ini or not os.path.exists(ini):
        return False, t("[{name}] 还没有生效的 php.ini，请先生成后再开启邮件捕获。",
                        name=getattr(v, "name", "?")), ""
    name = getattr(v, "name", "?")
    if mode() == "smtp":
        ok, msg = mail_sink.start(smtp_port())
        if not ok:
            return False, msg, ""
        ok, msg, backup = _transform_smtp(ini, True)
        if not ok:
            return False, msg, ""
        return True, t("已开启邮件捕获：邮件将写入 {dir}\n"
                       "（SMTP 127.0.0.1:{port}，重启 [{name}] 后生效；"
                       "捕获服务随 phpvm 运行，phpvm 关闭期间 PHP 发信会失败）",
                       dir=mail_dir(), port=smtp_port(), name=name), backup
    ok, msg = install_shim()
    if not ok:
        return False, msg, ""
    ok, msg, backup = _transform(ini, shim_path(), True)
    if not ok:
        return False, msg, ""
    return True, t("已开启邮件捕获：邮件将写入 {dir}\n（重启 [{name}] 后生效）",
                   dir=mail_dir(), name=name), backup


def disable(v) -> tuple[bool, str, str]:
    """关闭该版本的邮件捕获并还原原有设置。返回 ``(ok, 消息, 备份路径)``。"""
    ini = getattr(v, "ini", "") or ""
    if not ini or not os.path.exists(ini):
        return False, t("配置文件不存在：{path}", path=ini or "—"), ""
    if mode() == "smtp":
        host, port = active_smtp(ini)
        if not (host.strip().lower() in ("127.0.0.1", "localhost")
                and port.strip() == str(smtp_port())):
            return True, t("该版本未开启邮件捕获，无需操作。"), ""
        ok, msg, backup = _transform_smtp(ini, False)
        if not ok:
            return False, msg, ""
        return True, t("已关闭邮件捕获并还原原有设置（重启生效）。"), backup
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
