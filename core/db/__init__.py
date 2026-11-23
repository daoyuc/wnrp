# -*- coding: utf-8 -*-
"""数据库常规管理（连接 / 只读浏览 / 受控写 + 审计）。

第一期：连接持久化、审计落盘、MySQL（本机 mysql 客户端）与 SQLite 只读浏览。
写路径（DML/DDL）后续接入时必须经 ``audit.append``，见 ADR 28。
"""
from . import audit, facade, store

__all__ = ["audit", "facade", "store"]
