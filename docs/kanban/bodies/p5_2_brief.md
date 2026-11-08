# 任务：P5-2 模板带生效值 + 差异预览（S3）

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令必须先 cd 到该目录**，你的进程 cwd 不是项目目录）
先读：`docs/reviews/2026-10-31-总体复查.md`（问题条目的证据与修法）、`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试（跑出 RED）→ 实现 → 跑绿（GREEN）。提交前必须贴真实测试输出，禁止编造。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿，条数不低于 576；`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须通过。
4. 只改本任务相关文件；不要重构无关模块，不要删除现有功能，不要改无关的测试断言。
5. 不实现「需要现场提供信息」的东西（有效性业务判据 E1、阈值推送告警 E3、设备使用信息字段）。
6. 完成后把证据（命令、真实输出、关键代码位置、前后对比）写到 `reports/2026-11-02-p5-2-verification.txt`，并在 summary 里写清：改了什么文件、提交 sha、pytest 条数、验收项逐条是否通过。

## 目标
### S3 模板只带覆盖 diff → 跨任务导入参数静默漂移
- 现状：`tree_template.py:168-169` 只导出 `params_json`（覆盖项）；源任务 band_high=1000（无覆盖）导出 `params={}`，导入到默认 500 的目标任务后生效值变 500，无提示。
- 修法（Q2 已拍板：**按生效值搬运 + 预览列出差异**）：
  1. 导出时每个 metric 节点同时写 `effective_params`（该节点实际生效的完整参数快照）。
  2. 模板版本向前兼容：仍接受 v1/v2；带 `effective_params` 的导出可用 v3 或 v2 扩展字段（自行决定，**但必须在报告里写清版本号与兼容规则**，且旧版本文件仍可导入）。
  3. 导入时增加参数策略三选一：`override`（用模板生效值覆盖，默认）/ `locked_only`（只覆盖源里锁定的项）/ `follow_default`（只搬覆盖 diff，保持旧行为）。
  4. 导入预览列出「N 个指标参数与目标任务默认不同」并可展开明细（节点路径 + 参数 + 目标默认 → 模板生效值）。
- 不要改动已有 v2 五条验收脚本的行为（`scripts/verify_template_v2.py` 必须仍能跑通，必要时同步升级脚本并说明）。

## 验收
1. 源 band_high=1000（无覆盖）→ 导出含 effective_params → 导入到默认 500 的任务（override 策略）→ 目标节点生效值 = 1000。
2. 预览模式列出该差异 1 处，文案含「500 → 1000」。
3. `follow_default` 策略仍得到 500（旧行为保留）。
4. v1 文件仍可导入；无 `effective_params` 的 v2 文件按 `follow_default` 处理并提示。
5. `scripts/verify_template_v2.py` 跑通。

## 完成定义
- 上述验收项**逐条**有真实命令输出支撑（写进 reports 文件）。
- `git status --porcelain` 为空（除 reports 文件已提交）。
- summary 用中文，明确「未做项」是哪些（没有就写无）。
