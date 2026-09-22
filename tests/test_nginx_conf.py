# -*- coding: utf-8 -*-
"""core/nginx_conf：按上下文读写 nginx 指令（命中替换 / 未命中插入 / 块缺失新建）。"""
import unittest

from core import nginx_conf

CONF = """# 顶部注释
worker_processes  1;
events {
    worker_connections  1024;
}
http {
    include       mime.types;
    keepalive_timeout  65;  # 注释保留
    server {
        listen 80;
        client_max_body_size 2m;
    }
}
"""


class NginxConfTest(unittest.TestCase):
    def test_context_map(self):
        ctxs = nginx_conf.context_map(CONF)
        self.assertIn("events", ctxs)
        self.assertIn("http", ctxs)
        self.assertIn("http/server", ctxs)

    def test_get_directive_reads_block_and_inherits_from_child(self):
        self.assertEqual(nginx_conf.get_directive(CONF, "worker_connections", "events"), "1024")
        self.assertEqual(nginx_conf.get_directive(CONF, "keepalive_timeout", "http"), "65")
        # server 子块内的指令也算 http 当前生效值（继承）
        self.assertEqual(nginx_conf.get_directive(CONF, "client_max_body_size", "http"), "2m")
        self.assertIsNone(nginx_conf.get_directive(CONF, "worker_connections", "http"))

    def test_get_directive_ignores_comments(self):
        text = "# keepalive_timeout  600;\nhttp {\n    keepalive_timeout 65;\n}\n"
        self.assertEqual(nginx_conf.get_directive(text, "keepalive_timeout", "http"), "65")

    def test_set_directive_replaces_value_keeps_comment(self):
        new, action = nginx_conf.set_directive(CONF, "keepalive_timeout", "30", "http")
        self.assertEqual(action, "replaced")
        self.assertEqual(nginx_conf.get_directive(new, "keepalive_timeout", "http"), "30")
        self.assertIn("# 注释保留", new, "行尾注释应保留")
        self.assertIn("keepalive_timeout  ", new, "保留原有的键/值分隔空格")

    def test_set_directive_inserts_into_block(self):
        new, action = nginx_conf.set_directive(CONF, "gzip", "off", "http")
        self.assertEqual(action, "inserted")
        self.assertEqual(nginx_conf.get_directive(new, "gzip", "http"), "off")
        # 必须插进 http 块内，而不是文件最外层
        self.assertIn("gzip off;", new.split("server {")[0].split("http {")[1])

    def test_set_directive_inserts_into_main(self):
        new, action = nginx_conf.set_directive(CONF, "worker_rlimit_nofile", "4096", "main")
        self.assertEqual(action, "inserted")
        self.assertEqual(nginx_conf.get_directive(new, "worker_rlimit_nofile", "main"), "4096")

    def test_set_directive_creates_missing_block(self):
        text = "worker_processes auto;\n"
        new, action = nginx_conf.set_directive(text, "worker_connections", "2048", "events")
        self.assertEqual(action, "inserted")
        self.assertIn("events {", new)
        self.assertEqual(nginx_conf.get_directive(new, "worker_connections", "events"), "2048")

    def test_set_directive_same_value_is_unchanged(self):
        new, action = nginx_conf.set_directive(CONF, "keepalive_timeout", "65", "http")
        self.assertEqual(action, "unchanged")
        self.assertEqual(new, CONF)

    def test_apply_changes_batch(self):
        new, results = nginx_conf.apply_changes(
            CONF, [("worker_processes", "auto", "main"),
                   ("worker_connections", "4096", "events"),
                   ("tcp_nodelay", "on", "http")])
        self.assertEqual([r["action"] for r in results],
                         ["replaced", "replaced", "inserted"])
        self.assertEqual(nginx_conf.get_directive(new, "worker_processes", "main"), "auto")
        self.assertEqual(nginx_conf.get_directive(new, "worker_connections", "events"), "4096")
        self.assertEqual(nginx_conf.get_directive(new, "tcp_nodelay", "http"), "on")
        # server 块内的原有指令不受影响
        self.assertEqual(nginx_conf.get_directive(new, "listen", "http/server"), "80")


if __name__ == "__main__":
    unittest.main()
