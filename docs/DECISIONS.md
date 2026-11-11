# 关键取舍记录（ADR）

记录**已经生效、且后续开发必须遵守**的设计取舍：为什么这么选、代价是什么。
适用场景：想推翻某条约定时先读这里；新增同类能力时照抄这里的取舍模式。

> 每条都对应代码中的既有事实。若代码与此处不符，先确认是代码退化还是决策已变更，
> 再当轮修正其中一方（维护规则见 [`README.md`](README.md)）。

---

## 01. PHP 版本以「FastCGI 端口」为唯一身份

- **背景**：环境里同时存在 php / php56 / php72 … php85 共 10+ 个 `php-cgi.exe`，早期脚本用
  `taskkill /IM php-cgi.exe` 一刀切，启停一个版本会误杀其它版本。
- **决策**：所有启停/状态判定都按端口定位进程（`端口监听 + PID 存活 + 命令行含本版本 cgi` 三重校验），
  绝不按进程名批量杀（`core/php_manager.py`）。
- **影响**：端口与版本强绑定 → 改端口必须同步 vhost 的 `fastcgi_pass`（提供「一键同步」）；
  同一版本不能同时跑两个端口实例。

## 02. `core/` 不依赖界面；CLI 与 GUI 共用同一实现

- **背景**：同一件事（建站、改 ini、写 hosts）早期在 CLI 与 GUI 各写一遍，失败策略与回滚范围开始漂移。
- **决策**：`core/*` 禁止 import tkinter / `ui/*`；界面只做参数收集与结果展示，
  跨步骤流程下沉为 core 模块（如 `core/site_service.py`）。
- **影响**：能无 GUI 运行（SSH / CI / 守护进程）；新增流程应先在 core 落地，再让两处调用。

## 03. 一个 PHP 版本只用一份 `php.ini`（CLI 与 FastCGI 共用）

- **背景**：曾为 php82 / php85 使用 `php-web.ini` 分离 CLI 与 Web 配置，导致「改了 php.ini 不生效」的困惑。
- **决策**：`_resolve_ini` 统一回版本目录内的 `php.ini`；新装版本也只生成一份（FastCGI 关键项追加到文件尾）。
- **影响**：历史 `php-web.ini` 仍留在部分版本目录里但**不再被读取**；改 Web 行为要改 `php.ini` 并重启该版本。

## 04. 自动启动服务默认只拉起「版本号最新」的 PHP

- **背景**：开机自启 / 启动 phpvm 时自动启动服务原先无条件遍历全部版本（本机 11 个），
  登录瞬间拉起一批用不到的 php-cgi，吃内存且拖慢启动。
- **决策**：引入 PHP 启动范围 `settings.autostart_php_scope`（默认 `newest`），
  另提供 `used`（站点实际引用 + 最新）/ `active`（跟随 cmd 生效版本）/ `all`；
  手动「全部启动」与 `stop_all()` 仍覆盖全部（用户显式意图优先）。
- **影响**：默认策略下**只有最新版在跑**，若某些 vhost 指向其它端口会 502 ——
  这类环境把范围设为 `used` 或手动启动对应版本；「跳过」清单会写进运行日志与汇总结果，
  避免被误判成启动失败。

## 05. 对外部文件的写入必须「备份 → 改 → 校验 → 失败回滚」

- **背景**：nginx 配置 / ini / hosts 改坏会导致站点全挂甚至系统解析异常。
- **决策**：写前统一经 `core/file_backup.py` 生成 `.bak`（hosts 为 `hosts.phpvm.bak`）；
  改完跑校验（`nginx -t`、`php -c` 自检、JSON 解析）；失败按记录自动还原，宁可放弃也不能半成品。
- **影响**：新增任何写文件能力都必须走这条链；CLI 侧额外提供 `--dry-run` 预览。

## 06. 运行期数据与程序文件分离（只读安装目录 → `~/.phpvm`）

- **背景**：安装到 `Program Files` / `.app` 时包目录只读，且升级会覆盖程序文件。
- **决策**：`core/app_paths.py` 判定可写性——包目录可写就用包目录（与历史行为一致），
  否则改用 `~/.phpvm`；`PHPVM_CONFIG` / `PHPVM_HOME` 可强制指定。
- **影响**：升级替换程序不会丢端口映射与设置；文档与测试要把「数据目录」当作变量而非固定路径。

## 07. 模块开关是「启动期」行为，不动态卸载

- **背景**：可选模块（Redis / MySQL / SQLite / Nginx 日志）的界面与 manager 都有可观启动开销。
- **决策**：开关持久化在 `settings.disabled_modules`，只在启动时决定「是否建页签、是否实例化 manager、
  是否 import 该模块代码」；改完需重启生效（`core/modules.py`）。
- **影响**：不能用开关做运行期热插拔；刚需模块（PHP / Nginx / 站点映射）不可停用。

## 08. 崩溃自愈用「独立守护进程」，不用 GUI 线程

- **背景**：自愈要覆盖 php-cgi 崩溃、进程被外部清理、GUI 未打开等多种情况。
- **决策**：`core/crash_watchdog.py` 作为独立进程常驻（锁文件单例），
  带防抖（60s）、限次（每小时 `auto_recover_limit` 次）、连续失败升级（5 次解除看护）、
  手动停止宽限（300s）；GUI 只负责告警与「确保守护进程存活」。
- **影响**：自愈生命周期与界面解耦；关开关靠守护进程自行退出而非杀进程；
  决策历史写 `recover_history.json`，可事后复盘。

## 09. CLI 是「一等公民」，不是附属脚本

- **背景**：AI/脚本需要无人值守地检查与改动环境。
- **决策**：`cli.py` 只依赖标准库 + `core/*`，永不弹窗、永不等待输入；
  `--json` 输出单一文档、字段英文、退出码分级；`schema --json` 自描述契约；
  写操作支持 `--dry-run`，删除 `--yes`，Redis 高危命令 `--force`。
- **影响**：命令与参数变更即契约变更 → 必须同步 `docs/CLI.md`（由 `_docs_cli.py` 生成，
  `tests/test_docs.py` 会校验一致性）。

## 10. 文案 i18n：源码中文原文即 msgid

- **背景**：界面需要 5 种语言，但开发与排障主要用中文。
- **决策**：源码直接写 `t("中文文案")`，zh_CN 无词条表（回落原文），
  其余语言在 `i18n/<lang>/*.json` 提供映射；新增文案跑 `python _i18n_scan.py --report` 检查覆盖度。
- **影响**：改中文文案等于改 msgid → 必须同步删除旧词条、补 4 语言新词条，否则出现漏译。

## 11. 只操作「自己托管的东西」

- **背景**：hosts、nginx 配置是用户既有资产，误删代价高。
- **决策**：hosts 只增删 `# >>> phpvm-managed >>>` 标记块；禁用站点用改名 `.conf.disabled` 而非删除；
  回滚只还原**被覆盖**的文件，本次新建的文件不删。
- **影响**：用户手工编辑的内容不会被覆盖；「站点被禁用」在文件系统里可见、可手动恢复。

## 12. 文档体系按读者意图分层，可生成的部分不手写

- **背景**：README 越来越长，功能说明与架构说明混在一起，CLI 参数靠手抄必然漂移。
- **决策**：`README`（怎么用）/ `docs/ARCHITECTURE`（怎么做）/ `docs/MODULES`（在哪）/ `docs/FLOWS`（链路）/
  `ROADMAP`（下一步）/ `AGENTS`（AI 契约）分工；`docs/CLI.md` 由 `_docs_cli.py` 从 argparse 生成；
  正确性用 `tests/test_docs.py` 守（链接可达、代码路径存在、CLI 参考同步、表格列数一致）。
- **影响**：改代码要连带改对应文档（规则见 `docs/README.md`）；手改 `docs/CLI.md` 会被测试打回。

## 13. 崩溃看护列表跨重启按「开机集合」裁剪

- **背景**：看护列表（`crash_watchdog.json` 的 `watch`）会持久化，而 `unwatch()` 只在用户**手动停止**
  版本时调用 —— 正常关机不会清空它。于是上一轮运行过的全部版本会在下次开机被逐条判定「进程失联」
  并拉起，绕过 04 号决策的 `settings.autostart_php_scope`：实测只勾了「开机自动启动 phpvm」，
  登录后 11 个 PHP 版本全被拉活。
- **决策**：守护进程启动首轮执行 `_prune_watch_for_boot()` —— 只丢弃「当前未运行 **且** 不属于本次
  开机集合」的看护项；开机集合由 `service_group.php_targets()` 按同一套策略算出（故 04 号决策的
  下拉与 PHP 页的勾选名单天然一致）。正在运行的版本一律保留，策略解析失败时也保守不动。
- **影响**：看护语义变为「会话内保持」——同一会话里手动启动的版本照旧受失联保护，跨重启则只按开机
  集合复活；勾选名单（`settings.autostart_php_versions`）因此成为真正生效的开关。

## 14. 插件用「声明式内置目录」，采集与展示靠独立守护进程 + 只读库

- **背景**：后续会持续增加插件，若每加一个都要改设置页枚举、启动快照、CLI 分组与刷新登记，
  接线必然遗漏（历史上已有「命令成功但界面没变」的教训）；键鼠统计还要求主窗口关闭后仍继续记录。
- **决策**：插件只在 `core/plugins/<name>/` 导出 `MANIFEST: PluginSpec`（key / 名称 / 面板路径 /
  后台脚本 / 默认设置 / 支持平台），框架用 `pkgutil` + `importlib` 发现并投影进 `core/modules.py` 的
  `MODULES`；面板实现放 `ui/plugins/`，由主窗口延迟导入（`core` 不反向依赖界面）。采集走独立
  `pythonw` 守护进程（`daemon.lock` 的 PID + `daemon.heartbeat` 新鲜度判单例，钩子回调零 I/O、
  批量 upsert 两层聚合表），GUI 与 CLI 只以 `mode=ro` 读 SQLite；插件停用后守护进程自行退出。
- **影响**：新增插件 = 加一个目录 + 一个面板 + 一组 CLI，设置页 / 启动快照 / 刷新登记无需改动；
  单个插件导入失败只 warn、不阻断启动；数据只落 vkCode 与时间戳、不上传；非 Windows 平台显示
  「当前平台不支持采集」而非报错；`settings.plugin_settings` 是插件设置的唯一落点。

## 15. 低层键盘钩子的消息泵必须「等消息」，不能 sleep 轮询

- **背景**：用户反馈开启键鼠统计后打字 / 输入法反应变慢。定量复现（向钩子线程投递线程消息做等价测量，不向系统注入按键）：`PeekMessageW + time.sleep(50ms)` 平均 **31.6ms**、最大 32.8ms 才处理一条消息；改成消息等待后平均 **0.7ms**。根因是 `WH_KEYBOARD_LL` 回调由系统向**安装钩子的线程**投递消息触发，只有该线程检索消息时才执行，休眠期间到达的按键会整批排队；持续延迟还可能逼近 `LowLevelHooksTimeout` 被系统静默摘钩。
- **决策**：`core/plugins/keymouse/collector.py::pump()` 先 `PeekMessageW(PM_REMOVE)` 排空；空闲时用 `MsgWaitForMultipleObjects(0, NULL, FALSE, 最长 50ms, QS_ALLINPUT)` 等待消息，返回后**立即再排空一次**；`idle_sleep` 从「每轮休眠」改为「最长维护周期」。钩子回调仍保持零 I/O。
- **影响**：按键响应延迟从约 31ms 降到约 1ms（真实 `KeyboardCollector.pump()` 实测 1.5ms）；空闲时线程照样阻塞，不忙等烧 CPU（20 次空闲泵约 1.25s）。任何新增的低层钩子 / 高优先级消息循环都不得在该线程用 `time.sleep` 轮询；`tests/test_keymouse_collector.py::PumpTest` 守住这条契约。

## 16. 项目启动器的命令只能来自白名单或用户设置，绝不从项目配置执行

- **背景**：第二个插件要把「本地项目 → 常用命令」做成一键终端。`.phpvm.json` 的模块注释明确写着「团队可直接放进项目仓库」（`core/project_config.py`），而 `process_utils.open_terminal(command=...)` 是**立即执行**语义。若把项目配置里的字段喂给它，克隆一个仓库就等于执行任意代码；即便只做「自动推荐」，命令来源也会变成不可信输入。
- **决策**：命令只有两类来源 —— `core/plugins/project_launcher/commands.py::BUILTIN` 固定模板（按 `composer.json` / `artisan` / `package.json` 等标志文件筛选）与用户在面板 / CLI 手动添加、只落 `settings.plugin_settings.project_launcher.custom_commands` 的自定义命令。`.phpvm.json` 只读 `php` / `domains` 作展示元信息，且**只读项目根自己的那一份**，不向上继承父目录配置。命令过元字符与长度校验；自定义命令运行前二次确认；插件无后台进程、无定时执行，只有用户点击 / 显式 CLI 调用才打开一次浏览器或终端；写操作先 `--dry-run`、删除需 `--yes`。
- **URL 也是不可信输入**：「在浏览器打开」的地址只由 nginx vhost 的 `server_name` 生成（`projects.valid_site_url()` 限定 http/https + 合法域名、无路径 / 端口 / 凭据），`.phpvm.json` 的 `domains` 只作展示；`launcher browse --url` 也必须命中该项目的候选列表，否则带 `available` 失败，避免把任意字符串交给 `webbrowser.open`。已禁用站点（`.conf.disabled`）不产出 URL。

## 17. 访问日志聚合只读 nginx 自己的日志目录，不接受任意路径

- **背景**：第三个插件要把「访问日志 → 状态码 / 热门路径 / 错误请求」做成概览。日志**内容**不可信（路径、UA 都能被请求方构造），而「读哪个文件」如果做成任意路径参数，就等于给 GUI / CLI 开了一个读任意文件的入口；把统计结果落库还会额外引入运行期数据与隐私面。
- **决策**：`core/plugins/access_insights/aggregate.py` 的 `access_logs()` 只枚举 `NginxManager().logs_dir` 下匹配 `*access*.log`（含 `.log.N` 轮转）的文件，并对每个候选做 `realpath` 前缀校验（符号链接逃逸同样被拒）；`report()` 只接受**候选列表里的文件名**，未知名字返回 `not_found` + `available`，不存在「传路径」的入口。逐行解析失败（含时间不可解析）计入 `skipped`，不抛异常；单次最多回溯 8MB / 20 万行，避免大日志打满内存。结果只存在于内存与响应体里，**不落库、不写文件、不上传**；插件无后台进程、无定时任务，刷新由主窗口 tick 或用户点击驱动。
- **影响**：`insights files` / `insights report --file` 与面板的文件下拉共享同一份候选来源；后续若要支持自定义日志路径，必须先在 `NginxManager` 侧声明受管日志目录，而不是把路径直接交给插件。`tests/test_access_insights.py` 守住「只列 access 日志」「未知文件列出候选」「畸形行计入 skipped」。
- **影响**：后续扩展插件不得引入「从项目仓库读取可执行内容」的路径；CLI 的 `--command` 参数必须使用独立 `dest`（`command_text`），否则覆盖 `Result.command` 的 `<group>.<action>` 契约；顺带修好 `open_terminal` Linux 分支忽略 `command` 的既有缺陷，并用 `shlex.quote` 引用路径。

## 18. macOS 键鼠采集用 CGEventTap(listen-only) + 输入监控授权

- **背景**：键鼠统计原本只有 Windows 实现（`WH_KEYBOARD_LL`），macOS 上插件直接显示「当前平台不支持采集」。移植时踩到的真实门槛是**授权类型**：listen-only 的 `CGEventTapCreate` 不要求「辅助功能」，而要求「输入监控」。实测本机 `AXIsProcessTrusted() = True` 但 `CGPreflightListenEventAccess() = False` 时，tap **能建出来**、`CGEventTapIsEnabled() = True`，却 **0 次回调**，且系统不报任何错——只按「辅助功能」判断会得出「已授权但收不到事件」的死胡同。
- **决策**：`core/plugins/keymouse/collector_mac.py` 用 `ctypes` 直调 CoreGraphics（零第三方依赖）：`CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap, kCGEventTapOptionListenOnly, …)` + `CFMachPortCreateRunLoopSource` + `CFRunLoopAddSource/CFRunLoopRun`。tap 与 `CFRunLoop` **必须同线程**（`keymouse-tap` 守护线程内建 tap 并跑 runloop，`pump()` 只休眠、不轮询），回调对象必须被实例持有防 GC，回调内**绝不抛异常**；`install()` 用 `_ready` 事件等 runloop 起来，再以 `CGEventTapIsEnabled()` 复核，未启用即判定被系统挂起并回滚。授权判定统一走 `CGPreflightListenEventAccess()`（旧系统回落 `AXIsProcessTrusted()`），申请走 `CGRequestListenEventAccess()`，设置页跳 `Privacy_ListenEvent`。事件侧沿用 Windows 口径：macOS 虚拟键码经 `MAC_KEYCODE_TO_VK` 映射成 VK 后入库（键名、聚合、导出完全复用）；`_down` 集合 + `CG_FIELD_AUTOREPEAT` 过滤长按自动重复；注入过滤按**两个字段**判（`CG_FIELD_SOURCE_STATE_ID < 0` 或 `kCGEventSourceUnixProcessID != 0`，可用 `filter_injected` 关闭）——只判前者会漏掉最常见的 `CGEventPost` 默认 source 路径：那种事件 state ID 为 0、只有 PID 字段非 0，实测会把合成按键当真实按键计数；修饰键按 `CG_EVENT_FLAGS_CHANGED` 做按下/抬起交替；暂停期仍维护键状态以免恢复时重复计数。
- **影响**：`MANIFEST.platforms` 扩为 `("win32", "darwin")`，`supported()` 与守护进程、CLI、面板的文案统一为「仅 Windows / macOS」；未授权时守护进程拒绝启动（退出码 6）并在状态里暴露 `permission` 三字段（`required` / `granted` / `settings_url`），面板状态点转 `WARN` 并给出「去授权」按钮。`tests/test_keymouse_collector.py` 的钩子相关用例加 `skipUnless(IS_WIN)`，纯逻辑（键码映射、自动重复、注入过滤、暂停、`take`）由 `tests/test_keymouse_collector_mac.py` 在任意平台覆盖。后续任何新增的低层事件源同样要回答「这次拿的是哪种系统权限」，不要凭「辅助功能」推断 tap 是否可用。

## 19. 项目启动器：编辑器命令按 argv 启动，扫描目录与最近使用落插件设置

- **背景**：项目启动器原来只有「浏览器 / 终端 / 目录」三个出口，扫描根目录被写死在 `<WNRP_ROOT>/www`（`www_dir` 有默认键但没有任何写入口），列表不能搜索路径、不能排序，也记不住刚用过哪个项目。补「在编辑器打开」时面临与自定义命令同一类问题：模板来自用户输入，若直接拼成 shell 字符串，目录名里的空格、括号、`$` 都会被解释。
- **决策**：新增 `core/plugins/project_launcher/editor.py`。模板来源只有两处——`EDITORS` 内置白名单（`shutil.which()` 探测，不解释参数）与 `plugin_settings.project_launcher.editor`；解析时 `shlex.split()` 成 **argv 列表**，`{path}` 做字面替换、缺省则把项目目录追加到末尾，再交给 `subprocess.Popen`（Windows 的 `.cmd` 垫片走 `cmd /c`，POSIX 用 `start_new_session`），**全程不经过 shell**。打开前先 `root_exists()` 拒绝已移动/删除的目录。扫描根目录改为 `resolve_www_dir()`（设置值优先、空串回落默认），由面板「选择目录」和 `launcher set-www` 写 `plugin_settings.project_launcher.www_dir`；排序、搜索（名称 / 路径 / PHP / 域名 / URL / 标志文件）与最近使用（最多 20 条，`recent` + `recent_first`）都放在 `projects.py` 里做纯函数，面板只负责渲染。
- **影响**：新增 CLI `launcher set-www` / `set-editor` / `editor`，`launcher list` 支持 `--sort` / `--limit` 并在 `--json` 里回 `www_dir` / `recent`；面板增加搜索框、列头排序、最近使用勾选、编辑器下拉与右键菜单，并修掉「非预期队列消息被当成扫描失败」「每次渲染重置命令选中」「`_busy` 泄漏导致自动刷新静默跳过」三个旧缺陷。`tests/test_project_launcher.py` 覆盖 `www_dir` 校验、搜索/排序/最近使用与编辑器 argv 构造（含不启动进程的 `dry_run` 与注入 `spawn`）。后续任何「把用户输入的模板变成进程」的新入口都按本 ADR 走 argv，不要退回字符串拼接。

## 20. 站点巡检只探测 vhost 域名，GET / 且不跟随重定向、不经系统代理

- **背景**：第四个插件要回答「站点到底通不通」。与日志聚合不同，这次要**主动发请求**：如果允许传任意 URL，面板 / CLI 就成了一个「对内网或公网发任意 GET」的入口；如果跟随重定向，探测会跑到 vhost 之外；如果走系统代理，本机 `.test` 站点的请求可能被打到外部代理上，既不准也泄露。HTTPS 本地站点普遍是自签证书，直接按系统根校验会全部失败，无法区分「证书不受信」与「站点挂了」。
- **决策**：`core/plugins/site_health/probe.py` 的站点来源只有 `VhostManager().scan()` 的 `server_name`：跳过 `*.x` 泛解析、`_` 默认 server、非法域名与已禁用站点，同一行多域名展开、去重后按域名排序；`probe()` 只接受由这些域名拼出的 URL，用 `http.client` **直连**（不走 `core/net_ssl.py` 的环境代理路径），只发 `GET /`，**不跟随重定向**（3xx 原样记录 `Location`），不带 Cookie / 认证头，响应体最多读 64 KB 用于计数后立即关闭；错误归一为 `bad_url` / `timeout` / `refused` / `dns` / `tls` / `network` 六个稳定码，文案在界面层翻译。HTTPS 先按系统根校验证书，`SSLCertVerificationError` 时降级为 `CERT_NONE` 重试并标 `insecure=True`（此时不读证书到期时间）；校验通过才从 `getpeercert()` 取 `cert_days`。批量探测用 `ThreadPoolExecutor`（默认 6 线程、单站点 5s 超时），结果只存在于内存与响应体里，**不落库、不写文件、无后台进程、无定时任务**。
- **影响**：面板 `auto_refresh()` 由主窗口 8s tick 驱动，但内部按 **60s 节流**，避免把本机站点持续打满；`health sites` 不发起任何请求，`health probe` 才探测。后续若要做「自定义 URL 巡检」，必须先在 vhost 侧声明受管域名或增加白名单，不能直接把 URL 交给 `probe()`；任何新增的主动探测同样要回答「地址从哪来、会不会跟随跳转、会不会走代理」三问。`tests/test_site_health.py` 守住目标展开过滤、200 / 404 / 拒绝 / 超时 / 非法 URL 与汇总口径。

## 21. 主界面导航改为左侧竖向分组，页面记忆与状态点都挂在稳定 key 上

- **背景**：主界面原来用横向 `ttk.Notebook` 排页签。模块与插件全开时页面有 14 个（总览 / PHP / Nginx / Redis / MySQL / 站点映射 / SQLite / 日志 / 邮件 / 运行日志 / 项目启动器 / 键鼠统计 / 访问日志聚合 / 关于），在 1080px 的默认窗口下页签条会溢出到窗口外，而 Tk 的页签**没有任何横向滚动或溢出菜单**——右侧那几页等于不可达。同时「每次启动都从第一页开始」与「服务是否在跑要去各自页面看」是两处反复被提到的日常摩擦。
- **决策**：新增 `ui/nav.py::SideNav` 取代 Notebook：左侧 184px 窄栏按 `core/modules.py::GROUPS`（服务 / 站点 / 诊断 / 其他）分组，分组是 `Treeview` 的父节点、页面是子节点；点分组行不切页（还原选中到当前页）。页面切换只做 `pack` / `pack_forget`，不销毁面板——这样 `_panel_visible()` 依赖的 `winfo_ismapped()` 语义与页签完全一致，不可见页的自动刷新照旧暂停，`_refresh_panels()` / `_tick()` 的 MySQL 降频查询都不用改。分组归属单一来源：内置模块写 `_BUILTIN.group`，插件写 `PluginSpec.nav_group`（默认 `other`），界面不再硬编码分组。当前页记 `settings.last_page`（稳定 key，不是控件引用或中文标题），重启 `restore()`，key 已失效（模块/插件被停用）时回落第一页；写盘用 `after(800)` 合并，避免连续切页反复重写 `config.json`。四个服务的圆点复用 `_tick()` 的 4 轮降频（≈32s）在 worker 线程探测、`_post()` 回主线程；状态未变不重绘。
- **影响**：`SideNav` 刻意保留 Notebook 的顺序 API 子集（`add` / `insert` / `tabs` / `index` / `select`），主窗口与冒烟测试平滑迁移；新增页面必须给它一个稳定 key（记忆页面依赖它）。`ttk.Notebook` 的样式保留在 `theme.py`（对话框仍在用），不要因为主窗口不再用页签就删掉。默认窗口加宽到 1180×720（`min_width` 1040），保证导航吃掉 184px 后内容区仍按原留白排布。后续若要加「分组折叠」「快捷键切页」，在同一控件内扩展，不要退回横向页签或另造第二套导航。
## 22. Git 工作区只跑只读 git 子命令，仓库来源不递归磁盘

- **背景**：第五个插件要回答「哪些项目还有没提交 / 没推送的东西」。这类需求很容易顺手写成「自动 fetch 看看落后多少」或「一键 commit」，但两者都会改变用户仓库状态：`fetch` 会联网并改写 `refs/remotes`，`status` 在极端情况下也会因为刷新 index 而写 `.git/index`；再加上「扫描整个磁盘找仓库」会让冷启动不可控。
- **决策**：`core/plugins/git_status/repos.py` 只调用两条只读命令 —— `git status --porcelain=v2 --branch` 与 `git log -1 --pretty=%h%x1f%ct%x1f%s`。所有命令以 argv 列表交给 `subprocess.run`（不经 shell），固定加 `--no-optional-locks` 并设 `GIT_OPTIONAL_LOCKS=0`（连 `status` 的 index 刷新也关掉）、`GIT_TERMINAL_PROMPT=0`（绝不等待凭据输入），同时剔除继承来的 `GIT_DIR` / `GIT_WORK_TREE`，避免把查询打到别的仓库上。仓库来源复用 `project_launcher.projects.scan()` 的发现口径（`www` 一层 + vhost root 去重），再补 `WNRP_ROOT` 下一层里**确实含 `.git`** 的目录（因此 phpvm 自身可见，`php82` 之类环境目录不会进来）；只看项目根自己的 `.git`（目录或 worktree 的 gitfile），目录不是仓库根时**回退上一级**再找一次 —— 站点 root 常指向 Laravel 的 `public` 等子目录，真正的 `.git` 在项目根；但**只回退一级、不无限向上**，否则整个 `www` 会被当成一个巨大仓库。领先 / 落后只读 `# branch.ab` 的本地记录，**不 fetch**：没有上游就显示「（无上游）」，不猜 `origin/HEAD`。
- **影响**：面板 `auto_refresh()` 由 8s tick 驱动但按 **60s 节流** —— 每个仓库要起 2 个 git 子进程，不能被 tick 放大；`repos list` 并发 4 线程、单仓库 6s 超时，超时只影响该行（`timeout`），其余照常。状态口径固定为 `dirty`（有改动）> `ahead`（有未推送）> `clean`，另有 `none` / `missing` / `error`；错误码 `no_git` / `timeout` / `not_repo` / `missing` / `failed` 由界面翻译。后续若要做「一键提交 / 推送」，必须是**新的、显式确认的写操作**，不能挂在本插件的刷新路径上；任何新增的 git 调用同样要回答「会不会写仓库、会不会联网、会不会等输入」三问。`tests/test_git_status.py` 用真实临时仓库守住 clean / untracked / staged / 非仓库 / 缺目录 / 无 git / 超时，并用纯字符串覆盖 porcelain=v2 解析（含重命名与 detached HEAD）。

## 23. SSH 传输用 paramiko，但保持「缺依赖也能启动」

- **背景**：SSH 管理需要真实握手、密码 / 私钥认证、远端执行与输出回显，标准库没有 SSH 客户端；调用系统 `ssh` 命令行无法拿到结构化的主机指纹与退出码，也无法在不把密码写进 argv 的前提下做认证。另一方面，`cli.py` 与 `AGENTS.md` 长期承诺「不依赖第三方库即可运行」，安装包也是「按平台构建、不带虚拟环境」。
- **决策**：引入 `paramiko>=3.4,<4.0`，作为**唯一**第三方依赖写进根目录 `requirements.txt`。三条配套约束：① `core/ssh_manager.py` 里 paramiko **只在函数内 import**，`available()` 先探测、缺依赖时抛 `ParamikoMissing`，上层转成带 `install_hint()` 的可读错误；② 面板与 CLI 顶层绝不 import paramiko，因此缺依赖时 GUI 照常启动、其余命令不受影响；③ 打包由 `packaging/build.py::_install_vendor()` 在**目标平台**执行 `pip install --target <包>/vendor -r requirements.txt`，`main.py` 启动时把 `vendor/` 插到 `sys.path` 最前，失败只告警不阻断构建（SSH 模块退化为提示安装）。
- **影响**：安装包体积增加数 MB（cryptography / bcrypt / pynacl 的 wheel），且**必须按平台各自构建**——macOS 包里不能塞 Windows wheel；`requirements.txt` 同时打进安装包，vendor 缺失时就地补装。升级 paramiko 主版本要重跑 `tests/test_ssh_manager.py`（尤其指纹策略与认证参数）。任何时候都不许把 import 提到模块顶层，否则「缺依赖也能启动」即刻失效。

## 24. SSH 凭据独立存放、指纹必须显式信任、片段永不自动执行

- **背景**：主机密码与私钥口令是本机最敏感的数据，而 `config.json` 会随现有备份 / 迁移流程被打包带走；首次连接若用 `AutoAddPolicy` 静默信任，等于给中间人开门；「命令片段」本质是待执行的 shell 命令，最容易被顺手做成「打开面板就跑一遍」。
- **决策**：① 主机（含密码 / 口令）存 `core/app_paths.data_file("ssh_hosts.json")`，写前经 `core/file_backup.backup()` 生成 `.bak`，两者都收紧权限（POSIX `0600`、Windows `icacls` 仅当前用户，失败只告警）；`config.json` 只放不含敏感信息的 `settings.ssh_snippets`（因此片段能随备份迁移，恢复后悬空 `host_ids` 直接忽略）。② 主机指纹走自定义拒绝策略（等价 `RejectPolicy`，另把算法与指纹带回上层）+ 数据目录下的 `ssh_known_hosts`：首次连接抛 `HostKeyNotTrusted`，GUI 二次确认、CLI 显式 `--trust-host` 后才由 `save_host_keys()` 写入；已存在的主机键发生变化时 paramiko 抛 `BadHostKeyException`，按失败处理，**不自动更新指纹**。③ 交互式终端只拼 `ssh -p 端口 -i 私钥 用户@主机` 交给 `process_utils.open_terminal()`，**密码永不进命令行**（会泄露到进程列表与终端回显），密码认证在终端内自行输入；字段过白名单正则、端口限 1–65535，私钥路径「填了就必须存在」（只拦笔误，不要求必须填）；**私钥认证留空时，表单自动填入本机 `~/.ssh` 里第一个存在的常用私钥**（`id_ed25519` → `id_ecdsa` → `id_rsa` → `id_dsa`，`default_key_path()`），填进去的是具体路径、界面可见可改，连接仍显式传 `key_filename`，**没有**因此启用 ssh-agent 或隐式尝试其它密钥。④ 片段只在用户显式点击 / `ssh snippet run --yes` 时执行一次，支持 `--dry-run` 预览，标准输出 / 错误各限 256 KB 并标 `truncated`，超时默认 30 秒。⑤ **保存与连通性解耦**：主机「确定」即落盘，不做任何连通性校验，密码 / 私钥允许暂缺（`missing_secret()` 标出「还差什么」，`public().credential_ready` 供界面与 CLI 展示）；连接时才由 `_connect()` 抛 `MissingCredential` → `error_kind=credential`。⑥ **预置片段**：首次用到片段（面板进入 SSH 页 / `ssh snippet list|run`）时由 `ensure_builtin_snippets()` 把 `builtin_snippets()` 的 20 条常用运维命令写进 `settings.ssh_snippets`，并用 `settings.ssh_snippets_builtin` 记版本号 —— 同一版本内用户删掉的不会再冒出来，升版本只补当时不存在的 id，同名片段以用户的为准。预置内容限定为只读探测（不提权、不带 `sudo`、不写远端、输出截断），名称与标签按播种时的界面语言落盘。
- **影响**：`ssh_hosts.json` 与 `ssh_known_hosts` 必须进 `.gitignore`、`packaging/build.py::EXCLUDE_NAMES` 与 `docs/README.md` 的运行期文件清单，`tests/test_ssh_cli.py` 断言备份导出包不含凭据文件。凭据迁移只能手工复制该文件；任何新增「自动执行片段」的钩子都属回退，需先改本 ADR。⑤ 的代价是清单里会出现「看着像配好了、其实连不上」的主机 —— 用「凭据未配置」标记与连接时的 `credential` 错误兜住，且**不能**为了少一次点击在保存时偷偷发起连接（那会让保存变慢并引入网络副作用）。⑥ 的代价是首次进入会看到 20 条与自己环境无关的片段：靠「全通用（不绑主机）+ 可编辑可删除 + 删后不复活」兜住，且预置内容**只能**是只读探测 —— 任何会改远端状态的命令都不许进 `builtin_snippets()`，想加就先改本 ADR。
