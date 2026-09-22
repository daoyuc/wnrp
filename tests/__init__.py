# -*- coding: utf-8 -*-
"""phpvm 单元测试（仅标准库 unittest，无第三方依赖）。

运行（仓库根目录）：

    python -m unittest discover -s tests -t . -v

覆盖重点：会「写用户文件 / 系统 hosts」的高风险路径 —— vhost 改写、
hosts 写入与移除、ini 写回、建站编排与回滚。全部用临时目录与假 nginx，
不触碰真实的 nginx 配置与系统 hosts。
"""
