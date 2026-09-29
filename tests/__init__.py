# -*- coding: utf-8 -*-
"""phpvm 单元测试（仅标准库 unittest，无第三方依赖）。

运行（仓库根目录）：

    python -m unittest discover -s tests -t . -v

覆盖重点：会「写用户文件 / 系统 hosts」的高风险路径 —— vhost 改写、
hosts 写入与移除、ini 写回、建站编排与回滚。全部用临时目录与假 nginx，
不触碰真实的 nginx 配置与系统 hosts。

数据目录同样隔离：本包的导入副作用把 PHPVM_HOME / PHPVM_CONFIG 指向一次性
临时目录（不覆盖显式设置的环境变量），否则 run_log / config.json / 崩溃自愈
状态会被测试直接写进开发者的真实数据目录。
"""
import atexit
import os
import shutil
import tempfile

_DATA_DIR = tempfile.mkdtemp(prefix="phpvm-tests-")
os.environ.setdefault("PHPVM_HOME", _DATA_DIR)
os.environ.setdefault("PHPVM_CONFIG", os.path.join(_DATA_DIR, "config.json"))
# 注册在 run_log 的 atexit 冲刷之后执行（atexit 为后进先出）
atexit.register(lambda: shutil.rmtree(_DATA_DIR, ignore_errors=True))
