# 功能模块地图

「某个能力在哪、改它要动哪些文件」的查表入口。**架构与约定**见 [`ARCHITECTURE.md`](ARCHITECTURE.md)，
**用户视角的功能说明**见 [`../README.md`](../README.md)。

## 1. 主窗口结构

页面由 `core/modules.py` 的模块开关动态构建，**左侧竖向导航**（`ui/nav.py::SideNav`）按固定分组与顺序排列
（括号为模块 key / 分组来源，`ui/main_window.py::_build()`）：

```
服务  总览(overview) │ PHP 版本管理(php,刚需) │ Nginx 管理(nginx,刚需) │ Redis 管理(redis) │ MySQL 管理(mysql)
站点  站点映射(vhost,刚需) │ SQLite 数据库(sqlite)
诊断  Nginx 日志(log) │ 邮件(mail) │ 运行日志(常驻，不受开关影响) │ 访问日志聚合(access_insights)
其他  SSH 管理(ssh) │ 键鼠统计(keymouse) │ 项目启动器(project_launcher) │ 站点巡检(site_health) │ Git 工作区(git_status) │ 关于(设置区/模块开关在此)
```

- 顶部：菜单栏（外观 / 语言 / 帮助）、状态栏（就绪消息 + 整机内存槽位 + 更新提示 + 崩溃告警）。
- 分组定义在 `core/modules.py::GROUPS`（服务 / 站点 / 诊断 / 其他）；内置模块的归属写在 `_BUILTIN` 的
  `group` 字段，插件用 `PluginSpec.nav_group` 声明（默认 `other`）。页面按分组归位、组内保持登记顺序。
- 插件页在 `_build()` 末尾按 `plugins.enabled_specs(config)` 动态追加，面板经 `importlib` 从 `ui/plugins/`
  延迟导入；单个插件导入失败只记 warn 并跳过该导航项。
- 切页只 `pack` 当前页、其余 `pack_forget()`：`_panel_visible()` 依赖的 `winfo_ismapped()` 语义与旧页签一致，
  不可见页的自动刷新照旧暂停。
- 当前页面记在 `settings.last_page`，重启回到上次停留页；该页已停用（模块/插件被关）时回落第一页。
- 服务状态圆点（PHP / Nginx / Redis / MySQL）：`MainWindow._refresh_nav_states()` 在 worker 线程探测、主线程
  写 UI，复用 `_tick()` 的 4 轮降频（≈32s）；状态未变化不重绘。
- 托盘（Windows）：动态菜单 —— 一键启停整套服务、Nginx 子菜单、各 PHP 版本、Redis 子菜单、显示/隐藏/退出。
- 导航构建在 `ui/main_window.py::_build()`；「关于」页（含设置区、模块开关、服务编排按钮）在 `_build_about()`。

## 2. 能力 → 落点对照表

| 能力 | UI 入口 | core 落点 | 持久化 / 副作用 | 关键约束 |
|---|---|---|---|---|
| PHP 版本扫描与启停 | `ui/php_panel.py` | `php_manager.py`、`process_utils.py` | `config.json → ports` | 按端口 + PID + 命令行三重校验，不误杀其它版本 |
| php.ini 查看 / 编辑 / 初始化 | `ui/php_panel.py`、`ui/dialogs.py` | `ini_editor.py`、`php_manager.ini_target/init_ini` | `<版本目录>/php.ini` + `.bak` | CLI 与 FastCGI 共用同一份 `php.ini`（`php-web.ini` 已废弃） |
| PHP 扩展启停 / 在线安装 | `ui/extension_dialog.py` | `php_extension.py`、`php_downloader.py` | ini 写回 + `.bak` | PECL / xdebug.org 按「主版本 + NTS/TS + 编译器 + 架构」匹配；下载复用 `php_downloader`（并行分块 / 断点续传 / 有界重试），对话框有进度条 + 速度，关闭窗口即中断下载 |
| 下载安装新版 PHP | `ui/download_dialog.py` | `php_installer.py`、`php_downloader.py` | 新版本目录 + `config.ports` | SHA-256 校验、防穿越解压、端口按 `9000+版本号` 推导；下载走多连接分块并行（服务器支持 Range 时自动启用，单流兜底），分片与单流均断点续传；目标已是 PHP 版本目录时**版本内升级**：整体替换文件、保留用户 `php.ini`、运行中拒绝、失败恢复旧版 |
| 终端 `php` 版本切换 | 主窗口顶部下拉 / 对话框 | `path_manager.py` | Win 用户 PATH / posix shell rc | 仅影响新开的终端窗口 |
| 版本自检 | `ui/dialogs.py` | `health_monitor.py` | — | 3 项：版本解析 / 关键扩展 / 配置加载 |
| Nginx 启停 / 重载 / 配置检查 | `ui/nginx_panel.py` | `nginx_manager.py`、`nginx_conf.py` | nginx 进程；改 `nginx.conf` 前 `.bak` | 改配置后必须 `nginx -t` 通过才 reload，失败回滚 |
| 站点映射 / 端口同步 / include 检测 | `ui/vhost_panel.py` | `vhost_manager.py` | `vhost/*.conf` + `.bak` | 只改目标 `server` 块；端口与 `config.ports` 强耦合 |
| 站点搜索过滤 / 行操作扩展 | `ui/vhost_panel.py` | — | 只读（终端 / 复制 / 体检均不改配置） | 顶部搜索框实时过滤（域名 / root / 文件 / PHP / 端口 / 说明）；右键补「在终端打开、复制域名 / URL、体检此站点」 |
| 既有站点一键 HTTPS | `ui/vhost_panel.py` 右键 / `site secure`（`unsecure`） | `site_service.secure_site/unsecure_site`、`site_templates.apply_https`、`cert_manager.py` | vhost（`.bak`）+ `<nginx>/SSL/` 证书 | 追加 / 移除 443 server 块，保留原站点规则；`nginx -t` 失败回滚 |
| 一键体检 / 502 诊断 | `ui/vhost_panel.py` 的「一键体检」按钮 / `diag` | `diag.py` | 只读（必要时调用 start / ensure_include，均自带备份 + nginx -t 回滚） | 串联 vhost/php/nginx/hosts 只读检查；修复动作复用既有 manager |
| 站点与应用日志聚合 | `ui/nginx_log_panel.py`（升级为「日志」页）/ `logs` | `log_sources.py` | 只读（不改任何文件；`settings.log_last_source` 记忆上次来源） | 按「来源」（Nginx / PHP / 站点）推导候选日志，复用既有增量读 + 着色 + 过滤 |
| 首页总览仪表盘 | `ui/overview_panel.py` / `overview` | `overview.py` | 只读（聚合各 manager 状态，不新增轮询/进程；模块可停用） | 服务状态卡（内嵌每服务内存/CPU 占用，F12）+ 站点告警（未映射 hosts / 端口未映射 / 证书缺失）+ 最近崩溃/运行日志；一键全部启动/停止/体检全部 |
| 轻量资源监控（CPU / 内存） | `monitor` / 总览面板「服务状态」行内 | `resource_monitor.py` | 只读（不引 psutil；posix 走 `ps` 一次快照 + TTL 缓存，Windows 走 ctypes 取 RSS/CPU 时间） | 按服务聚合 phpvm 管理的 nginx / 各 PHP 版本 / Redis / MySQL 进程的 RSS 与 CPU%；CLI `monitor snapshot` 输出机器可读快照，GUI 在总览行内展示并合计；**Windows 服务进程**（mysqld 等以 SYSTEM 运行、非提权 `OpenProcess` 一律拒绝）自动改用 `NtQuerySystemInformation` 系统级快照补 RSS/CPU，不再恒显示「— / 未知」 |
| 环境备份与迁移 | `ui/main_window.py` 关于页 / `backup` | `backup_bundle.py` | 导出 zip；恢复时改前 `.bak` | 只打包配置（config.json / vhost / nginx.conf / php.ini）；恢复后 `nginx -t` 失败整体回滚；不含数据库数据 |
| 邮件捕获（.eml） | `ui/mail_panel.py` / `mail` | `mail_catcher.py`、`mail_sink.py` | php.ini（`.bak`）+ `<数据目录>/mail/*.eml` | 两种落地方式同一目录：Unix 用 `sendmail_path` POSIX 垫片；Windows 把 `SMTP`/`smtp_port`/`sendmail_from` 指向内置 sink（`mail_sink.py`，127.0.0.1:1025，随 phpvm 运行）；关闭时还原用户原值 |
| 项目级配置 `.phpvm.json` | `ui/vhost_panel.py` 右键 / `project` | `project_config.py` | vhost（`.bak`）+ hosts + 服务进程 | `apply` 按配置对齐：切站点 PHP 端口 / 写 hosts / 启服务；不改项目业务代码；用 JSON 避免 YAML 依赖 |
| Adminer 数据库 GUI | `ui/main_window.py` 关于页 / `adminer` | `adminer.py` | `<数据目录>/adminer/index.php` + 站点配置 | 下载单文件 Adminer 并托管为 `adminer.test`；离线可「用户自备」；写库能力仅限本地开发 |
| 新建站点向导 / HTTPS 证书 | `ui/site_wizard.py` | `site_service.py`、`site_templates.py`、`cert_manager.py` | vhost + hosts + 证书文件 | 5 步编排，任一步失败可整体回滚 |
| hosts 写入 / 移除 / 还原 | `ui/vhost_panel.py` 按钮、向导 | `hosts_manager.py` | 系统 hosts + `hosts.phpvm.bak` | 只动 `# >>> phpvm-managed >>>` 块；写入需提权 |
| Redis 多实例管理 | `ui/redis_panel.py` | `redis_manager.py` | redis 进程 / 日志 | 危险命令需二次确认 |
| MySQL 实例管理 | `ui/mysql_panel.py` | `mysql_manager.py` | Windows 服务 / 进程 | 实例发现：环境根 `mysql*` + 自动探测（Windows 服务 bin 路径 / PATH / 常见安装位置）+ `settings.mysql_dirs` 显式指定，`bin` 目录自动上溯；服务模式启停需管理员，非管理员时禁用并提示；「刷新状态」会重新发现实例 |
| SQLite 只读查询 | `ui/sqlite_panel.py` | `sqlite_manager.py` | `settings.sqlite_last_db` | 只读（mode=ro + query_only + 关键字白名单） |
| Nginx 日志查看 | `ui/nginx_log_panel.py` | `nginx_manager.py`（日志目录推导） | — | 增量 tail + 自动跟随 |
| 运行日志 | `ui/run_log_panel.py` | `run_log.py` | `run_log.log(.1)` | 内存 2000 条 / 单文件 1MB 轮转 |
| 整套服务一键启停 | 关于页按钮、托盘菜单 | `service_group.py` | 各服务进程 | 顺序：启动 PHP→Redis→MySQL→Nginx，停止反向 |
| 自动启动的 PHP 范围 | 关于页「自动启动服务的 PHP 版本」 | `service_group.php_targets()` | `settings.autostart_php_scope` | `newest`(默认)/`used`/`active`/`custom`/`all`；手动「全部启动」与停止始终覆盖全部 |
| 逐个版本的开机自启勾选 | PHP 页「开机自启」列（点击切换） | `php_panel._toggle_autostart()` → `service_group.set_autostart_versions()` | `settings.autostart_php_versions` | 仅 `custom` 策略下生效；勾选/取消会自动把策略切到 `custom` |
| 开机自启 phpvm / 服务 | 关于页设置区 | `autostart.py`、`main.py --start-all` | Win Run 项 + 启动目录 vbs / mac LaunchAgent | 服务启动范围同上；日志 `autostart_services.log` |
| 崩溃检测与自愈 | 状态栏告警 + 崩溃详情对话框 | `health_monitor.py`、`crash_watchdog.py`、`recover_history.py` | `recover_history.json`、`crash_watchdog.*` | 自愈默认关闭；防抖 + 每小时限次 + 手动停止宽限期；守护启动首轮按开机自启范围裁剪看护列表（见 `_prune_watch_for_boot`） |
| 开发环境配置推荐 | `ui/tuning_dialog.py` | `tuning.py`、`nginx_conf.py` | php.ini / nginx.conf + `.bak` | 按硬件分档出建议，逐条勾选后写入，`nginx -t` 失败回滚 |
| 外部工具（Composer） | `ui/php_panel.py` | `tool_manager.py` | — | 以所选 PHP 版本运行（临时前置 PATH） |
| 软件更新 | `ui/update_dialog.py` + 状态栏 | `updater.py`、`version.py`、`app_paths.py` | `updates/` 缓存 | 检查 GitHub Releases → SHA-256 校验 → 替换后重启 |
| 模块开关 | 关于页「功能模块」 | `modules.py` | `settings.disabled_modules` | 刚需：PHP / Nginx / 站点映射；停用后**重启**才不加载 |
| 界面语言 / 外观主题 | 菜单栏 + 关于页 | `i18n.py`、`core/theme.py`、`ui/theme.py` | `settings.lang` / `settings.theme` | 语言重启生效；主题立即生效（走各面板 `refresh_theme()`） |
| 托盘 | `ui/tray.py`（Windows） | `icon.py` | `phpvm.ico`（生成） | 最小化即隐藏到托盘；关闭框提供「重启」 |
| CLI（AI / 脚本通道） | `cli.py` | 全部 `core/*` | 同 GUI（共用 config.json） | 不 import tkinter；写操作支持 `--dry-run` |
| 打包与发布 | `packaging/build.py`、`packaging/phpvm.iss` | `.github/workflows/release.yml` | `dist/` | 打 tag 自动构建双平台并发布 Release |
| 状态栏整机内存槽位 | 主窗口底部状态栏（`ui/main_window.py::_refresh_memory()`） | `core/system_memory.py` | 只读（不写文件、不缓存进程状态） | Windows `GlobalMemoryStatusEx` / Linux `/proc/meminfo` / macOS `sysctl`+`vm_stat`；每 8s 由 worker 采集、只经 `_post()` 回主线程写 `StringVar`；取不到显示「内存 —」而非报错；与 `core/resource_monitor.py` 的「phpvm 管理进程 RSS/CPU」不是一回事 |
| 插件框架（发现 / 开关 / 设置 / 后台拉起） | 关于页「功能模块」（自动并入） | `core/plugins/__init__.py`、`base.py`、`settings.py`、`core/modules.py` | `settings.disabled_modules`、`settings.plugin_settings` | 内置目录只声明 `MANIFEST: PluginSpec`，`pkgutil` + `importlib` 发现；单插件导入失败只 warn、不阻断启动；设置页与启动快照无需各写一遍枚举 |
| 键鼠统计（今日 / 本周 / 本月 / 本年 / 全部） | `ui/plugins/keymouse_panel.py` / `plugin`、`keymouse` 命令组 | `core/plugins/keymouse/`（`collector`、`store`、`stats`、`daemon`、`keymap`） | `<数据目录>/plugins/keymouse/stats.db` + `daemon.*` / `paused` | 独立守护进程写库，GUI / CLI 只读 `mode=ro`；Windows / macOS 采集（macOS 需「输入监控」授权）；只落 vkCode + 时间戳；插件停用后守护进程 ≤60s 自行退出 |
| 项目启动器（本地项目 → 站点 / 命令 / 编辑器 → 浏览器、终端或 IDE） | `ui/plugins/project_launcher_panel.py` / `launcher` 命令组 | `core/plugins/project_launcher/`（`projects`、`commands`、`editor`）、`core/process_utils.py::open_terminal()` | `settings.plugin_settings.project_launcher.www_dir` / `custom_commands` / `editor` / `recent` / `recent_first` | 只读扫描 `www_dir`（默认 `<WNRP_ROOT>/www`，可在面板或 `launcher set-www` 改）+ vhost root 并去重；站点 URL 只由 vhost 的 `server_name` 生成（`launcher browse` / 面板按钮、双击、右键），命令只来自内置白名单或用户手动添加，编辑器只来自内置探测或用户模板且按 argv 启动（不拼 shell），**绝不读 `.phpvm.json` 当命令或 URL 来源**（ADR 16 / ADR 19）；无后台进程、无自动执行 |
| 访问日志聚合（access.log → 状态码 / 热门路径 / 错误请求） | `ui/plugins/access_insights_panel.py` / `insights` 命令组 | `core/plugins/access_insights/aggregate.py` | 无（不落库、不写任何文件） | 只读 nginx 自身日志目录里的 `*access*.log`（含轮转），文件名只能从候选列表里选、`realpath` 前缀校验；解析失败的行计入 `skipped`；**不读项目内任何文件**；无后台进程、无自动执行（ADR 17） |
| 站点巡检（vhost 域名 → 状态码 / 耗时 / 证书剩余天数） | `ui/plugins/site_health_panel.py` / `health` 命令组 | `core/plugins/site_health/probe.py` | 无（不落库、不写任何文件） | 待探测地址只由 nginx vhost 的 `server_name` 生成（泛解析 / `_` / 非法域名 / 禁用站点跳过），只发 `GET /`、**不跟随重定向**、不带 Cookie、**不经系统代理**（`http.client` 直连），响应体最多读 64 KB；HTTPS 证书校验失败（本地自签）降级不校验并标 `insecure`；无后台进程；面板自动刷新按 60s 节流（ADR 20） |
| Git 工作区（本地项目 → 分支 / 未提交 / 领先落后 / 最近提交） | `ui/plugins/git_status_panel.py` / `repos` 命令组 | `core/plugins/git_status/repos.py` | 无（不 fetch / pull / commit，不写任何文件） | 只跑 `git status --porcelain=v2 --branch` 与 `git log -1`，argv 调用不经 shell，`--no-optional-locks` + `GIT_TERMINAL_PROMPT=0` 保证不写 index、不等凭据；仓库来源复用项目启动器的发现口径（www 一层 + vhost root）并补环境根下含 `.git` 的目录，不递归磁盘；`ThreadPoolExecutor` 4 线程、单仓库 6s 超时；无后台进程；面板自动刷新按 60s 节流（ADR 22） |
| SSH 管理（主机清单 → 连接测试 / 交互式终端 / 命令片段远端执行） | `ui/ssh_panel.py` / `ssh` 命令组 | `core/ssh_store.py`（主机与片段持久化）、`core/ssh_manager.py`（paramiko 传输） | `ssh_hosts.json`（凭据，权限收紧）+ `ssh_known_hosts`（显式确认过的指纹）+ `config.json → settings.ssh_snippets`（片段） | 凭据不进 `config.json`、不进备份导出包与安装包；**保存只做本地字段校验、不要求连通**（密码 / 私钥可先空着，详情标「凭据未配置」，连接时由 `MissingCredential` 提示补齐，`error_kind=credential`）；私钥认证留空时按 OpenSSH 惯例自动选用 `~/.ssh` 里的 `id_ed25519` / `id_ecdsa` / `id_rsa` / `id_dsa`（目录可用 `PHPVM_SSH_KEY_DIR` 覆盖），只是表单默认值、路径可见可改，不启 ssh-agent；首次连接一律拒绝并要求确认指纹（`RejectPolicy` + `--trust-host`）；密码只经 `--password-stdin` 或终端窗口输入，绝不进命令行；远端执行需 `--yes` 且支持 `--dry-run`，stdout / stderr 各限 256 KB 并标 `truncated`；片段永不自动执行；首次用到片段时预置 20 条只读运维命令（`ssh_store.builtin_snippets()`，用 `settings.ssh_snippets_builtin` 记版本号，删掉不复活）；缺少 `paramiko` 时降级为安装提示（ADR 23 / 24） |

> 跨模块约定：`core/*` 的全部 HTTPS 请求（PHP 版本/扩展列表、PHP 与 Adminer 下载、软件更新）
> 统一经 `core/net_ssl.py::ssl_context()` 取 SSL 上下文 —— 系统默认根 + 仓库内置
> `core/cacert.pem`（Mozilla CA 集，certifi / MPL-2.0），用于规避 Windows 证书库里
> 已过期的交叉签名根（如 DST Root CA X3）；证书校验与主机名校验始终开启。
> 详见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

## 3. 模块开关实际影响

`core/modules.py` 是唯一注册表（`key / name / required`）：

| key | 名称 | 刚需 | 停用后的效果 |
|---|---|---|---|
| `php` | PHP 版本管理 | ✅ | 不可停用 |
| `nginx` | Nginx 管理 | ✅ | 不可停用 |
| `vhost` | 站点映射 | ✅ | 不可停用 |
| `redis` | Redis 管理 | ❌ | 不建导航项、不实例化 `RedisManager`、不导入该模块代码 |
| `mysql` | MySQL 管理 | ❌ | 同上 |
| `sqlite` | SQLite 数据库 | ❌ | 同上 |
| `log` | Nginx 日志 | ❌ | 同上 |
| `keymouse` | 键鼠统计 | ❌ | 不建插件导航项、不拉起后台记录进程；已在运行的守护进程下一轮配置检查（≤60s）自行 flush 并退出 |
| `project_launcher` | 项目启动器 | ❌ | 不建插件导航项；无后台进程需停止，已添加的自定义命令保留在 `plugin_settings` 中 |
| `access_insights` | 访问日志聚合 | ❌ | 不建插件导航项；无后台进程与数据文件，停用不影响 nginx 日志本身 |
| `site_health` | 站点巡检 | ❌ | 不建插件导航项；无后台进程、无数据文件，停用不影响 nginx / 站点本身 |
| `git_status` | Git 工作区 | ❌ | 不建插件导航项；无后台进程、无数据文件，停用不影响任何仓库（本插件从不写仓库） |
| `ssh` | SSH 管理 | ❌ | 不建导航项、不实例化 `SshPanel`、不 import `core/ssh_manager`；已保存的主机与片段保留在 `ssh_hosts.json` / `settings.ssh_snippets`，`ssh` 命令组返回「模块已停用」错误 |

插件的 key / 名称来自各 `core/plugins/<name>/` 的 `MANIFEST`，由 `core/modules.py` 在启动期自动并入
`MODULES` —— 关于页勾选框、启动快照与 CLI `plugin enable/disable` 因此无需各写一遍。

停用是**启动期行为**（界面只在启动时构建），改完需重启 phpvm；`--start-all` 与 CLI 也遵守该开关
（停用的模块不加载其 manager，因此不会去启停对应服务）。

## 4. 服务与端口约定

- PHP 版本目录 `C:\wnrp\php*` → 端口见 `README.md`「端口映射」表；`php82 = 9000` 是多数 vhost 的默认目标，
  `php85 = 9085` 供 `localhost` / `gmlbm.conf` 使用。
- 端口 ↔ vhost 的 `fastcgi_pass` 必须手工或经「一键同步」保持一致，这是最常见的 502 来源。
- 本机（WNRP）特有：PHP 8.5 的 `opcache.jit` 必须关闭（tracing JIT 在 php-cgi 下会段错误），
  CLI 运行 artisan 等命令建议加 `-d opcache.enable_cli=0`。详见工作区根 `AGENTS.md`。
