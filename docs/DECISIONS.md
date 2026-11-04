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
- **影响**：后续扩展插件不得引入「从项目仓库读取可执行内容」的路径；CLI 的 `--command` 参数必须使用独立 `dest`（`command_text`），否则覆盖 `Result.command` 的 `<group>.<action>` 契约；顺带修好 `open_terminal` Linux 分支忽略 `command` 的既有缺陷，并用 `shlex.quote` 引用路径。
