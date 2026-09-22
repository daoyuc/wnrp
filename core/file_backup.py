# -*- coding: utf-8 -*-
"""文件备份 / 还原的统一入口（写配置的「先备份、失败能回滚」纪律）。

为什么需要：nginx 配置、php.ini、扩展 dll、hosts 的写入都必须先备份，
失败或校验不通过时回滚。此前各模块各自 ``shutil.copy2(path, path + ".bak")``
再手写还原，备份命名与回滚语义容易漂移（有的还原后删备份、有的保留）。

统一约定：

- 备份路径 = ``<path><suffix>``，默认 ``.bak``（hosts 用 ``.phpvm.bak``）；
- :func:`backup` 失败**抛 OSError** —— 写前备份失败必须让调用方感知并中止；
- :func:`restore` 失败**静默返回 False** —— 回滚本身是兜底路径，不应再抛错
  掩盖原始故障；默认还原后删除备份（保持历史行为）；
- 调用方判断「文件是否存在」的语义不变：源文件不存在时 :func:`backup`
  返回空串且不创建备份（如「仅覆盖时才备份」的 vhost 写入）。
"""
import os
import shutil

DEFAULT_SUFFIX = ".bak"


def backup_path(path: str, suffix: str = DEFAULT_SUFFIX) -> str:
    """按约定推导备份路径（不检查是否存在）。"""
    return f"{path}{suffix}"


def has_backup(path: str, suffix: str = DEFAULT_SUFFIX) -> bool:
    """约定的备份文件是否存在。"""
    return bool(path) and os.path.exists(backup_path(path, suffix))


def backup(path: str, suffix: str = DEFAULT_SUFFIX) -> str:
    """把 ``path`` 备份为 ``<path><suffix>``，返回备份路径。

    - ``path`` 为空或不存在：返回空串（不创建备份，语义同「无需备份」）；
    - 复制失败：抛出 OSError（调用方应中止写入，不要在没有备份时改文件）。
    """
    if not path or not os.path.exists(path):
        return ""
    target = backup_path(path, suffix)
    shutil.copy2(path, target)
    return target


def restore(bak: str, path: str = "", remove_backup: bool = True) -> bool:
    """用备份 ``bak`` 还原 ``path``，成功返回 True（失败静默返回 False）。

    ``path`` 省略时按默认后缀从备份路径反推。``remove_backup=True`` 时还原后
    删除备份文件（与历史回滚行为一致，避免残留误导后续排查）。
    """
    if not bak or not os.path.exists(bak):
        return False
    target = path or _strip_suffix(bak)
    try:
        shutil.copy2(bak, target)
    except OSError:
        return False
    if remove_backup:
        discard(bak)
    return True


def restore_latest(path: str, suffix: str = DEFAULT_SUFFIX,
                   remove_backup: bool = True) -> bool:
    """按约定名找回 ``path`` 的备份并还原（无需调用方记住备份路径）。"""
    return restore(backup_path(path, suffix), path, remove_backup)


def discard(bak: str) -> None:
    """删除备份文件（失败静默：备份残留不影响业务）。"""
    if not bak:
        return
    try:
        os.remove(bak)
    except OSError:
        pass


#: 已知备份后缀（长后缀必须排在前面：".phpvm.bak" 也以 ".bak" 结尾）
KNOWN_SUFFIXES = (".phpvm.bak", DEFAULT_SUFFIX)


def _strip_suffix(bak: str) -> str:
    """从备份路径反推原文件路径（找不到已知后缀时原样返回）。"""
    for suffix in KNOWN_SUFFIXES:
        if bak.endswith(suffix):
            return bak[: -len(suffix)]
    return bak
