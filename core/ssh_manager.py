# -*- coding: utf-8 -*-
"""SSH 连接与远端执行（唯一接触 paramiko 的模块）。

为什么延迟导入 paramiko
----------------------
``cli.py`` 与 ``AGENTS.md`` 都承诺「不装第三方依赖也能跑其余功能」。若在本模块
顶部 import，未装依赖时 GUI 启动与全部 CLI 命令都会崩；因此只在真正要连主机时
才 import，缺失时抛 :class:`ParamikoMissing`，由上层转成可读错误。

主机指纹
--------
默认 ``RejectPolicy`` + 数据目录下的 ``ssh_known_hosts``：首次连接抛
:class:`HostKeyNotTrusted`（带算法与 SHA256 指纹），必须由用户显式确认
（``trust=True`` / 面板二次确认）后才写入，不做静默 ``AutoAddPolicy``。

密码边界
--------
密码 / 口令只交给 paramiko 的 ``connect()``；交互式终端只用 ``ssh`` 命令，
密码由终端自己提示输入，**绝不出现在命令行**（否则会进进程列表与回显）。
"""
import base64
import hashlib
import os
import shlex
import socket
import subprocess
import sys
import time

from . import app_paths, process_utils, ssh_store
from .i18n import t

IS_WIN = sys.platform.startswith("win")
KNOWN_HOSTS_FILE = "ssh_known_hosts"
DEFAULT_TIMEOUT = 10.0
DEFAULT_EXEC_TIMEOUT = 30.0
DEFAULT_MAX_BYTES = 256 * 1024
INSTALL_HINT = "pip install -r requirements.txt"


class ParamikoMissing(RuntimeError):
    """未安装 paramiko（上层据此给出安装引导，而不是崩栈）。"""

    def __init__(self) -> None:
        super().__init__(
            t("缺少依赖 paramiko，请先安装：{cmd}", cmd=INSTALL_HINT))


class HostKeyNotTrusted(RuntimeError):
    """主机指纹不在 known_hosts 中，需要用户显式确认。"""

    def __init__(self, hostname: str, key_type: str, fingerprint: str) -> None:
        super().__init__(t("主机指纹尚未信任：{host}", host=hostname))
        self.hostname = hostname
        self.key_type = key_type
        self.fingerprint = fingerprint


class MissingCredential(RuntimeError):
    """主机已保存但还没填凭据（保存与连通性解耦，连接时才提示补齐）。"""

    def __init__(self, host: dict) -> None:
        self.kind = ssh_store.missing_secret(host)
        if self.kind == "key":
            msg = t("这台主机还没有选择私钥文件，请先在「编辑主机」里补上")
        else:
            msg = t("这台主机还没有保存密码，请先在「编辑主机」里补上")
        super().__init__(msg)


def known_hosts_path() -> str:
    """指纹库路径（与主机凭据同目录，同样收紧权限）。"""
    env = (os.environ.get("PHPVM_SSH_KNOWN_HOSTS") or "").strip()
    return os.path.expanduser(env) if env else app_paths.data_file(KNOWN_HOSTS_FILE)


def _paramiko():
    """延迟导入 paramiko；缺失时抛 ParamikoMissing。"""
    try:
        import paramiko  # noqa: PLC0415 - 必须延迟，见模块 docstring
    except ImportError as e:  # pragma: no cover - 取决于环境
        raise ParamikoMissing() from e
    return paramiko


def available() -> bool:
    """paramiko 是否可用（面板据此显示安装引导，不抛错）。"""
    try:
        _paramiko()
    except ParamikoMissing:
        return False
    return True


def install_hint() -> str:
    return INSTALL_HINT


def fingerprint(key) -> str:
    """公钥的 SHA256 指纹（``SHA256:`` + base64，去掉尾部 =）。"""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def target_label(host: dict) -> str:
    """``user@address:port`` 展示串。"""
    port = int(host.get("port", 22) or 22)
    return f"{host.get('user', '')}@{host.get('host', '')}:{port}"


def _host_key_name(host: dict) -> str:
    """known_hosts 里的主机键（非 22 端口写 ``[host]:port``）。"""
    address = str(host.get("host", ""))
    port = int(host.get("port", 22) or 22)
    return address if port == 22 else f"[{address}]:{port}"


class _RejectWithKey:
    """paramiko 缺失主机键策略：记录后抛专用异常（鸭子类型即可）。"""

    def missing_host_key(self, client, hostname, key):  # noqa: ARG002
        raise HostKeyNotTrusted(hostname, key.get_name(), fingerprint(key))


def _new_client(trust: bool):
    """构造 SSHClient：加载自有 known_hosts，按 trust 决定是否自动接受。"""
    p = _paramiko()
    client = p.SSHClient()
    path = known_hosts_path()
    if os.path.isfile(path):
        try:
            client.load_host_keys(path)
        except Exception:  # noqa: BLE001 - 文件损坏按「无已知主机」处理
            pass
    client.set_missing_host_key_policy(p.AutoAddPolicy() if trust else _RejectWithKey())
    return client


def _connect_kwargs(host: dict, timeout: float) -> dict:
    kwargs = {
        "hostname": str(host.get("host", "")),
        "port": int(host.get("port", 22) or 22),
        "username": str(host.get("user", "")),
        "timeout": timeout,
        "banner_timeout": timeout,
        "auth_timeout": timeout,
        # 只按配置认证：不扫 ~/.ssh 的默认密钥、不碰 ssh-agent，行为可预期
        "look_for_keys": False,
        "allow_agent": False,
    }
    if str(host.get("auth", "password")) == "key":
        kwargs["key_filename"] = str(host.get("key_path", ""))
        if host.get("passphrase"):
            kwargs["passphrase"] = str(host["passphrase"])
    else:
        kwargs["password"] = str(host.get("password", ""))
    return kwargs


def _connect(host: dict, timeout: float = DEFAULT_TIMEOUT, trust: bool = False):
    """建立连接并返回 SSHClient；调用方负责 close。"""
    if ssh_store.missing_secret(host):
        # 先于建链检查：没有凭据时 paramiko 只会回「No authentication methods」，
        # 那句话对用户没有指导意义。
        raise MissingCredential(host)
    client = _new_client(trust)
    key_name = _host_key_name(host)
    was_known = False
    try:
        was_known = client.get_host_keys().lookup(key_name) is not None
    except Exception:  # noqa: BLE001
        was_known = False
    try:
        client.connect(**_connect_kwargs(host, timeout))
    except Exception:
        client.close()
        raise
    if trust and not was_known:
        path = known_hosts_path()
        try:
            client.save_host_keys(path)
            ssh_store.set_secret_permissions(path)
        except Exception:  # noqa: BLE001 - 写指纹失败不该让已成功的连接失败
            pass
    return client


def _classify(exc: Exception) -> str:
    """把底层异常归类成稳定枚举（CLI / GUI 据此给不同提示）。"""
    p = None
    try:
        p = _paramiko()
    except ParamikoMissing:
        pass
    if p is not None:
        if isinstance(exc, p.AuthenticationException):
            return "auth"
        if isinstance(exc, p.BadHostKeyException):
            return "host_key"
    if isinstance(exc, MissingCredential):
        return "credential"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, OSError):
        return "network"
    return "error"


def _error_text(exc: Exception, kind: str, target: str, timeout: float) -> str:
    if kind == "credential":
        return str(exc)
    if kind == "auth":
        return t("认证失败：用户名或凭据不正确（{target}）", target=target)
    if kind == "timeout":
        return t("连接超时（{n} 秒）", n=f"{timeout:g}")
    if kind == "network":
        return t("无法连接 {target}：{err}", target=target, err=exc)
    return t("连接失败：{err}", err=exc)


def test_connection(host: dict, timeout: float = DEFAULT_TIMEOUT,
                    trust: bool = False) -> dict:
    """测试一次真实握手。

    返回 ``{ok, target, latency_ms, banner, server, fingerprint, key_type,
    trusted, error, error_kind}``；不抛异常（依赖缺失也转成 ``error_kind``）。
    """
    target = target_label(host)
    out: dict = {
        "ok": False, "target": target, "latency_ms": 0,
        "banner": "", "server": "", "fingerprint": "", "key_type": "",
        "trusted": False, "error": "", "error_kind": "",
    }
    started = time.time()
    try:
        client = _connect(host, timeout, trust)
    except HostKeyNotTrusted as e:
        out.update(error_kind="host_key", fingerprint=e.fingerprint,
                   key_type=e.key_type,
                   error=t("主机指纹尚未信任：{fp}", fp=e.fingerprint))
        return out
    except ParamikoMissing as e:
        out.update(error_kind="missing_dependency", error=str(e))
        return out
    except MissingCredential as e:
        out.update(error_kind="credential", error=str(e))
        return out
    except Exception as e:  # noqa: BLE001 - 统一收敛成结果字典
        kind = _classify(e)
        out.update(error_kind=kind,
                   error=_error_text(e, kind, target, timeout))
        return out
    try:
        transport = client.get_transport()
        banner = str(getattr(transport, "remote_version", "") or "")
        out.update(ok=True, banner=banner, server=banner,
                   trusted=True,
                   latency_ms=int((time.time() - started) * 1000))
        try:
            key = transport.get_remote_server_key() if transport else None
            if key is not None:
                out.update(fingerprint=fingerprint(key),
                           key_type=key.get_name())
        except Exception:  # noqa: BLE001
            pass
    finally:
        client.close()
    return out


def _drain(chan, timeout: float, limit: int):
    """边读边限流，避免远端刷屏把内存吃满；返回 ``(stdout, stderr, status, truncated)``。"""
    out = bytearray()
    err = bytearray()
    truncated = False
    deadline = time.time() + max(1.0, float(timeout))
    while True:
        got = False
        if chan.recv_ready():
            data = chan.recv(65536)
            got = True
            room = max(0, limit - len(out))
            if room:
                out += data[:room]
            if len(data) > room:
                truncated = True
        if chan.recv_stderr_ready():
            data = chan.recv_stderr(65536)
            got = True
            room = max(0, limit - len(err))
            if room:
                err += data[:room]
            if len(data) > room:
                truncated = True
        if not got:
            if chan.exit_status_ready():
                break
            if time.time() > deadline:
                truncated = True
                break
            time.sleep(0.02)
    status = chan.recv_exit_status() if chan.exit_status_ready() else -1
    return (out.decode("utf-8", "replace"), err.decode("utf-8", "replace"),
            status, truncated)


def exec_command(host: dict, command: str,
                 timeout: float = DEFAULT_EXEC_TIMEOUT,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 trust: bool = False) -> dict:
    """在远端执行一条命令并回显输出（命令直接交给 channel，不经本地 shell）。"""
    target = target_label(host)
    out: dict = {
        "ok": False, "target": target, "command": command, "stdout": "",
        "stderr": "", "exit_status": -1, "duration_ms": 0, "truncated": False,
        "error": "", "error_kind": "",
    }
    started = time.time()
    try:
        client = _connect(host, timeout, trust)
    except HostKeyNotTrusted as e:
        out.update(error_kind="host_key", fingerprint=e.fingerprint,
                   key_type=e.key_type,
                   error=t("主机指纹尚未信任：{fp}", fp=e.fingerprint))
        return out
    except ParamikoMissing as e:
        out.update(error_kind="missing_dependency", error=str(e))
        return out
    except MissingCredential as e:
        out.update(error_kind="credential", error=str(e))
        return out
    except Exception as e:  # noqa: BLE001
        kind = _classify(e)
        out.update(error_kind=kind,
                   error=_error_text(e, kind, target, timeout))
        return out
    try:
        _stdin, stdout, _stderr = client.exec_command(command, timeout=timeout)
        so, se, status, truncated = _drain(stdout.channel, timeout, max_bytes)
        out.update(ok=status == 0, stdout=so, stderr=se, exit_status=status,
                   truncated=truncated,
                   duration_ms=int((time.time() - started) * 1000))
        if status != 0 and not se:
            out["error"] = t("远端命令退出码为 {code}", code=status)
            out["error_kind"] = "remote"
    except Exception as e:  # noqa: BLE001
        kind = _classify(e)
        out.update(error_kind=kind,
                   error=_error_text(e, kind, target, timeout))
    finally:
        client.close()
    return out


def build_ssh_argv(host: dict) -> list[str]:
    """构造交互式 ``ssh`` 参数表（密码不参与，由终端自行提示）。"""
    args = ["ssh", "-o", f"UserKnownHostsFile={known_hosts_path()}"]
    port = int(host.get("port", 22) or 22)
    if port != 22:
        args += ["-p", str(port)]
    if str(host.get("auth", "password")) == "key" and host.get("key_path"):
        args += ["-i", str(host["key_path"])]
    args.append(f"{host.get('user', '')}@{host.get('host', '')}")
    return args


def terminal_command(host: dict) -> str:
    """把参数表转成当前平台可用的命令行字符串（正确加引号）。"""
    argv = build_ssh_argv(host)
    if IS_WIN:
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def open_terminal(host: dict) -> bool:
    """用系统终端打开该主机的 SSH 会话；返回是否成功拉起终端。"""
    return process_utils.open_terminal(command=terminal_command(host))
