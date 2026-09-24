<!-- 由 _docs_cli.py 自动生成，请勿手改；改命令后运行 python3 _docs_cli.py -->

# CLI 参考（自动生成）

> 本文由 `python3 _docs_cli.py` 从 `cli.py` 的 argparse 结构生成，**请勿手改**。
> 一致性校验：`python3 _docs_cli.py --check`（`tests/test_docs.py` 亦会校验）。
> 机器可读契约（AI 优先读这个）：`python3 cli.py schema --json`。

## 通用约定

| 项 | 说明 |
|---|---|
| json | 加 `--json`：stdout **只**输出一个 `{"ok","command","data","warnings"}` 文档；字段名与枚举值一律英文（稳定契约），说明性文本按 `--lang` 输出 |
| exit_codes | `0` 成功 / `1` 业务失败（`ok=false`，含 `data.error`）/ `2` 参数用法错误 / `130` 被中断 |
| non_interactive | 永不弹窗、不等待输入；写 hosts 会在无权限时触发系统授权框（需显式 `--hosts`） |
| safety | 写操作支持 `--dry-run`（只报告将做什么）；删除类需 `--yes`；Redis 高危命令（FLUSHALL 等）需 `--force`；改动前自动备份 `.bak` |
| recommended_flow | `schema --json` → `env --json` → `site create … --dry-run` → `site create …` |

以下全局参数对**所有动作**可用（各动作表格里不再重复）：

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--json` |  |  | 以 JSON 输出（stdout 只有一个 JSON 文档，供程序解析） |
| `--lang` |  |  | 输出语言（en / zh_CN / zh_TW / ja / ko），默认取配置 |
| `--quiet -q` |  |  | 安静模式（仅输出错误） |
| `--debug` |  |  | 出错时打印完整堆栈 |
| `--version` |  |  | 输出版本信息后退出 |

## 命令总览

| 分组 | 说明 | 动作 |
|---|---|---|
| `config` | 配置读写（config.json） | `get` · `list` · `set` |
| `env` | 输出全部服务快照（nginx / PHP / Redis / MySQL） | — |
| `hosts` | hosts 映射管理 | `add` · `remove` · `restore` · `status` |
| `module` | 功能模块开关 | `disable` · `enable` · `list` |
| `mysql` | MySQL 管理 | `list` · `log` · `restart` · `start` · `status` · `stop` |
| `nginx` | Nginx 管理 | `logs` · `reload` · `start` · `status` · `stop` · `test` |
| `php` | PHP 版本管理 | `check` · `ext-list` · `ext-set` · `ini` · `list` · `port` · `restart` · `start` · `status` · `stop` |
| `redis` | Redis 管理 | `cmd` · `list` · `ping` · `restart` · `start` · `status` · `stop` |
| `schema` | 输出全部命令的自描述契约（供 AI 读取） | — |
| `services` | 整套服务编排 | `start-all` · `stop-all` |
| `site` | 站点（vhost）管理 | `create` · `disable` · `enable` · `list` · `php` · `remove` · `render` · `show` · `sync-port` |
| `sqlite` | SQLite 只读查询 | `columns` · `query` · `tables` |
| `tune` | 开发环境配置推荐（PHP / Nginx） | `apply` · `suggest` |
| `update` | 软件更新 | `check` · `download` |
| `version` | 输出版本与环境路径信息 | — |

## 分组与动作

## `config` — 配置读写（config.json）

### `config get` — 读取配置键（ports.<name> / settings.<key>）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<key>` | 是 |  | 配置键，如 ports.php82 / settings.lang；ports / settings 取整体 |

### `config list` — 列出全部端口映射与设置

（无参数）

### `config set` — 写入配置键

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<key>` | 是 |  | 配置键，如 ports.php82 / settings.check_update_on_start |
| `<value>` | 是 |  | 新值（true/false/null/整数/JSON 数组/字符串） |
| `--dry-run` |  |  | 只报告将做什么 |

## `env` — 输出全部服务快照（nginx / PHP / Redis / MySQL）

## `hosts` — hosts 映射管理

### `hosts add` — 写入映射（只动 phpvm 托管块）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<domains>` | 是 |  | 域名列表 |
| `--ip` |  | 127.0.0.1 | 目标 IP（默认 127.0.0.1） |
| `--dry-run` |  |  | 只报告将做什么 |

### `hosts remove` — 移除托管块内的映射

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<domains>` | 是 |  | 域名列表 |
| `--dry-run` |  |  | 只报告将做什么 |

### `hosts restore` — 用写入前的备份还原 hosts（整文件覆盖）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--dry-run` |  |  | 只报告将做什么 |
| `--yes` |  |  | 确认覆盖当前 hosts |

### `hosts status` — 查询域名映射

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<domains>` | 是 |  | 域名列表 |

## `module` — 功能模块开关

### `module disable` — 停用模块（重启 GUI 生效）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<keys>` | 是 |  | 模块 key：redis/mysql/sqlite/log |
| `--dry-run` |  |  | 只报告将做什么 |

### `module enable` — 启用模块

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<keys>` | 是 |  | 模块 key：redis/mysql/sqlite/log |
| `--dry-run` |  |  | 只报告将做什么 |

### `module list` — 列出模块与启用状态

（无参数）

## `mysql` — MySQL 管理

### `mysql list` — 列出实例

（无参数）

### `mysql log` — 查看错误日志尾部

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 实例名（默认第一个） |
| `--lines` |  | 100 | 尾部行数（默认 100） |

### `mysql restart` — 重启实例

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 实例名（默认第一个） |

### `mysql start` — 启动实例

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 实例名（默认第一个） |

### `mysql status` — 实例状态

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 实例名（默认第一个） |

### `mysql stop` — 停止实例

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 实例名（默认第一个） |

## `nginx` — Nginx 管理

### `nginx logs` — 查看日志尾部

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--file` |  |  | 日志文件名（默认优先 error.log） |
| `--lines` |  | 100 | 尾部行数（默认 100） |
| `--list` |  |  | 只列出日志文件 |

### `nginx reload` — 平滑重载配置

（无参数）

### `nginx start` — 启动 nginx

（无参数）

### `nginx status` — 运行状态与路径信息

（无参数）

### `nginx stop` — 停止 nginx

（无参数）

### `nginx test` — 配置检查（nginx -t）

（无参数）

## `php` — PHP 版本管理

### `php check` — 版本自检（版本/扩展/配置加载）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 版本名 |

### `php ext-list` — 列出本地扩展与启用状态

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 版本名 |

### `php ext-set` — 启用 / 禁用扩展（写 ini，自动备份）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 版本名 |
| `--enable` |  |  | 要启用的扩展（逗号分隔） |
| `--disable` |  |  | 要禁用的扩展（逗号分隔） |
| `--dry-run` |  |  | 只报告将做什么 |

### `php ini` — 读取关键配置 / 已启用扩展

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 版本名 |
| `--key` |  |  | 只读取指定配置键 |
| `--all` |  |  | 附带输出 ini 全文 |

### `php list` — 列出所有 PHP 版本与运行状态

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--no-status` |  |  | 不探测运行状态（更快） |
| `--precise` |  |  | 精确状态判定（校验进程命令行，较慢） |

### `php port` — 查看或修改 FastCGI 端口

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 版本名 |
| `--set` |  |  | 设置为新端口（1-65535，不可与其它版本重复） |
| `--dry-run` |  |  | 只报告将做什么 |

### `php restart` — 重启版本（name 可为 all）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 版本名，或 all 表示全部版本 |

### `php start` — 启动版本（name 可为 all）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 版本名，或 all 表示全部版本 |

### `php status` — 查看单个版本状态

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 版本名，如 php82 / 8.2 / 82 |

### `php stop` — 停止版本（name 可为 all）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 版本名，或 all 表示全部版本 |

## `redis` — Redis 管理

### `redis cmd` — 执行单条命令

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 实例名（默认第一个） |
| `--command` |  | redis.cmd | Redis 命令，如 'GET foo' |
| `--db` |  |  | 逻辑库 0-15（默认 0） |
| `--force` |  |  | 允许高危命令（FLUSHALL/FLUSHDB/SHUTDOWN 等） |
| `--dry-run` |  |  | 只报告将做什么 |

### `redis list` — 列出实例

（无参数）

### `redis ping` — PING 测试

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 实例名（默认第一个） |

### `redis restart` — 重启实例

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 实例名（默认第一个） |

### `redis start` — 启动实例

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 实例名（默认第一个） |

### `redis status` — 实例状态

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 实例名（默认第一个） |

### `redis stop` — 停止实例

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<name>` | 是 |  | 实例名（默认第一个） |

## `schema` — 输出全部命令的自描述契约（供 AI 读取）

## `services` — 整套服务编排

### `services start-all` — 按序启动 PHP → Redis → MySQL → Nginx

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--dry-run` |  |  | 只报告将做什么 |
| `--php` |  |  | PHP 启动范围：newest（仅最新）/ used（站点引用+最新）/ active（跟随 cmd 生效版本）/ all（全部，默认），或逗号分隔的版本名（如 php82,php85） |

### `services stop-all` — 停止全部服务

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--dry-run` |  |  | 只报告将做什么 |

## `site` — 站点（vhost）管理

### `site create` — 创建站点：写 vhost → 补 include → nginx -t → hosts → 重载

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--domain` | 是 |  | 域名，可多个（空格分隔，支持 *.dev 泛解析） |
| `--root` | 是 |  | 项目目录（Laravel/ThinkPHP 自动拼 /public） |
| `--template` |  | laravel | 模板：laravel / wordpress / thinkphp / php / static / spa |
| `--php` |  |  | PHP 版本名（决定 fastcgi_pass 端口） |
| `--port` |  |  | 直接指定 FastCGI 端口（与 --php 二选一） |
| `--no-php` |  |  | 不使用 PHP（静态 / 纯前端站点） |
| `--conf` |  |  | 配置文件名（默认 <安全域名>.conf） |
| `--https` |  |  | 生成 443 变体（需 openssl / mkcert） |
| `--dry-run` |  |  | 只输出将要写入的配置，不落盘 |
| `--hosts` |  |  | 同时写入 hosts（可能需要系统授权弹窗） |
| `--no-reload` |  |  | 创建成功后不重载 nginx |

### `site disable` — 禁用站点（改名 .conf.disabled）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<target>` | 是 |  | 域名 / 配置文件名 / 配置文件路径 |

### `site enable` — 启用站点

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<target>` | 是 |  | 域名 / 配置文件名 / 配置文件路径 |

### `site list` — 列出全部站点与 hosts/include 状态

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--domain` |  |  | 按域名过滤 |

### `site php` — 切换站点使用的 PHP 版本（改 fastcgi_pass）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<target>` | 是 |  | 域名 / 配置文件名 / 配置文件路径 |
| `--php` | 是 |  | 目标 PHP 版本名 |

### `site remove` — 删除站点配置（需 --yes，自动备份）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<target>` | 是 |  | 域名 / 配置文件名 / 配置文件路径 |
| `--yes` |  |  | 确认删除 |
| `--dry-run` |  |  | 只报告将做什么 |
| `--no-reload` |  |  | 删除成功后不重载 nginx |

### `site render` — 只渲染站点配置文本，不写入（预览用）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--domain` | 是 |  | 域名，可多个（空格分隔，支持 *.dev 泛解析） |
| `--root` | 是 |  | 项目目录（Laravel/ThinkPHP 自动拼 /public） |
| `--template` |  | laravel | 模板：laravel / wordpress / thinkphp / php / static / spa |
| `--php` |  |  | PHP 版本名（决定 fastcgi_pass 端口） |
| `--port` |  |  | 直接指定 FastCGI 端口（与 --php 二选一） |
| `--no-php` |  |  | 不使用 PHP（静态 / 纯前端站点） |
| `--conf` |  |  | 配置文件名（默认 <安全域名>.conf） |
| `--https` |  |  | 生成 443 变体（需 openssl / mkcert） |
| `--dry-run` |  |  | 只输出将要写入的配置，不落盘 |

### `site show` — 查看单个站点详情与配置内容

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<target>` | 是 |  | 域名 / 配置文件名 / 配置文件路径 |

### `site sync-port` — 一键把引用旧端口的 fastcgi_pass 替换为新端口

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--old` | 是 |  | 旧端口 |
| `--new` | 是 |  | 新端口 |
| `--reload` |  |  | 同步成功后平滑重载 nginx |
| `--dry-run` |  |  | 只列出将被修改的文件 |

## `sqlite` — SQLite 只读查询

### `sqlite columns` — 查看表结构

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<path>` | 是 |  | 数据库文件路径 |
| `<table>` | 是 |  | 表名 |

### `sqlite query` — 执行只读查询（SELECT / WITH / PRAGMA / EXPLAIN / VALUES）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<path>` | 是 |  | 数据库文件路径 |
| `<sql>` | 是 |  | SQL 语句（建议加引号） |
| `--limit` |  | 200 | 返回行数上限（默认 200） |
| `--offset` |  |  | 跳过前 N 行 |

### `sqlite tables` — 列出表 / 视图

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `<path>` | 是 |  | 数据库文件路径 |

## `tune` — 开发环境配置推荐（PHP / Nginx）

### `tune apply` — 写入建议项（自动备份 .bak；nginx 写入后跑 nginx -t，失败自动还原）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--target` | 是 |  | 目标：php / nginx |
| `--name` |  |  | PHP 版本名（--target php） |
| `--items` |  |  | 要写入的键名（逗号分隔）；省略表示全部建议 |
| `--reload` |  |  | （--target nginx）写入成功后平滑重载 |
| `--dry-run` |  |  | 只报告将做什么 |

### `tune suggest` — 按本机硬件列出开发环境推荐值（只读，不写文件）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--target` | 是 |  | 目标：php / nginx |
| `--name` |  |  | PHP 版本名（--target php；省略时若只有一个版本则用它） |
| `--only` |  |  | 只看指定项（逗号分隔键名） |

## `update` — 软件更新

### `update check` — 检查 GitHub Releases 最新版本

（无参数）

### `update download` — 下载本平台安装包（SHA-256 校验）

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--dry-run` |  |  | 只报告将做什么 |

## `version` — 输出版本与环境路径信息
