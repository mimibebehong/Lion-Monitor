# 任务：P5-1 params_hash 补全指标码/采样率/生效带通（S6）

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令必须先 cd 到该目录**，你的进程 cwd 不是项目目录）
先读：`docs/reviews/2026-10-31-总体复查.md`（问题条目的证据与修法）、`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试（跑出 RED）→ 实现 → 跑绿（GREEN）。提交前必须贴真实测试输出，禁止编造。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿，条数不低于 576；`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须通过。
4. 只改本任务相关文件；不要重构无关模块，不要删除现有功能，不要改无关的测试断言。
5. 不实现「需要现场提供信息」的东西（有效性业务判据 E1、阈值推送告警 E3、设备使用信息字段）。
6. 完成后把证据（命令、真实输出、关键代码位置、前后对比）写到 `reports/2026-11-02-p5-1-verification.txt`，并在 summary 里写清：改了什么文件、提交 sha、pytest 条数、验收项逐条是否通过。

## 目标
### S6 指纹不含 code / fs / 夹紧后的生效带通
- 现状：`src/lion_monitor/params.py:195` 附近生成 `params_hash`；`band_high=16000` 与 `30000` 在 fs=32000 下都被夹到 15200，数值相同但指纹不同；指纹里也没有指标码与 fs。
- 修法：指纹输入加入 ① 指标 `code` ② `fs` ③ **夹紧后**的生效带通（clamp 之后再哈希，不是原始输入）。保持同一 `params_hash` 在相同生效参数下稳定（跨进程、跨运行一致）。
- 建议同时把 `effective_params`（夹紧后实际生效的参数 JSON）随值落库到 `metric_value.effective_params_json`（新增列 + 迁移，历史行为空）。如果这一项改动面超预期，可只做指纹，但要在报告里说明取舍。

## 验收
1. `band_high=16000` 与 `band_high=30000`、fs=32000 → 两者 clamp 结果相同 → `params_hash` **相同**。
2. fs=16000 与 fs=32000（同参数）→ 指纹**不同**。
3. 同参数、不同指标 code → 指纹**不同**。
4. 若加 `effective_params_json`：新算出的值行能读出实际生效带到 15200 的证据。

## 完成定义
- 上述验收项**逐条**有真实命令输出支撑（写进 reports 文件）。
- `git status --porcelain` 为空（除 reports 文件已提交）。
- summary 用中文，明确「未做项」是哪些（没有就写无）。
