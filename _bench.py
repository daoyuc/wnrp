# -*- coding: utf-8 -*-
r"""性能基准：对比旧 netstat / ctypes 单端口 / 批量快照三种状态刷新口径。

用法：C:\Python312\python.exe C:\wnrp\phpvm\_bench.py
用于验证 auto_refresh 从「逐版本查端口表」改为「一次快照本地匹配」的收益。
"""
import time, sys
sys.path.insert(0, r"C:\wnrp\phpvm")
from core import process_utils as pu

# 模拟 phpvm 全部版本 + nginx 的端口集合
PORTS = [9000, 9001, 9056, 9072, 9073, 9074, 9080, 9081, 9085, 80]

t0 = time.perf_counter()
for _ in range(6):
    for p in PORTS:
        pu.port_to_pid(p)
old = time.perf_counter() - t0
print(f"[旧] netstat 逐端口 x{len(PORTS)} x6: {old:.3f}s")

t0 = time.perf_counter()
for _ in range(6):
    for p in PORTS:
        pu.port_to_pid_fast(p)
new = time.perf_counter() - t0
print(f"[新] ctypes 逐端口(快照缓存) x{len(PORTS)} x6: {new:.3f}s")

t0 = time.perf_counter()
for _ in range(6):
    snap = pu.get_tcp_snapshot()
    alive = pu.get_process_snapshot()
    n_running = 0
    for p in PORTS:
        pids = snap.get(p) or []
        if any(pid in alive for pid in pids):
            n_running += 1
batch = time.perf_counter() - t0
print(f"[批] 一次TCP快照+一次进程快照 x6: {batch:.3f}s (本轮运行端口={n_running})")

print("old pids(9000):", pu.port_to_pid(9000))
print("new pids(9000):", pu.port_to_pid_fast(9000))
t0 = time.perf_counter()
a = pu.alive_pids()
b = pu.alive_pids()
print(f"[新] alive_pids x2: {time.perf_counter()-t0:.3f}s, 存活进程数={len(a)}")
