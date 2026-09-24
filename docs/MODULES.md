# 功能模块地图

「某个能力在哪、改它要动哪些文件」的查表入口。**架构与约定**见 [`ARCHITECTURE.md`](ARCHITECTURE.md)，
**用户视角的功能说明**见 [`../README.md`](../README.md)。

## 1. 主窗口结构

页签按 `core/modules.py` 的模块开关动态构建，顺序固定为（括号为对应的模块 key，`ui/main_window.py::_build()`）：

```
PHP 版本管理(php,刚需) │ Nginx 管理(nginx,刚需) │ Redis 管理(redis) │ MySQL 管理(mysql)
│ 站点映射(vhost,刚需) │ SQLite 数据库(sqlite) │ Nginx 日志(log)
│ 运行日志(常驻，不受开关影响) │ 关于(设置区/模块开关在此)
```

- 顶部：菜单栏（外观 / 语言 / 帮助）、状态栏（就绪消息 + 更新提示 + 崩溃告警）。
- 托盘（Windows）：动态菜单 —— 一键启停整套服务、Nginx 子菜单、各 PHP 版本、Redis 子菜单、显示/隐藏/退出。
- 页签构建在 `ui/main_window.py::_build()`；「关于」页（含设置区、模块开关、服务编排按钮）在 `_build_about()`。

## 2. 能力 → 落点对照表

| 能力 | UI 入口 | core 落点 | 持久化 / 副作用 | 关键约束 |
|---|---|---|---|---|
| PHP 版本扫描与启停 | `ui/php_panel.py` | `php_manager.py`、`process_utils.py` | `config.json → ports` | 按端口 + PID + 命令行三重校验，不误杀其它版本 |
| php.ini 查看 / 编辑 / 初始化 | `ui/php_panel.py`、`ui/dialogs.py` | `ini_editor.py`、`php_manager.ini_target/init_ini` | `<版本目录>/php.ini` + `.bak` | CLI 与 FastCGI 共用同一份 `php.ini`（`php-web.ini` 已废弃） |
| PHP 扩展启停 / 在线安装 | `ui/extension_dialog.py` | `php_extension.py`、`php_downloader.py` | ini 写回 + `.bak` | PECL / xdebug.org 按「主版本 + NTS/TS + 编译器 + 架构」匹配 |
| 下载安装新版 PHP | `ui/download_dialog.py` | `php_installer.py`、`php_downloader.py` | 新版本目录 + `config.ports` | SHA-256 校验、防穿越解压、端口按 `9000+版本号` 推导 |
| 终端 `php` 版本切换 | 主窗口顶部下拉 / 对话框 | `path_manager.py` | Win 用户 PATH / posix shell rc | 仅影响新开的终端窗口 |
| 版本自检 | `ui/dialogs.py` | `health_monitor.py` | — | 3 项：版本解析 / 关键扩展 / 配置加载 |
| Nginx 启停 / 重载 / 配置检查 | `ui/nginx_panel.py` | `nginx_manager.py`、`nginx_conf.py` | nginx 进程；改 `nginx.conf` 前 `.bak` | 改配置后必须 `nginx -t` 通过才 reload，失败回滚 |
| 站点映射 / 端口同步 / include 检测 | `ui/vhost_panel.py` | `vhost_manager.py` | `vhost/*.conf` + `.bak` | 只改目标 `server` 块；端口与 `config.ports` 强耦合 |
| 一键体检 / 502 诊断 | `ui/vhost_panel.py` 的「一键体检」按钮 / `diag` | `diag.py` | 只读（必要时调用 start / ensure_include，均自带备份 + nginx -t 回滚） | 串联 vhost/php/nginx/hosts 只读检查；修复动作复用既有 manager |
| 新建站点向导 / HTTPS 证书 | `ui/site_wizard.py` | `site_service.py`、`site_templates.py`、`cert_manager.py` | vhost + hosts + 证书文件 | 5 步编排，任一步失败可整体回滚 |
| hosts 写入 / 移除 / 还原 | `ui/vhost_panel.py` 按钮、向导 | `hosts_manager.py` | 系统 hosts + `hosts.phpvm.bak` | 只动 `# >>> phpvm-managed >>>` 块；写入需提权 |
| Redis 多实例管理 | `ui/redis_panel.py` | `redis_manager.py` | redis 进程 / 日志 | 危险命令需二次确认 |
| MySQL 实例管理 | `ui/mysql_panel.py` | `mysql_manager.py` | Windows 服务 / 进程 | 服务模式启停需管理员，非管理员时禁用并提示 |
| SQLite 只读查询 | `ui/sqlite_panel.py` | `sqlite_manager.py` | `settings.sqlite_last_db` | 只读（mode=ro + query_only + 关键字白名单） |
| Nginx 日志查看 | `ui/nginx_log_panel.py` | `nginx_manager.py`（日志目录推导） | — | 增量 tail + 自动跟随 |
| 运行日志 | `ui/run_log_panel.py` | `run_log.py` | `run_log.log(.1)` | 内存 2000 条 / 单文件 1MB 轮转 |
| 整套服务一键启停 | 关于页按钮、托盘菜单 | `service_group.py` | 各服务进程 | 顺序：启动 PHP→Redis→MySQL→Nginx，停止反向 |
| 自动启动的 PHP 范围 | 关于页「自动启动服务的 PHP 版本」 | `service_group.php_targets()` | `settings.autostart_php_scope` | `newest`(默认)/`used`/`active`/`all`；手动「全部启动」与停止始终覆盖全部 |
| 开机自启 phpvm / 服务 | 关于页设置区 | `autostart.py`、`main.py --start-all` | Win Run 项 + 启动目录 vbs / mac LaunchAgent | 服务启动范围同上；日志 `autostart_services.log` |
| 崩溃检测与自愈 | 状态栏告警 + 崩溃详情对话框 | `health_monitor.py`、`crash_watchdog.py`、`recover_history.py` | `recover_history.json`、`crash_watchdog.*` | 自愈默认关闭；防抖 + 每小时限次 + 手动停止宽限期 |
| 开发环境配置推荐 | `ui/tuning_dialog.py` | `tuning.py`、`nginx_conf.py` | php.ini / nginx.conf + `.bak` | 按硬件分档出建议，逐条勾选后写入，`nginx -t` 失败回滚 |
| 外部工具（Composer） | `ui/php_panel.py` | `tool_manager.py` | — | 以所选 PHP 版本运行（临时前置 PATH） |
| 软件更新 | `ui/update_dialog.py` + 状态栏 | `updater.py`、`version.py`、`app_paths.py` | `updates/` 缓存 | 检查 GitHub Releases → SHA-256 校验 → 替换后重启 |
| 模块开关 | 关于页「功能模块」 | `modules.py` | `settings.disabled_modules` | 刚需：PHP / Nginx / 站点映射；停用后**重启**才不加载 |
| 界面语言 / 外观主题 | 菜单栏 + 关于页 | `i18n.py`、`core/theme.py`、`ui/theme.py` | `settings.lang` / `settings.theme` | 语言重启生效；主题立即生效（走各面板 `refresh_theme()`） |
| 托盘 | `ui/tray.py`（Windows） | `icon.py` | `phpvm.ico`（生成） | 最小化即隐藏到托盘；关闭框提供「重启」 |
| CLI（AI / 脚本通道） | `cli.py` | 全部 `core/*` | 同 GUI（共用 config.json） | 不 import tkinter；写操作支持 `--dry-run` |
| 打包与发布 | `packaging/build.py`、`packaging/phpvm.iss` | `.github/workflows/release.yml` | `dist/` | 打 tag 自动构建双平台并发布 Release |

## 3. 模块开关实际影响

`core/modules.py` 是唯一注册表（`key / name / required`）：

| key | 名称 | 刚需 | 停用后的效果 |
|---|---|---|---|
| `php` | PHP 版本管理 | ✅ | 不可停用 |
| `nginx` | Nginx 管理 | ✅ | 不可停用 |
| `vhost` | 站点映射 | ✅ | 不可停用 |
| `redis` | Redis 管理 | ❌ | 不建页签、不实例化 `RedisManager`、不导入该模块代码 |
| `mysql` | MySQL 管理 | ❌ | 同上 |
| `sqlite` | SQLite 数据库 | ❌ | 同上 |
| `log` | Nginx 日志 | ❌ | 同上 |

停用是**启动期行为**（界面只在启动时构建），改完需重启 phpvm；`--start-all` 与 CLI 也遵守该开关
（停用的模块不加载其 manager，因此不会去启停对应服务）。

## 4. 服务与端口约定

- PHP 版本目录 `C:\wnrp\php*` → 端口见 `README.md`「端口映射」表；`php82 = 9000` 是多数 vhost 的默认目标，
  `php85 = 9085` 供 `localhost` / `gmlbm.conf` 使用。
- 端口 ↔ vhost 的 `fastcgi_pass` 必须手工或经「一键同步」保持一致，这是最常见的 502 来源。
- 本机（WNRP）特有：PHP 8.5 的 `opcache.jit` 必须关闭（tracing JIT 在 php-cgi 下会段错误），
  CLI 运行 artisan 等命令建议加 `-d opcache.enable_cli=0`。详见工作区根 `AGENTS.md`。
