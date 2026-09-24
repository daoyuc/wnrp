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
- 不要提交运行期文件：`config.json`、`recover_history.json`、`crash_watchdog.*`、`run_log.log*`、`updates/`、`dist/`。

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

# 服务编排（--php 决定启动哪些 PHP：newest|used|active|all|<版本名列表>，默认 all）
python3 cli.py services start-all        # PHP → Redis → MySQL → Nginx
python3 cli.py services start-all --php newest   # 只启动版本号最新的 PHP（开机自启的默认策略）
python3 cli.py nginx test                # 任何改配置后都建议先跑
```

## 项目结构速记

- `core/` 服务层（nginx / php / redis / mysql / vhost / hosts / sqlite / updater / modules …），CLI 与 GUI 共用
  - 建站流程只有一个实现：`core/site_service.py`（证书 → 写 vhost → 补 include → `nginx -t` → hosts → 重载 + 回滚），CLI 与 GUI 向导都调它
  - 文件备份统一走 `core/file_backup.py`（写配置前必 `.bak`，还原默认删备份）
- `tests/` 单元测试（标准库 unittest）：`python -m unittest discover -s tests -t .`
- `ui/` 界面层（tkinter），**不可在 CLI / 脚本中 import**
- `config.json` 端口映射与设置（运行期生成，不入库）；端口与 vhost 的 `fastcgi_pass` 必须一致，用 `site sync-port` 一键同步
- `README.md` 有完整功能与端口映射表
