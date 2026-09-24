# phpvm · 功能缺口调研与开发路线（ROADMAP）

> 定位：phpvm（Windows / macOS / Linux，Windows 环境根 `C:\wnrp`）是本地 PHP 多版本 / Nginx / Redis / SQLite 集成环境管理 GUI。
> 本文档记录「现状能力基线 → 与同类工具（Laragon / phpStudy / phpEnv）的对标差距 → 分优先级后续开发路线」，作为功能规划的决策依据。**只做规划，不承诺落地时间**；每条路线均标注实现落点、风险与验收要点。
>
> 事实标注：`[已确认]` = 经代码/环境核查；`[待确认]` = 需人工核实后再动工；`[读码发现·待复核]` = 静态读码发现的可疑点，需运行验证。
> **本版基于最新代码（`a726743`，含站点向导 / hosts 写入 / SQLite 面板 / i18n / macOS 支持）于 2026-09-10 整体重做**；旧版中「新建站点向导」「hosts 写入」等已被实现，相关条目已移出缺口清单。
>
> 与 `README.md` 的关系：`README.md` 描述**已实现现状**；本文档描述**尚未实现的规划**。

---

## 一、现状能力矩阵（已实现）

| 分组 | 已实现能力 | 主要落点 |
|---|---|---|
| PHP 版本管理 | 自动发现（Windows `C:\wnrp\php*` + Homebrew keg）、按端口精确启停/重启/状态、批量双快照刷新、端口编辑与校验 | `core/php_manager.py`、`core/process_utils.py`、`ui/php_panel.py` |
| php.ini | 查看三页（12 项关键配置 / 已启用扩展 / 全文）；11 项表单编辑（类型校验、`.bak`、latin-1 无损替换）；**各版本统一编辑 `php.ini`（CLI 与 FastCGI 共用）**；缺配置时「初始化 php.ini」 | `core/ini_editor.py`、`ui/dialogs.py`、`ui/php_panel.py` |
| 版本自检 | 3 项检查（版本 / 9 项关键扩展 / 配置加载） | `core/health_monitor.py` |
| 扩展与版本安装 | ext 扫描启停 + PECL/xdebug.org 在线安装（redis/xdebug/imagick/swoole/memcached）；php.net 下载安装新版本（SHA-256、生成单份 `php.ini`（含 FastCGI 关键项）、端口推导、VC 检测） | `core/php_extension.py`、`php_downloader.py`、`php_installer.py` |
| cmd / 终端 php 切换 | Windows 改用户 PATH 置顶；posix 维护 `.zshrc` / `.bash_profile` 的 phpvm 块 | `core/path_manager.py` |
| Nginx | 启停 / 平滑重载 / 配置检查；Win `-p` 模式与 brew 模式；日志目录按生效配置推导 | `core/nginx_manager.py` |
| **新建站点向导** | 4 步向导（域名+目录+PHP 版本 → 6 套模板 + 实时预览 → hosts 映射 → 落盘）；自动补 include、`nginx -t`、平滑重载、失败回滚 | `ui/site_wizard.py`、`core/site_templates.py`、`core/vhost_manager.py` |
| **hosts 写入** | `ensure_entries` 追加带 `# >>> phpvm-managed >>>` 标记块；冲突 IP 不覆盖；Windows PowerShell RunAs / mac osascript 提权 | `core/hosts_manager.py` |
| 站点映射 | server 块矩阵 + hosts 状态列（✓/✗/⚠/泛解析）+ include 检测与一键修复 + 端口同步（备份/`nginx -t`/回滚/reload） | `core/vhost_manager.py`、`ui/vhost_panel.py` |
| **站点行级操作** | 右键站点：浏览器打开 / 打开项目根目录 / **启用禁用**（改名 `.conf.disabled`）/ **站点级切换 PHP 版本**（只改目标 server 块）/ 从 hosts 移除映射；改前备份、`nginx -t` 失败自动还原 | `core/vhost_manager.set_site_enabled/set_site_php`、`ui/vhost_panel` 右键菜单 |
| **hosts 管理** | 写入（提权）+ **删除条目** + **写入前自动备份 `hosts.phpvm.bak` 与一键还原** + 向导失败回滚 hosts；只操作 `# >>> phpvm-managed >>>` 块 | `core/hosts_manager.py`、`ui/site_wizard.py` |
| **SQLite 面板** | 自动发现库文件（含各站点 root）、表/视图与结构浏览、只读查询（mode=ro + query_only + 关键字白名单，500 行上限、10s 超时）、记忆上次库 | `core/sqlite_manager.py`、`ui/sqlite_panel.py` |
| Redis | 多实例发现与启停、信息卡与日志、**命令执行**（DB 0–15、6 个危险命令确认）、**DB 键空间柱状图** | `core/redis_manager.py`、`ui/redis_panel.py` |
| **MySQL** | 实例发现（my.ini 解析端口/数据目录）；**Windows 服务优先**控制（`sc`/`net`，非管理员禁用启停并提示，进程模式兜底）；状态卡、错误日志 tail、配置与数据目录直达 | `core/mysql_manager.py`、`ui/mysql_panel.py` |
| **HTTPS 证书** | 探测 openssl / mkcert → 为站点生成证书（`<nginx>/SSL/`）+ 443 模板变体；未检测到工具时禁用选项并给安装指引 | `core/cert_manager.py`、`core/site_templates.py`、`ui/site_wizard.py` |
| **服务编排** | 一键全启停（PHP → Redis → MySQL → Nginx，反向停止）+ **自动启动时的 PHP 范围可选**（仅最新 / 站点引用+最新 / 跟随 cmd / 全部，默认仅最新）+ 「打开终端」（PATH 前置所选 PHP 版本） | `core/service_group.py`、`pu.open_terminal`、`ui/php_panel.py`、`ui/main_window.py` |
| **模块开关** | 功能模块可勾选（关于页）：停用的模块不创建页签、不实例化 manager、不导入其代码；PHP/Nginx/站点映射 为刚需不可取消 | `core/modules.py`、`main.py`、`ui/main_window.py` |
| 崩溃防护 | Windows 事件日志 + **macOS `.ips`** 双数据源；详情弹窗（含自愈历史页）与清空；**独立守护进程**自愈（防抖 60s、每 3600s 限 N 次、连续失败 5 次解除、手动停止 300s 宽限、`recover_history.json`） | `core/health_monitor.py`、`crash_watchdog.py`、`recover_history.py` |
| **分发与自动升级** | `packaging/build.py` 跨平台构建 macOS `.app`/`.dmg`（标准库写 PNG → `iconutil` 转 `.icns`，附 `/Applications` 快捷方式）与 Windows 目录/zip/Inno Setup 安装程序；内置 `core/updater.py` 从 GitHub Releases 检查更新 → 下载（`.part` 原子改名）→ SHA-256 校验 → 一键替换重启（mac `hdiutil`+`ditto`；Win 静默安装）；数据目录自动外置（`~/.phpvm`），升级不丢配置 | `core/updater.py`、`core/version.py`、`core/app_paths.py`、`ui/update_dialog.py`、`packaging/`、`.github/workflows/release.yml` |
| **国际化** | 5 语言（zh_CN 为源码原文；en / zh_TW / ja / ko 词条表）、系统语言自动探测、`settings.lang` 持久化、重启生效 | `core/i18n.py`、`i18n/`、`_i18n_scan.py` |
| **开发环境配置推荐** | 按本机 CPU/内存/平台分档（low/mid/high）生成 php.ini 与 nginx.conf 的**开发向**建议（opcache / realpath / 上传上限 / worker 与连接数 / fastcgi 超时与缓冲等），逐条「当前值 → 建议值 + 理由」让用户勾选；写入前备份、nginx 写完 `nginx -t` 失败自动还原；CLI `tune suggest/apply` 同源 | `core/tuning.py`、`core/nginx_conf.py`、`ui/tuning_dialog.py` |
| **跨平台** | 环境根可配置（`WNRP_ROOT`）、lsof/ps 快照、brew 前缀探测、LaunchAgent 自启、单实例 socket 锁、`open` 打开路径、窗口工作区自适应、可写数据目录自动判定（`.app`/只读安装目录 → `~/.phpvm`） | `core/config.py`、`process_utils.py`、`core/app_paths.py`、`ui/window_utils.py` |
| 系统集成 | 托盘动态菜单（Nginx / Redis / 各 PHP）、开机自启、最小化到托盘、**关闭框「重启」**、状态栏崩溃告警 | `main.py`、`ui/tray.py`、`ui/main_window.py`、`core/autostart.py` |

> 工程约束（新增功能须遵守）：
> - Python 3.x + **仅标准库**（tkinter），不引入第三方依赖；
> - 分层：`core/xxx_manager.py` + `ui/xxx_panel.py` / `ui/dialogs.py`；
> - 异步一律「worker 线程 + `queue.Queue` + `after()` 轮询回主线程」，禁止跨线程调用 tk；
> - 持久化统一 `config.json`；配置文件写回前先 `.bak`；
> - **所有新增界面文案必须用 `t()` 包裹**，并运行 `python _i18n_scan.py --report` 补齐各语言词条；
> - 跨平台：Windows 与 posix 行为均需考虑（`IS_WIN` 分流），路径用 `os.path`，打开文件用 `pu.open_path`。

---

## 二、同类工具对标

| 功能点 | Laragon | phpStudy/phpEnv | phpvm | 备注 |
|---|---|---|---|---|
| 多 PHP 版本启停 / 切换 | ✓ | ✓ | ✓ | 端口级独立精确控制 |
| PHP 新版本下载安装 | ✓ | ✓ | ✓ | php.net 官方包 |
| php.ini / 扩展可视化编辑 | ✓ | ✓ | ✓ | 含 PECL 在线扩展 |
| 站点创建向导（域名+目录+vhost） | ✓ | ✓ | ✓ | 6 套模板 + 实时预览 + 回滚 |
| hosts 自动写入 | ✓ | ✓ | ✓ | 提权写入 + 标记块 |
| hosts 条目管理（删除/备份/回滚） | ✓ | ~ | ✓ | P0-4 已实现：写入/删除均自动备份 `hosts.phpvm.bak`，可一键还原 |
| 站点启用 / 禁用 | ✓ | ✓ | ✓ | P0-3 已实现：改名 `.conf.disabled` |
| 站点级选择 PHP 版本 | ✓ | ✓ | ✓ | P0-3 已实现：只改目标 server 块 |
| 浏览器 / 资源管理器直达站点 | ✓ | ~ | ✓ | P0-3 已实现：右键菜单 |
| MySQL / MariaDB 管理 | ✓ | ✓ | ✓ | P1-2 已实现（Windows 服务优先，需管理员才能启停） |
| SQLite 只读查询 | ~ | ~ | ✓ | phpvm 特色（只读兜底） |
| Redis 管理 | ~ | ~ | ✓ | 多实例 + 命令 + 键空间图表 |
| HTTPS 本地证书一键生成 | ✓ | ✓ | ✓ | P1-1 已实现（mkcert > openssl 自签；本机已装 OpenSSL-Win64） |
| Composer / Node 等工具随附 | ✓ | ~ | ✓ | P2-1 已集成：探测 `C:\ProgramData\ComposerSetup`（2.8.1），可按 PHP 版本运行 |
| 一键启停整套服务 | ✓ | ✓ | ✓ | P1-3 已实现（托盘 + 关于页） |
| 崩溃检测 / 自愈 | ~ | ✗ | ✓ | phpvm 特色（双数据源 + 独立守护 + 失联兜底） |
| 多语言界面 | ~ | ~ | ✓ | phpvm 特色（5 语言） |
| 跨平台（Win + mac） | ✗ | ✗ | ◐ | GUI/服务已适配，仅缺 mac 托盘 |

结论：phpvm 的**进程级深度**（精确端口、崩溃自愈、Redis 工具化）与**建站闭环**（向导 + hosts + 回滚）已达同类水准；差距集中在「**站点日常运维动作**」「**MySQL 纳管**」「**证书与工具链集成**」三处，另有 1 个既有功能疑似失效（P0-1）与 1 个 ini 选择缺陷（P0-2）。

---

## 三、缺口与路线

> 优先级：P0 = 修既有问题 + 把站点闭环补完（零外部依赖）；P1 = 需提权或外部二进制；P2 = 探测型与打磨项；P3 = 远期候选。

### P0 · 修既有问题 + 站点运维闭环

#### P0-1 ✅ 已修复「编辑配置 → 保存」失效
- 现象：`ui/dialogs.py` 保存逻辑中 `key, value = meta["key"], self._vars[key].get()...` 右值先整体求值，首次迭代 `key` 尚未绑定 → 应抛 `UnboundLocalError`。`[读码发现·待复核]`
- 动工前：先手动打开「编辑配置」点保存复现；若确认，修法为拆成两行赋值（先取 `key` 再取 `value`）。
- 验收：任意修改一项并保存成功、生成 `.bak`、ini 内容正确变更、重启版本后生效。

#### P0-2 ✅ 已修复 php83 / php84 的 FastCGI ini 选择
- 事实：`php_manager._resolve_ini` 的白名单仅 `("php82","php85")`，但安装器为 php83/php84 也生成了 `php-web.ini` → 这两个版本 FastCGI 实际加载 `php.ini`，改 `php-web.ini` 不生效（尤其 `opcache.jit` 等排障项）。`[已确认]`
- 当年修法：改为「目录内存在 `php-web.ini` 即优先」（或按安装器产物标记），并同步 README 与自检提示。落点 `core/php_manager._resolve_ini`。
- **后续收口（第十二轮）**：双 ini 机制本身带来「改哪份才生效」的持续困惑，已整体回退为**统一使用 `php.ini`**（CLI 与 FastCGI 共用），`php-web.ini` 不再被读取 —— 本条修复随机制退场而失效。

#### P0-3 ✅ 已实现站点行级操作（把「建站」延伸到「日常运维」）
- 目标（vhost 面板加右键菜单/列按钮）：
  1. **浏览器打开站点** `http://<域名>`（无 `webbrowser` 依赖，标准库即可）；
  2. **打开站点 root 目录**（现有 `pu.open_path` 仅用于 conf 与 vhost 目录）；
  3. **启用 / 禁用站点**：conf 改名 `.conf.disabled`（`scan_files()` 只收 `.conf`，天然生效），反向恢复；
  4. **为该站点切换 PHP 版本**：按 `server_name` 定位到目标 server 块，只改该块 `fastcgi_pass` 端口 → `nginx -t` → 平滑重载。
- 复用：`vhost_manager._iter_server_blocks`（块定位）、`_replace_in_file` + `.bak` + `nginx -t` + 失败还原（`sync_port` 已有全套流程）。
- 风险：同文件多 server / 多 location 时替换必须限定在目标块内；改前备份，重载失败自动还原。
- 验收：禁用后该站点不可访问且矩阵标注「已禁用」；换版本后目标站点 `phpinfo()` 变化，同端口其它站点不受影响。

#### P0-4 ✅ 已实现 hosts 管理补全（安全网）
- 现状缺口：`hosts_manager` 只有 `ensure_entries`（追加）语义 —— **无删除、无启用/禁用、无任何备份**，且 `site_wizard._rollback` 不还原 hosts。这是目前唯一「写系统文件却无回滚」的路径，而本机 hosts 含真实公网 IP 映射行。`[已确认]`
- 目标：
  1. 增 `remove_entries(domains)`、`backup()` / `restore()`（`hosts.bak`），且**只操作自身 `phpvm-managed` 标记块**，绝不触碰其它行；
  2. 向导落盘失败时一并回滚 hosts（接入 `site_wizard._rollback`）；
  3. （可选）提供 hosts 条目列表视图，支持逐条删除。
- 落点：`core/hosts_manager.py`、`ui/site_wizard.py`。
- 验收：建站失败后 hosts 无残留条目；删除域名后 `ping` 失效；其它 IP 行全程未被修改。

#### P0-5 ✅ 文档同步（已完成）
- 按最新代码重写 README（补齐 8 秒刷新周期、7 页签、Redis / 扩展 / 下载 / i18n / 单实例 / 窗口自适应章节、php83/84、ini 11 项、托盘 Redis 子菜单、崩溃守护参数、`config.json` 四键、macOS 适配清单），并给 README ↔ ROADMAP 互链。**每次功能落地后须同步两份文档**，避免再次漂移。

### P1 · 需提权或外部二进制的能力

#### P1-1 ✅ 已实现 HTTPS 本地证书 + 443 模板变体
- 事实：本机 `nginx/SSL` 仅有 `rootSSL.pem/key` 与 1 份 CSR/私钥，无站点证书；30 个 vhost 与 6 套模板全部 `listen 80`；`openssl.exe` / `mkcert` 在 wnrp 各目录均未找到。`[已确认]`
- MVP 边界：Python 标准库无法签发 X.509 证书 → 只能**探测并调用外部 openssl / mkcert**；
  1. 探测系统 openssl（含常见 git `usr/bin`）与 mkcert；
  2. 找到 → 向导增加「启用 HTTPS」步骤（签发证书 + 生成 443 server 变体 + hosts 提示）；
  3. 未找到 → 只展示检测报告与安装指引，**不内置下载第三方二进制**。
- 落点：`core/cert_manager.py`（新）、`core/site_templates.py`（443 变体）、`ui/site_wizard.py`。
- 前置：P0-4（hosts 能力已就绪）。

#### P1-2 ✅ 已实现 MySQL 管理面板（服务控制优先）
- 事实（2026-09-10 实测）：`C:\wnrp\mysql` 为解压版 **MySQL 5.7.34**，`my.ini` 于安装目录内（`port=3306`、`utf8`、`INNODB`，`datadir` 含约 40 个业务库）；**当前以 Windows 服务运行**：服务名 `MySQL`、Running、启动类型 Automatic，3306 由 PID 10616 监听；wnrp 内无任何脚本引用 mysql。`[已确认]`
- 目标：
  - `core/mysql_manager.py`：探测安装目录与 `my.ini`；**状态优先走服务**（`sc query MySQL` / `Get-Service`），辅以 3306 占用进程校验；启停用 `net start/stop MySQL`（或 `sc`）；服务不存在才降级进程模式（`RunHiddenConsole mysqld --defaults-file=`，停止用 `mysqladmin shutdown`）。
  - `ui/mysql_panel.py`：状态卡（版本 / 服务名 / 启动类型 / PID / 端口 / 数据目录）→ 启停重启 → `my.ini` 只读查看 → `data\*.err` tail（复用 `nginx_log_panel`）→ 「在 bin 打开终端」。
- 风险：**启停 Windows 服务需管理员权限** —— 非管理员时按钮禁用并提示「以管理员身份运行 phpvm」，或 `ShellExecuteW runas` 提权；停止前二次确认（影响本机全部站点）；严禁服务与进程双启。
- `[待确认]`：`root@localhost` 凭据 → 未确认前**不做**任何 SQL 执行 / 库表浏览功能。
- 验收：面板正确显示 5.7.34 / Running / 3306；停止后端口释放且不影响 PHP/Nginx/Redis。

#### P1-3 ✅ 已实现一键全启停 + 「打开终端」
- 一键全启停：托盘顶层与关于页加「全部启动 / 全部停止」（Nginx + 各 PHP + Redis[+ MySQL]），并发编排并汇总结果。落点 `core/service_group.py`（新）+ `ui/main_window` 托盘菜单。
- 「打开终端」：php_panel 行按钮 → 新开终端，PATH 前置所选 PHP 目录，工作目录可选。落点 `core/process_utils.open_terminal()`（Windows `start cmd /k`，mac `osascript Terminal`）+ `ui/php_panel.py`。
- 风险：全停会中断本机全部站点，需二次确认；MySQL 若纳入需管理员权限。

### P2 · 工具集成与打磨

- **P2-1 ✅ 已实现 Composer 探测 + 按版本运行**：系统已装 `C:\ProgramData\ComposerSetup\bin\composer(.bat)`（`[已确认]`）→ 只需探测路径、回显 `composer -V` 与其绑定的 PHP，并提供「用选中 PHP 版本运行 composer」入口（PATH 前置），**无需下载安装分支**。落点 `core/tool_manager.py`（新）。
- **P2-2 ✅ 已实现开机自动启动整套服务**（Windows「启动」目录 + `main.py --start-all`；服务化/`sc create` 未做，仍列 P3）：依赖 P1-3 的编排层；Windows 需 `sc create` 与权限设计。落点 `core/autostart.py` 扩展。
- **P2-3 ✅ 已实现 i18n 覆盖度守护**（`_i18n_scan.py --fail-under=N`，缺失超阈值退出码 1）：`_i18n_scan.py --report` 已有缺失统计，建议加阈值退出码或在关于页加「语言覆盖度」诊断（读 `missing_keys()`），避免漏译不可见。
- **P2-4 ✅ 已实现 SQLite 面板增强**：结果分页（每页 500 行，上一页 / 下一页）、导出 CSV（SQL 导出未做）。
- **P2-5 ✅ 已实现一致性打磨**：托盘图标（`core/icon.py` 用标准库生成 `phpvm.ico`）、`SelfCheckDialog` 底部改为显示真实扩展数（9 项）。

### P3 · 远期候选（仅记录）

- 轻量资源监控（php-cgi 进程数 / 内存、Nginx 连接数）
- 多 server 块逐块编辑（改端口时按块粒度操作）
- hosts 分节管理 UI（按项目分组、一键 on/off 某段）—— 前置能力 P0-4 完成后才现实
- 配置迁移 / 备份导出（自身更新通道已于第七轮实现，仅剩配置导入导出）
- macOS 菜单栏托盘（纯标准库实现成本高，倾向「不做」，或改用状态栏入口替代）

---

## 四、风险与共同约束

1. **hosts 安全**：当前写入无备份、无删除、向导不回滚 —— 扩任何 hosts 能力前先补备份与「仅操作 phpvm-managed 块」的边界；本机 hosts 含真实公网 IP 行，误改代价高。
2. **nginx conf 精确改写**：凡改文件先 `nginx -t` 再平滑重载，失败自动还原；多 server 同文件必须定位到目标块（`_iter_server_blocks`）。
3. **MySQL 服务态**：本机已是 Windows 服务（自动启动），面板只能走服务接口，禁止再拉第二个 mysqld；启停需管理员权限。
4. **纯标准库边界**：证书签发等能力只能「探测并调用系统既有 exe」，探测不到就给明确引导，不静默失败、不内置下载第三方二进制。
5. **i18n 纪律**：新增/修改界面文案必须用 `t()` 包裹并跑 `_i18n_scan.py --report`；zh_CN 无词条表（源码原文），改中文文案即等于改 msgid，需同步其它语言。
6. **异步与跨平台纪律**：后台操作一律线程 + queue + `after` 轮询；新代码需同时考虑 Windows 与 posix 分支。
7. **文档一致性**：功能落地同步 `README.md`（现状）与本文件（勾选完成项）—— 上一轮文档漂移的教训是「代码先走、文档留在旧版」。

## 五、本轮范围声明

- **第二轮（2026-09-10，文档）**：拉取远端最新代码后重做 `README.md` 与本文档，补齐 i18n / Redis / 扩展 / 下载 / 单实例 / 窗口自适应 / macOS 适配等章节，并将「新建站点向导」「hosts 写入」标记为已实现。
- **第三轮（2026-09-10，P0 落地）**：实现 P0 全部条目 —— 修复「编辑配置保存」崩溃与 php83/84 ini 选择；新增站点行级操作（浏览器 / 目录直达、启用禁用、站点级换 PHP 版本）与 hosts 删除 / 备份 / 一键还原 / 向导回滚；新增界面文案已用 `t()` 包裹（其它语言待补词条，回落中文）。18 项冒烟用例全部通过（hosts 部分使用临时文件，未触碰系统 hosts）。
- **第四轮（2026-09-10，P1 落地）**：新增 MySQL 管理页（`core/mysql_manager.py` + `ui/mysql_panel.py`，Windows 服务优先）；新增 HTTPS 证书（`core/cert_manager.py` + 443 模板变体 + 向导勾选，实测本机装有 OpenSSL-Win64 可用）；新增一键全启停（`core/service_group.py`，托盘与关于页入口）与「打开终端」（`pu.open_terminal` + PHP 页按钮）。17 项冒烟用例全部通过（只读，未真的启停 MySQL）。
- **第五轮（2026-09-10，P2 落地）**：新增 Composer 探测与按版本运行（`core/tool_manager.py` + PHP 页按钮）；新增开机自动启动整套服务（`main.py --start-all` + `autostart.enable_services()` 写用户「启动」目录，仅 Windows）；SQLite 结果分页与 CSV 导出；i18n 扫描新增 `--fail-under` 覆盖率守护；托盘图标由 `core/icon.py` 生成；自检文案改为显示真实扩展数。18 项冒烟用例通过，并修复了过程中发现的两个缺陷（Composer 版本取到 PHP 告警行、VBS 内引号未转义）。
- **第六轮（2026-09-10，模块开关）**：新增 `core/modules.py` 模块注册表与关于页勾选设置 —— 默认全部启用，取消勾选（刚需模块除外）后重启不再创建该页签、不实例化 manager、不导入其代码（实测停用 sqlite/log 后页签从 8 个减为 6 个且 `core.sqlite_manager` 未进入 `sys.modules`）。
- **本轮同时修复 P1 遗留缺陷**：`ui/mysql_panel.py` 状态卡混用 `pack` 与 `grid`，导致 MySQL 面板构建即抛 `TclError`（只读冒烟无法发现，改为真实构建主窗口验证后定位）。
- **P0 / P1 / P2 均已完成**，剩余为 P3 远期候选（资源监控、多 server 块逐块编辑、hosts 分节 UI、服务化 `sc create`、SQLite SQL 导出、配置迁移 / 备份导出）。新增文案记得跑 `python _i18n_scan.py --report` 补齐各语言。
- **第八轮（2026-09-22，运行期性能优化，无功能变更）**：① 新增 `process_utils` 进程镜像名快照（`EnumProcesses` + `QueryFullProcessImageNameW`，零子进程）与缓存的 `tasklist` 全量兜底 —— Nginx/Redis/MySQL 的状态判定入口全部改走快照，`pid_to_name`/`pid_to_path`（原逐 PID `tasklist` / PowerShell `Get-Process`）同样改为本地取值；实测单次状态判定从 ~162ms（tasklist）降到 ~1ms。② Redis 一轮刷新只取一次进程/端口快照（新增 `status_snapshot()`，N 实例不再付 N 次子进程）；MySQL `_mysqld_pids` 不再逐 PID 起 `tasklist`（MySQL 未运行时原为数百次子进程/轮），`sc query` 加 3 秒 TTL 缓存并在启停服务后主动失效。③ `run_log` 落盘改为后台单线程批量写 + `atexit` 冲刷（调用方零磁盘 IO，实测 2000 条写入 0.02s）；运行日志面板改为增量追加渲染（过滤条件变化或截断才全量重绘）。④ Redis 键空间 Canvas 在数据/尺寸/主题/语言未变时跳过重绘。⑤ `php_panel._apply_status` 线性查找改字典。
- **第九轮（2026-09-22，结构性整顿：去重 + 回滚统一 + 测试）**：① 新增 `core/site_service.py`，把「证书 → 写 vhost → 补 include → `nginx -t` → hosts → 平滑重载 + 回滚」下沉为唯一实现，CLI 与 GUI 向导都改为调用它（`SitePlan` 入参 + `SiteResult` 步骤明细 + `SiteRollback` 回滚记录）；两处差异用显式开关表达（`allow_missing_main_conf`：GUI 宽容 / CLI 严格）。② 新增 `core/file_backup.py` 统一备份/还原（`<file>.bak`，hosts 用 `.phpvm.bak`），替换 vhost / ini / 扩展 dll / hosts / CLI 中散落的 `shutil.copy2` 样板。③ `valid_domain` / `safe_conf_base` / `nginx_path` 三个重复 helper 收敛到 `core/vhost_manager.py`，CLI 与向导共用。④ 补上 hosts「一键还原」入口（站点映射页按钮 + `cli.py hosts restore --yes`），此前 `restore_backup()` 是无入口的死代码、与 README 描述不符。⑤ 新增 `tests/`（标准库 unittest，35 个用例）覆盖 vhost 改写 / hosts 写入移除 / ini 写回 / 建站编排与回滚 / file_backup，全部用临时目录与假 nginx，不触碰真实 nginx 配置与系统 hosts。⑥ 过程中修掉 4 个既有缺陷：`include_status()` 循环变量遮蔽 `t()` 导致「未 include 站点目录」路径必崩（该路径正是自动补 include 的入口）、CLI 在 GBK 控制台打印 ✓/✗ 抛 UnicodeEncodeError、`sync_port` 的「替换数」把命中数当改动数、`_i18n_scan.py` 输出路径分隔符随平台变化导致 `_keys.json` 整文件 churn。
- **第十轮（2026-09-23，外观优化，无功能变更）**：① 调色板现代化并重排对比层级 —— `primary` 换用更亮的蓝、文本/边框/斑马纹改用中性灰阶，新增 `border_strong` / `header_bg` / `hover` 三个角色（两套主题键一致，由 `tests/test_theme.py` 断言）。② `ui/theme.py` 新增设计令牌：字号层级 `FS_TITLE/SECTION/BODY/SMALL` + 间距层级 `PAD_XS~XL` + `font()` / `divider()` / `accent_bar()`，替换各面板散落的 6/10/14 硬编码与 `(FONT, 9)` 字面量。③ 结构感：主窗口标题区改为「描边卡片 + 左侧强调竖条」，状态栏加上沿细线并改用 `panel_alt` 底色，崩溃/更新徽标改为 `Alert/Link.Status.TLabel`（随主题自动换色，不再硬编码前景色）。④ 控件重做：常规按钮细描边 + 聚焦强调色（主/危按钮保持实心），输入框/下拉聚焦变强调色，表头独立底色且去掉描边，滚动条改细条（10px）无描边，页签选中态为「卡片底 + 强调色描边 + 强调色文字」并把文字留白从空格改为 `padding`（避免 i18n 文案长度变化时错位）。⑤ 关于页整页改为卡片（`Card.TFrame` + `Card.TLabelframe` + `Card.TCheckbutton`），消除此前「灰底控件压在白卡片上」的色块。⑥ Windows 高 DPI：`main.py` 在创建 Tk 根窗口前开启感知（PER_MONITOR_V2 → 系统级 → 放弃），解决高分辨率屏文字发虚。⑦ 新增 `tests/test_theme.py`（6 例），全量 68 例通过；新增样式经「建窗口 + 双主题热切换」冒烟验证可实例化。
- **第十一轮（2026-09-23，php.ini 初始化入口）**：`_resolve_ini` 在 Windows 上恒返回 `<版本目录>\php.ini`（历史假设官方包一定有），posix 上找不到才返回 ""，于是「有没有生效配置」只能按文件是否存在判定 —— 此前 `read_ini` / `read_key_ini` / 编辑配置 / 推荐设置都用 `if not v.ini` 判断，Windows 上永不成立，缺 ini 的版本点「编辑配置」只会看到读取失败，推荐设置也无从下手。现新增 `PhpManager.ini_target()` / `ini_ready()` / `ini_template()` / `init_ini()`：目标路径兜底为版本目录内 php.ini，优先复制官方模板 `php.ini-production` → `php.ini-development`，都没有则写入内置最小骨架（`INI_SKELETON`，未列出的指令沿用编译默认值）；已存在不覆盖，成功后同步 `v.ini`。界面侧：PHP 面板新增「初始化 php.ini」按钮（仅在选中版本确实缺配置时可用），配置文件列缺配置时加 ⚠ 前缀，编辑/推荐设置的告警改为指向该按钮。新增 `tests/test_php_ini.py`（8 例），全量 76 例通过；新增文案已补齐 en / zh_TW / ja / ko。
- **第十二轮（2026-09-23，ini 机制回退：统一使用 php.ini）**：① `core/php_manager._resolve_ini` 取消 `php-web.ini` 优先（含散落 `*.ini` 兜底里显式跳过它），CLI 与 FastCGI 回到同一份 `php.ini`；FastCGI 启动本来就是 `php-cgi -b <port> -c <解析结果>`，因此只需改解析即可整体生效。② `core/php_installer.generate_ini` 只产出唯一 `php.ini`，把原先写在 php-web.ini 末尾的 FastCGI 关键项（`cgi.fix_pathinfo=1` / `cgi.force_redirect=0` / `cgi.fastcgi=1` / `opcache.enable=1` / `opcache.enable_cli=0`）改为追加到 php.ini 末尾（后出现的值覆盖先前值），新装版本不再产生 php-web.ini。③ 界面文案：关于页「PHP FastCGI 配置」改为「各版本目录内的 php.ini（CLI 与 FastCGI 共用）」，下载进度「正在生成 php.ini / php-web.ini」改为「正在生成 php.ini」；README / ROADMAP / 本机 AGENTS.md 同步，废弃词条从 4 语言表移除并补新词条。④ **本机环境对齐（必要）**：切回 php.ini 前实测 php85 的 php.ini 是 `opcache.enable=1 + opcache.jit=tracing + jit_buffer_size=128M`（正是 2026-08-26 记录的 php-cgi 段错误配方）、php82 的 php.ini 是 `opcache.enable=0`；已按原 php-web.ini 的可用状态用 `core/ini_editor.save_values` 写回（php82：opcache 开 + JIT 关；php85：JIT 关 + 补 `date.timezone`），自动留 `.bak`。⑤ 新增 `tests/test_php_installer_ini.py`（4 例）与 `ResolveIniTest`（3 例），全量 83 例通过。⑥ 端到端验证：重启 php85 后命令行确认为 `php-cgi.exe -b 127.0.0.1:9085 -c C:\wnrp\php85\php.ini`；经 nginx 请求 `http://gmlbm.test/` 连续 10 次 200（另用绕过系统代理的直连 3 次复核），php-cgi PID 未变化、事件日志无新增崩溃。
- **第十三轮（2026-09-24，自动启动不再拉起全部 PHP 版本）**：开机自启/启动时自动启动服务原先无条件遍历全部版本（本机 11 个），现改为按范围挑选。① `core/service_group.py` 新增范围常量 `PHP_SCOPES`（`newest` 默认 / `used` / `active` / `all`）、`scope_label()`、`php_targets()`，`start_all(php_scope=..., php_names=...)` 只启动命中的版本并把其余写为「跳过」；`stop_all` 与手动「全部启动」按钮仍覆盖全部（保持显式意图）。② 「最新版」判定新增 `core/php_manager.version_key()`：display（`php -v` 解析）优先、目录名数字兜底（`php85`→(8,5,0)），避免直接比较目录名字符串时 `php8 > php85` 的错误；display 为空时先 `resolve(refresh_status=False)`（带 mtime 缓存，实测 11 个版本约 3.6s 首次、之后走缓存）。③ 新增设置 `settings.autostart_php_scope`（默认 `newest`），关于页「设置」区新增下拉 + 说明；两条自启路径（`main.py --start-all`、GUI `start_services_on_launch`）都按它挑版本，`main.py --start-all --php-scope=<scope>` 可临时覆盖，`autostart_services.log` 追加 `php_scope=` 行。④ 「跳过 PHP 版本：…（策略：…）」写入汇总与运行日志，避免误判为启动失败。⑤ CLI `services start-all` 新增 `--php newest|used|active|all|<逗号分隔版本名>`，`--dry-run` 输出将启动/跳过的版本清单。⑥ 新增 `tests/test_service_group.py`（20 例，含临时目录 + 假 nginx 验证 `used` 的端口反查），全量 103 例通过；实测 `--php newest` 仅选 php85、`--php used` 选 php / php74 / php82 / php85。新增 15 条文案已补齐 en / zh_TW / ja / ko。
- **已知存量问题（未处理）**：i18n 尚缺约 150 条译文（多为此前几轮新增界面文案未补，集中在站点行级操作 / vhost 面板）；P3 远期候选（资源监控、多 server 块逐块编辑、hosts 分节 UI、服务化 `sc create`、SQLite SQL 导出、配置迁移 / 备份导出）仍未动工；本机 php82 / php83 / php84 / php85 目录下的历史 `php-web.ini` 仍在（已不被读取，可手动删除）。
- **第七轮（2026-09-10，安装包与自动升级）**：新增 `core/version.py`（版本号/发布源/资产命名单一来源）、`core/app_paths.py`（可写数据目录判定，包目录只读时自动改用 `~/.phpvm`）、`core/updater.py`（检查 → 下载 → SHA-256 校验 → 替换安装）与 `ui/update_dialog.py`（更新窗口 + 启动静默检查）；关于页新增版本/数据目录信息与「检查更新 / 打开下载缓存目录 / 启动时自动检查更新」，菜单栏新增「帮助」，状态栏新增新版本提示；新增 `packaging/build.py`（macOS `.app`/`.dmg`、Windows 包、`SHA256SUMS.txt`）与 `packaging/phpvm.iss`（AppId 固定故识别为升级、`config.json` 以 `onlyifdoesntexist` 保护）；新增 `.github/workflows/release.yml`（打 tag 自动构建双平台并发布 Release）。实测 macOS 出包成功（`phpvm-1.0.0-macos.dmg`，575 KB，含图标与排除项校验），升级链路自测通过（检查解析 / 下载校验 / 校验和不匹配拒绝并清理 / 取消下载）。新增 63 条文案已补齐 en / zh_TW / ja / ko 四语言。

---

## 附录：事实核查记录（2026-09-10，基于 `a726743`）

| 项目 | 结论 | 状态 |
|---|---|---|
| MySQL | `mysqld Ver 5.7.34 for Win64`；**Windows 服务**：`MySQL` / Running / Automatic，3306 由 PID 10616 监听；wnrp 内无脚本引用 | [已确认] |
| MySQL root 凭据 | 认证方式 / 密码未知（未尝试登录探测） | [待确认] |
| composer | 系统已装 `C:\ProgramData\ComposerSetup\bin\composer.bat`，版本 **2.8.1**；`composer -V` 前置输出含 PHP Deprecated 警告，读取版本须取版本行而非首行（已处理） | [已确认] |
| openssl / mkcert | wnrp 各目录内未找到，但**系统装有 OpenSSL-Win64**：`C:\Program Files\OpenSSL-Win64\bin\openssl.exe`（mkcert 未装）→ HTTPS 走 openssl 自签可用 | [已确认（2026-09-10 更正）] |
| hosts | 42 行平铺，`.test` 为主，含真实公网 IP 行；phpvm 仅追加标记块，**无删除 / 无备份 / 向导不回滚** | [已确认] |
| nginx vhost | 30 个 conf，形态统一（listen 80 + `*.test` + Laravel root + fastcgi_pass 9000/9085），已 include `C:/wnrp/nginx/conf/vhost/*.conf`；无 443 | [已确认] |
| 站点行级操作 | vhost_panel 仅 3 按钮（新建 / 打开目录 / 刷新）+ 双击打开 conf；无启禁用、无站点级换 PHP、无浏览器/目录直达 | [已确认] |
| 编辑配置保存 | `ui/dialogs.py` 保存处 `UnboundLocalError` —— **已修复**（拆分元组解包赋值） | [已修复] |
| php83 / php84 ini | 两目录均有 `php-web.ini` 但未被采用 —— 当年**已修复**（改为「存在即用」）；第十二轮起统一使用 `php.ini`，`php-web.ini` 整体退场 | [已修复→已统一] |
| php82 / php85 php.ini | 切回统一 `php.ini` 前实测：php85 的 `php.ini` 是 `opcache.enable=1 + opcache.jit=tracing + jit_buffer_size=128M`（即 2026-08-26 记录的 php-cgi 段错误配方），php82 的 `php.ini` 是 `opcache.enable=0` —— 已按原 `php-web.ini` 的可用状态对齐（JIT 关、php82 恢复 opcache、php85 补时区），写入前自动生成 `.bak` | [已确认·已对齐] |
| 托盘图标 | `<phpvm>\phpvm.ico` 不存在，回退系统默认图标 | [已确认] |
| README 状态（重做前） | 仍写 4 秒刷新、五页签、缺 Redis / 扩展 / 下载 / i18n / 单实例章节、目录树与端口表不全、macOS 进度过时 | [已确认·本轮已修复] |
