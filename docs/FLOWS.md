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
 │     ├─ _build() 末尾按 plugins.enabled_specs() 延迟挂载插件页签（见流程 8）
 │     ├─ after(2600ms) plugins.ensure_backgrounds()（幂等拉起插件后台进程，见流程 8）
 │     ├─ _build() 状态栏建整机内存槽位；启动取一次、_tick() 每 8s 再取
 │     │    └─ worker 读 core/system_memory.py → _post() → 主线程写 StringVar
 │     └─ after(1200ms) _maybe_start_services_on_launch()（设置开启时，见流程 2）
 └─ mainloop() 退出后：释放单实例锁 + 写「phpvm 已退出」（插件守护进程不随之退出）
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
 │    custom       只启动 PHP 页勾选了「开机自启」的版本（见 settings.autostart_php_versions；
 │                 一个都没勾 → 回落 newest）
 │    all          全部版本（手动「全部启动」按钮与 CLI 默认值）
 ├─ 逐个 start：PHP → Redis → MySQL → Nginx（已运行的跳过；单项失败不影响后续）
 ├─ 被跳过的 PHP 写成「跳过 PHP 版本：…（策略：…）」进入汇总与运行日志（避免误判为启动失败）
 └─ 结果：GUI 走托盘/状态栏队列；--start-all 追加到 autostart_services.log（含 php_scope=）

设置项：settings.autostart_php_scope（默认 newest）；custom 的名单在 PHP 页「开机自启」列勾选
        （`settings.autostart_php_versions`）；命令行可临时覆盖
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
  启动首轮：按 settings.autostart_php_scope / 勾选名单裁剪持久化看护列表
    （只保留「本次开机集合」与当前正在运行的版本；看护列表跨会话持久化，正常关机
      不会清空它，若不裁剪则上一轮运行过的全部版本会在下次开机被逐条「失联复活」）
  决策顺序（任一命中即跳过本轮自愈，并写入 recover_history.json）：
    仍在运行 → 什么都不做
    手动停止宽限期内（unwatch() 记时间戳，300s）→ 尊重用户意图，不拉回
    距上次自愈不足 60s（防抖）→ skip_interval
    该版本本小时已自愈达 auto_recover_limit 次（默认 3）→ skip_limit
  动作：重启该版本 php-cgi；连续失败达 5 次 → 解除看护并提示人工介入
  关闭开关：守护进程下一轮自行退出（不杀进程）
  界面启动/重启某版本 → watch_version()；手动停止 → unwatch()
  一键启停 / CLI（services stop-all、php stop、总览「全部停止」）
    → service_group.sync_crash_watch() 批量同步：停止解除看护（否则失联探测会把
      停掉的版本逐个拉回，表现为「点完全部停止又自己起来了」），启动反向登记
  宽限判定先于登记：崩溃事件不得把刚被停止的版本重新塞回看护名单
    （否则宽限期一过又被失联探测拉活）
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

## 8. 插件框架与键鼠后台记录（`core/plugins` + `ui/plugins`）

```
启动（GUI / CLI / 开机自启）
 ├─ plugins.discover() 扫描 core/plugins/ 各子包，importlib 读 MANIFEST: PluginSpec
 │    └─ 单个插件导入失败 → run_log.warn + 跳过，不阻断启动
 ├─ core/modules.py 把插件 key 并入 MODULES → 关于页模块开关自动出现该项
 ├─ 主窗口 _build() 末尾：按 enabled_specs(config) 延迟 import ui/plugins/*_panel.py
 │    └─ 面板进 _plugin_panels + _refresh_panels()（定时刷新 / 主题重绘）
 ├─ after(2600ms) ensure_backgrounds(config) → keymouse.ensure_daemon()
 │    └─ daemon.spawn()：pythonw + CREATE_NO_WINDOW；daemon.lock(PID) + daemon.heartbeat 判活，幂等
 └─ 关闭主窗口：不杀守护进程，记录继续

采集（独立进程，Windows / macOS）
 macOS：CFRunLoop 线程跑 CGEventTap（listen-only，需「输入监控」授权）
         tap 被系统挂起 → 回调里立即 CGEventTapEnable 重启用；pump() 仅休眠
 Windows 分支如下：
 消息泵：PeekMessageW 排空 → 空闲时 MsgWaitForMultipleObjects 等消息（≤50ms）
         绝不在钩子线程 time.sleep 轮询：休眠期间到达的按键会排队 ~30ms，
         打字 / 输入法组合输入发粘，久了还可能触发 LowLevelHooksTimeout
 WH_KEYBOARD_LL 钩子回调 → 只 deque.append((ts, vk)) + CallNextHookEx（零 I/O）
   ├─ 过滤 LLKHF_INJECTED 注入事件；只计「抬起→按下」跳变（排除长按自动重复）
   └─ 主循环每 2s / 攒够 600 事件：内存聚合 → 一个事务 upsert
        key_day(day,vk,count) + typing_minute(ts_minute,presses,peak_kpm)（WAL）
 暂停：写 <数据目录>/plugins/keymouse/paused（≤1s 生效，flush 后停钩子）
 停止：写 daemon.stop；停用插件：下一轮配置检查（≤60s）自行 flush 并退出
 钩子看护：每 5s 检查、每 60s 重装一次（句柄丢失立即重装、正在打字时推迟），
           兜住回调超时被系统静默移除的低层钩子；失败只在状态翻转时记一条日志
 保留期：默认 730 天，每 24h 清理一次

展示（GUI / CLI，与采集分离）
 sqlite3 file:...?mode=ro → stats.summary(range, top)（today/week/month/year/all）
   ├─ 面板 auto_refresh() 在 worker 线程查 → queue + drain 回主线程重绘
   └─ CLI keymouse summary / export / status；keymouse clear --yes 清空
 开机自启：HKCU Run 值名 phpvm-keymouse（独立于 phpvm 自身），
           写的是守护脚本的隐藏启动命令，登录即记录，无需打开主窗口
```

**第二个插件「项目启动器」是无后台进程的另一种形态**：`MANIFEST` 里
`daemon_script` / `ensure_background` 均为 None，`ensure_backgrounds()` 直接跳过；
`_build()` 只挂面板，`auto_refresh()` 每 8s 起 worker 扫 www 目录 + vhost root
（按 normcase 去重）→ queue → 主线程渲染。扫描时同时从 vhost `server_name`
生成 `Project.urls`（`valid_site_url()` 限 http/https + 合法域名，`listen 443`
近似判 HTTPS，禁用站点不产出 URL）；点「在浏览器打开」/ 双击 / 右键选域名走
`commands.open_site()` → `webbrowser.open()`，同样经 worker + queue 回主线程，
无站点的项目按钮置灰。点「在终端运行」才经 `process_utils.open_terminal()`
打开一次终端并前置项目 PHP 版本的 PATH。
命令只来自内置白名单或用户设置，URL 只来自 vhost，不从 `.phpvm.json` 执行或取域名
（ADR 16）；CLI 对应 `launcher browse --project <name> --dry-run`。

**扫描根目录可改**：`projects.resolve_www_dir()` 取 `plugin_settings.project_launcher.www_dir`，
空串回落 `<WNRP_ROOT>/www`；面板「选择目录」走 `askdirectory` → `set_www_dir()` 校验目录存在后落盘
（`launcher set-www --dir/--reset`），主窗口照常在 `auto_refresh()` 里重扫，不需要重启。

**打开编辑器**：`core/plugins/project_launcher/editor.py` 先看用户模板
（`plugin_settings.project_launcher.editor`，如 `code {path}`），没有就用 `shutil.which()`
在 `EDITORS` 白名单里挑第一个可用项；模板经 `shlex.split()` 拆成 **argv 列表**再交给
`subprocess.Popen`（`{path}` 字面替换，缺省把它追加到末尾），**不拼 shell 字符串**，
目录名带空格 / 括号也不会被解释；Windows 的 `code.cmd` 走 `cmd /c` 包装，POSIX 用
`start_new_session`。面板按钮 / 右键 / `launcher editor --project <name>` 同一入口，
`--dry-run` 只回 argv 不启动。

**搜索 / 排序 / 最近使用**：同一个搜索框同时过滤项目与命令列表，项目侧匹配
名称、路径、PHP 版本、域名、站点 URL 与标志文件（`projects.match_query()`）；
点列头切排序（名称 / PHP / 域名，再点反向），勾「最近使用优先」按
`plugin_settings.project_launcher.recent`（最多 20 条，打开站点 / 终端 / 编辑器 / 目录后写入）
排在最前，列表里星标显示。目录被移走的项目名字后带「（目录缺失）」，打开目录直接拒绝并提示。

**第三个插件「访问日志聚合」是纯只读分析**：`MANIFEST` 同样没有后台进程，面板
`auto_refresh()` 起 worker 读 `NginxManager().logs_dir` 下的 access 日志（默认
`access.log`，最多回溯 8MB / 20 万行）→ 逐行正则解析 combined 格式（兼容末尾
`$request_time`）→ 聚合 → queue → 主线程渲染。日志文件只能从 `access_logs()`
的候选里按名字选，`realpath` 前缀校验保证读不到日志目录之外；解析失败的行计入
`skipped`，不会抛错打断统计。结果不落库、不上传；停用插件只是不建导航项
（ADR 17）；CLI 对应 `insights report --file <name> --errors`。

**第四个插件「站点巡检」是只读 HTTP(S) 探测**：同样的无后台进程形态。面板 `auto_refresh()`
由主窗口 8s tick 调用，但内部按 **60s 节流**（真实请求不能跟 tick 同频）：

```
探测（worker 线程，ThreadPoolExecutor 默认 6）
 ├─ sites()：VhostManager().scan() → 每个 server_name 展开为域名
 │    ├─ 跳过 *.x 泛解析、`_` 默认 server、非法域名、已禁用站点（.conf.disabled）
 │    └─ 去重 + 按域名排序 → http(s)://<domain>/
 ├─ probe(url, timeout=5)
 │    ├─ http.client 直连（不经系统代理），connect → GET /（UA 固定、Connection: close）
 │    ├─ 不跟随重定向：3xx 原样记 status + Location
 │    ├─ 响应体最多读 64 KB 用于计数，读完立即 close
 │    ├─ HTTPS：先按系统根校验 → 失败（本地自签）降级不校验重试，标 insecure
 │    │        校验通过时读 getpeercert().notAfter → cert_days
 │    └─ 任何异常归一为 bad_url / timeout / refused / dns / tls / network
 ├─ state：ok(<400) / warn(4xx) / down(5xx 或连不上)
 └─ report()：total / up / warn / down / avg_ms（avg 只统计有响应的站点）
     └─ 面板 queue → 主线程渲染四张卡 + 八列表格（双击浏览器打开）
```

**为什么不是 8s 一轮**：面板探测的是真实站点（含 Laravel 冷启动），跟主窗口 tick 同频会把
本机站点持续打满；60s 节流 + 手动「刷新」按钮，既能看到最新状态，也不制造负载。首次探测
遇到站点冷启动（>3s）会如实记为超时，属站点侧问题，不是探测缺陷。

**失败分支**：域名没进 hosts → `dns`；nginx 未启动 → `refused` / `timeout`；HTTPS 自签 →
正常出结果但证书列显示「自签」；vhost 里没有 `server_name` → `health sites` 提示无站点。
**边界**：地址只来自 vhost（不接受任意 URL），只读、不落库、不写文件、无定时任务（ADR 20）；
CLI 对应 `health sites` / `health probe --domain <substr> --scheme https`。

**第五个插件「Git 工作区」是只读子进程查询**：无后台进程，也**不碰仓库内容**，
只把每个项目根的 `git` 状态读出来：

```
查询（worker 线程，ThreadPoolExecutor 默认 4）
 ├─ 仓库来源：project_launcher.projects.scan()（www 一层 + vhost root 去重）
 │    └─ 补 WNRP_ROOT 下一层里含 .git 的目录（只收真仓库，php82 之类不进来）
 ├─ describe(root)：不带 .git → 回退上一级再找一次（站点 root 常指 public 子目录），
 │    两级都没有 → none；目录已删 → missing；没有 git 命令 → no_git
 │    ├─ git --no-optional-locks -C <root> status --porcelain=v2 --branch
 │    │    ├─ # branch.head / # branch.upstream / # branch.ab +A -B → 分支 / 上游 / 领先落后
 │    │    ├─ 1/2 行：X 位非 . → staged；Y 位非 . → modified；u 行 → conflict
 │    │    └─ ? 行 → untracked；路径只留前 200 条、每条截断 300 字符
 │    ├─ git log -1 --pretty=%h%x1f%ct%x1f%s → 最后提交（空仓库静默跳过）
 │    └─ 状态：dirty > ahead > clean；异常归一为 timeout / failed / no_git
 └─ report()：total / repos / clean / dirty / ahead / behind / none / errors
     └─ 面板 queue → 主线程渲染五张卡 + 七列表格（双击 / 右键打开终端）
```

**为什么 60s 节流**：每个仓库要起 2 个 `git` 子进程，几十个项目就是上百次进程创建；
主窗口 tick 是 8s，跟同频会让磁盘和 CPU 一直有负载。面板改为 60s 节流 + 手动「刷新」。

**为什么不用 `git status` 之外的命令**：`fetch` / `pull` 会联网并改 `refs`，`add` / `commit`
更不用说；`--no-optional-locks` + `GIT_OPTIONAL_LOCKS=0` 连 `status` 顺带刷新 index 的行为也关掉，
`GIT_TERMINAL_PROMPT=0` 保证不会卡在凭据输入。因此本插件对仓库是**纯读者**（ADR 22）。

**失败分支**：没装 git → `repos list` 给出提示、面板状态显示「未安装 git」；仓库加了
`core.fsmonitor` 之类的钩子导致 `status` 很慢 → 单仓库 6s 超时记为 `timeout`，其余仓库照常
出结果；分支没有上游 → 显示「（无上游）」，不会去猜 `origin/HEAD`。
CLI 对应 `repos list --only <substr>` / `repos show <name> --files 20`。

**失败分支**：安全软件拦截全局键盘钩子 → 守护进程写 `daemon.log` 并退出，面板显示「后台未运行」+ 诊断提示；
插件面板导入失败 → warn + 跳过该导航项；非 Windows → `supported()` 为 False，页面显示「当前平台不支持采集」而非报错。
**隐私**：只落 vkCode 与时间戳，不记录字符 / 输入文本 / 窗口标题 / 剪贴板，数据仅在本机且不上传。
**排查现场**：`<数据目录>/plugins/keymouse/daemon.log` 是守护进程自己的日志（不写共享 `run_log.log`，避免多进程追加截断）。

---

## 9. SSH 管理：测试连接 / 信任指纹 / 远端执行（`core/ssh_manager.py`）

**入口**：面板 `ui/ssh_panel.py`（点「测试连接」「打开终端」「在远端执行」）与 `ssh` 命令组
（`ssh host test|connect`、`ssh snippet run`）。两者都只做参数解析与结果渲染，握手逻辑只有 `core/ssh_manager.py` 一份。

```
读主机（core/ssh_store.load_hosts，凭据在 ssh_hosts.json）
   → paramiko.SSHClient + 拒绝策略（等价 RejectPolicy，另回传指纹）+ <数据目录>/ssh_known_hosts
   → connect(hostname, port, username, password | pkey, timeout=10)
   → 测试连接：回显 latency / server banner / fingerprint
   → 远端执行：exec_command(cmd, timeout) → 边读边限流（stdout / stderr 各 256 KB）
   → 交互式终端：build_ssh_argv() → core/process_utils.open_terminal()
```

**信任指纹（首次连接）**：`RejectPolicy` 抛出的 `UnknownServer` 由 `ssh_manager` 转成 `HostKeyNotTrusted`，
带上主机、算法、`SHA256:` 指纹。GUI 弹「主机 xxx 的指纹 … 确认无误并信任吗？」，确认后以 `trust=True`
重连并由 `client.save_host_keys()` 写入 `ssh_known_hosts`；CLI 必须显式加 `--trust-host` 才写。
从不使用 `AutoAddPolicy`——静默信任等于给中间人开门。**指纹变了**（`BadHostKeyException`）一律按失败处理，
不做「自动更新已知主机」，需用户手工删除 `ssh_known_hosts` 中的对应行后重新确认。

**密码与私钥**：密码只来自三处——`ssh host add|update --password-stdin`（stdin，绝不进 argv）、
面板密码框（只落 `ssh_hosts.json`，权限 0600）、交互式终端里用户自己输入。私钥连接把 `key_path`
交给 `connect(key_filename=...)`（可选 `passphrase`），并固定 `look_for_keys=False` /
`allow_agent=False`——只按配置认证，不扫 `~/.ssh` 的默认密钥、不碰 ssh-agent，行为可预期。
任何路径上都不把密码拼进命令行。

**保存与连通性解耦**：主机「确定」即落盘，不做连通性校验，密码 / 私钥允许先空着（`validate_host`
只拦字段明显错误：名称、地址、端口、用户名，以及「填了私钥路径却指不到文件」）。缺凭据的主机由
`ssh_store.missing_secret()` 标出，面板详情显示「凭据未配置」、保存时给一次 warn 提示、CLI 在
`notes` 里提示，`data.host.credential_ready` 为 `false`；真正连接时 `_connect()` 先检查凭据，
抛 `MissingCredential` → `error_kind=credential` 并给出「去编辑主机补上」的文案，而不是让用户
面对 paramiko 的 `No authentication methods`。这样「先在清单里登记十台机器、稍后逐个补密码」
也能成立。

**默认私钥兜底**：私钥认证留空时，表单（面板对话框 / `ssh host add|update --auth key`）会调用
`ssh_store.default_key_path()`，按 OpenSSH 惯例顺序在 `~/.ssh` 里找 `id_ed25519` → `id_ecdsa` →
`id_rsa` → `id_dsa`，取第一个真实存在的文件填进去，并说明「已自动选用默认私钥」（CLI 在 `notes`，
面板在私钥行右侧）。填进去的是**具体路径**：界面上看得见、能改、能清空，连接时仍显式传
`key_filename`——不启用 ssh-agent、不隐式尝试未列出的密钥，认证边界没有放宽（ADR 24）。找不到
候选就保持空白，按「凭据未配置」处理。

**远端执行**：`exec_command` 的 `timeout` 默认 30 秒（CLI `--timeout`），输出边读边限流并置
`truncated=True`，退出码原样作为 `exit_status` 返回；`exit_status != 0` 视为业务失败（退出码 1，
`data.error` 说明退出码），网络 / 认证 / 指纹 / 缺依赖 / 缺凭据类错误分别归到 `error_kind`
（`auth` / `host_key` / `timeout` / `network` / `missing_dependency` / `credential` / `remote` / `error`），供界面翻译。
**片段永不自动执行**：必须用户点击或 `ssh snippet run --host X --yes`；`--dry-run` 只回显将要执行的命令。

**预置片段**：首次用到片段（面板 `SshPanel.reload()`、CLI `ssh snippet list` / `run`）时调用
`ssh_store.ensure_builtin_snippets()` 浇一次种子，把 `builtin_snippets()` 里的 20 条常用命令写进
`settings.ssh_snippets`：系统负载 / 系统信息 / 时间时区 / CPU 与内存 Top10 / 内存与 swap / 磁盘空间 /
目录占用 / 大文件 / 监听端口 / 在线用户 / 最近登录 / Nginx 配置与状态与错误日志 / PHP-FPM / Redis /
MySQL / Docker / 系统错误日志。全部是只读探测：不提权、不带 `sudo`、不写远端、输出都截断到几十行，
名称与标签按播种时的界面语言落盘（之后同普通片段一样可改可删）。是否播过由
`settings.ssh_snippets_builtin` 记版本号决定 —— 用户删掉的内置片段不会因为重启又冒出来，升版本时
只补当时还不存在的那些（同名片段以用户的为准）。

**失败分支**：没装 paramiko → `available()` 为 False，面板顶部显示 `install_hint()`、CLI 返回
`data.error`（含安装命令），其余功能不受影响；主机名 / 用户名含非法字符或端口越界 → 在
`ssh_store.validate_host()` 阶段就被拒（不发起连接）；连接超时 → `error_kind=timeout`，不自动重试。
本模块不写 `run_log`（失败回显在面板输出区与 CLI 的 `data.error` 里），因此**凭据与口令不会进任何日志**。

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
| SSH 提示「缺少依赖 paramiko」 | 开发态 `pip install -r requirements.txt`；安装包看 `<包目录>/vendor/paramiko` 是否存在（vendor 装失败构建日志里有 warn），或就地 `pip install -r requirements.txt`（流程 9） |
| SSH 首次连接被拒 / 指纹变了 | 面板弹窗确认指纹（或 `--trust-host`）后写入 `ssh_known_hosts`；指纹变化需先手工删掉该行再确认 |
| SSH 连上但远端执行没输出 | 看 `exit_status` 与 `stderr`（`exit_status=-1` 表示超时被截断）；`truncated=true` 说明超过 256 KB 上限 |
| 键鼠统计一直显示「后台未运行」 | `<数据目录>/plugins/keymouse/daemon.log`（钩子是否被安全软件拦截）→ `daemon.lock` 的 PID 与 `daemon.heartbeat` 是否新鲜 → 设置页是否启用了「键鼠统计」模块（流程 8） |
