# -*- coding: utf-8 -*-
"""测试替身：假 nginx 布局（避免真实读写 C:\\wnrp\\nginx）。"""
import os

SUCCESS = "nginx: configuration file ... syntax is ok\nnginx: configuration file ... test is successful"
FAILED = "nginx: [emerg] unexpected \"}\" in ...\nnginx: configuration file ... test failed"


class FakeConfig:
    """只实现被测代码用到的那部分 Config（ports），避免读写真实 config.json。"""

    def __init__(self, ports: dict | None = None):
        self.ports = dict(ports or {})


class FakeNginx:
    """只需实现 VhostManager / site_service 用到的那几个成员。"""

    def __init__(self, prefix: str, test_output: str = SUCCESS):
        self.prefix = prefix
        self.site_base = os.path.join(prefix, "conf")
        self.main_conf = os.path.join(self.site_base, "nginx.conf")
        self.vhost_dir = os.path.join(self.site_base, "vhost")
        self.exe = os.path.join(prefix, "nginx.exe")
        self.test_output = test_output
        self.running = False
        self.reload_count = 0

    def test_config(self) -> str:
        return self.test_output

    def get_status(self):
        return self.running, ([4242] if self.running else [])

    def reload(self) -> str:
        self.reload_count += 1
        return "reload ok"


def make_layout(root: str, *, with_nginx_exe: bool = True,
                http_block: bool = True, include_vhost: bool = False) -> FakeNginx:
    """在 root 下铺一套 nginx 目录：conf/nginx.conf + conf/vhost/。"""
    ng = FakeNginx(root, SUCCESS)
    os.makedirs(ng.vhost_dir, exist_ok=True)
    lines = ["events {}"]
    if http_block:
        lines.append("http {")
        lines.append("    include       mime.types;")
        if include_vhost:
            lines.append("    include %s/*.conf;" % ng.vhost_dir.replace("\\", "/"))
        lines.append("}")
    with open(ng.main_conf, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if with_nginx_exe:
        with open(ng.exe, "w", encoding="utf-8") as f:
            f.write("")  # 仅需存在（site_service 用它判断能否跑 nginx -t）
    return ng


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
