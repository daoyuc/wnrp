# -*- coding: utf-8 -*-
"""core/mysql_manager.py 实例发现回归测试。

守护「本机装了 MySQL，但面板检测不到」：实例发现不能只看环境根
（C:\\wnrp\\mysql*），还必须覆盖 Windows 服务 / PATH / 常见位置 / 显式配置，
且 bin 目录要上溯到安装根、同名实例要能区分。
"""
import os
import tempfile
import unittest
from unittest import mock

from core import mysql_manager
from core import process_utils as pu
from core.mysql_manager import (
    SERVER_EXE,
    MysqlManager,
    _bin_exe_path,
    _iter_instance_dirs,
    _unique_name,
)


def _fake_install(root: str, name: str = "mysql", port: int = 3310) -> str:
    """在 root 下造一个最小 MySQL 安装目录（bin/mysqld.exe + my.ini）。"""
    d = os.path.join(root, name)
    os.makedirs(os.path.join(d, "bin"), exist_ok=True)
    for exe in (SERVER_EXE, "mysql.exe" if os.name == "nt" else "mysql"):
        open(os.path.join(d, "bin", exe), "wb").close()
    with open(os.path.join(d, "my.ini"), "w", encoding="utf-8") as f:
        f.write(f"[mysqld]\nport = {port}\ndatadir={os.path.join(d, 'data')}\n")
    return d


class MysqlDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.env_root = os.path.join(self._tmp.name, "env")
        os.makedirs(self.env_root)
        # 清掉模块级缓存，避免用例间互相污染
        mysql_manager._discover_cache = None
        mysql_manager._service_cache = None
        mysql_manager._version_cache.clear()
        # 不依赖真实机器：进程/服务探测全部打桩
        for patch in (
            mock.patch.object(pu, "port_to_pid_fast", return_value=[]),
            mock.patch.object(pu, "port_to_pid", return_value=[]),
            mock.patch.object(pu, "find_pids_by_image_name", return_value=[]),
            mock.patch.object(pu, "run_cmd",
                              return_value=(0, "mysqld  Ver 8.0.36 for Win64", "")),
            mock.patch.object(mysql_manager, "_mysql_service_configs", return_value=[]),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_env_root_instance_still_discovered(self):
        """原有约定不变：环境根下的 mysql* 目录照常识别。"""
        install = _fake_install(self.env_root, "mysql")
        mgr = MysqlManager(root=self.env_root, discover=False)
        self.assertEqual([i.dir for i in mgr.instances], [install])
        self.assertEqual(mgr.instances[0].port, 3310)
        self.assertEqual(mgr.instances[0].version, "8.0.36")

    def test_external_install_discovered(self):
        """环境根之外（如 D:\\mysql）的安装也要被发现。"""
        ext = _fake_install(os.path.join(self._tmp.name, "D"), "mysql")
        with mock.patch.object(mysql_manager, "_discovered_dirs", return_value=[ext]):
            mgr = MysqlManager(root=self.env_root)
        self.assertEqual([i.dir for i in mgr.instances], [ext])
        self.assertEqual(mgr.instances[0].name, "mysql")

    def test_discover_false_skips_external(self):
        """discover=False 时只扫环境根（测试 / 纯自管理场景不被机器环境影响）。"""
        ext = _fake_install(os.path.join(self._tmp.name, "D"), "mysql")
        with mock.patch.object(mysql_manager, "_discovered_dirs",
                               return_value=[ext]) as m:
            MysqlManager(root=self.env_root, discover=False)
        m.assert_not_called()

    def test_bin_dir_normalized_to_install_root(self):
        """配置/PATH 常直接指向 ...\\mysql\\bin，应上溯到安装根。"""
        install = _fake_install(self.env_root, "mysql")
        found = _iter_instance_dirs([os.path.join(install, "bin")])
        self.assertEqual(found, [install])

    def test_container_dir_expanded(self):
        """C:\\Program Files\\MySQL 这类容器目录要展开一层子目录。"""
        container = os.path.join(self._tmp.name, "ProgramFiles", "MySQL")
        os.makedirs(container)
        install = _fake_install(container, "MySQL Server 8.0")
        self.assertEqual(_iter_instance_dirs([container]), [install])

    def test_same_basename_gets_distinct_names(self):
        """两个都叫 mysql 的目录（C:\\wnrp 与 D:\\）名字必须可区分。"""
        used = {"mysql"}
        name = _unique_name("mysql", os.path.join(self._tmp.name, "D", "mysql"), used)
        self.assertNotEqual(name, "mysql")
        self.assertIn("@", name)

    def test_service_binary_path_parsed(self):
        """sc qc 的 BINARY_PATH_NAME 去掉引号与启动参数后取到 exe。"""
        raw = '"D:\\mysql\\bin\\mysqld.exe" --defaults-file="D:\\mysql\\my.ini" MySQL'
        self.assertEqual(_bin_exe_path(raw), "D:\\mysql\\bin\\mysqld.exe")

    def test_configured_dirs_from_settings(self):
        """settings.mysql_dirs 中的显式目录生效（支持单个字符串）。"""
        install = _fake_install(self._tmp.name, "mydb")
        fake_cfg = mock.Mock()
        fake_cfg.settings = {"mysql_dirs": [install]}
        with mock.patch("core.config.Config", return_value=fake_cfg):
            self.assertEqual(mysql_manager._configured_dirs(), [install])
        fake_cfg.settings = {"mysql_dirs": install}
        with mock.patch("core.config.Config", return_value=fake_cfg):
            self.assertEqual(mysql_manager._configured_dirs(), [install])


if __name__ == "__main__":
    unittest.main()
