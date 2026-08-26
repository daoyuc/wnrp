import time, sys
sys.path.insert(0, r"C:\wnrp\phpvm")
from core import process_utils as pu

t0 = time.perf_counter()
for _ in range(6):
    pu.port_to_pid(9000)
old = time.perf_counter() - t0
print(f"[旧] netstat x6: {old:.3f}s")

t0 = time.perf_counter()
for _ in range(6):
    pu.port_to_pid_fast(9000)
new = time.perf_counter() - t0
print(f"[新] ctypes x6: {new:.3f}s")

print("old pids:", pu.port_to_pid(9000))
print("new pids:", pu.port_to_pid_fast(9000))

t0 = time.perf_counter()
a = pu.alive_pids()
b = pu.alive_pids()
print(f"[新] alive_pids x2: {time.perf_counter()-t0:.3f}s, 存活进程数={len(a)}")
