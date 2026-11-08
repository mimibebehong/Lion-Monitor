# 任务：P4-1 部分唯一索引 + 删除旧阈值播种（G3 + G5）

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令必须先 cd 到该目录**，你的进程 cwd 不是项目目录）
先读：`docs/reviews/2026-10-31-总体复查.md`（问题条目的证据与修法）、`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试（跑出 RED）→ 实现 → 跑绿（GREEN）。提交前必须贴真实测试输出，禁止编造。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿，条数不低于 576；`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须通过。
4. 只改本任务相关文件；不要重构无关模块，不要删除现有功能，不要改无关的测试断言。
5. 不实现「需要现场提供信息」的东西（有效性业务判据 E1、阈值推送告警 E3、设备使用信息字段）。
6. 完成后把证据（命令、真实输出、关键代码位置、前后对比）写到 `reports/2026-11-02-p4-1-verification.txt`，并在 summary 里写清：改了什么文件、提交 sha、pytest 条数、验收项逐条是否通过。

## 目标
### G3 唯一约束含 deleted_at，NULL 使约束失效
- 现状：`src/lion_monitor/models.py` 里 `metric_assign`（约 :218）与 `metric_limit`（约 :237）的唯一索引包含 `deleted_at` 列；SQLite 里 NULL 不等于 NULL，同键可插入两行，`metric_jobs` 字典静默取最后一条。
- 修法：改为**部分唯一索引** `CREATE UNIQUE INDEX ... ON metric_assign(<键列>) WHERE deleted_at IS NULL`（metric_limit 同理，键列以现有约束为准）。
- 迁移：写进 `store/db.py::init_db(engine)` 的迁移逻辑；SQLite 不能直接改既有索引 → 先 `DROP INDEX` 旧索引再建部分唯一索引。**先在副本上验证**：`cp data/lion.db /tmp/verify_p4.db`，对副本跑 init_db，确认：旧行数不变、无数据丢失、重复键在软删后允许、未软删时第二次插入抛 IntegrityError。
- 老库里若已存在「同键且都未软删」的重复行（复查时人为插入过），迁移不能静默失败：先按 `id` 保留最小 id 的行，其余置 `deleted_at=now` 并在日志里记条数（或直接报错让操作员处理——二选一，选不丢数据的那个并在报告里说明理由）。

### G5 旧 threshold 表播种
- 现状：每次建任务播种 3 行旧 `threshold` 表，`resolve_threshold` 调用方 0，纯误导。
- 修法：删除播种逻辑（定位：grep `threshold` 在 `store/` 下的 insert/seed 调用），表保留（历史数据不动），代码注释标明「历史遗留，不再写入、无人读取」。

## 验收（逐条实测写进 reports）
1. 同一 metric_assign 键插入两次（deleted_at 均为 NULL）→ 第二次抛 `IntegrityError`。
2. 第一条软删（deleted_at 非空）后再插入同键 → 成功。
3. metric_limit 同样两条通过。
4. 副本库迁移后：行数前后一致、部分唯一索引存在（`PRAGMA index_list` + `PRAGMA index_info` 输出）。
5. 新建任务后旧 `threshold` 表新增 0 行。

## 完成定义
- 上述验收项**逐条**有真实命令输出支撑（写进 reports 文件）。
- `git status --porcelain` 为空（除 reports 文件已提交）。
- summary 用中文，明确「未做项」是哪些（没有就写无）。
