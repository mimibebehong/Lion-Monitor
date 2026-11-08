# 任务：S4W-1 window_s/step_s 切窗真正生效（频域指标）

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令必须先 cd 到该目录**，你的进程 cwd 不是项目目录）
先读：`docs/reviews/2026-10-31-总体复查.md`（问题条目的证据与修法）、`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试（跑出 RED）→ 实现 → 跑绿（GREEN）。提交前必须贴真实测试输出，禁止编造。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿，条数不低于 576；`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须通过。
4. 只改本任务相关文件；不要重构无关模块，不要删除现有功能，不要改无关的测试断言。
5. 不实现「需要现场提供信息」的东西（有效性业务判据 E1、阈值推送告警 E3、设备使用信息字段）。
6. 完成后把证据（命令、真实输出、关键代码位置、前后对比）写到 `reports/2026-11-02-s4w1-verification.txt`，并在 summary 里写清：改了什么文件、提交 sha、pytest 条数、验收项逐条是否通过。

## 目标
### S4（切窗）参数占位符未生效
- 现状：`params` 里已有 `window_s` / `step_s` / `window_fn`，但 `features/metrics.py` 无任何 `window_s` 使用（grep 为空）→ 计算仍按整段统计，趋势无法逐点。
- 修法（**范围限频域指标**：v_RMS、主峰频率、谱质心、包络类）：
  1. 按 `window_s` / `step_s` 切分信号，逐窗计算 → 一次导入产出多个时点的 `metric_value`（`computed_at` 用窗中心时间偏移，或按批起始时间 + 窗序号，方案自行定但**必须在报告里写清口径**并在趋势图上连续）。
  2. `window_fn` 生效（至少支持 `hann` / `rect`；默认保持现状）。
  3. 末段不足一窗丢弃（马哥口径：2 s/2 s 不重叠）。
  4. 与已有 `params_hash` 一致：同一窗参数产生同一指纹；P5-1 完成后指纹应含 fs 与生效带通。
  5. 性能：单批 20 s 数据、2 s 窗 → 每指标 10 点，24 万行性能基线不能被打破（跑 `scripts/bench_pages.py` 或报告本轮行数）。

## 验收
1. 20 s 数据、window_s=2、step_s=2 → 每个频域指标恰好 10 个值（贴行数与时间戳）。
2. window_s=5、step_s=2 → 8 个值（20-5)/2+1 = 8，末段丢弃规则明确。
3. window_fn=hann 与 rect → 数值不同（贴两个值），指纹不同。
4. 趋势页能按节点+指标画出 10 个点（AppTest 或取数函数验证）。
5. 全量 pytest 全绿；ruff 通过。

## 完成定义
- 上述验收项**逐条**有真实命令输出支撑（写进 reports 文件）。
- `git status --porcelain` 为空（除 reports 文件已提交）。
- summary 用中文，明确「未做项」是哪些（没有就写无）。
