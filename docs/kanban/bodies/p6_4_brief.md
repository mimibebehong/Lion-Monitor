# 任务：P6-4 参数/阈值变更审计留痕（T3）

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令必须先 cd 到该目录**，你的进程 cwd 不是项目目录）
先读：`docs/reviews/2026-10-31-总体复查.md`（问题条目的证据与修法）、`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试（跑出 RED）→ 实现 → 跑绿（GREEN）。提交前必须贴真实测试输出，禁止编造。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿，条数不低于 576；`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须通过。
4. 只改本任务相关文件；不要重构无关模块，不要删除现有功能，不要改无关的测试断言。
5. 不实现「需要现场提供信息」的东西（有效性业务判据 E1、阈值推送告警 E3、设备使用信息字段）。
6. 完成后把证据（命令、真实输出、关键代码位置、前后对比）写到 `reports/2026-11-02-p6-4-verification.txt`，并在 summary 里写清：改了什么文件、提交 sha、pytest 条数、验收项逐条是否通过。

## 目标
### T3 参数变更/阈值变更无留痕
- 修法：新增 `change_log` 表（`models.py` + 迁移）：`id / entity_type(metric_assign|metric_limit|metric_def|tree_node) / entity_id / field / old_value / new_value / actor / created_at / note`。
- 写入点（集中到一个 `store/audit.py::record_change(session, ...)`，不要在页面散写）：
  - `store/limits.py::set_limit`（阈值 warn/alert/方向 变更）
  - 参数覆盖/固化保存（`metric_assign.params_json` 变更，含 locked 变化）
  - 指标定义默认参数变更（如走 UI）
  - 节点转速/频率参数变更（若已有写入点）
- 变更只记**真正变化的字段**（old == new 不记）。
- 审计查询：回收站/设置页或阈值页加「变更历史」区（按实体过滤、倒序、最多 200 条），显示时间/实体/字段/旧值→新值。
- 不实现「自动重算」，只留痕（Q4：重算做手动按钮，本轮不做重算链路）。

## 验收
1. 阈值从 warn=5/alert=10 改为 warn=6/alert=12 → `change_log` 2 行（或 1 行含两字段，按你的 schema 说明），含 old/new 与 actor。
2. 保存与旧值相同的参数 → 0 行新增。
3. 参数覆盖保存 → 1 行，entity_type=metric_assign，old/new 是 dict 差异（只含改动键）。
4. 页面「变更历史」能读出这 2~3 条（AppTest 断言）。

## 完成定义
- 上述验收项**逐条**有真实命令输出支撑（写进 reports 文件）。
- `git status --porcelain` 为空（除 reports 文件已提交）。
- summary 用中文，明确「未做项」是哪些（没有就写无）。
