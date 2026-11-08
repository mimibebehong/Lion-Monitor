# 任务：P6-1 日志落盘轮转 + SQLite WAL（G15 + T10）

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令必须先 cd 到该目录**，你的进程 cwd 不是项目目录）
先读：`docs/reviews/2026-10-31-总体复查.md`（问题条目的证据与修法）、`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试（跑出 RED）→ 实现 → 跑绿（GREEN）。提交前必须贴真实测试输出，禁止编造。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿，条数不低于 576；`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须通过。
4. 只改本任务相关文件；不要重构无关模块，不要删除现有功能，不要改无关的测试断言。
5. 不实现「需要现场提供信息」的东西（有效性业务判据 E1、阈值推送告警 E3、设备使用信息字段）。
6. 完成后把证据（命令、真实输出、关键代码位置、前后对比）写到 `reports/2026-11-02-p6-1-verification.txt`，并在 summary 里写清：改了什么文件、提交 sha、pytest 条数、验收项逐条是否通过。

## 目标
### G15 日志只到 stderr
- 现状：全仓无 `logging.basicConfig` / 文件 handler；`features/pipeline.py:340` 的跳过/损坏日志只在 Streamlit 终端。
- 修法：加统一日志初始化（放 `src/lion_monitor/logging_setup.py`，被 `app/` 入口与 `scripts/` 调用）：`RotatingFileHandler` 写 `logs/lion-monitor.log`（10 MB × 5），格式含时间/级别/模块；控制台 handler 保留。启动时创建 `logs/` 目录。`LION_LOG_DIR` 环境变量可覆盖。
- 关键日志点补 `logger.info/warning`：导入开始/结束（批次、文件数、坏文件数、缺通道）、指标跳过原因、迁移执行、备份触发。

### T10 SQLite WAL + 超时
- 现状：`journal_mode=delete`，无 busy timeout。
- 修法：`store/db.py::make_engine` 建连接时 `PRAGMA journal_mode=WAL`、`PRAGMA synchronous=NORMAL`、`connect_args={"timeout": 30}`；迁移期用 `BEGIN IMMEDIATE`。

## 验收
1. 触发一次含坏文件的导入 → `logs/lion-monitor.log` 出现对应 warning 行（贴行内容）。
2. 日志文件轮转配置生效（可临时把 maxBytes 调小验证，或贴 handler 配置 + 单测）。
3. `PRAGMA journal_mode` 返回 `wal`；`PRAGMA busy_timeout` 返回 30000。
4. 全量 576+ 测试全绿（WAL 不破坏现有行为）。

## 完成定义
- 上述验收项**逐条**有真实命令输出支撑（写进 reports 文件）。
- `git status --porcelain` 为空（除 reports 文件已提交）。
- summary 用中文，明确「未做项」是哪些（没有就写无）。
