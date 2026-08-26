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
- **编辑端口**：修改后持久化到 `config.json`；端口合法性（1-65535）与唯一性校验；保存后提示同步 nginx vhost 的 `fastcgi_pass`
- **查看配置**：展示 `php.ini` 关键配置（memory_limit、post_max_size、upload_max_filesize、max_execution_time、扩展列表等）、完整 ini 内容，并可一键用编辑器打开
- **cmd php 版本切换**：顶部实时显示当前 `php` 命令行生效版本（如 `CMD php：php82 · PHP 8.2.4`），点击「切换」可选择任意版本置顶
- 每 4 秒自动刷新运行状态与 cmd php 版本

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
> 在界面中修改端口后，**必须**同步修改对应 vhost 的 `fastcgi_pass` 才会生效（修改后弹窗会提示并可直接打开 vhost 目录 `C:\wnrp\nginx\conf\vhost`）。

## 目录结构

```
C:\wnrp\phpvm\
├── main.py                # 入口（单实例保护）
├── phpvm.bat              # 双击启动脚本
├── config.json            # 端口映射配置（首次运行自动生成）
├── README.md
├── core/                  # 服务层
│   ├── config.py          # 配置加载/保存/端口校验
│   ├── process_utils.py   # netstat/tasklist/taskkill/隐藏启动封装
│   ├── php_manager.py     # 版本扫描/解析/启停/状态（三重校验）
│   ├── path_manager.py    # cmd php 命令版本切换（用户 PATH 置顶）
│   └── nginx_manager.py   # nginx 启停/重载/配置检查
└── ui/                    # 界面层
    ├── main_window.py     # 主窗口（三页签 + 状态栏）
    ├── php_panel.py       # PHP 版本管理页
    ├── nginx_panel.py     # Nginx 管理页
    ├── dialogs.py         # 端口编辑/配置查看对话框
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

- **端口被占用启动失败**：界面会提示占用进程（名称+PID）。请先停止占用进程，或在「编辑端口」中更换端口并同步 vhost。
- **修改端口后站点 502/404**：检查对应 vhost 的 `fastcgi_pass` 是否已同步为新端口。
- **php82/php85 特殊**：FastCGI 使用 `php-web.ini`（与 CLI 的 `php.ini` 区分），工具已自动处理。
