# -*- coding: utf-8 -*-
"""常用 Web 应用 nginx vhost 模板库（供「新建站点」向导生成配置）。

每个模板是一份可直接落盘的 server 配置文本，内含占位 token：
  {{server_name}}   server_name（域名，可多个，空格分隔）
  {{docroot}}       文档根目录（nginx 正斜杠写法）
  {{port}}          FastCGI 端口（来自所选 PHP 版本）
  {{php_block}}     由渲染器按 needs_php 注入的 PHP 处理 location

渲染使用 token 替换而非 str.format —— nginx 配置本身含大量花括号，不会冲突。

fastcgi 参数直接内联（不依赖 include fastcgi_params），保证任意 nginx
布局下 nginx -t 均可通过，且不要求用户环境存在公共 fastcgi 片段。
"""
import time

_PHP_LOCATION = r"""    location ~ \.php$ {
        try_files $uri =404;
        fastcgi_param  QUERY_STRING       $query_string;
        fastcgi_param  REQUEST_METHOD     $request_method;
        fastcgi_param  CONTENT_TYPE       $content_type;
        fastcgi_param  CONTENT_LENGTH     $content_length;
        fastcgi_param  SCRIPT_NAME        $fastcgi_script_name;
        fastcgi_param  REQUEST_URI        $request_uri;
        fastcgi_param  DOCUMENT_URI       $document_uri;
        fastcgi_param  DOCUMENT_ROOT      $document_root;
        fastcgi_param  SERVER_PROTOCOL    $server_protocol;
        fastcgi_param  REQUEST_SCHEME     $scheme;
        fastcgi_param  HTTPS              $https if_not_empty;
        fastcgi_param  GATEWAY_INTERFACE  CGI/1.1;
        fastcgi_param  SERVER_SOFTWARE    nginx/$nginx_version;
        fastcgi_param  REMOTE_ADDR        $remote_addr;
        fastcgi_param  REMOTE_PORT        $remote_port;
        fastcgi_param  SERVER_ADDR        $server_addr;
        fastcgi_param  SERVER_PORT        $server_port;
        fastcgi_param  SERVER_NAME        $server_name;
        fastcgi_param  REDIRECT_STATUS    200;
        fastcgi_param  SCRIPT_FILENAME    $document_root$fastcgi_script_name;
        fastcgi_index index.php;
        fastcgi_pass 127.0.0.1:{{port}};
    }
"""


#: 模板目录（顺序即向导中的展示顺序）
TEMPLATES = [
    {
        "key": "laravel",
        "name": "Laravel",
        "summary": "Laravel 5.x ~ 12.x：root 指向 /public，支持前端控制器与静态资源长缓存，"
                   "并禁止访问 .env/.git 等点文件。",
        "hint": "项目目录请选择 Laravel 项目根（包含 artisan / public 的目录），工具会自动把文档根拼为 <项目>/public。",
        "needs_php": True,
        "root_suffix": "/public",
        "body": r"""server {
    listen       80;
    server_name  {{server_name}};

    root "{{docroot}}";
    index index.php;
    charset utf-8;
    client_max_body_size 50m;
    keepalive_timeout 65;

    # Laravel 前端控制器
    location / {
        try_files $uri $uri/ /index.php?$query_string;
    }

    # 拦截点文件（.env / .git / .env.example 等），放行 /.well-known
    location ~ /\.(?!well-known).* {
        deny all;
    }

    # 编译产物 / 静态资源长缓存
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|webp|woff2?|eot|ttf)$ {
        expires 30d;
        access_log off;
        add_header Cache-Control "public, immutable";
    }

{{php_block}}
}
""",
    },
    {
        "key": "wordpress",
        "name": "WordPress",
        "summary": "WordPress 固定链接伪静态（pretty permalink）、静态资源缓存、"
                   "并禁止 wp-content/uploads 目录被当成 PHP 执行（安全加固）。",
        "hint": "项目目录请选择 WordPress 安装目录（含 wp-config.php / wp-content 的目录），文档根即该目录。",
        "needs_php": True,
        "root_suffix": "",
        "body": r"""server {
    listen       80;
    server_name  {{server_name}};

    root "{{docroot}}";
    index index.php;
    charset utf-8;
    client_max_body_size 50m;
    keepalive_timeout 65;

    # WordPress 固定链接（伪静态）
    location / {
        try_files $uri $uri/ /index.php?$args;
    }

    # 安全加固：禁止直接访问 wp-config.php
    location ~* ^/wp-config\.php$ {
        deny all;
    }

    # 安全加固：uploads 下文件不作为 PHP 执行
    location ~* /wp-content/uploads/.*\.(php|php\d)$ {
        deny all;
    }

    # 静态资源缓存
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|webp|woff2?|eot|ttf)$ {
        expires 30d;
        access_log off;
        add_header Cache-Control "public";
    }

{{php_block}}
}
""",
    },
    {
        "key": "thinkphp",
        "name": "ThinkPHP",
        "summary": "ThinkPHP 5 / 6 / 8（入口在 public）：伪静态交给 index.php，支持 PATHINFO / 模块化 URL。",
        "hint": "项目目录请选择 ThinkPHP 项目根（含 public 的目录），工具自动把文档根拼为 <项目>/public。\n"
                "若为 ThinkPHP 3.x（入口在项目根），请改用「通用 PHP（无框架）」模板。",
        "needs_php": True,
        "root_suffix": "/public",
        "body": r"""server {
    listen       80;
    server_name  {{server_name}};

    root "{{docroot}}";
    index index.php;
    charset utf-8;
    client_max_body_size 50m;
    keepalive_timeout 65;

    # ThinkPHP 入口伪静态：不存在的路径统一交给 index.php 解析
    location / {
        if (!-e $request_filename) {
            rewrite ^(.*)$ /index.php?s=$1 last;
        }
    }

    # 静态资源缓存
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|webp|woff2?|eot|ttf)$ {
        expires 7d;
        access_log off;
        add_header Cache-Control "public";
    }

{{php_block}}
}
""",
    },
    {
        "key": "php",
        "name": "通用 PHP（无框架）",
        "summary": "无框架 / 自带路由的纯 PHP 站点：静态资源缓存 + 不存在路径回退 index.php。",
        "hint": "文档根即所选项目目录（不自动拼子目录）。适合 TP3、原生 PHP、Yii/CodeIgniter 老项目等。",
        "needs_php": True,
        "root_suffix": "",
        "body": r"""server {
    listen       80;
    server_name  {{server_name}};

    root "{{docroot}}";
    index index.php index.html index.htm;
    charset utf-8;
    client_max_body_size 50m;
    keepalive_timeout 65;

    # 前端控制器式入口：不存在路径回退 index.php
    location / {
        try_files $uri $uri/ /index.php?$query_string;
    }

    # 静态资源缓存
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|webp|woff2?|eot|ttf)$ {
        expires 7d;
        access_log off;
        add_header Cache-Control "public";
    }

{{php_block}}
}
""",
    },
    {
        "key": "static",
        "name": "静态站点",
        "summary": "纯静态站点 / 文档目录：不经过 PHP，未命中文件返回 404。",
        "hint": "无需选择 PHP 版本；文档根即所选目录。",
        "needs_php": False,
        "root_suffix": "",
        "body": r"""server {
    listen       80;
    server_name  {{server_name}};

    root "{{docroot}}";
    index index.html index.htm;
    charset utf-8;

    location / {
        try_files $uri $uri/ =404;
    }

    # 静态资源缓存
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|webp|woff2?|eot|ttf)$ {
        expires 30d;
        access_log off;
        add_header Cache-Control "public";
    }
}
""",
    },
    {
        "key": "spa",
        "name": "前端 SPA（history 路由回退）",
        "summary": "Vue / React 等打包产物：未命中路由回退到 index.html，注释示例了 /api 反向代理写法。",
        "hint": "文档根请选择前端构建产物目录（dist / build / public，含 index.html）。",
        "needs_php": False,
        "root_suffix": "",
        "body": r"""server {
    listen       80;
    server_name  {{server_name}};

    root "{{docroot}}";
    index index.html;
    charset utf-8;

    # history 路由：非文件请求一律回退 index.html
    location / {
        try_files $uri $uri/ /index.html;
    }

    # 静态资源缓存
    location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|webp|woff2?|eot|ttf|map)$ {
        expires 30d;
        access_log off;
        add_header Cache-Control "public, immutable";
    }

    # 前后端分离：把 /api 代理到后端（按需取消注释并修改地址）
    # location /api/ {
    #     proxy_pass http://127.0.0.1:8000;
    #     proxy_set_header Host $host;
    #     proxy_set_header X-Real-IP $remote_addr;
    # }
}
""",
    },
]

TEMPLATE_MAP: dict[str, dict] = {t["key"]: t for t in TEMPLATES}


def render_config(key: str, *, server_name: str, docroot: str,
                  port: int | None) -> str:
    """渲染完整 vhost 配置文件文本。

    server_name：已格式化的域名串（空格分隔，可含通配符）
    docroot    ：nginx 正斜杠文档根（已去尾斜杠）
    port       ：FastCGI 端口（PHP 模板必填；非 PHP 模板可传 None）
    """
    meta = TEMPLATE_MAP[key]
    body = meta["body"]
    if meta["needs_php"]:
        body = body.replace("{{php_block}}", _PHP_LOCATION)

    replacements = {
        "server_name": server_name.strip(),
        "docroot": docroot.strip().rstrip("/"),
        "port": str(port) if port else "",
    }
    for token, value in replacements.items():
        body = body.replace("{{%s}}" % token, value)

    # 若 PHP 模板缺端口却仍残留占位（异常），给出显式注释便于发现
    if "{{port}}" in body:
        body = body.replace("{{port}}", "0  # 未选择 PHP 版本")

    stamp = time.strftime("%Y-%m-%d %H:%M")
    meta_name = meta["name"]
    return (
        "# ============================================================\n"
        f"# {meta_name} · 由 phpvm「新建站点」生成 · {stamp}\n"
        "# 可手动修改本文件；改后执行「配置检查(nginx -t)」并「平滑重载」。\n"
        "# ============================================================\n"
        + body
    )
