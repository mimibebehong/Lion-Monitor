# 任务：MISC 停止自动播种 + band_low 下限（Q5 + Q9）

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令必须先 cd 到该目录**，你的进程 cwd 不是项目目录）
先读：`docs/reviews/2026-10-31-总体复查.md`（问题条目的证据与修法）、`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试（跑出 RED）→ 实现 → 跑绿（GREEN）。提交前必须贴真实测试输出，禁止编造。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿，条数不低于 576；`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须通过。
4. 只改本任务相关文件；不要重构无关模块，不要删除现有功能，不要改无关的测试断言。
5. 不实现「需要现场提供信息」的东西（有效性业务判据 E1、阈值推送告警 E3、设备使用信息字段）。
6. 完成后把证据（命令、真实输出、关键代码位置、前后对比）写到 `reports/2026-11-02-misc-verification.txt`，并在 summary 里写清：改了什么文件、提交 sha、pytest 条数、验收项逐条是否通过。

## 目标
### Q5 空库不再自动播种示例任务（G7）
- 现状：`app/store_ctx.py:65` 调 `bootstrap_example_task(session)`，空库首次访问会生成 21 节点示例树，现场库混进假数据。
- 修法：移除自动调用；健康树页已有「恢复内置示例树」按钮，确认其仍可用（无任务时才显示）。`bootstrap_example_task` 函数保留但不再被隐式调用。
- 验收：删空库后首次启动 → 任务数 0；点「恢复内置示例树」→ 出现 21 节点示例树。

### Q9 band_low 下限保护（0.1 Hz → v_RMS 556 mm/s 的荒谬值）
- 现状：`src/lion_monitor/params.py` 只校验 `window_s ≥ 10 / band_low`，指标定义默认值可绕过窗口校验；band_low 允许 0.1 Hz。
- 修法（按台账推荐，二选一并在报告说明）：① 校验下限抬到 **2 Hz**；或 ② 按「至少 10 个周期」自动抬高 `band_low`（`band_low = max(user_band_low, 10 / window_s)`）并在界面提示「已按窗长自动抬高到 X Hz」。
  推荐 ①+② 组合：硬下限 2 Hz（拒绝并给中文提示），同时在 band_low < 10/window_s 时自动抬到 10/window_s 并提示。默认值 `VELOCITY_BAND=(10,1000)` 不变。
- 验收：`band_low=0.1` → 保存被拒（中文原因）；`band_low=0.5` 且 window_s=2 → 自动抬到 5 Hz 并提示；`band_low=10` 不受影响；v_RMS 不再出现 556 mm/s 量级荒谬值（用合成数据验证一条）。

## 完成定义
- 上述验收项**逐条**有真实命令输出支撑（写进 reports 文件）。
- `git status --porcelain` 为空（除 reports 文件已提交）。
- summary 用中文，明确「未做项」是哪些（没有就写无）。
