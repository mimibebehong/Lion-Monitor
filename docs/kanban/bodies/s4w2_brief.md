# 任务：S4W-2 a_Peak/a_RMS 补 window_s 口径一致（时域指标）

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令必须先 cd 到该目录**，你的进程 cwd 不是项目目录）
先读：`docs/reviews/2026-10-31-总体复查.md`（问题条目的证据与修法）、`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试（跑出 RED）→ 实现 → 跑绿（GREEN）。提交前必须贴真实测试输出，禁止编造。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿，条数不低于 576；`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须通过。
4. 只改本任务相关文件；不要重构无关模块，不要删除现有功能，不要改无关的测试断言。
5. 不实现「需要现场提供信息」的东西（有效性业务判据 E1、阈值推送告警 E3、设备使用信息字段）。
6. 完成后把证据（命令、真实输出、关键代码位置、前后对比）写到 `reports/2026-11-02-s4w2-verification.txt`，并在 summary 里写清：改了什么文件、提交 sha、pytest 条数、验收项逐条是否通过。

## 目标
### 时域指标无 window_s（整段统计，与频域口径不一致）
- 现状：`a_Peak` / `a_RMS` 按整段统计，没有 `window_s`；S4W-1 完成后频域已逐窗，时域仍整段 → 同一通道两套口径。
- 修法：给 `a_Peak`、`a_RMS`（以及峭度/峰峰值/波形因子等时域类，按 `metric_def` 现有清单）补 `window_s` / `step_s` 参数到 `PARAM_SCHEMA`，计算层按窗切分逐窗产出（复用 S4W-1 的切窗工具函数，不要复制一份逻辑）。
- 默认值与频域一致：2 s / 2 s（马哥已定，照抄不要调整）。
- 迁移：已有节点无覆盖 → 走默认；历史值保持原样（不改写），新算出的值按窗。

## 验收
1. 20 s 数据 2 s 窗 → a_Peak 与 a_RMS 各 10 个值；第 k 个 a_RMS 与手工对第 k 段算的 RMS 相对误差 < 1e-9（贴对比）。
2. 参数表单里 a_Peak/a_RMS 出现「计算时长/步长」两项（AppTest 断言）。
3. 峭度/峰峰值等时域指标同样按窗（若有）。
4. 全量 pytest 全绿（历史整段口径的测试需按新口径更新，并在报告里列出改动的测试名与理由）。

## 完成定义
- 上述验收项**逐条**有真实命令输出支撑（写进 reports 文件）。
- `git status --porcelain` 为空（除 reports 文件已提交）。
- summary 用中文，明确「未做项」是哪些（没有就写无）。
