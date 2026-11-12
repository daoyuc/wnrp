# -*- coding: utf-8 -*-
"""「拉」式系统概览：点击主机时一次性 SSH 执行只读采集，回本地解析。

设计取舍（对比「推」式 agent，见 AGENTS / DECISIONS ADR 24 ⑦）：

- **不在远端装任何常驻进程**，不靠 agent、不开端口；要数据就发起一次 SSH
  连接、跑一条只读脚本、把 ``/proc`` 与 ``df`` 的结果拉回本地解析。
- 只采**只读**信息（负载 / 内存 / 交换 / 磁盘 / 网络累计字节），不改远端状态、
  不提权、不依赖 ``jq`` ``bc`` 等额外工具，目标就是一台普通 Linux PHP 服务器。
- **不存历史**：概览是「最近一次点击拉到的快照」，没有时序库、没有后台轮询；
  想看变化就再点一次「刷新状态」。这与 phpvm 定位（本地管理 + 按需 SSH）一致。
"""
from core import ssh_manager

# 单条复合命令：一次性把各指标以 ``KEY=value`` 形式打印，磁盘每个挂载点一行
# （``mount used_kb total_kb``，空格分隔）。全程只用 ``awk print`` / ``cut``，
# 不出现需要 shell 引号转义的 ``printf "%..."``，便于在 bash / busybox ash 下通用。
# 网络累计字节取自 ``/proc/net/dev``：第 2 列为接收字节、第 10 列为发送字节，
# 跳过标题行（``NR>2``）与回环 ``lo``。
_COLLECT_SCRIPT = (
    "echo UPTIME=$(awk '{print int($1)}' /proc/uptime);"
    "echo LOAD=$(cut -d' ' -f1-3 /proc/loadavg);"
    "echo MEM=$(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{print t, a}' /proc/meminfo);"
    "echo SWAP=$(awk '/SwapTotal/{t=$2}/SwapFree/{f=$2}END{print t, f}' /proc/meminfo);"
    "echo DISK=$(df -Pk -x tmpfs -x devtmpfs 2>/dev/null | awk 'NR>1{print $6, $3, $2}');"
    "echo NET=$(awk 'NR>2 && $1 ~ /^lo:/{next}{s+=$10;r+=$2}END{print s, r}' /proc/net/dev)"
)


def _scale(kb: float):
    """把 1024 为基的 KB 换算成 (数值, 单位)；返回最接近的人类可读量级。"""
    b = float(kb) * 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if b < 1024.0 or unit == "PB":
            return b, unit
        b /= 1024.0
    return b, "PB"


_DIVISORS = {u: 1024.0 ** i for i, u in enumerate(("B", "KB", "MB", "GB", "TB", "PB"))}


def _fmt_pair(used_kb: float, total_kb: float):
    """用 total 的量级同时表示 used / total，保证「x / y 单位」一致。"""
    _, unit = _scale(total_kb)
    div = _DIVISORS[unit]
    return (f"{used_kb * 1024.0 / div:.1f}",
            f"{total_kb * 1024.0 / div:.1f}", unit)


def _fmt_bytes(kb: float):
    v, unit = _scale(kb)
    return f"{v:.1f} {unit}"


def _parse_uptime(seconds: int) -> str:
    """把秒数翻成「Nd Mh」式中文短串。"""
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return t_fmt("{d}天{h}小时", d=days, h=hours)
    if hours:
        return t_fmt("{h}小时{m}分", h=hours, m=minutes)
    return t_fmt("{m}分", m=minutes)


def t_fmt(msg, **kw):
    """概览文案用当前语言拼接（避免在常量里写死中文）。"""
    from core.i18n import t
    return t(msg, **kw)


def parse_overview(stdout: str) -> dict:
    """把采集脚本的输出解析成结构化快照；任何字段缺失都给安全的默认值。"""
    data = {
        "uptime_seconds": 0,
        "load": [0.0, 0.0, 0.0],
        "mem_total_kb": 0, "mem_avail_kb": 0,
        "swap_total_kb": 0, "swap_free_kb": 0,
        "disks": [],            # [{"mount","used_kb","total_kb"}]
        "net_send_bytes": 0, "net_recv_bytes": 0,
    }
    disk_block = ""
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if line.startswith("UPTIME="):
            try:
                data["uptime_seconds"] = int(line[len("UPTIME="):])
            except ValueError:
                pass
        elif line.startswith("LOAD="):
            parts = line[len("LOAD="):].split()
            try:
                data["load"] = [float(x) for x in parts[:3]] + [0.0, 0.0, 0.0]
                data["load"] = data["load"][:3]
            except ValueError:
                pass
        elif line.startswith("MEM="):
            parts = line[len("MEM="):].split()
            if len(parts) == 2:
                data["mem_total_kb"] = _to_int(parts[0])
                data["mem_avail_kb"] = _to_int(parts[1])
        elif line.startswith("SWAP="):
            parts = line[len("SWAP="):].split()
            if len(parts) == 2:
                data["swap_total_kb"] = _to_int(parts[0])
                data["swap_free_kb"] = _to_int(parts[1])
        elif line.startswith("DISK="):
            chunk = line[len("DISK="):].strip()
            if chunk:
                disk_block = (disk_block + " " + chunk).strip() if disk_block \
                    else chunk
        elif line.startswith("NET="):
            parts = line[len("NET="):].split()
            if len(parts) == 2:
                data["net_send_bytes"] = _to_int(parts[0])
                data["net_recv_bytes"] = _to_int(parts[1])
    # 每个挂载点是「mount used_kb total_kb」三个一组（命令替换把 df 的多行压成空格）
    parts = disk_block.split()
    for i in range(0, len(parts) - 2, 3):
        data["disks"].append({
            "mount": parts[i],
            "used_kb": _to_int(parts[i + 1]),
            "total_kb": _to_int(parts[i + 2]),
        })
    return data


def _to_int(text: str) -> int:
    try:
        return int(float(text))
    except (ValueError, TypeError):
        return 0


def overview_text(data: dict, collected_at: str = "") -> str:
    """把结构化快照渲染成多行展示文本（面板用，纯展示）。"""
    lines = []
    lines.append(t_fmt("运行时长：{u}", u=_parse_uptime(data["uptime_seconds"])))
    load = data["load"]
    lines.append(t_fmt("负载（1/5/15 分钟）：{a} {b} {c}",
                       a=f"{load[0]:.2f}", b=f"{load[1]:.2f}", c=f"{load[2]:.2f}"))
    if data["mem_total_kb"]:
        used, total, unit = _fmt_pair(
            data["mem_total_kb"] - data["mem_avail_kb"], data["mem_total_kb"])
        pct = int(round((data["mem_total_kb"] - data["mem_avail_kb"])
                        / data["mem_total_kb"] * 100)) if data["mem_total_kb"] else 0
        lines.append(t_fmt("内存：{u} / {t} {unit}（{p}%）",
                           u=used, t=total, unit=unit, p=pct))
    if data["swap_total_kb"]:
        used, total, unit = _fmt_pair(
            data["swap_total_kb"] - data["swap_free_kb"], data["swap_total_kb"])
        pct = int(round((data["swap_total_kb"] - data["swap_free_kb"])
                        / data["swap_total_kb"] * 100)) if data["swap_total_kb"] else 0
        lines.append(t_fmt("交换：{u} / {t} {unit}（{p}%）",
                           u=used, t=total, unit=unit, p=pct))
    if data["disks"]:
        lines.append(t_fmt("磁盘："))
        for d in data["disks"]:
            used, total, unit = _fmt_pair(d["used_kb"], d["total_kb"])
            pct = int(round(d["used_kb"] / d["total_kb"] * 100)) if d["total_kb"] else 0
            lines.append(t_fmt("  {m}  {u} / {t} {unit}（{p}%）",
                               m=d["mount"], u=used, t=total, unit=unit, p=pct))
    lines.append(t_fmt("网络累计发送：{s} · 接收：{r}",
                       s=_fmt_bytes(data["net_send_bytes"] / 1024.0),
                       r=_fmt_bytes(data["net_recv_bytes"] / 1024.0)))
    if collected_at:
        lines.append(t_fmt("（采集于 {t}）", t=collected_at))
    return "\n".join(lines)


def collect_overview(host: dict, trust: bool = False,
                     timeout: float | None = None) -> dict:
    """执行一次只读采集并返回解析后的快照。

    直接复用 ``ssh_manager.exec_command``（指纹 / 凭据 / 依赖缺失等都已在该层
    收敛成结果字典，这里只透传并附加 ``data``）。失败时不抛异常。
    """
    if timeout is None:
        timeout = ssh_manager.DEFAULT_EXEC_TIMEOUT
    res = ssh_manager.exec_command(host, _COLLECT_SCRIPT,
                                   timeout=timeout, trust=trust)
    if res.get("ok"):
        res = dict(res)
        res["data"] = parse_overview(res.get("stdout", ""))
    return res
