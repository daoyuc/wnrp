# phpvm 文档体系

phpvm 的文档按「**读者意图**」分层，每份文档只解决一类问题，避免互相抄写造成漂移。

## 文档地图

| 文档 | 回答什么问题 | 读者 | 何时必须改 |
|---|---|---|---|
| [`../README.md`](../README.md) | **它能做什么、怎么用**：功能清单、界面说明、CLI 用法、端口映射、config.json | 使用者 / 新同事 | 用户可见行为变化（新增按钮、参数、设置项） |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | **它是怎么做出来的**：分层、依赖方向、并发模型、数据落点、横切铁律 | 二次开发者 / 接手人 | 新增层、改变线程或进程模型、新增运行期文件 |
| [`MODULES.md`](MODULES.md) | **功能落在哪些文件**：能力 → UI 入口 → core 落点 → 持久化 | 改需求前先定位代码 | 新增/拆分模块、面板改名、模块开关调整 |
| [`FLOWS.md`](FLOWS.md) | **关键动作的完整链路**：启动、建站、同步端口、自动启动、崩溃自愈、升级 | 排查线上现象 / 做影响面评估 | 流程步骤或回滚策略变化 |
| [`../ROADMAP.md`](../ROADMAP.md) | **还没做什么、为什么**：现状基线 → 对标差距 → 优先级路线 | 规划者 | 功能立项/落地后 |
| [`../AGENTS.md`](../AGENTS.md) | **AI / 脚本怎么调用**：CLI 唯一入口、响应契约、安全约束 | AI 编码助手 / 自动化脚本 | 命令行契约变化（同时跑 `cli.py schema` 核对） |

## 推荐阅读路径

- **第一次接触本项目**：`README.md` 的「功能说明」→ `ARCHITECTURE.md` 全文 → 需要动手时查 `MODULES.md`
- **定位「某功能在哪」**：`MODULES.md` 表格 → 直接跳 core 文件；CLI 侧补看 `AGENTS.md`
- **评估一次改动的影响面**：`ARCHITECTURE.md` 的「横切铁律」+ `FLOWS.md` 对应流程
- **让 AI 干活**：`AGENTS.md`（命令契约）→ `MODULES.md`（落点）→ 相关代码

## 维护规则（轻量但强制）

1. **改代码同轮同步文档**：按上表「何时必须改」执行；文档与代码不一致时，以代码为准并当轮修文档。
2. **一份事实只写一遍**：功能怎么用写在 `README.md`，架构约束写在 `ARCHITECTURE.md`，其余文档用链接引用，不复制正文。
3. **命令行契约以 `python cli.py schema --json` 为准**，文档只写「典型用法」。
4. **新增界面文案**：用 `t()` 包裹后跑 `python _i18n_scan.py --report`，补 `i18n/<lang>/*.json` 词条。
5. **不提交运行期文件**：`config.json`、`run_log.log*`、`recover_history.json`、`crash_watchdog.*`、`updates/`、`dist/`。
6. **文档事实基线**：本文档体系初次建立并核对代码于 `1d5c5e9`（2026-09-24）；此后按第 1 条滚动维护，不单独维护「基线」，因为每轮改动都必须同步。

## 现有实现基调（写文档时请守住这些前提）

- `core/*` 不依赖 tkinter，可被 CLI / 守护进程 / 测试单独使用；`ui/*` 单向依赖 `core/*`。
- 所有对外部文件的写入走「备份 → 改 → 校验 → 失败回滚」，见 `core/file_backup.py`。
- 跨平台差异集中在少数模块（`process_utils` / `path_manager` / `autostart` / `nginx_manager` / `app_paths` / `theme`），其余代码保持平台无关。
