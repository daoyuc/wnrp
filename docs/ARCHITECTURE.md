# 架构总览

phpvm 是一个**本机开发环境管理器**（PHP 多版本 + Nginx + Redis + MySQL 的启停与配置编排），
提供三条使用通道：Tk 图形界面、无界面命令行、开机静默脚本。
本文件说明「代码怎么分层、进程/线程怎么跑、状态存在哪、改代码要守哪些铁律」。

## 1. 设计原则

| 原则 | 具体含义 |
|---|---|
| 按端口精确启停 | PHP 版本以 FastCGI 监听端口为唯一身份，绝不 `taskkill php-cgi` 一刀切（`core/php_manager.py`） |
| 核心层无界面 | `core/*` 不 import tkinter，CLI / 守护进程 / 测试都能直接复用（`cli.py` 的硬约束） |
| 单一实现 | 同一件事只有一份实现：建站走 `core/site_service.py`，备份走 `core/file_backup.py`，否则 CLI 与 GUI 必然漂移 |
| 写前必备份、失败必回滚 | 改 ini / vhost / nginx.conf / hosts 前先备份，改完跑校验（`nginx -t`），失败自动还原（见 §5） |
| 模块可停用 | 可选功能可在配置里停用，停用后**不建页签、不实例化 manager、不加载其代码**（`core/modules.py`） |
| 文案全 i18n | 界面与日志文案统一 `t("中文原文")`，词条表在 `i18n/<lang>/*.json`（5 语言） |
| 跨平台收敛 | 平台差异集中在少数模块，其余代码平台无关（见 §6） |

## 2. 分层与依赖方向

```
┌── 入口层 ─────────────────────────────────────────────────────────────┐
│ main.py            GUI 入口：单实例锁 → 语言 → --start-all 分支 → 建窗口 │
│ cli.py             命令行入口：argparse → core.*（不 import tkinter）   │
│ core/crash_watchdog.py  自愈守护进程入口（可直接被 spawn 成独立进程）    │
│ phpvm.bat / phpvm.command  双击启动（Windows 用 pythonw，无控制台）      │
│ packaging/build.py + phpvm.iss  构建 .dmg / 安装包                     │
└───────────────────────────────┬───────────────────────────────────────┘
                                │ 单向依赖
┌── 界面层 ui/ ─────────────────┴───────────────────────────────────────┐
│ main_window.py  主窗口（页签 + 菜单栏 + 状态栏 + 设置区 + 8s 轮询）      │
│ *_panel.py      各功能页（php / nginx / vhost / redis / mysql / sqlite │
│                 / nginx 日志 / 运行日志）                              │
│ dialogs.py · site_wizard.py · download_dialog.py · extension_dialog.py │
│ update_dialog.py · tuning_dialog.py   各类对话框与向导                  │
│ theme.py · tray.py · window_utils.py  主题样式 / 托盘 / 窗口自适应      │
└───────────────────────────────┬───────────────────────────────────────┘
                                │ 单向依赖（ui → core，core 绝不反向）
┌── 服务层 core/ ───────────────┴───────────────────────────────────────┐
│ 基础设施：version · app_paths · config · i18n · theme(数据) · run_log   │
│           file_backup · modules · process_utils                        │
│ 领域服务：php_manager · php_installer · php_downloader · php_extension │
│           ini_editor · path_manager · nginx_manager · nginx_conf       │
│           vhost_manager · site_service · site_templates · hosts_manager│
│           cert_manager · redis_manager · mysql_manager · sqlite_manager│
│           service_group · autostart · tuning · tool_manager · icon     │
│           health_monitor · crash_watchdog · recover_history · updater  │
└───────────────────────────────┬───────────────────────────────────────┘
                                │ 通过子进程 / 系统 API 操作
┌── 系统层 ─────────────────────┴───────────────────────────────────────┐
│ C:\wnrp\{php*,nginx,Redis*,mysql}  被管理的真实程序（php-cgi / nginx…） │
│ 进程与端口 · Windows 注册表/PATH/事件日志 · hosts 文件 · PATH(PATH 切换)│
└───────────────────────────────────────────────────────────────────────┘
```

**依赖规则（评审时的硬标准）**

- `ui/` 可以 import `core/`；`core/` **不得** import `ui/`，也不得 import `tkinter`。
- `cli.py` 只依赖标准库与 `core/*`；测试（`tests/`）同样只碰 `core/*` + 临时目录。
- 新增跨平台分支时，优先放进 `process_utils.py` / `path_manager.py` / `autostart.py` /
  `nginx_manager.py` / `app_paths.py` / `theme.py`，不要散落到业务模块。

## 3. 运行时与并发模型

| 角色 | 载体 | 职责 | 约束 |
|---|---|---|---|
| 界面主线程 | Tk 主循环（`ui/main_window.py`） | 绘制与事件分发；每 **8 秒** `_tick()` 触发一轮状态刷新（面板内部自己排队异步执行），崩溃告警约 8 个 tick 查一次 | 主线程里禁止做阻塞 IO（启停服务、扫描端口一律下沉到 worker） |
| 面板 worker 线程 | 各 `*_panel.py` / `main_window` 中的 `threading.Thread(daemon=True)` | 启停服务、扫描版本、写配置等耗时动作 | 结果经 `queue` + `after()` 回投主线程渲染，禁止在工作线程直接改控件 |
| 日志写盘线程 | `core/run_log.py` 单写线程 + `atexit` 冲刷 | 调用方只入内存/队列（零磁盘 IO），批量落盘、超 1MB 轮转 | 队列满即丢弃，日志系统不阻塞业务 |
| 自愈守护进程 | `core/crash_watchdog.py`（被 `spawn()` 以 `pythonw` 拉起） | 与 GUI 生命周期解耦地轮询崩溃事件与进程失联，按防抖/限次策略重启 php-cgi | 用 `crash_watchdog.lock` 单例；关闭自愈开关后守护进程自行退出，不杀进程 |
| 单实例保护 | `main.py` | Windows 命名互斥体 `Global\wnrp_phpvm_singleton_mutex`；posix `/tmp/phpvm_singleton.sock` | 第二实例提示后退出并写一条 warn 日志；「重启」功能用 `PHPVM_RESTART=1` 抢锁 |
| 无界面服务启动 | `main.py --start-all` → `core/service_group.py` | 登录/启动时静默拉起 Nginx / Redis / MySQL / 按范围挑选的 PHP | 不创建窗口、不占单例锁；结果追加到 `autostart_services.log` |

> 记日志的接口只有 `run_log.info/ok/warn/error(scope, message)`，`scope` 取值见 `README.md`「运行日志页」。

## 4. 运行期状态与落点

| 文件 / 位置 | 内容 | 谁写 | 备注 |
|---|---|---|---|
| `config.json` | 端口映射 `ports` + 设置 `settings`（语言、主题、模块开关、自启动范围…） | `core/config.py` | 包目录可写时就在仓库根；只读安装态自动改用 `~/.phpvm`（`core/app_paths.py`） |
| `run_log.log` / `.1` | phpvm 自身运行轨迹（启停结果、异常、环境快照） | `core/run_log.py` | 内存保留最近 2000 条；`PHPVM_RUN_LOG=0` 可整体关闭 |
| `recover_history.json` | 自愈决策历史（判定与结果） | `core/recover_history.py` | GUI「崩溃详情 → 自愈历史」可查 |
| `crash_watchdog.json` / `.lock` / `.log` | 守护进程看护清单、单例锁、守护自身日志 | `core/crash_watchdog.py` | 均为运行期文件，不入库 |
| `autostart_services.log` | 每次 `--start-all` 的逐项结果（含 `php_scope=`） | `main.py` | 排查「登录后服务没起来」的第一现场 |
| `updates/` | 升级包下载缓存（`.part` 原子改名 + SHA-256 校验） | `core/updater.py` | 可从关于页直达 |
| `C:\wnrp\{php*,nginx,Redis*,mysql}` | 被管理的真实程序本体 | 用户 / 安装器 | 环境的唯一事实来源；根路径可用 `WNRP_ROOT` 覆盖 |

**强耦合提醒**：`config.json` 的端口 ↔ 各 vhost 的 `fastcgi_pass 127.0.0.1:<端口>` 必须一致，
改端口后要走「一键同步 vhost」（`site sync-port` / 界面自动弹窗），否则站点 502。

## 5. 横切铁律：任何「写外部文件」都必须这样写

```
校验入参 → core/file_backup.backup() 生成 .bak（hosts 用 .phpvm.bak）
        → 最小范围替换（只改目标 server 块 / 目标 ini 行）
        → 校验（nginx -t / php -v 自检 / JSON 解析）
        → 失败：自动还原全部备份并向调用方返回错误（绝不半途而废）
        → 成功：必要时 reload（nginx）或提示重启该版本（php.ini）
```

配套约定：

- **CLI 写操作先 `--dry-run`**，删除类操作要 `--yes`，危险 Redis 命令要 `--force`。
- **提权只用于 hosts**：Windows PowerShell RunAs / macOS osascript，失败即中止并提示。
- **只操作自己的托管块/文件**：hosts 只动 `# >>> phpvm-managed >>>` 块；站点禁用用改名
  `.conf.disabled`，不删文件。
- **界面与日志文案一律 `t()`**；新增文案后跑 `python _i18n_scan.py --report` 补词条。

## 6. 平台差异集中点

| 模块 | 差异内容 |
|---|---|
| `core/process_utils.py` | 进程/端口快照（Win `GetExtendedTcpTable` + `EnumProcesses`；posix `lsof` / `ps`）、开终端、打开路径 |
| `core/path_manager.py` | 终端 `php` 切换（Win 改用户 PATH 置顶；posix 写 shell rc 的 phpvm 块） |
| `core/autostart.py` | Win 写 `HKCU\...\Run` 与「启动」目录 vbs；mac 写 LaunchAgent plist |
| `core/nginx_manager.py` | Win `-p` 前缀模式 vs brew 布局；vhost / 日志目录由生效配置推导 |
| `core/app_paths.py` | 数据目录可写性判定（包目录 → `~/.phpvm`） |
| `core/theme.py` | 系统深浅色探测（注册表 / `defaults` / `gsettings`） |
| `ui/tray.py` | 托盘仅 Windows 实现（macOS/Linux 以 Dock 驻留代替，`_init_tray` 直接返回） |

## 7. 新增一个功能，落点在哪

| 要做的事 | 落点 |
|---|---|
| 新增一个服务管理器（如 Memcached） | `core/<name>_manager.py`（纯逻辑）+ `ui/<name>_panel.py`（页签）+ 在 `core/modules.py` 注册可选模块 + `main.py` 按开关实例化 |
| 新增一个 CLI 命令 | `cli.py` 内加 `cmd_*` + `build_parser()` 里 `_leaf(...)`；随后跑 `python3 _docs_cli.py` 重新生成 `docs/CLI.md`（`tests/test_docs.py` 会校验一致性），契约以 `schema --json` 为准 |
| 新增一个设置项 | `core/config.py` 的 `DEFAULT_SETTINGS`（否则 `config set` 会拒绝）+ 界面/自启路径读取 |
| 新增一个桌面集成/写系统文件的能力 | 守 §5 铁律；备份走 `core/file_backup.py` |
| 新增界面文案 | `t()` 包裹 → `python _i18n_scan.py --report` → 补 4 语言词条 |
| 新增单元测试 | `tests/test_<模块>.py`（标准库 unittest，用 `tests/fakes.py` 的临时目录 + 假 nginx，禁止碰真实配置与 hosts） |

> 架构层的新增/变更（新层、新进程、新运行期文件、新横切约定）必须同步更新本文件；
> 功能落点变化同步更新 [`MODULES.md`](MODULES.md)；流程变化同步更新 [`FLOWS.md`](FLOWS.md)。
