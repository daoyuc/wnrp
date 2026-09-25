# phpvm · PHP 版本管理器

**简体中文** · [English](README.en.md)

跨平台（Windows / macOS / Linux）桌面工具，把本地 PHP 开发环境集中到一个界面管理：
**多版本 PHP、Nginx、Redis、MySQL、SQLite、站点与 HTTPS**。按端口精确启停各 PHP 版本，
互不干扰（不再像旧 `start_phpXX.bat` 那样 `taskkill /IM php-cgi.exe` 一刀切）。

- 仅本机使用，无需登录；纯 Python 标准库（tkinter），**无第三方依赖**
- GUI 与命令行（`cli.py`）共用同一套 `core/*` 能力
- 界面支持 5 种语言（简中 / 繁中 / English / 日本語 / 한국어），浅色 / 深色 / 跟随系统
- 环境根目录：Windows `C:\wnrp`，其它平台 `~/wnrp`（可用 `WNRP_ROOT` 覆盖）

> 本文只描述**已实现**的功能。开发路线见 [ROADMAP.md](ROADMAP.md)，架构与流程见 [docs/](docs/README.md)，
> 给 AI / 脚本的命令契约见 [AGENTS.md](AGENTS.md)。

## 主要功能

**体检与可观测**

- **一键体检 / 502 诊断**：逐站检查 Nginx 运行、include、hosts、端口映射、PHP 运行、项目根、证书、错误日志，输出可读结论
- **首页总览仪表盘**：服务状态 + 站点告警 + 最近崩溃 / 运行日志，一屏看懂「现在环境健康吗」，支持一键全部启停
- **日志页**：Nginx / PHP / 站点日志统一查看（增量跟随、着色、过滤）；另有 phpvm 自身的「运行日志」可查可导出

**站点**

- **站点映射矩阵**：域名 / 配置文件 / 端口 / PHP 版本 / 项目根 / hosts 状态一览，异常条目高亮并说明原因
- **新建站点向导**：6 套模板（Laravel / WordPress / ThinkPHP / 通用 PHP / 静态 / SPA），自动补 include、`nginx -t`、写 hosts、平滑重载，失败可一键回滚
- **一键 HTTPS**：新建与**既有**站点都能启用 / 关闭（mkcert 优先、openssl 自签兜底；改前备份，校验失败回滚）
- **搜索与右键操作**：实时过滤；右键可切换 PHP、启用 / 禁用站点、在终端打开、复制域名 / URL、体检此站点、清 hosts、套用项目配置
- **项目级配置 `.phpvm.json`**：项目根声明 PHP 版本 / 域名 / 服务，`project apply` 一次对齐（切 fastcgi_pass、写 hosts、启服务）

**服务**

- **PHP**：多版本自动发现（含 Homebrew）、按端口启停 / 重启、编辑端口并一键同步 vhost、ini 表单编辑与推荐设置、扩展管理与在线安装、在线下载新版本、版本自检、Composer 探测、切换终端 `php`、**Xdebug 一键调试开关**
- **Nginx**：启停 / 平滑重载 / 配置检查 / 推荐设置
- **Redis**：多实例发现与启停、命令执行（危险命令二次确认）、DB 键空间图表
- **MySQL**：实例发现与启停（Windows 服务优先）、配置与错误日志
- **SQLite**：只读浏览表结构与执行查询

**数据与运维**

- **环境备份与迁移**：config.json / vhost / nginx.conf / php.ini 一键导出为 zip、一键恢复（改前备份，`nginx -t` 失败整体回滚；不含数据库数据）
- **Adminer 数据库 GUI**：下载单文件并托管为 `adminer.test`（零驱动依赖，仅供本地开发）
- **崩溃检测与自愈**：识别 php-cgi 崩溃并告警，可选独立守护进程自动拉起（防抖 + 限次）
- **自动升级**（安装版）、**开机自启**、**系统托盘**（Windows）

## 启动方式

**安装包（推荐）**

- macOS：下载 `phpvm-<版本>-macos.dmg`，把 `phpvm.app` 拖入「应用程序」（首次打开若提示来源不明，右键「打开」）
- Windows：下载 `phpvm-<版本>-windows-setup.exe` 双击安装
- 安装版启动后会静默检查新版本，可在「关于」页一键升级

**源码 / 绿色版**

```bash
python3 main.py                # macOS / Linux（也可双击 phpvm.command）
phpvm.bat                      # Windows（pythonw 后台运行，无控制台窗口）
python3 main.py --start-all    # 无界面启动整套服务（供开机自启脚本调用）
```

依赖 Python 3.10+ 与 tkinter（macOS 可 `brew install python-tk@3.13`），无需第三方包。

## 命令行入口（`cli.py`）

GUI 之外提供**无界面、完全非交互**的命令行入口，供 AI 工具 / 运维脚本使用（不 import tkinter，可在 SSH / CI 下运行）：

```bash
python3 cli.py env --json       # 环境与全部服务快照
python3 cli.py schema --json    # 全部命令 / 参数 / 默认值的自描述契约
```

- 任意命令加 `--json`：stdout 只有一个 `{"ok", "command", "data", "warnings"}` 文档（字段名英文，稳定契约）
- 退出码：`0` 成功 / `1` 业务失败（见 `data.error`）/ `2` 参数用法错误
- 安全默认：写操作支持 `--dry-run`；删除需 `--yes`；Redis 危险命令需 `--force`；改动前自动备份 `.bak`
- 覆盖 PHP / Nginx / 站点 / hosts / Redis / MySQL / SQLite / 服务编排 / 体检 / 日志 / 备份 / 项目配置 / Adminer 等分组

完整命令参考见 [docs/CLI.md](docs/CLI.md)（由 `cli.py` 自动生成，不会漂移）。

## 端口映射（默认）

| 版本目录 | 默认端口 | 说明 |
|---|---|---|
| php | 9001 | 兼容 `fund.conf` / `type_test.conf` |
| php56 | 9056 | |
| php72 | 9072 | |
| php73 | 9073 | |
| php74 | 9074 | |
| php8 | 9080 | |
| php81 | 9081 | |
| php82 | 9000 | 主版本，vhost 默认指向 |
| php85 | 9085 | |
| php83 / php84 | — | 在线安装的新版本按 `9000 + 版本号` 推导并写入 `config.json` |

vhost 里的 `fastcgi_pass 127.0.0.1:<端口>` 决定站点由哪个 PHP 版本处理；改端口后用「一键同步」替换 vhost 并平滑重载即可生效。

## 配置与数据目录

- **config.json** 保存端口映射与设置（自愈开关、语言、主题、自动启动范围等）。程序目录可写时放在程序目录；只读安装目录（`.app` / `Program Files`）下自动改用 `~/.phpvm`，因此升级替换程序文件**不会丢配置**；`PHPVM_HOME` 可强制指定数据目录
- 配置损坏或 JSON 解析失败时回退内置默认值并覆盖保存
- CLI 亦可读写：`cli.py config get settings.theme` / `cli.py config set settings.theme dark`

## 文档

| 文档 | 内容 |
|---|---|
| [AGENTS.md](AGENTS.md) | 给 AI / 自动化脚本的命令契约（唯一入口 `cli.py`） |
| [docs/CLI.md](docs/CLI.md) | 完整命令参考（自动生成） |
| [docs/MODULES.md](docs/MODULES.md) | 能力 → UI 入口 → core 落点对照表 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 分层、并发模型、数据落点、写操作铁律 |
| [docs/FLOWS.md](docs/FLOWS.md) | 启动 / 建站 / 同步端口 / 自动启动 / 自愈 / 升级流程 |
| [docs/DECISIONS.md](docs/DECISIONS.md) | 关键取舍记录 |
| [ROADMAP.md](ROADMAP.md) | 功能缺口调研与开发路线 |

## 常见问题

- **改了端口后站点 502 / 404**：保存端口后在「一键同步」对话框执行替换并重载 Nginx；或到「站点映射」页看异常高亮条目
- **php-cgi 反复崩溃（站点 502）**：状态栏会弹告警，点开可看故障模块 / 异常码 / 偏移；`0xc0000005` 常见于 opcache JIT，可在「PHP 版本管理 → 编辑配置」关闭 `opcache.jit`
- **FastCGI 用哪份 ini**：一律用版本目录内的 `php.ini`，**CLI 与 FastCGI 共用同一份配置**
- **端口被占用启动失败**：界面会提示占用进程（名称 + PID），停掉它或换端口后同步 vhost

## macOS / Linux 支持

- 环境根目录默认 `~/wnrp`；PHP 扫描 Homebrew keg，Nginx / Redis 识别 brew 安装，终端 `php` 切换维护 `~/.zshrc` 与 `~/.bash_profile` 的托管块
- hosts 写入经 `osascript` 请求系统授权；崩溃检测解析 `~/Library/Logs/DiagnosticReports` 下的 `php-cgi-*.ips`
- 安装包：`python3 packaging/build.py macos` 产出 `.dmg`；用户数据落 `~/.phpvm`，升级不丢配置
- 尚未支持：macOS 菜单栏托盘；在线安装 PHP / 扩展在 mac 改为 brew / pecl 引导
