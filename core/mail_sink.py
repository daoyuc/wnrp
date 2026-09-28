# -*- coding: utf-8 -*-
"""邮件捕获（F11 · Windows 路径）：跑在 127.0.0.1 上的极简 SMTP sink。

为什么需要它：官方 php.ini 明确 ``sendmail_path`` **仅 Unix 有效**，Windows 的
``mail()`` 只能走 SMTP（``SMTP`` / ``smtp_port``）。POSIX 侧的垫片脚本
（见 ``core/mail_catcher.py``）在 Windows 无从落地，因此这里提供一个只收不发的
最小 SMTP 服务，把邮件原文写进捕获目录（``.eml``，与 POSIX 侧同一目录、同一阅读页）。

取舍：
- 只绑定回环地址 ``127.0.0.1``，只在「开启捕获」后运行；无认证、无 TLS —— 本机
  开发自用，不对局域网暴露；
- 生命周期跟随 phpvm 主进程（daemon 线程 + 关闭时 stop）：phpvm 没运行时 PHP
  发信会连接失败，界面上有明确提示；
- 只用标准库 ``socketserver``，零第三方依赖、零外部二进制（不托管 Mailpit 之类）。
"""
import atexit
import email.utils
import os
import socket
import socketserver
import threading
import time

from .i18n import t

DEFAULT_PORT = 1025

_server = None
_thread = None
#: 端口已被**另一个 phpvm 进程**的 sink 占用时记下端口（本进程不持有 server，
#: 但状态显示 / enable 判定都应视为「已在运行」）
_external_port = None
_lock = threading.Lock()
_seq = 0
_atexit_installed = False


def _install_atexit() -> None:
    """首次启动时登记退出钩子：任何退出路径（GUI / CLI / 无界面）都关掉监听。"""
    global _atexit_installed
    if not _atexit_installed:
        atexit.register(stop)
        _atexit_installed = True


def _mail_dir() -> str:
    """捕获目录（延迟导入 ``mail_catcher`` 以避免循环依赖）。"""
    from . import mail_catcher

    return mail_catcher.mail_dir()


def _next_name() -> str:
    global _seq
    with _lock:
        _seq += 1
        n = _seq
    return "%s-%d-%d.eml" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid(), n)


def _store(envelope_from: str, envelope_to: list[str], lines: list[str]) -> str:
    """把一封邮件写成 .eml（补 Received / 信封头，便于回溯是谁发给谁的）。"""
    head = [
        "Received: from phpvm-mail-sink (127.0.0.1)",
        "\tby phpvm; %s" % email.utils.formatdate(localtime=True),
        "X-phpvm-Envelope-From: %s" % envelope_from,
        "X-phpvm-Envelope-To: %s" % ", ".join(envelope_to),
    ]
    body = "\r\n".join(lines)
    path = os.path.join(_mail_dir(), _next_name())
    with open(path, "wb") as f:
        f.write(("\r\n".join(head) + "\r\n" + body).encode("utf-8", "replace"))
    return path


class _Handler(socketserver.StreamRequestHandler):
    """最小 SMTP 会话：EHLO/HELO → MAIL → RCPT → DATA → QUIT。"""

    def _reply(self, text: str) -> None:
        self.wfile.write((text + "\r\n").encode("ascii", "replace"))

    def handle(self):  # noqa: C901 - SMTP 分支本身就是一段状态机
        self._reply("220 phpvm mail sink ready")
        sender = ""
        rcpts: list[str] = []
        data: list[str] = []
        in_data = False
        while True:
            raw = self.rfile.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if in_data:
                if line == ".":  # DATA 结束：落盘并确认
                    try:
                        _store(sender, rcpts, data)
                    except OSError:
                        self._reply("451 local error, try again later")
                    else:
                        self._reply("250 OK queued")
                    sender, rcpts, data, in_data = "", [], [], False
                    continue
                data.append(line[1:] if line.startswith("..") else line)  # 还原 dot-stuffing
                continue
            cmd, _, arg = line.partition(" ")
            cmd = cmd.upper()
            if cmd == "EHLO":
                # 多行响应：末行用 250 空格，其余用 250-
                self._reply("250-%s" % socket.gethostname())
                self._reply("250 OK")
            elif cmd == "HELO":
                self._reply("250 %s" % socket.gethostname())
            elif cmd == "MAIL":
                sender = arg.partition(":")[2].strip()
                self._reply("250 OK")
            elif cmd == "RCPT":
                rcpts.append(arg.partition(":")[2].strip())
                self._reply("250 OK")
            elif cmd == "DATA":
                in_data = True
                self._reply("354 End data with <CR><LF>.<CR><LF>")
            elif cmd == "RSET":
                sender, rcpts, data, in_data = "", [], [], False
                self._reply("250 OK")
            elif cmd == "NOOP":
                self._reply("250 OK")
            elif cmd == "QUIT":
                self._reply("221 Bye")
                break
            else:  # 不认识的命令一律 250，别把 PHP 的探测卡死
                self._reply("250 OK")

    def handle_error(self, *_args) -> None:  # pragma: no cover - 单连接异常不应影响服务
        pass


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def running() -> bool:
    """sink 是否在运行（含「本进程之外」已由另一个 phpvm 进程监听着的情况）。"""
    return _server is not None or _external_port is not None


def bound_port() -> int:
    """当前实际监听端口（未运行时为 0）。"""
    if _server is None:
        return int(_external_port or 0)
    try:
        return int(_server.server_address[1])
    except (IndexError, TypeError):  # pragma: no cover - 防御
        return 0


def probe(port: int, timeout: float = 0.5) -> bool:
    """端口上是否已有**本工具的** sink 在监听。

    凭 SMTP 打招呼横幅识别，避免把别的服务（甚至别的 SMTP 服务器）误当成自己的；
    典型场景：phpvm GUI 里已启动 sink，随后命令行再执行 ``mail enable``。
    """
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            banner = s.recv(128).decode("ascii", "replace")
    except OSError:
        return False
    return "phpvm mail sink" in banner


def start(port: int = DEFAULT_PORT) -> tuple[bool, str]:
    """启动 sink；已在运行（含另一进程的 sink）则直接返回成功。返回 ``(ok, 消息)``。

    ``port=0`` 时由系统分配空闲端口（测试用），实际端口见 :func:`bound_port`。
    """
    global _server, _thread, _external_port
    with _lock:
        if _server is not None or _external_port is not None:
            return True, t("邮件捕获服务已在运行（127.0.0.1:{port}）", port=bound_port())
        try:
            srv = _Server(("127.0.0.1", int(port)), _Handler)
        except OSError as e:
            if int(port) > 0 and probe(int(port)):
                _external_port = int(port)   # 另一个 phpvm 进程已在收信
                return True, t("邮件捕获服务已在运行（127.0.0.1:{port}）", port=int(port))
            return False, t("无法监听 127.0.0.1:{port}：{err}", port=port, err=e)
        _server = srv
        _install_atexit()
        _thread = threading.Thread(target=srv.serve_forever, name="phpvm-mail-sink",
                                   daemon=True)
        _thread.start()
    return True, t("邮件捕获服务已启动：127.0.0.1:{port}（随 phpvm 运行，关闭 phpvm 即停止）",
                   port=bound_port())


def stop() -> tuple[bool, str]:
    """停止本进程启动的 sink（phpvm 退出时调用；未运行则无动作）。"""
    global _server, _thread, _external_port
    with _lock:
        srv, _server, _thread, _external_port = _server, None, None, None
    if srv is None:
        return True, t("邮件捕获服务未在运行")
    try:
        srv.shutdown()
        srv.server_close()
    except Exception:  # noqa: BLE001 - 关闭阶段的异常不值得上抛
        pass
    return True, t("已停止邮件捕获服务")
