# 任务：P6-3 节点级回收站 + 7 天保留期（G6）

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令必须先 cd 到该目录**，你的进程 cwd 不是项目目录）
先读：`docs/reviews/2026-10-31-总体复查.md`（问题条目的证据与修法）、`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试（跑出 RED）→ 实现 → 跑绿（GREEN）。提交前必须贴真实测试输出，禁止编造。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿，条数不低于 576；`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须通过。
4. 只改本任务相关文件；不要重构无关模块，不要删除现有功能，不要改无关的测试断言。
5. 不实现「需要现场提供信息」的东西（有效性业务判据 E1、阈值推送告警 E3、设备使用信息字段）。
6. 完成后把证据（命令、真实输出、关键代码位置、前后对比）写到 `reports/2026-11-02-p6-3-verification.txt`，并在 summary 里写清：改了什么文件、提交 sha、pytest 条数、验收项逐条是否通过。

## 目标
### G6 删节点/删授权不可撤销
- 现状：`reports/2026-10-31-review-danger.txt` §D1/D3：模板整树替换后旧树 7 节点被软删，`deletion_log` 0 行、回收站 0 条。
- 修法：
  1. `soft_delete_node` 写 `deletion_log`（已有表，检查字段）：`entity_type='task_node'`、`entity_id`、`payload` = **子树快照 JSON**（节点属性 + 子节点 + 该节点上的 metric_assign 参数/阈值引用），`deleted_at`、`deleted_by`（有就用，没有可置 `system`）。
  2. 模板整树替换同样走软删并逐节点留痕（不要静默批量删）。
  3. 回收站页（`app/pages/recycle*.py`）支持 `task_node` 类型：列出节点路径 + 删除时间 + 剩余保留天数；「恢复」重建节点与子树（父节点不存在时恢复到任务根下并提示），恢复时清 `deleted_at` 并写一条恢复记录。
  4. **保留期 7 天（马哥 2026-11-01 定）**：`deletion_log` 增加 `expires_at`（或按 deleted_at + 7 天计算），应用启动时清理过期记录并真删对应软删行（只处理超过 7 天的 task_node 记录，日志记删除条数）。保留天数做成常量 `RECYCLE_RETENTION_DAYS = 7`，可环境变量覆盖。

## 验收
1. 删除一个带 2 个子节点 + 1 个指标授权的节点 → `deletion_log` 1 行，payload 含完整子树。
2. 回收站列出该条，恢复后节点、子树、指标授权全部回来（逐项查询证明），且 `deleted_at` 为空。
3. 模板整树替换 → 旧节点逐条进 deletion_log（条数 = 被替换节点数）。
4. 保留期：手工插入 deleted_at = 8 天前的记录 + 对应软删行 → 启动清理后记录与软删行均不再存在；deleted_at = 6 天前的不受影响。

## 完成定义
- 上述验收项**逐条**有真实命令输出支撑（写进 reports 文件）。
- `git status --porcelain` 为空（除 reports 文件已提交）。
- summary 用中文，明确「未做项」是哪些（没有就写无）。
