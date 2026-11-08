# 任务：P7 回归补齐（T9，覆盖 P4–P6 与本轮全部修复）

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令必须先 cd 到该目录**，你的进程 cwd 不是项目目录）
先读：`docs/reviews/2026-10-31-总体复查.md`（问题条目的证据与修法）、`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试（跑出 RED）→ 实现 → 跑绿（GREEN）。提交前必须贴真实测试输出，禁止编造。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿，条数不低于 576；`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须通过。
4. 只改本任务相关文件；不要重构无关模块，不要删除现有功能，不要改无关的测试断言。
5. 不实现「需要现场提供信息」的东西（有效性业务判据 E1、阈值推送告警 E3、设备使用信息字段）。
6. 完成后把证据（命令、真实输出、关键代码位置、前后对比）写到 `reports/2026-11-02-p7-verification.txt`，并在 summary 里写清：改了什么文件、提交 sha、pytest 条数、验收项逐条是否通过。

## 目标
### T9 回归用例补齐
本轮已修复：G2/G3/G4/G5/G6/G8/G15 + S3/S6 + T2/T3/T10 + Q5/Q9 + S4 切窗（S4W-1/S4W-2）+ 回收站 7 天保留期。
要求：**每条缺陷至少 1 条先 RED 后 GREEN 的回归用例**（在本轮实现提交里若已有，就在此卡补齐缺口并核对覆盖），并在文件头注明对应缺陷编号。

必须覆盖：
| 编号 | 用例要点 |
|---|---|
| G2 | 4 通道树 + 3 文件 → missing_channels == ["CH4"] |
| G3 | 未软删同键插入两次 → IntegrityError；软删后可插 |
| G4 | 阈值页对象列表 == 监测对象页 |
| G5 | 建任务后旧 threshold 表 0 行新增 |
| G6 | 删节点 → deletion_log 有子树快照 → 恢复后子树与授权回来 |
| G8 | 坏头文件单文件降级，其余文件正常入库 |
| G15 | 坏文件导入后日志文件有 warning 行 |
| S3 | 模板 effective_params 跨任务导入后生效值一致 |
| S6 | 16000/30000 夹紧后同指纹；fs 不同不同指纹 |
| T2 | 备份文件可读且行数一致；迁移前自动备份 |
| T3 | 阈值/参数变更写 change_log，同值不写 |
| T10 | journal_mode == wal |
| Q5 | 空库启动任务数 0 |
| Q9 | band_low=0.1 被拒/被抬高 |
| 回收站 | 8 天前记录被清理，6 天前保留 |
| 切窗 | 20s/2s/2s → 10 点 |

## 验收
1. 上表逐行有对应测试名（贴 `pytest --collect-only` 或测试文件列表 + 名称）。
2. 全量 `.venv/bin/python -m pytest` 全绿，条数 ≥ 600；贴最后一行。
3. 抽查 2 条用例做「反证」：故意改坏被测逻辑让测试变红，改回恢复绿，贴两次输出（证明不是空转断言）。
4. `ruff check` + `ruff format --check` 通过。

## 完成定义
- 上述验收项**逐条**有真实命令输出支撑（写进 reports 文件）。
- `git status --porcelain` 为空（除 reports 文件已提交）。
- summary 用中文，明确「未做项」是哪些（没有就写无）。
