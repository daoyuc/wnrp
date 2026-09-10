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
| php.ini | 查看三页（12 项关键配置 / 已启用扩展 / 全文）；11 项表单编辑（类型校验、`.bak`、latin-1 无损替换）；php82/85 编辑 `php-web.ini` | `core/ini_editor.py`、`ui/dialogs.py` |
| 版本自检 | 3 项检查（版本 / 9 项关键扩展 / 配置加载） | `core/health_monitor.py` |
| 扩展与版本安装 | ext 扫描启停 + PECL/xdebug.org 在线安装（redis/xdebug/imagick/swoole/memcached）；php.net 下载安装新版本（SHA-256、生成双 ini、端口推导、VC 检测） | `core/php_extension.py`、`php_downloader.py`、`php_installer.py` |
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
| **服务编排** | 一键全启停（PHP → Redis → MySQL → Nginx，反向停止）+ 「打开终端」（PATH 前置所选 PHP 版本） | `core/service_group.py`、`pu.open_terminal`、`ui/php_panel.py`、`ui/main_window.py` |
| 崩溃防护 | Windows 事件日志 + **macOS `.ips`** 双数据源；详情弹窗（含自愈历史页）与清空；**独立守护进程**自愈（防抖 60s、每 3600s 限 N 次、连续失败 5 次解除、手动停止 300s 宽限、`recover_history.json`） | `core/health_monitor.py`、`crash_watchdog.py`、`recover_history.py` |
| **国际化** | 5 语言（zh_CN 为源码原文；en / zh_TW / ja / ko 词条表约 700 条）、系统语言自动探测、`settings.lang` 持久化、重启生效 | `core/i18n.py`、`i18n/`、`_i18n_scan.py` |
| **跨平台** | 环境根可配置（`WNRP_ROOT`）、lsof/ps 快照、brew 前缀探测、LaunchAgent 自启、单实例 socket 锁、`open` 打开路径、窗口工作区自适应 | `core/config.py`、`process_utils.py`、`ui/window_utils.py` |
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
| Composer / Node 等工具随附 | ✓ | ~ | **✗** | 系统已装 composer，未集成，见 P2-1 |
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
- 建议：改为「目录内存在 `php-web.ini` 即优先」（或按安装器产物标记），并同步 README 与自检提示。
- 落点：`core/php_manager._resolve_ini`。
- 验收：php83/php84 面板显示的配置文件为 `php-web.ini`，修改后 FastCGI 行为随之变化。

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

- **P2-1 Composer 探测 + 按版本运行**：系统已装 `C:\ProgramData\ComposerSetup\bin\composer(.bat)`（`[已确认]`）→ 只需探测路径、回显 `composer -V` 与其绑定的 PHP，并提供「用选中 PHP 版本运行 composer」入口（PATH 前置），**无需下载安装分支**。落点 `core/tool_manager.py`（新）。
- **P2-2 整套服务开机自启 / 可选服务化**：依赖 P1-3 的编排层；Windows 需 `sc create` 与权限设计。落点 `core/autostart.py` 扩展。
- **P2-3 i18n 覆盖度守护**：`_i18n_scan.py --report` 已有缺失统计，建议加阈值退出码或在关于页加「语言覆盖度」诊断（读 `missing_keys()`），避免漏译不可见。
- **P2-4 SQLite 面板增强**：结果分页（现 500 行截断）、导出 CSV / SQL。
- **P2-5 一致性打磨**：托盘图标（`ui/tray` 加载 `<phpvm>\phpvm.ico`，当前不存在 → 回退系统图标）、`SelfCheckDialog` 底部「关键扩展核对 N 项」用的是检查项数 3 而非扩展数 9。

### P3 · 远期候选（仅记录）

- 轻量资源监控（php-cgi 进程数 / 内存、Nginx 连接数）
- 多 server 块逐块编辑（改端口时按块粒度操作）
- hosts 分节管理 UI（按项目分组、一键 on/off 某段）—— 前置能力 P0-4 完成后才现实
- phpvm 自身更新通道与配置迁移 / 备份导出
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
- 后续开发按 **P2** 立项（P0 / P1 均已完成），每条动工前先补齐其标注的 `[待确认]` 项；新增文案记得跑 `python _i18n_scan.py --report` 补齐各语言。

---

## 附录：事实核查记录（2026-09-10，基于 `a726743`）

| 项目 | 结论 | 状态 |
|---|---|---|
| MySQL | `mysqld Ver 5.7.34 for Win64`；**Windows 服务**：`MySQL` / Running / Automatic，3306 由 PID 10616 监听；wnrp 内无脚本引用 | [已确认] |
| MySQL root 凭据 | 认证方式 / 密码未知（未尝试登录探测） | [待确认] |
| composer | 系统已装 `C:\ProgramData\ComposerSetup\bin\composer(.bat)`；wnrp 目录内没有 | [已确认] |
| openssl / mkcert | wnrp 各目录内未找到，但**系统装有 OpenSSL-Win64**：`C:\Program Files\OpenSSL-Win64\bin\openssl.exe`（mkcert 未装）→ HTTPS 走 openssl 自签可用 | [已确认（2026-09-10 更正）] |
| hosts | 42 行平铺，`.test` 为主，含真实公网 IP 行；phpvm 仅追加标记块，**无删除 / 无备份 / 向导不回滚** | [已确认] |
| nginx vhost | 30 个 conf，形态统一（listen 80 + `*.test` + Laravel root + fastcgi_pass 9000/9085），已 include `C:/wnrp/nginx/conf/vhost/*.conf`；无 443 | [已确认] |
| 站点行级操作 | vhost_panel 仅 3 按钮（新建 / 打开目录 / 刷新）+ 双击打开 conf；无启禁用、无站点级换 PHP、无浏览器/目录直达 | [已确认] |
| 编辑配置保存 | `ui/dialogs.py` 保存处 `UnboundLocalError` —— **已修复**（拆分元组解包赋值） | [已修复] |
| php83 / php84 ini | 两目录均有 `php-web.ini` 但未被采用 —— **已修复**（改为「存在即用」，php82/85 行为不变，php74 仍用 `php.ini`） | [已修复] |
| 托盘图标 | `<phpvm>\phpvm.ico` 不存在，回退系统默认图标 | [已确认] |
| README 状态（重做前） | 仍写 4 秒刷新、五页签、缺 Redis / 扩展 / 下载 / i18n / 单实例章节、目录树与端口表不全、macOS 进度过时 | [已确认·本轮已修复] |
