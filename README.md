# phpvm · PHP 版本管理器

Windows 桌面 GUI 工具，用于统一管理 `C:\wnrp` 开发环境中的 **多个 PHP 版本** 与 **Nginx**，取代手动双击各种 bat 脚本的繁琐操作。

仅本机使用，无需登录。按端口精确启停各 PHP 版本，互不干扰 —— 不再像旧版 `start_phpXX.bat` 那样 `taskkill /IM php-cgi.exe` 一刀切误杀其它版本进程。

## 启动方式

- 双击 `C:\wnrp\phpvm\phpvm.bat`（使用 `pythonw.exe` 后台运行，无控制台窗口）
- 或命令行执行：`C:\Python312\python.exe C:\wnrp\phpvm\main.py`
- 依赖：Python 3.x（优先 `C:\Python312\pythonw.exe`，其次 PATH 中的 `pythonw`），tkinter 标准库，无需第三方包

## 功能说明

### PHP 版本管理页
- **自动发现**：扫描 `C:\wnrp\php*` 目录，自动识别 php / php56 / php72 / php73 / php74 / php8 / php81 / php82 / php85，无需手动注册
- **状态灯**：`●` 绿色=运行中 / `○` 灰色=已停止；列表显示 PHP 版本号、FastCGI 端口、进程 PID、配置文件路径
- **启动 / 停止 / 重启**：对选中版本操作，按端口精确定位进程，不影响其它版本
- **编辑端口**：修改后持久化到 `config.json`；端口合法性（1-65535）与唯一性校验；保存后自动弹出「一键同步 vhost」流程（见下）
- **查看配置 / 编辑配置**：查看 `php.ini` 关键配置与完整内容；「编辑配置」提供常用项表单（memory_limit、post_max_size、upload_max_filesize、max_execution_time、display_errors、date.timezone、opcache.enable 等），带类型校验与自动备份（`.bak`），改后提示重启生效
- **版本自检**：对选中版本一键执行 `php -v`、`php -m`（核对 redis / pdo_mysql / openssl / curl / mbstring / gd 等 9 项关键扩展）、指定 ini 配置加载校验，结果分级展示（正常/异常）
- **cmd php 版本切换**：顶部实时显示当前 `php` 命令行生效版本（如 `CMD php：php82 · PHP 8.2.4`），点击「切换」可选择任意版本置顶
- 每 4 秒自动刷新运行状态（批量快照：一次系统调用完成全部版本状态判定）与 cmd php 版本（带缓存）

### 站点映射页
- **映射矩阵**：解析 `nginx.conf` 与 `vhost/*.conf` 的全部 server 块，展示「域名 / 配置文件 / fastcgi_pass 端口 / 对应 PHP 版本 / 项目 root」
- **异常高亮**：端口未映射到任何已配置 PHP 版本的条目标红，一眼定位「端口改了但 vhost 没同步」等 502 根源
- **一键同步 vhost**：修改端口保存后，自动扫描引用旧端口的配置文件 → 备份（`.bak`）→ 替换 `fastcgi_pass` → `nginx -t` 校验（失败自动还原全部备份）→ 一键平滑重载生效

### 崩溃检测告警
- 周期读取 Windows 事件日志（Application/1000），识别 `php-cgi.exe` 崩溃（如 JIT 导致的 0xc0000005）
- 发现新崩溃：状态栏红色告警 + 托盘气泡 + 弹窗详情（崩溃时间、版本、故障模块、异常码、偏移、完整消息）
- 详情弹窗底部**完整展示事件原始消息**（含出错应用程序名称/路径、错误模块路径、报告 ID 等，逐项换行可读）
- 详情弹窗提供**「清空记录」**：确认已读后重置检测游标、关闭状态栏告警，历史事件不会再次弹出
- 启动时回溯最近 24h，仅状态栏/托盘提示，不打扰操作
- **崩溃自愈（默认关闭）**：可在「关于」页开启；检测到运行中崩溃后自动重启对应版本 php-cgi，带防抖（60s）与限次（每版本每小时最多 3 次），避免崩溃循环刷进程

### Nginx 日志页
- 下拉选择 `C:\wnrp\nginx\logs\` 下的日志（默认 `error.log` / `access.log`）
- 首次加载读取文件尾部（最大 256KB / 2000 行），之后增量追加；日志轮转自动重置
- 「自动跟随」勾选时随主窗口周期自动刷新；支持手动刷新 / 清屏 / 打开日志目录
- 顶部实时显示文件大小与最后修改时间；error 日志行红色高亮

### 系统托盘
- **最小化到托盘**：点最小化按钮直接隐藏到托盘；关闭按钮弹确认框（最小化到托盘 / 退出 / 取消）
- **右键动态菜单**：每次弹出实时生成——
  - `显示 phpvm` / `隐藏到托盘` / `退出`
  - `Nginx` 子菜单：状态（PID）+ 启动 / 停止 / 重载配置 / 配置检查（按运行状态自动禁用）
  - 各 PHP 版本子菜单：状态（端口/PID）+ 启动 / 停止 / 重启；运行中的版本前缀 `●`
- 托盘操作结果：状态栏提示 + 气泡通知，并立即刷新各面板状态
- 双击托盘图标恢复主窗口

### cmd php 版本切换（顶部栏）
- 顶部右侧实时显示系统 `cmd` / 终端中 `php` 命令当前生效的版本目录与版本号
- 点击「切换」弹出版本列表，选择目标版本后「设为当前」
- 原理：修改**用户级** PATH（`HKCU\Environment`，优先级高于系统 PATH）并将目标目录置顶，**无需管理员权限**；新打开的 cmd 生效，已打开的窗口不生效
- 提供「在新窗口测试 php -v」按钮，直观验证切换结果

### Nginx 管理页
- 启动 / 平滑重载（`-s reload`）/ 配置检查（`-t`）/ 停止
- 实时显示运行状态、进程 PID、版本号、前缀目录
- 命令输出写入右侧日志区，配置检查结果直观可见

### 关于页
- 环境信息（FastCGI 配置、Nginx 前缀、配置持久化位置等）
- **设置区**：开机自动启动 phpvm（写 `HKCU\...\Run`，pythonw 隐藏运行，仅当前用户）、php-cgi 崩溃自愈开关（默认关闭）

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

> **端口与 vhost 的关系**：nginx vhost 中 `fastcgi_pass 127.0.0.1:9000;` 决定该站点由哪个 PHP 版本解析。
> 在界面中修改端口后，**必须**同步修改对应 vhost 的 `fastcgi_pass` 才会生效。phpvm 已内置「一键同步」：
> 保存端口后自动列出引用旧端口的配置文件，一键替换 + `nginx -t` 校验（失败自动还原）+ 平滑重载，全程无需手动改文件。

## 目录结构

```
C:\wnrp\phpvm\
├── main.py                # 入口（单实例保护）
├── phpvm.bat              # 双击启动脚本
├── config.json            # 端口映射配置（首次运行自动生成）
├── _bench.py              # 状态刷新性能基准（netstat / 单端口 / 批量快照对比）
├── README.md
├── core/                  # 服务层
│   ├── config.py          # 配置加载/保存/端口校验
│   ├── process_utils.py   # 批量快照 API（GetExtendedTcpTable/EnumProcesses）+ 启停封装
│   ├── php_manager.py     # 版本扫描/解析/启停/状态（三重校验）+ 批量状态刷新
│   ├── path_manager.py    # cmd php 命令版本切换（用户 PATH 置顶）+ 版本缓存
│   ├── nginx_manager.py   # nginx 启停/重载/配置检查
│   ├── vhost_manager.py   # 站点映射解析 + 端口一键同步（备份/回滚/nginx -t 校验）
│   ├── health_monitor.py  # php-cgi 崩溃检测（事件日志）+ 版本一键自检
│   ├── ini_editor.py      # ini 关键配置项表单编辑（校验/备份/精确行替换）
│   └── autostart.py       # 开机自启（HKCU Run 注册表项，pythonw 隐藏运行）
└── ui/                    # 界面层
    ├── main_window.py     # 主窗口（五页签 + 状态栏 + 崩溃告警/自愈 + 设置区）
    ├── php_panel.py       # PHP 版本管理页
    ├── nginx_panel.py     # Nginx 管理页
    ├── vhost_panel.py     # 站点映射页
    ├── nginx_log_panel.py # Nginx 日志页（tail 增量 / 自动跟随）
    ├── dialogs.py         # 端口同步/配置查看编辑/自检/崩溃详情对话框
    ├── tray.py            # 系统托盘（动态右键菜单/气泡告警/最小化到托盘）
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
- **php82/php85 特殊**：FastCGI 使用 `php-web.ini`（与 CLI 的 `php.ini` 区分），工具已自动处理。
