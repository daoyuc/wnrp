import sys, time
sys.path.insert(0, r"C:\wnrp\phpvm")
from core.config import Config
from core.php_manager import PhpManager

m = PhpManager(Config())
vs = m.scan_versions()
print("扫描到版本:", [v.name for v in vs])
t0 = time.perf_counter()
m.resolve(fast=True)
dt = time.perf_counter() - t0
print(f"resolve(fast) 耗时: {dt:.3f}s")
for v in vs:
    print(f"  {v.name}: running={v.running} pid={v.pid} ver={v.display}")
