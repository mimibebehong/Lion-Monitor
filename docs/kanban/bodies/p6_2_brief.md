# 任务：P6-2 全库备份 + 迁移前自动备份（T2）

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令必须先 cd 到该目录**，你的进程 cwd 不是项目目录）
先读：`docs/reviews/2026-10-31-总体复查.md`（问题条目的证据与修法）、`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试（跑出 RED）→ 实现 → 跑绿（GREEN）。提交前必须贴真实测试输出，禁止编造。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿，条数不低于 576；`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须通过。
4. 只改本任务相关文件；不要重构无关模块，不要删除现有功能，不要改无关的测试断言。
5. 不实现「需要现场提供信息」的东西（有效性业务判据 E1、阈值推送告警 E3、设备使用信息字段）。
6. 完成后把证据（命令、真实输出、关键代码位置、前后对比）写到 `reports/2026-11-02-p6-2-verification.txt`，并在 summary 里写清：改了什么文件、提交 sha、pytest 条数、验收项逐条是否通过。

## 目标
### T2 无备份能力
- 修法：
  1. 新增 `scripts/backup_db.py`：用 `sqlite3.Connection.backup()`（在线安全备份，不用 cp）把 `data/lion.db` 备份到 `data/backups/lion-YYYYmmdd-HHMMSS.db`；保留最近 N 份（N 默认 10，可用 `--keep`）；输出备份路径与大小；支持 `--db` 指定库。
  2. `store/db.py::init_db` 检测到需要执行迁移（结构变更）时，**迁移前自动备份**（同一函数，备份失败则中止迁移并报中文错误，不允许「没备份就改表」）。
  3. 页面入口：工作台或设置页加「立即备份数据库」按钮 + 显示最近一次备份时间/路径/大小（用 AppTest 断言按钮存在与点击后文件生成）。
  4. 备份目录与保留策略写进 README 运维小节。

## 验收
1. 跑 `scripts/backup_db.py` → 生成文件，能 `sqlite3 <backup> "select count(*) from dataset"` 读出与原库一致的行数。
2. 保留策略：连续跑 12 次（--keep 3）→ 只剩 3 个文件，最新保留。
3. 模拟迁移（临时库加一列后跑 init_db）→ 迁移前生成备份文件；把备份目录设为不可写 → 迁移中止并报错，库结构未被修改。
4. 页面按钮点击后确实产出文件（不是只显示文案）。

## 完成定义
- 上述验收项**逐条**有真实命令输出支撑（写进 reports 文件）。
- `git status --porcelain` 为空（除 reports 文件已提交）。
- summary 用中文，明确「未做项」是哪些（没有就写无）。
