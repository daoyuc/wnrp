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
- **状态栏系统内存槽位**：底部状态栏常驻显示**整机**内存占用率与「已用 / 总量」（每 8 秒刷新；Windows / Linux / macOS 均可用，取不到时显示「内存 —」）。与总览页的「服务状态」不同，后者只统计 phpvm 管理的进程

**站点**

- **站点映射矩阵**：域名 / 配置文件 / 端口 / PHP 版本 / 项目根 / hosts 状态一览，异常条目高亮并说明原因
- **新建站点向导**：6 套模板（Laravel / WordPress / ThinkPHP / 通用 PHP / 静态 / SPA），自动补 include、`nginx -t`、写 hosts、平滑重载，失败可一键回滚
- **一键 HTTPS**：新建与**既有**站点都能启用 / 关闭（mkcert 优先、openssl 自签兜底；改前备份，校验失败回滚）
- **搜索与右键操作**：实时过滤；右键可切换 PHP、启用 / 禁用站点、在终端打开、复制域名 / URL、体检此站点、清 hosts、套用项目配置
- **项目级配置 `.phpvm.json`**：项目根声明 PHP 版本 / 域名 / 服务，`project apply` 一次对齐（切 fastcgi_pass、写 hosts、启服务）

**服务**

- **PHP**：多版本自动发现（含 Homebrew）、按端口启停 / 重启、编辑端口并一键同步 vhost、ini 表单编辑与推荐设置、扩展管理与在线安装、在线下载新版本、版本自检、Composer 探测、切换终端 `php`、**Xdebug 一键调试开关**、**按版本勾选开机自启**（点击列表「开机自启」列切换，配合设置区「按 PHP 页勾选」策略）
- **Nginx**：启停 / 平滑重载 / 配置检查 / 推荐设置
- **Redis**：多实例发现与启停、命令执行（危险命令二次确认）、DB 键空间图表
- **MySQL**：实例发现与启停（Windows 服务优先；除环境根 `mysql*` 外还会自动探测服务 / PATH / 常见安装位置，可用 `settings.mysql_dirs` 显式指定）、配置与错误日志
- **SQLite**：只读浏览表结构与执行查询

**数据与运维**

- **环境备份与迁移**：config.json / vhost / nginx.conf / php.ini 一键导出为 zip、一键恢复（改前备份，`nginx -t` 失败整体回滚；不含数据库数据）
- **Adminer 数据库 GUI**：下载单文件并托管为 `adminer.test`（零驱动依赖，仅供本地开发）
- **邮件捕获**：PHP `mail()` 邮件落盘为 `.eml` 并在「邮件」页查看 —— Unix 用 `sendmail_path` 垫片脚本（零外部依赖），Windows 用内置 SMTP sink（`127.0.0.1:1025`，随 phpvm 运行，见「邮件」页）
- **崩溃检测与自愈**：识别 php-cgi 崩溃并告警，可选独立守护进程自动拉起（防抖 + 限次）；看护列表跨重启按开机自启范围裁剪 —— 上一轮运行过的其它版本不会被「失联复活」；手动或一键停止会同步解除看护，停止后不会被守护进程拉回
- **自动升级**（安装版）、**开机自启**、**系统托盘**（Windows）

**插件**

- **内置插件框架**：插件随版本分发，在「关于 → 模块」统一启用 / 停用；停用后不建导航项、不加载其代码，也不再拉起后台记录进程
- **键鼠统计**（Windows / macOS）：独立后台进程记录按键次数与时间点，按今日 / 本周 / 本月 / 本年 / 全部汇总 —— 大数字卡（按键总数与日均、打字速度与峰值 / 均值）、键位 Top 10 横条榜、24 小时活动柱状图；支持暂停 / 继续、显示报告、导出 CSV、清空记录与登录后自动运行。**只记录按了哪个键，不记录输入内容、窗口标题或剪贴板**；关闭 phpvm 主窗口后仍继续记录。macOS 首次使用需在「系统设置 → 隐私与安全性 → 输入监控」中授权（面板会引导），授权后重启后台记录即可
- **项目启动器**：自动汇总本地项目（`<WNRP_ROOT>/www/*` + 站点 root 去重，读取 `.phpvm.json` 里的 PHP 版本 / 域名等元信息），从站点映射（vhost）的 `server_name` 生成访问地址：选中项目后点「在浏览器打开」、双击项目行，或右键选择具体域名 / 复制 URL；也可按 `composer.json` / `artisan` / `package.json` 等标志文件推荐常用命令，一键在终端打开并前置对应 PHP 版本的 PATH。命令只来自内置白名单或你自己添加的设置项，**不会自动执行，也绝不读取项目里的配置当命令运行**；URL 同样不采信 `.phpvm.json` 的域名。支持添加 / 删除自定义命令（可限定单个项目）；没有建站点的项目只能打开目录 / 终端，浏览器按钮会置灰
- **访问日志聚合**：只读解析 nginx `access.log`（含 `.log.N` 轮转文件），汇总状态码分布、Top 路径 / IP / UA、按小时请求量与最近 4xx/5xx 明细，支持「只看错误请求」快速排查。日志只能从候选文件名里选，不读项目内任何文件，结果不落库、不上传（ADR 17）
- **站点巡检**：从 nginx vhost 的 `server_name` 生成待探测域名，只发 `GET /` 做一次可用性探测，给出状态码、耗时、页面大小与证书剩余天数（本地自签站点降级探测并标注「自签」）；自动刷新按 60 秒节流。地址只来自 vhost、不跟随重定向、不带 Cookie、不经系统代理，结果不落库（ADR 20）
- **Git 工作区**：把本地项目的 Git 状态汇总到一屏 —— 当前分支、未提交文件数（暂存 / 修改 / 未跟踪 / 冲突）、与上游的领先 / 落后、最后提交时间与标题，双击直接在该目录打开终端。仓库来源是「项目启动器」的扫描目录加上环境根下的仓库，**只跑 `git status` / `git log` 两条只读命令**：不 fetch、不 pull、不提交，也不写 index；自动刷新按 60 秒节流（ADR 22）
- **SSH 管理**：主机清单（名称 / 地址 / 端口 / 用户名 / 密码或私钥 + 口令 / 标签 / 备注）与命令片段库（可关联多台主机或设为通用，按主机与标签筛选、复制）。「测试连接」真实握手并回显耗时、服务端标识与指纹 —— **首次必须手动确认指纹**后才记为可信，不做静默信任；「打开终端」用系统终端建会话，密码在终端窗口内输入；选中片段可在远端执行并回显 stdout / stderr 与退出码（默认 30 秒超时、每个流 256 KB 上限并标注截断）。凭据单独存 `ssh_hosts.json`（POSIX 0600，不进 `config.json`、不进备份导出包），片段存设置随备份迁移；片段**永不自动执行**。**保存与连通性无关**：主机信息填完点「确定」就存下来，即便当时连不上、密码或私钥还没拿到 —— 这类主机在详情里标「凭据未配置」，等真有凭据时再编辑补上。私钥认证没选文件时，自动从 `~/.ssh` 按 `id_ed25519` → `id_ecdsa` → `id_rsa` → `id_dsa` 的顺序挑一个现成的填进表单（只是默认值，路径可见可改，仍不启用 ssh-agent）（ADR 23 / 24）

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

依赖 Python 3.10+ 与 tkinter（macOS 可 `brew install python-tk@3.13`），**核心无需第三方包**。
唯一可选依赖是 SSH 管理用的 `paramiko`：源码运行 `pip install -r requirements.txt`，安装包已自带 `vendor/`；
未安装时只有「SSH 管理」页签与 `ssh` 命令组提示安装，其余功能不受影响。

## 命令行入口（`cli.py`）

GUI 之外提供**无界面、完全非交互**的命令行入口，供 AI 工具 / 运维脚本使用（不 import tkinter，可在 SSH / CI 下运行）：

```bash
python3 cli.py env --json       # 环境与全部服务快照
python3 cli.py schema --json    # 全部命令 / 参数 / 默认值的自描述契约
```

- 任意命令加 `--json`：stdout 只有一个 `{"ok", "command", "data", "warnings"}` 文档（字段名英文，稳定契约）
- 退出码：`0` 成功 / `1` 业务失败（见 `data.error`）/ `2` 参数用法错误
- 安全默认：写操作支持 `--dry-run`；删除需 `--yes`；Redis 危险命令需 `--force`；改动前自动备份 `.bak`
- 覆盖 PHP / Nginx / 站点 / hosts / Redis / MySQL / SQLite / 服务编排 / 体检 / 日志 / 备份 / 项目配置 / Adminer / 插件（`plugin`）/ 键鼠统计（`keymouse`）/ 项目启动器（`launcher`）/ 访问日志聚合（`insights`）/ 站点巡检（`health`）/ Git 工作区（`repos`）/ SSH 管理（`ssh`）等分组

SSH 管理常用命令（密码只从 stdin 读，绝不进命令行）：

```bash
python3 cli.py ssh host add --name demo --host 10.0.0.8 --user deploy \
    --auth key --key-path ~/.ssh/id_ed25519 --tags prod --dry-run --json
python3 cli.py ssh host test demo --json              # 首次会被指纹校验拒绝
python3 cli.py ssh host test demo --trust-host --json # 确认指纹无误后再信任
python3 cli.py ssh host connect demo --dry-run --json # 只看将执行的 ssh 命令行
python3 cli.py ssh snippet add --name "磁盘占用" --command-text "df -h" --host demo
python3 cli.py ssh snippet run snip-xxxx --host demo --dry-run --json
python3 cli.py ssh snippet run snip-xxxx --host demo --yes --json
```

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

- **config.json** 保存端口映射与设置（自愈开关、语言、主题、自动启动范围与各版本勾选名单等）。程序目录可写时放在程序目录；只读安装目录（`.app` / `Program Files`）下自动改用 `~/.phpvm`，因此升级替换程序文件**不会丢配置**；`PHPVM_HOME` 可强制指定数据目录
- 配置损坏或 JSON 解析失败时回退内置默认值并覆盖保存
- **插件设置与数据**：插件设置统一落 `settings.plugin_settings.<插件 key>`——键鼠统计的保留天数在 `retention_days`，项目启动器的自定义命令在 `project_launcher.custom_commands`；键鼠统计的数据在 `<数据目录>/plugins/keymouse/` —— `stats.db`（SQLite / WAL 聚合表）与 `daemon.lock` / `daemon.heartbeat` / `daemon.stop` / `paused` / `daemon.log`。均为运行期文件，不入库，也不上传
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
