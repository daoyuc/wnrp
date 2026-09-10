# phpvm · PHP 版本管理器

跨平台（Windows / macOS / Linux）桌面 GUI 工具，用于统一管理开发环境（Windows 默认 `C:\wnrp`，其它平台 `~/wnrp`，可用环境变量 `WNRP_ROOT` 覆盖）中的 **多个 PHP 版本**、**Nginx**、**Redis**、**MySQL** 与 **SQLite**，取代手动双击各种 bat 脚本的繁琐操作。

仅本机使用，无需登录。按端口精确启停各 PHP 版本，互不干扰 —— 不再像旧版 `start_phpXX.bat` 那样 `taskkill /IM php-cgi.exe` 一刀切误杀其它版本进程。

> **功能路线规划见 [ROADMAP.md](ROADMAP.md)**：现状能力矩阵、同类工具对标、P0-P3 缺口路线。本文档只描述**已实现**的功能。

## 启动方式

- Windows：双击 `phpvm.bat`（使用 `pythonw.exe` 后台运行，无控制台窗口）
- macOS / Linux：双击 `phpvm.command`（自动挑选带 tkinter 的 Python）或执行 `python3 main.py`
- 或命令行执行：`C:\Python312\python.exe C:\wnrp\phpvm\main.py`
- 无界面启动整套服务（供开机自启脚本调用）：`pythonw main.py --start-all`，结果追加到 `autostart_services.log`
- 依赖：Python 3.x（Windows 优先 `C:\Python312\pythonw.exe`，其次 PATH 中的 `pythonw`；mac 若缺 tkinter 会提示 `brew install python-tk@3.13`），tkinter 标准库，无需第三方包

## 功能说明

### PHP 版本管理页
- **自动发现**：扫描 `C:\wnrp\php*` 目录（要求目录内存在 `php-cgi.exe`，跳过 `phpvm` / `phpcbf`），无需手动注册；macOS/Linux 额外扫描 Homebrew 的 `<prefix>/opt/php*`。当前环境已识别 php / php56 / php72 / php73 / php74 / php8 / php81 / php82 / php83 / php84 / php85（php83、php84 为在线安装新增，端口按 `9000 + 版本号` 规则推导）
- **状态灯**：`●` 绿色=运行中 / `○` 灰色=已停止；列表显示 PHP 版本号、FastCGI 端口、进程 PID、配置文件路径
- **启动 / 停止 / 重启**：对选中版本操作，按端口精确定位进程，不影响其它版本
- **编辑端口**：修改后持久化到 `config.json`；端口合法性（1-65535）与唯一性校验；保存后自动弹出「一键同步 vhost」流程（见下）
- **查看配置**：分三页展示「关键配置」（12 项）、「已启用扩展」与 ini 全文，并提供「用编辑器打开」
- **编辑配置**：提供 **11 项**常用配置表单（memory_limit、post_max_size、upload_max_filesize、max_file_uploads、max_execution_time、max_input_time、display_errors、error_reporting、date.timezone、default_charset、opcache.enable）：
  - 类型校验：size（数字可带 K/M/G）、int（≥ -1 的整数）、onoff（On/Off）、enum（error_reporting 四个预设级别下拉）、timezone（须为合法时区）、str（非空）
  - 保存前自动备份为 `<ini>.bak`（成功也保留），二进制 latin-1 无损逐行替换，未找到的键追加到文件尾
  - 编辑对象是该版本实际使用的配置文件：**目录内存在 `php-web.ini` 时优先编辑它（php82 / php83 / php84 / php85 等由安装器生成的版本均是），否则编辑 `php.ini`**
- **版本自检**：对选中版本一键执行三项检查并分级展示（正常/异常）——① `php -v` 版本解析；② `php -m` 核对 **9 项关键扩展**（redis、pdo_mysql、mysqli、openssl、curl、mbstring、gd、fileinfo、zip）；③ `php -c <该版本 ini>` 校验配置能否正常加载
- **扩展管理**：扫描 `ext/*.dll` 对照 ini 启停扩展（`.bak` 备份、二进制安全写回）；在线安装 redis / xdebug / imagick / swoole / memcached —— 按「PHP 主版本 + NTS/TS + 编译器 + 架构」从 PECL 与 xdebug.org 自动匹配（macOS 提供 brew/pecl 引导）
- **下载新版本**：从 php.net 下载安装任意 PHP 系列新版本（SHA-256 校验、防穿越解压、生成 `php.ini` + `php-web.ini`、默认启用 20 个扩展、按规则规划端口、VC 运行库缺失检测）
- **打开终端**：新开终端窗口并把所选版本目录前置到 PATH（新窗口生效），便于直接以该版本运行 php / composer
- **Composer**：自动探测系统 Composer（`PATH` 与 `C:\ProgramData\ComposerSetup` 等常见位置），点击后在该 PHP 版本的 PATH 下打开终端执行 `composer -V`；未安装时给出安装指引（不内置下载）
- **cmd php 版本切换**：顶部实时显示当前 `php` 命令行生效版本（如 `CMD php：php82 · PHP 8.2.4`），点击「切换」可选择任意版本置顶
- 每 **8 秒**自动刷新运行状态（批量快照：一次 TCP 端口快照 + 一次进程快照完成全部版本状态判定）；顶部 cmd php 版本号按「版本目录 + php.exe 修改时间」缓存，并降频为每 4 轮（约 32 秒）刷新一次

### 站点映射页
- **映射矩阵**：解析 `nginx.conf` 与 `vhost/*.conf` 的全部 server 块，展示「域名 / 配置文件 / fastcgi_pass 端口 / 对应 PHP 版本 / 项目 root」
- **hosts 状态列**：逐域名显示当前 hosts 映射状态（✓ 本机 / ✗ 未映射 / ⚠ 指向其它 IP / `*xxx` 泛解析），新建站点后自动刷新
- **include 检测**：自动检测主配置是否已 `include vhost/*.conf`；未包含时显示提示并提供「自动补 include 并校验」（改前备份 `nginx.conf.bak`，补行后自动 `nginx -t`，通过且 nginx 在跑时询问是否平滑重载）
- **异常高亮**：server 块的 `fastcgi_pass 127.0.0.1:<端口>` 若在 phpvm 端口表中未映射到任何 PHP 版本，该条目标红并显示原因（指向 upstream 或无 PHP 处理的块不算异常），一眼定位「端口改了但 vhost 没同步」等 502 根源
- **一键同步 vhost**：修改端口保存后，自动扫描引用旧端口的配置文件 → 备份（`.bak`）→ 替换 `fastcgi_pass` → `nginx -t` 校验（失败自动还原全部备份）→ 一键平滑重载生效；无文件引用旧端口时只弹普通提示
- 页内按钮为「＋ 新建站点…」「打开 vhost 目录」「刷新」，双击任意行用默认程序打开对应配置文件；**「一键同步」不在本页** —— 它由「编辑端口」保存后自动弹出的同步对话框提供
- **右键站点行**可对该站点直接操作（改配置前自动备份、变更后 `nginx -t` 校验，失败自动还原）：
  - 打开站点（浏览器）/ 打开项目根目录 / 打开配置文件
  - **切换 PHP 版本** ▸ 只改该 `server` 块的 `fastcgi_pass` 端口（同文件多站点互不影响；静态站点无 `fastcgi_pass` 时禁用）
  - **启用 / 禁用站点**：配置改名 `<name>.conf.disabled`（nginx 不再加载）与反向恢复；已禁用站点在列表中置灰显示
  - **从 hosts 移除映射**：只清理 `# >>> phpvm-managed >>>` 托管块内的域名，不碰用户手写映射

### 新建站点向导（可视化分步建站）
- **入口**：站点映射页与 Nginx 管理页的「＋ 新建站点…」按钮
- **第 1 步 基本信息**：填域名（支持多个、`*.test`/`*.dev` 泛解析）、选择项目目录、选择 PHP 版本（自动决定 `fastcgi_pass` 端口）
- **第 2 步 应用模板**：内置 **Laravel / WordPress / ThinkPHP / 通用 PHP（无框架）/ 静态站点 / 前端 SPA（history 路由回退）** 六套模板，自动拼文档根（Laravel / ThinkPHP → `/public`），可自定义生成的配置文件名（默认 `<安全域名>.conf`），右侧**深色实时预览**最终 nginx 配置
- **第 3 步 hosts 映射**：一键把域名写入 hosts 指向 `127.0.0.1` —— 已指向本机自动跳过、指向其它 IP 不覆盖仅提示、`*.` 泛解析跳过；写入内容带 `# >>> phpvm-managed >>>` 标记块；无写权限时 Windows 弹 UAC（PowerShell RunAs）、macOS 弹系统授权框（osascript）完成提权写入；提供「打开 hosts 文件」「刷新状态」
- **第 4 步 确认创建**：自动完成「写 vhost 配置（同名自动备份 `.bak`）→ 主配置未 include vhost 时自动补行 → `nginx -t` 校验 → 写入 hosts → 平滑重载」共 5 步，全程日志可视化；配置/补 include/校验为硬项，任一失败即询问保留或一键回滚
- **HTTPS（可选）**：第 2 步可勾选「启用 HTTPS」—— 检测到系统 openssl / mkcert 时可用，创建时先生成证书到 `<nginx>/SSL/` 再生成 443 变体配置（mkcert 优先，其证书本机自动信任；openssl 自签需浏览器手动信任；失败视为硬失败并触发回滚）；未检测到工具时选项禁用并给出安装指引
- 已知边界：未找到 nginx 可执行文件时跳过校验与重载（仍写配置与 hosts）；不支持编辑既有站点（同名覆盖重建）；未勾选 HTTPS 时模板为 `listen 80`
- **回滚范围**：失败或取消时还原 vhost 文件、`nginx.conf` **与本次写入的 hosts 映射**（hosts 每次写入前自动备份为 `hosts.phpvm.bak`，可一键还原）

### SQLite 数据库页
- **自动发现**：扫描环境根与各站点 `root` 目录下的 `*.db` / `*.sqlite` / `*.sqlite3` / `*.db3`（跳过 `node_modules`、`.git` 等依赖/缓存目录），下拉选择或「浏览…」手动指定
- **只读浏览**：左侧列出表 / 视图清单，选中即显示列名 / 类型 / 约束 / 默认值与行数；双击表名自动生成 `SELECT * FROM ... LIMIT 200` 并执行
- **执行查询**：SQL 编辑框支持 `SELECT` / `WITH` / `PRAGMA` / `EXPLAIN` / `VALUES`（Ctrl/Cmd+Enter 快捷执行），结果每页 500 行，可「上一页 / 下一页」翻页
- **导出结果**：把当前页结果导出为 CSV（UTF-8 BOM，Excel 可直接打开）
- **安全兜底**：连接以只读 URI（`mode=ro` + `PRAGMA query_only`）打开，叠加语句首关键字白名单（`SELECT` / `WITH` / `PRAGMA` / `EXPLAIN` / `VALUES`）双重限制，杜绝误写；查询超时（默认 10s，行数统计 3s）自动中断，避免大表卡死界面
- 发现范围：环境根 + 各站点 `root` 目录，向下最多 4 层、最多 300 个文件，跳过 `node_modules` / `.git` / `venv` 等依赖与缓存目录
- 记忆上次打开的数据库（`settings.sqlite_last_db`），下次启动自动带回
- 已知边界：只读浏览与查询，**无导出（CSV/SQL）、无结果分页（超 500 行截断）、无写操作与结构修改**

### 崩溃检测告警
- 数据源：Windows 读取事件日志（Application / Id=1000）；macOS 读取 `~/Library/Logs/DiagnosticReports` 与 `/Library/Logs/DiagnosticReports` 下的 `php-cgi-*.ips`（解析信号、终止原因、故障模块与偏移、调用栈）；识别 `php-cgi` 崩溃（如 JIT 导致的 0xc0000005）
- 发现新崩溃：状态栏红色告警 + 托盘气泡 + 弹窗详情（崩溃时间、版本、故障模块、异常码、偏移、完整消息）
- 详情弹窗底部**完整展示事件原始消息**（含出错应用程序名称/路径、错误模块路径、报告 ID 等，逐项换行可读）
- 详情弹窗提供**「清空记录」**：确认已读后重置检测游标、关闭状态栏告警，历史事件不会再次弹出
- 启动时回溯最近 24h（仅状态栏/托盘提示，不弹窗）；运行中每 8 秒心跳、每 8 轮（约 64 秒）查一次事件日志并保活守护进程
- 状态栏告警显示为可点击的「⚠ php-cgi 崩溃 N 次，点击查看」，**不会自动消失**，只能「清空记录」后清除
- **崩溃自愈（默认关闭）**：可在「关于」页开启（写入 `settings.auto_recover_crash`）。自愈由**独立守护进程** `core/crash_watchdog.py` 常驻执行（与 GUI 生命周期解耦，开关关闭后下一轮自行退出）：
  - 双重检测：崩溃事件 + 看护版本的端口 / 进程失联探测（覆盖「进程被清理但无崩溃事件」的故障）
  - 防抖 **60 秒**、每版本每 **3600 秒**最多 `auto_recover_limit`（默认 3）次；连续失败 **5 次**自动解除该版本看护并提示人工介入
  - GUI 手动停止某版本后进入 **300 秒宽限期**，期间不会被自动拉回；启动 / 重启成功则重新纳入看护
  - 守护每 10 秒做失联探测、每 3 轮（约 30 秒）查一次事件日志（回溯 6 小时）；单实例锁 `crash_watchdog.lock`，状态 `crash_watchdog.json`，日志 `crash_watchdog.log`
  - 每次决策写入 `recover_history.json`（上限 200 条），在崩溃详情对话框的「自愈历史」页可视化展示

### Nginx 日志页
- 下拉选择日志目录下的文件（默认优先 `error.log` → `access.log`，含轮转的 `.log.1` / `.log.2`）；**日志目录按生效的 nginx 布局推导**（Windows 为 `<nginx>/logs`，Homebrew 安装会取到 brew 的日志目录），不再写死
- 首次加载读取文件尾部（最大 256KB / 2000 行），之后按 offset 增量追加；日志轮转（文件变小）自动重置
- 「自动跟随」默认勾选，只有勾选时才随主窗口 8 秒心跳增量刷新；按钮为刷新 / 清屏（仅清显示区）/ 打开日志目录，另有**过滤输入框**（实时、不区分大小写、命中处高亮）
- 行级着色：`[error]/[crit]/[alert]/[emerg]` 红、`[warn]/[notice]` 橙、时间戳蓝、HTTP 5xx 红 / 4xx 橙
- 顶部信息栏实时显示：文件大小 · 最后修改时间 · 总行数 · 过滤后显示行数 · 错误行数 · 警告行数

### 系统托盘
- **最小化到托盘**：点最小化按钮直接隐藏到托盘；关闭按钮弹确认框（最小化到托盘 / **重启** / 退出 / 取消），「重启」会以 `PHPVM_RESTART=1` 重新拉起 phpvm 并抢回单实例锁
- **一键启停整套服务**：菜单顶部为「全部启动服务」「全部停止服务」（启动顺序 PHP → Redis → MySQL → Nginx，停止反之；已运行的自动跳过，结果逐项汇总）
- **右键动态菜单**：每次弹出实时生成——
  - `显示 phpvm` / `隐藏到托盘` / `退出`
  - `Nginx` 子菜单：状态（PID）+ 启动 / 停止 / 重载配置 / 配置检查（按运行状态自动禁用，配置检查始终可用）
  - `Redis` 子菜单（每实例一个，标题 `Redis [<目录名>]`）：状态（运行 / 停止 · PID · 端口）+ 启动 / 停止 / 重启；未发现实例时显示灰色的「Redis：未发现实例」
  - `MySQL` 子菜单（每实例一个，标题 `MySQL [<目录名>]`）：状态（运行 / 停止 · PID · 端口）+ 启动 / 停止 / 重启；未发现实例时显示灰色的「MySQL：未发现实例」（服务模式启停需管理员权限）
  - 各 PHP 版本子菜单：状态（端口/PID）+ 启动 / 停止 / 重启（停止与重启仅在运行中可用）；运行中的版本前缀 `●`
- 托盘操作结果：状态栏提示 + 气泡通知，并立即刷新各面板状态；手动停止某版本会同时解除崩溃看护
- 双击托盘图标恢复主窗口（托盘仅 Windows 可用，macOS 无菜单栏托盘）

### cmd php 版本切换（顶部栏）
- 顶部右侧实时显示系统 `cmd` / 终端中 `php` 命令当前生效的版本目录与版本号
- 点击「切换」弹出版本列表，选择目标版本后「设为当前」
- 原理：修改**用户级** PATH（`HKCU\Environment`，优先级高于系统 PATH）并将目标目录置顶，**无需管理员权限**；新打开的 cmd 生效，已打开的窗口不生效
- 提供「在新窗口测试 php -v」按钮，直观验证切换结果（macOS 用 Terminal 打开）
- macOS / Linux 改写 `~/.zshrc` 与 `~/.bash_profile` 中的 `# >>> phpvm-managed >>>` 块，同样新开终端才生效

### Nginx 管理页
- 启动 / 平滑重载（`-s reload`）/ 配置检查（`-t`）/ 停止 / 刷新状态，并有「＋ 新建站点向导…」入口
- 实时显示运行状态、进程 PID、版本号、前缀目录
- 命令输出写入右侧日志区（不同类型着色），配置检查结果直观可见
- 说明：Nginx 通过 `nginx.exe -p <prefix>` 直接调用（不创建控制台窗口），**不使用** `RunHiddenConsole.exe`；该隐藏启动器仅用于 php-cgi 与 redis-server 的后台启动

### Redis 管理页
- **多实例自动发现**：扫描环境根下的 `Redis*` 目录（macOS 含 Homebrew keg），顶部实例下拉**默认选版本号最高的实例**；状态（端口/PID/版本）+ 启动 / 停止 / 重启
- **状态与日志**：信息行显示 PID / 版本 / 端口 / 配置文件 / 数据目录；按钮为启动 Redis、重启、停止 Redis、测试连接（PING）、打开配置文件、刷新状态；右侧为命令输出区
- **Redis 命令**：选择目标逻辑库（DB 下拉 **0–15**）后输入单行命令，回车或点「执 行」（`redis-cli -p <端口> -n <db>` stdin 逐行模式，输出异步渲染）；`FLUSHALL` / `FLUSHDB` / `SHUTDOWN` / `SLAVEOF` / `REPLICAOF` / `DEBUG` 执行前弹二次确认；执行完成后自动刷新键空间
- **DB 键空间**：Canvas 柱状图展示各逻辑库 key 数（忽略空库，大数值按语言本地化为「万 / k / M」，底部显示总 key 数与统计时间）；点击图表或「刷新」重新统计

### MySQL 管理页
- **自动发现**：扫描环境根下含 `bin/mysqld` 的 `mysql*` 目录，解析 `my.ini` / `my.cnf` 得到端口（默认 3306）与数据目录，并显示 `mysqld --version` 版本
- **Windows 服务优先**：若该实例已注册为 Windows 服务（如服务名 `MySQL`），状态与启停走 `sc` / `net start|stop`，**不会再拉起第二个 mysqld**；未注册服务时退回独立进程模式（隐藏启动 mysqld，关闭优先 `mysqladmin shutdown`）
- 状态卡显示版本 / 端口 / PID / 运行方式 / 服务名 / 启动类型 / 配置文件 / 数据目录；按钮为启动 / 停止 / 重启 / 打开配置文件 / 打开数据目录 / 查看错误日志（读 `data/*.err` 尾部）
- **权限提示**：启停 Windows 服务需要管理员权限，非管理员运行时启停按钮禁用并提示「以管理员身份运行 phpvm」（状态查看不受影响）；停止前有二次确认
- 托盘菜单提供各实例快捷启停

### 关于页
- **环境信息**：环境根目录、PHP FastCGI 配置（php82 / php85 → php-web.ini，其余 → php.ini）、FastCGI 监听、Nginx 前缀、隐藏启动器（Windows）、配置持久化路径
- **功能模块**：勾选需要加载的模块（默认全部启用），取消勾选后**重启 phpvm 生效**，该模块的代码将不再加载（例如取消「SQLite 数据库」后不会创建该页签，也不会导入其管理器）
  - 可停用：Redis 管理 / MySQL 管理 / SQLite 数据库 / Nginx 日志
  - 刚需（不可取消）：PHP 版本管理 / Nginx 管理 / 站点映射
- **设置区**（3 项）：
  - 开机自动启动 phpvm（Windows 写 `HKCU\...\Run` 的 `phpvm` 值；macOS 写 LaunchAgent `com.phpvm.app`）
  - php-cgi 崩溃自愈开关（默认关闭），注明「防抖 60 秒、每版本每小时最多 3 次」
  - 界面语言下拉（简体中文 / 繁體中文 / English / 日本語 / 한국어），点「应用」后**重启 phpvm 生效**
  - 服务编排：「全部启动」「全部停止」两个按钮（关于页与托盘菜单均可操作，后台执行并逐项汇总结果）
  - 开机自动启动全部服务（登录时静默启动 Nginx + 各 PHP + Redis + MySQL，写入用户「启动」目录；**仅 Windows**，无需管理员）

### 界面语言（i18n）
- 全部界面文案经 `t()` 翻译，内置 **5 种语言**：简体中文（源码原文，无词条表）、繁體中文、English、日本語、한국어
- 切换入口：菜单栏「语言」与关于页「界面语言」下拉；选择写入 `config.json` 的 `settings.lang`，重启后生效（与关闭框的「重启」配合）
- 首次运行按系统 UI 语言自动选择（Windows 读 LCID，其它平台读 `LC_ALL` / `LANG`），未知语言回落 English
- 词条表位于 `i18n/<语言>/` 下 11 个分块 JSON（约 700 条）；`i18n/_keys.json` 与 `_i18n_scan.py` 是覆盖度扫描工具（`python _i18n_scan.py --report` 查看各语言缺失）

### 单实例与窗口自适应
- **单实例**：Windows 用命名互斥体 `Global\wnrp_phpvm_singleton_mutex`；macOS/Linux 用 `<tmp>/phpvm_singleton.sock` 抽象套接字锁（残留自动清理）。重复启动提示「phpvm 已经在运行中」后退出；带 `PHPVM_RESTART=1` 启动时会轮询约 5 秒抢锁，实现平滑重启
- **窗口自适应**：所有窗口经 `ui/window_utils.fit_window()` 计算 —— 按屏幕真实工作区（Windows 排除任务栏、macOS 预留程序坞）收敛期望尺寸并夹紧在可视区内，父窗口已映射时在其中偏上居中；对话框底部按钮栏优先布局，确保小屏也可见

## 端口映射（默认）

| 版本目录 | 默认端口 | 说明 |
|---|---|---|
| php   | 9001 | 兼容 fund.conf / type_test.conf 的 vhost |
| php56 | 9056 | |
| php72 | 9072 | |
| php73 | 9073 | |
| php74 | 9074 | 已按约定改为 9074 |
| php8  | 9080 | |
| php81 | 9081 | |
| php82 | 9000 | 主版本，vhost 默认指向 |
| php85 | 9085 | 新增版本，ini 已含 redis 扩展 |
| php83 | 9083 | 后续新增（不在内置默认表，端口按 `9000 + 版本号` 推导） |
| php84 | 9084 | 同上 |

> **端口与 vhost 的关系**：nginx vhost 中 `fastcgi_pass 127.0.0.1:9000;` 决定该站点由哪个 PHP 版本解析。
> 在界面中修改端口后，**必须**同步修改对应 vhost 的 `fastcgi_pass` 才会生效。phpvm 已内置「一键同步」：
> 保存端口后自动列出引用旧端口的配置文件，一键替换 + `nginx -t` 校验（失败自动还原）+ 平滑重载，全程无需手动改文件。

> **「默认」与「本机实际」**：上表前 9 行是 `core/config.py` 中 `DEFAULT_PORTS` 的内置默认值（**不含 php83 / php84**）；
> 新装版本按 `9000 + 版本号` 规则推导端口并写入 `config.json`，因此本机实际 ports 比默认表多 php83=9083、php84=9084 两项。

## 配置持久化（config.json）

```json
{
  "ports":    { "<版本目录名>": 9000 },
  "settings": {
    "auto_recover_crash": false,
    "auto_recover_limit": 3,
    "lang": null,
    "sqlite_last_db": ""
  }
}
```

- `ports`：键为版本目录名（php / php56 / … / php85），值为 FastCGI 监听端口；「编辑端口」与在线安装写回此处
- `settings.auto_recover_crash`：崩溃自愈开关，**默认 false**
- `settings.auto_recover_limit`：自愈限次（每版本每 3600 秒最多 N 次，默认 3）
- `settings.lang`：界面语言（空=跟随系统），`settings.sqlite_last_db`：上次打开的 SQLite 库，`settings.disabled_modules`：已停用的可选模块
- 文件损坏或 JSON 解析失败时回退内置默认值并覆盖保存（未知键会被丢弃）

## 目录结构

```
C:\wnrp\phpvm\
├── main.py                # 入口（单实例：Win 互斥体 / posix socket 锁；重启抢锁）
├── phpvm.bat              # Windows 双击启动脚本
├── phpvm.command          # macOS / Linux 双击启动脚本
├── config.json            # 端口映射 + 功能开关（auto_recover_crash / auto_recover_limit / lang / sqlite_last_db）
├── _bench.py              # 状态刷新性能基准
├── _e2e.py                # 端到端冒烟：版本扫描 + 状态判定耗时
├── _i18n_scan.py          # i18n 文案扫描（生成 i18n/_keys.json，--report 看覆盖度）
├── README.md
├── ROADMAP.md             # 功能缺口调研与开发路线（规划文档）
├── i18n/                  # 词条表：<语言>/00a_core_install … 10z_dynamic（en / ja / ko / zh_TW）
├── core/                  # 服务层
│   ├── config.py          # 配置加载/保存/端口校验 + 环境根 / brew 前缀推导
│   ├── i18n.py            # t() 翻译与语言检测/切换（settings.lang）
│   ├── process_utils.py   # 批量快照 API（Win: GetExtendedTcpTable/EnumProcesses；posix: lsof/ps）+ 启停/命令执行
│   ├── php_manager.py     # 版本扫描/解析/启停/状态（三重校验）+ 批量状态刷新（含 brew keg）
│   ├── php_downloader.py  # php.net / PECL 下载与安全解压（架构/编译器探测、SHA-256）
│   ├── php_installer.py   # 新版本在线安装（生成 php.ini + php-web.ini、端口规划、VC 检测）
│   ├── php_extension.py   # 扩展管理（ext 扫描 + ini 写回 + PECL/xdebug 在线安装）
│   ├── path_manager.py    # 终端 php 版本切换（Win 用户 PATH 置顶 / posix 写 .zshrc 块）
│   ├── nginx_manager.py   # nginx 启停/重载/配置检查（Win -p / brew 模式；派生 vhost 与日志目录）
│   ├── redis_manager.py   # Redis 多实例发现/启停/命令执行/键空间统计
│   ├── mysql_manager.py   # MySQL 发现/启停（Windows 服务优先，进程模式兜底）/错误日志
│   ├── cert_manager.py    # 本地 HTTPS 证书（探测 openssl/mkcert，生成与清理）
│   ├── service_group.py   # 整套服务编排：一键全启动 / 全停止（PHP+Redis+MySQL+Nginx）
│   ├── tool_manager.py    # 外部工具探测（Composer 路径/版本与按 PHP 版本运行）
│   ├── modules.py         # 可选模块注册表（开关/持久化，停用后不再加载该模块）
│   ├── icon.py            # 托盘图标 phpvm.ico 生成（标准库写 ICO，仓库不内置二进制）
│   ├── vhost_manager.py   # 站点映射解析 + 端口一键同步 + 站点文件写入 / include 自动补全
│   ├── site_templates.py  # 6 套站点 nginx 模板（Laravel/WordPress/ThinkPHP/通用/静态/SPA）
│   ├── hosts_manager.py   # hosts 自动映射（跨平台；PowerShell RunAs / osascript 提权写入）
│   ├── sqlite_manager.py  # SQLite 只读查询（发现/打开/表结构/查询，URI mode=ro 兜底）
│   ├── health_monitor.py  # 崩溃检测（Win 事件日志 / mac .ips）+ 版本一键自检
│   ├── crash_watchdog.py  # 崩溃自愈独立守护进程（事件 + 失联探测 + 防抖限次）
│   ├── recover_history.py # 自愈决策历史读写（recover_history.json）
│   ├── ini_editor.py      # ini 关键配置项表单编辑（校验/备份/精确行替换）
│   └── autostart.py       # 开机自启（Win HKCU Run / mac LaunchAgent）
└── ui/                    # 界面层
    ├── main_window.py     # 主窗口（七页签 + 菜单栏语言 + 状态栏 + 崩溃告警/自愈 + 设置区）
    ├── php_panel.py       # PHP 版本管理页（启停/端口/ini/自检/扩展/下载新版本）
    ├── nginx_panel.py     # Nginx 管理页（含新建站点向导入口）
    ├── redis_panel.py     # Redis 管理页（状态/日志 + 命令执行 + DB 键空间图表）
    ├── mysql_panel.py     # MySQL 管理页（服务/进程状态 + 启停 + 配置与错误日志）
    ├── vhost_panel.py     # 站点映射页（hosts 状态列 + include 检测 + 新建站点入口）
    ├── site_wizard.py     # 新建站点可视化向导（4 步 + 配置实时预览 + 自动校验/回滚）
    ├── sqlite_panel.py    # SQLite 数据库页（表/结构浏览 + 只读查询）
    ├── nginx_log_panel.py # Nginx 日志页（tail 增量 / 自动跟随 / 过滤）
    ├── dialogs.py         # 端口同步/配置查看编辑/自检/崩溃详情/CLI 切换对话框
    ├── download_dialog.py # 新版本下载安装对话框
    ├── extension_dialog.py# 扩展管理对话框（启停 + 在线安装）
    ├── tray.py            # 系统托盘（动态右键菜单/气泡告警/最小化到托盘，仅 Windows）
    ├── window_utils.py    # 窗口尺寸与位置自适应（工作区收敛/居中/夹紧）
    └── theme.py           # 统一主题样式
```

## 与现有脚本的关系

| 场景 | 推荐方式 |
|---|---|
| 日常启停 PHP 版本 | phpvm（按端口精确控制） |
| 批量切换主版本（杀 nginx+全部 php-cgi） | 仍可用 `start_nginx-phpXX.bat`，但会互相冲突 |
| 校验 nginx 配置 | phpvm「配置检查」或 `check_nginx_conf.bat` |
| 重启 nginx | phpvm「平滑重载」或 `reload_nginx.bat` |

## 常见问题

- **端口被占用启动失败**：界面会提示占用进程（名称+PID）。请先停止占用进程，或在「编辑端口」中更换端口并用「一键同步」同步 vhost。
- **修改端口后站点 502/404**：编辑端口保存后务必在弹出的一键同步对话框中执行替换并「重载 Nginx」；或在「站点映射」页检查异常高亮条目。
- **php-cgi 反复崩溃（站点 502）**：状态栏会弹出崩溃告警，点击查看事件详情（故障模块/异常码/偏移）。异常码 0xc0000005 常见于 opcache JIT 或扩展冲突，可检查 `php-web.ini` 中 `opcache.jit` 设置。
- **php82 / php85 特殊**：FastCGI 使用 `php-web.ini`（与 CLI 的 `php.ini` 区分），工具已自动处理。
- **FastCGI 用哪个 ini**：目录内存在 `php-web.ini` 时 FastCGI 就使用它（安装器为 php82 / php83 / php84 / php85 等均生成了该文件），否则用 `php.ini`。早期版本仅对 `php82`、`php85` 两个目录名生效，导致 php83 / php84 改 `php-web.ini` 不起作用，现已修正。

## macOS / Linux 支持

phpvm 已可在 macOS 管理本地多版本 PHP + Nginx + Redis（Windows 的路径与行为全部保留）。

- **环境根目录**：macOS/Linux 默认 `~/wnrp`（可用环境变量 `WNRP_ROOT` 覆盖）；Windows 仍为 `C:\wnrp`。
- **运行依赖**：Python ≥ 3.10 且带 tkinter（Homebrew：`brew install python-tk@3.13`），启动方式：
  ```bash
  cd <phpvm 目录>
  ./phpvm.command        # 或 python3 main.py
  ```
- **已适配**：
  - 进程 / 端口：`lsof` / `ps` 快照（与 Windows 双快照同接口）；后台启动用 `start_new_session`；终止 `SIGTERM` → `SIGKILL`
  - PHP：扫描 Homebrew keg `<prefix>/opt/php*`（`php@7.4` → `php74`），按 realpath 去重别名；仍以 `php-cgi -b 127.0.0.1:<port>` 运行（**不使用 php-fpm**）
  - Nginx：识别 brew 安装的 nginx（不带 `-p`，prefix 为 `<brew>/etc/nginx`），进程判定带锚点过滤避免误纳他人实例；日志目录按生效配置推导；`stop` 发 `SIGQUIT`、重载失败兜底 `SIGHUP`
  - Redis：扫描 brew keg，配置回退 `<prefix>/etc/redis.conf`
  - 终端 php 切换：维护 `~/.zshrc` / `~/.bash_profile` 的 `# >>> phpvm-managed >>>` 块
  - hosts 写入：`osascript ... with administrator privileges` 弹系统授权框提权
  - 崩溃检测：解析 `~/Library/Logs/DiagnosticReports` 与 `/Library/Logs/DiagnosticReports` 下的 `php-cgi-*.ips`
  - 开机自启：写入 LaunchAgent `com.phpvm.app`；单实例用 `<tmp>/phpvm_singleton.sock`；路径打开统一走 `open`
- **尚未支持**：macOS 菜单栏托盘（非 Windows 直接跳过托盘初始化）；在线下载/安装 PHP 与扩展在 mac 改为给出 brew / pecl 引导，不走 php.net 安装包。
