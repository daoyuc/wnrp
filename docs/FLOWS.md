# 关键流程

只记录**跨模块的完整链路**（单文件内部逻辑看代码注释）。每条流程都标出失败分支与落点，
便于排查现象与评估影响面。

---

## 1. GUI 启动（`main.py`）

```
python/phpvm.bat 启动
 ├─ _install_excepthook()                     未捕获异常 → run_log.error + traceback
 ├─ 单实例：Win CreateMutexW / posix socket 锁
 │    └─ 已存在实例且 PHPVM_RESTART≠1 → 弹框提示 + 写 warn 日志 → 退出
 ├─ Config() 载入 config.json（损坏则回退默认并覆盖保存）
 ├─ set_language(config.get_lang())          必须在任何界面文本/模块级 t() 之前
 ├─ "--start-all" 在参数里？ ── 是 → _start_all_services() 后直接返回（不建窗口、不占锁，见流程 2）
 ├─ _enable_high_dpi()                       必须在创建 Tk 根窗口之前
 ├─ 按 modules.is_enabled() 决定是否实例化 Redis / MySQL manager（停用则不 import）
 ├─ MainWindow(...)  ← 建窗口 → _build() 建页签 → 8s 轮询 _tick() → 托盘 → 启动静默检查更新
 │     └─ after(1200ms) _maybe_start_services_on_launch()（设置开启时，见流程 2）
 └─ mainloop() 退出后：释放单实例锁 + 写「phpvm 已退出」
```

**现象对照**：点了没反应 → 查 `run_log.log` 里有没有「phpvm 已在运行，本次启动已退出」。

## 2. 自动启动整套服务（两条路径，同一套编排）

| 触发 | 入口 | 时机 |
|---|---|---|
| 登录静默启动（仅 Windows） | 启动目录 vbs → `main.py --start-all` | 当前用户登录时，无界面 |
| 启动 phpvm 时自动启动 | `ui/main_window.py::_maybe_start_services_on_launch()` → `ServiceGroup.start_all()` | 主窗口创建后 1.2 秒（后台线程） |

```
ServiceGroup.start_all(php_scope=…)
 ├─ _refresh()                                  扫版本 + 批量刷新各服务状态（已运行的不重复启动）
 ├─ php_targets(scope, names)   ← 决定启动哪些 PHP
 │    newest(默认) 仅版本号最新（display 由 php -v 解析，解析不出用目录名兜底；见 php_manager.version_key）
 │    used         站点 fastcgi_pass 引用到的版本 + 最新版
 │    active       跟随 cmd/终端中 php 实际生效的版本（找不到回落最新）
 │    all          全部版本（手动「全部启动」按钮与 CLI 默认值）
 ├─ 逐个 start：PHP → Redis → MySQL → Nginx（已运行的跳过；单项失败不影响后续）
 ├─ 被跳过的 PHP 写成「跳过 PHP 版本：…（策略：…）」进入汇总与运行日志（避免误判为启动失败）
 └─ 结果：GUI 走托盘/状态栏队列；--start-all 追加到 autostart_services.log（含 php_scope=）

设置项：settings.autostart_php_scope（默认 newest）；命令行可临时覆盖
        pythonw main.py --start-all --php-scope=used
```

**注意**：`stop_all()` 与手动「全部启动」始终覆盖全部 PHP，不受该设置限制。
**排查**：登录后站点 502 → 先看 `autostart_services.log` 里的 `php_scope` 与「跳过」清单，
说明目标端口对应的版本没被启动（把范围改成 `used` 或手动启动该版本）。

## 3. 新建站点（`ui/site_wizard.py` → `core/site_service.py`）

```
第1步 域名 + 项目目录 + PHP 版本（→ 决定 fastcgi_pass 端口）
第2步 选模板（Laravel/WordPress/ThinkPHP/通用/静态/SPA）+ 实时预览；可选启用 HTTPS
第3步 hosts 映射预览（已映射跳过 / 指向其它 IP 不覆盖）
第4步 确认创建 → 执行编排（create_site，步骤顺序固定）：
  ① [可选] 生成证书到 <nginx>/SSL/（mkcert 优先，openssl 自签兜底）
  ② 写 vhost/<域名>.conf（同名先备份 .bak）
  ③ 主配置未 include vhost 目录 → 补 include（备份 nginx.conf）
  ④ nginx -t 校验
  ⑤ 写 hosts（只动 # >>> phpvm-managed >>> 块；无权限时弹系统授权窗口）
  ⑥ 平滑 reload（未找到 nginx 可执行文件时记为 skip 并提示）
硬项 = cert / file / include / test：任一失败即阻断后续写入（不再写 hosts、不重载），
       由调用方决定「保留还是回滚」——CLI 直接回滚，GUI 弹框让用户选
软项 = hosts / reload：失败只提示，不影响站点配置是否就绪
回滚边界：只还原**被覆盖**的文件（改前已存在的 vhost 与 nginx.conf、本次写入的 hosts 映射、
         本次生成的证书），本次新建的配置文件不删除
```

**落点**：模板 `core/site_templates.py`；编排与回滚 `core/site_service.py`（CLI `site create` 与向导共用同一实现）。

## 4. 改端口 → 一键同步 vhost（`core/vhost_manager.py`）

```
界面「编辑端口」保存 → config.json 更新
 → 扫描全部配置（nginx.conf + vhost/*.conf）找出引用旧端口的文件
 → 逐个文件备份 .bak → 替换目标行 fastcgi_pass 127.0.0.1:<旧> → <新>
 → nginx -t
     失败 → 还原全部备份（rolled_back=true）并向用户报错
     成功 → 询问/执行平滑 reload（nginx 未运行则只提示）
```

CLI 等价：`python3 cli.py site sync-port --old 9082 --new 9083 --reload`。

## 5. php.ini 编辑与初始化（`core/ini_editor.py`）

```
查看：解析 12 项关键配置 + 已启用扩展 + 全文
初始化（仅当版本目录缺 php.ini）：复制 php.ini-production → 缺失则 php.ini-development
        → 都没有则写内置最小骨架 INI_SKELETON（未列出的指令沿用 PHP 编译默认值）
编辑：类型校验（size/int/onoff/enum/timezone/str）→ 备份 .bak → latin-1 无损逐行替换
      → 未命中键追加到文件尾 → 提示「重启该 PHP 版本生效」
```

## 6. 崩溃检测与自愈（`health_monitor` + `crash_watchdog` + `recover_history`）

```
检测（GUI 侧，每约 8 个 8s tick 查一轮）
  health_monitor 读 Windows 应用事件日志 / macOS .ips 崩溃报告
   → 状态栏 + 托盘气泡告警（启动回溯时静默，不弹窗）→ 崩溃详情对话框（含自愈历史页）

自愈（独立守护进程，与 GUI 解耦）
  开启 auto_recover_crash → crash_watchdog.spawn()（pythonw，无窗口；lock 文件 + PID 校验单例）
  守护循环（core/crash_watchdog.py 顶部常量）：
    失联探测每 10s 一轮；每 3 轮（≈30s）查一次崩溃事件；事件回溯 6 小时
  决策顺序（任一命中即跳过本轮自愈，并写入 recover_history.json）：
    仍在运行 → 什么都不做
    手动停止宽限期内（unwatch() 记时间戳，300s）→ 尊重用户意图，不拉回
    距上次自愈不足 60s（防抖）→ skip_interval
    该版本本小时已自愈达 auto_recover_limit 次（默认 3）→ skip_limit
  动作：重启该版本 php-cgi；连续失败达 5 次 → 解除看护并提示人工介入
  关闭开关：守护进程下一轮自行退出（不杀进程）
  界面启动/重启某版本 → watch_version()；手动停止 → unwatch()
```

## 7. 软件更新（`core/updater.py` + `ui/update_dialog.py`）

```
启动静默检查（settings.check_update_on_start，默认开）→ 发现新版本仅状态栏提示
手动「检查更新」→ 更新窗口（更新说明 + 下载）
 → 下载到 updates/*.part → SHA-256 校验（不匹配则删除并拒绝）
 → 替换安装：mac 延迟脚本（hdiutil + ditto）；Win 延迟批处理静默安装 setup.exe
 → 重新拉起 phpvm（源码运行时不支持自动替换，改为提示手动安装）
数据目录外置（包目录只读时用 ~/.phpvm），因此升级覆盖程序文件不会丢 config.json
```

---

## 排查速查表

| 现象 | 先看哪里 |
|---|---|
| 站点 502 | 目标 vhost 的 `fastcgi_pass` 端口 → 该版本是否在运行（流程 2 的跳过清单）→ 端口是否与 `config.json` 一致（流程 4） |
| 点了 phpvm 没反应 | `run_log.log` 的单实例提示；任务栏/托盘是否已有窗口 |
| 登录后服务没起来 | `autostart_services.log`（逐项结果 + `php_scope=`）→ 再查 `run_log.log` |
| 改配置没生效 | php.ini 类需重启该版本；nginx 类需 reload；模块开关与语言需重启 phpvm |
| php-cgi 反复崩溃 | 崩溃详情对话框 + `recover_history.json`；Windows 事件查看器；本机 php85 记得 JIT 必须关闭 |
| CLI 行为与界面不一致 | 先 `python3 cli.py schema --json` 核对参数契约（文档只写典型用法） |
