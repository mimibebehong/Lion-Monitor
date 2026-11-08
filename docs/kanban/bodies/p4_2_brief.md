# 任务：P4-2 阈值页改台账 + missing_channels（G4 + G2）

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令必须先 cd 到该目录**，你的进程 cwd 不是项目目录）
先读：`docs/reviews/2026-10-31-总体复查.md`（问题条目的证据与修法）、`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试（跑出 RED）→ 实现 → 跑绿（GREEN）。提交前必须贴真实测试输出，禁止编造。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿，条数不低于 576；`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须通过。
4. 只改本任务相关文件；不要重构无关模块，不要删除现有功能，不要改无关的测试断言。
5. 不实现「需要现场提供信息」的东西（有效性业务判据 E1、阈值推送告警 E3、设备使用信息字段）。
6. 完成后把证据（命令、真实输出、关键代码位置、前后对比）写到 `reports/2026-11-02-p4-2-verification.txt`，并在 summary 里写清：改了什么文件、提交 sha、pytest 条数、验收项逐条是否通过。

## 目标
### G4 阈值页读旧模型
- 现状：`app/pages/thresholds.py:43` 用旧 `objects.list_objects`，与「监测对象」页（`object_registry`）列表不一致。
- 修法：改读 `object_registry` 同一来源；页面上对象下拉、阈值列表与「监测对象」页逐项一致（名称+部位）。旧 `objects` 模块若无其它调用方，不要删（out of scope），只切页面数据源。

### G2 missing_channels 未实现
- 现状：树上配了 4 个通道，本批只导入 3 个文件 → 批次状态仍「有效」，无任何提示，漏采看不出来。
- 修法：`ImportResult` 增加 `missing_channels: list[str]`（树上已装配的通道号 − 本批实际存在文件的通道号）；`features/pipeline.py::import_batch` 计算；`app/pages/ingest.py` 导入结果区提示中文文案「本批缺少通道：CH4（健康树已装配 4 路，本批 3 路）」。
- 注意：只提示，不阻断导入，也不改批次状态（三态已在 P1 落地）。不要与 `files_bad` 语义混用。

## 验收（逐条实测写进 reports）
1. 造 4 通道树 + 只导 3 个文件 → `ImportResult.missing_channels == ["CH4"]`，页面出现缺通道提示（用 AppTest 断言文案）。
2. 4 通道树 + 4 文件 → 空列表，页面无提示。
3. 3 通道树 + 3 文件 → 空列表（不能把「树上没有的通道」算成缺失）。
4. 阈值页对象列表 == 监测对象页列表（AppTest 或直接调用两边数据函数比对）。

## 完成定义
- 上述验收项**逐条**有真实命令输出支撑（写进 reports 文件）。
- `git status --porcelain` 为空（除 reports 文件已提交）。
- summary 用中文，明确「未做项」是哪些（没有就写无）。
