# AGENTS.md

给 AI 编码助手 / 自动化脚本的**操作契约**：本仓库是 phpvm（PHP 版本管理器，Tk GUI + `core/*` 服务层）。
需要查询或改变环境状态时，**用命令行，不要去操作图形界面**。

## 唯一入口

```bash
python3 cli.py <group> <action> [options] --json
```

`cli.py` 无 GUI、完全非交互（只依赖标准库与 `core/*`，不 import tkinter），可在 SSH / CI 下运行。

## 必读第一步

```bash
python3 cli.py schema --json   # 全部命令 / 参数 / 默认值 / 说明的自描述契约
python3 cli.py env --json      # 环境快照：nginx、PHP 版本与端口、Redis、MySQL、模块开关、路径
```

`schema` 由 argparse 结构自动派生，命令变更后不会漂移 —— **以它为准，不要凭记忆写参数**。

## 响应契约

| 项 | 说明 |
|---|---|
| JSON | 加 `--json` 时 stdout **只**有一个文档：`{"ok": bool, "command": "<group>.<action>", "data": {...}, "warnings": [...]}` |
| 退出码 | `0` 成功 · `1` 业务失败（见 `data.error`）· `2` 参数用法错误 |
| 字段名 | 一律英文（稳定契约）；说明性文本按当前语言输出（`--lang en\|zh_CN\|zh_TW\|ja\|ko`） |
| 失败 | 先看 `data.error`；常见带 `available`（可选值）或 `matched`（歧义）字段，据此重试 |

## 安全约束（必须遵守）

- **写操作先 `--dry-run`**：`site create`、`site remove`、`config set`、`php port --set`、`php ext-set`、`hosts add/remove`、`services *-all` 都支持，只报告将做什么。
- **删除必须 `--yes`**；Redis `FLUSHALL/FLUSHDB/SHUTDOWN/SLAVEOF/REPLICAOF/DEBUG` 必须 `--force`。
- **写 hosts 必须显式 `--hosts`**（会触发系统授权弹窗，无 TTY 时可能失败）。
- **不要编辑 `ui/` 下代码来做自动化**；也不要在脚本里 import `ui/*`（会拉起 tkinter）。
- 所有写入自动备份 `.bak`；`nginx -t` 失败会自动回滚并置 `rolled_back: true`。
- 不要提交运行期文件：`config.json`、`recover_history.json`、`crash_watchdog.*`、`run_log.log*`、`updates/`、`dist/`、`plugins/`（键鼠统计的 `stats.db` / `daemon.*`）。
- **插件命令来源**：`.phpvm.json` 是团队可共享的项目文件，**绝不能把它的字段当命令执行**（克隆仓库即 RCE）；项目启动器的命令只来自内置白名单或用户手动添加的设置项，且仅点击 / 显式 CLI 调用才打开一次浏览器 / 终端，不自动执行（ADR 16）。「在浏览器打开」的 URL 同样只从 nginx vhost 的 `server_name` 生成，不采信 `.phpvm.json` 的 `domains`。新增 CLI 参数时避开 `command` 这个 dest，否则会覆盖 `Result.command` 的 `<group>.<action>` 契约。

## 一致性约束（改动时必须收口）

下面是改完最容易漏、且单测不易捕获的坑，改动触及时必须自查：

- **取「PHP 是否在跑」必须刷新状态**：`core/php_manager.py` 的 `scan_versions()` 只探测版本二进制，**不刷新 `running`**，直接读会得到陈旧值。统一走 `resolve(refresh_status=True, fast=True)`（定时刷新的默认姿势；操作后精确校验用 `fast=False`）。任何「挑一个 PHP 来用」的逻辑（托管站点建站、TLS 自动续期、端口同步等）都必须先按 `running` 过滤，再回退到版本列表——否则站点会落到未启动的 PHP 上，表现为 `502`。
- **新增 UI 面板必须登记**：`ui/main_window.py` 的 `_refresh_panels()` 里有个面板元组，新面板不进这个列表，就不参与定时刷新和「操作完成后立即刷新」，会出现「命令成功但界面没变」（典型：新建站点 / 安装 Adminer 后站点映射页不更新）。
- **插件面板与后台进程按框架接线**：插件 key 由 `core/modules.py` 自动并入 `MODULES`（设置页、启动快照、CLI `plugin enable/disable` 无需另写枚举）；面板须实现 `auto_refresh()` / `refresh_theme()`，并确认 `ui/main_window.py` 的 `_plugin_panels` 已进 `_refresh_panels()`；后台进程状态文件名以 `core/plugins/keymouse/daemon.py` 顶部常量为准（`daemon.lock` / `daemon.heartbeat` / `daemon.stop` / `paused`），不要按字面猜 `daemon.pid` 或 `stop`；`WH_KEYBOARD_LL` 钩子线程必须用 `MsgWaitForMultipleObjects` 等消息、不得 `time.sleep` 轮询（实测每次按键约 30ms 延迟，见 ADR 15 与 `tests/test_keymouse_collector.py::PumpTest`）；macOS 端 tap 与 `CFRunLoop` 必须同线程，且授权看 `CGPreflightListenEventAccess`（「输入监控」），不是「辅助功能」—— 未授权时 tap 仍建得出来但收不到任何事件（见 ADR 18）。

## 常用流程

```bash
# 建站（推荐：先预览再落盘）
python3 cli.py site create --domain app.test --root ~/wnrp/www/app \
    --template laravel --php php82 --dry-run
python3 cli.py site create --domain app.test --root ~/wnrp/www/app \
    --template laravel --php php82 --hosts          # 写 vhost → 补 include → nginx -t → hosts → 重载

# 切 PHP / 同步端口
python3 cli.py site php app.test --php php83
python3 cli.py site sync-port --old 9082 --new 9083 --reload

# 只读查库（不会写 settings.sqlite_last_db）
python3 cli.py sqlite query /path/database.sqlite "select * from users limit 5" --json

# hosts 写入前会自动备份为 <hosts>.phpvm.bak，可整文件还原
python3 cli.py hosts restore --dry-run
python3 cli.py hosts restore --yes

# 外观主题（light / dark / system；GUI 切换立即生效，也允许脚本代改）
python3 cli.py config get settings.theme
python3 cli.py config set settings.theme dark

# 服务编排（--php 决定启动哪些 PHP：newest|used|active|custom|all|<版本名列表>，默认 all）
python3 cli.py services start-all        # PHP → Redis → MySQL → Nginx
python3 cli.py services start-all --php newest   # 只启动版本号最新的 PHP（开机自启的默认策略）
python3 cli.py services start-all --php custom --dry-run   # 按 PHP 页勾选的开机自启名单（先预览）
python3 cli.py config set settings.autostart_php_versions '["php82","php85"]'  # 直接改勾选名单
# 插件与键鼠统计（写操作先 --dry-run，清空需 --yes）
python3 cli.py plugin list --json
python3 cli.py plugin status keymouse --json
python3 cli.py plugin disable keymouse --dry-run
python3 cli.py keymouse status --json
python3 cli.py keymouse summary --range week --top 10 --json
python3 cli.py keymouse export --range month --out report.csv --dry-run
python3 cli.py keymouse pause --dry-run
python3 cli.py keymouse clear --yes --dry-run
python3 cli.py keymouse daemon start --json
python3 cli.py keymouse daemon status --json

# 项目启动器（写操作先 --dry-run，删除需 --yes）
python3 cli.py launcher list --json
python3 cli.py launcher browse --project demo --dry-run --json   # 站点 URL 只来自 vhost
python3 cli.py launcher commands --project demo --json
python3 cli.py insights report --file access.log --errors --json   # 只读 nginx access.log
python3 cli.py health sites --json                                  # 只列 vhost 域名，不发起请求
python3 cli.py health probe --domain gmlbm --json                   # 只读 GET /，不跟随重定向
python3 cli.py launcher add --label "Dev server" --command "php -S 127.0.0.1:8000" --dry-run --json
python3 cli.py launcher open --project demo --id php-v --dry-run --json
python3 cli.py launcher remove --id custom-xxxxxxxx --yes --dry-run --json

python3 cli.py nginx test                # 任何改配置后都建议先跑
```

## 项目结构速记

- `docs/` 开发者文档体系（`docs/README.md` 导航、`ARCHITECTURE.md` 架构与横切铁律、`MODULES.md` 能力→落点对照、`FLOWS.md` 关键流程）——改代码后按 `docs/README.md` 的维护规则同步文档
- `core/` 服务层（nginx / php / redis / mysql / vhost / hosts / sqlite / updater / modules …），CLI 与 GUI 共用
  - 建站流程只有一个实现：`core/site_service.py`（证书 → 写 vhost → 补 include → `nginx -t` → hosts → 重载 + 回滚），CLI 与 GUI 向导都调它
  - 文件备份统一走 `core/file_backup.py`（写配置前必 `.bak`，还原默认删备份）
  - `core/plugins/` 插件注册表与内置插件（声明式 `MANIFEST`，只声明面板路径、不 import `ui/*`）；运行期数据在 `<数据目录>/plugins/keymouse/`，不入库
- `ui/plugins/` 插件面板（由主窗口延迟导入）：新增面板必须实现 `auto_refresh()` / `refresh_theme()`，`core` 不得反向依赖它
- `tests/` 单元测试（标准库 unittest）：`python -m unittest discover -s tests -t .`；其中 `tests/test_docs.py` 守护文档（链接 / 引用的代码路径 / `docs/CLI.md` 与代码一致性 / Markdown 表格列数）、`tests/test_main_window_smoke.py` 真建一次主窗口（守 GUI 启动崩溃，无显示环境自动跳过）
- `docs/` 的 `CLI.md` 由 `_docs_cli.py` 从 `cli.py` 的 argparse 结构生成（**勿手改**）：改完命令跑 `python3 _docs_cli.py`，或用 `python3 _docs_cli.py --check` 单独校验
- `ui/` 界面层（tkinter），**不可在 CLI / 脚本中 import**
- `config.json` 端口映射与设置（运行期生成，不入库）；端口与 vhost 的 `fastcgi_pass` 必须一致，用 `site sync-port` 一键同步
- `README.md` 有完整功能与端口映射表
