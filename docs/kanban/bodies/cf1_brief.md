# 任务：CF-1 component_freq 台账后端 + CSV 导入导出

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令先 cd 到该目录**）
先读：`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/reviews/2026-10-31-总体复查.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试并跑出 RED（贴真实输出）→ 实现 → GREEN。禁止编造任何测试结果。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿；当前基线 **752 passed**，改动后条数只能持平或增加。`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须过。
4. 只改本任务相关文件；不要删现有功能，不要为省事改动无关测试断言。
5. 证据（命令 + 真实输出 + 代码位置 + 前后对比）写 `reports/2026-11-02-cf-1-model-io-verification.txt` 并提交。
6. 不实现需要现场给信息的项（E1 有效性业务判据、E3 阈值推送、设备使用信息字段）。

## 完成定义
- 验收项逐条有真实命令输出；`git status --porcelain` 为空；summary 用中文写清改了哪些文件、提交 sha、pytest 条数、未做项。

## 背景（马哥 2026-11-02 定）
按 `docs/plans/2026-10-31-指标参数体系与频率参数设计.md` §S3 做部件频率台账，**配置参数以附件为准且支持导入导出**。
样例数据（本次入库，已转 UTF-8 + 保留 GBK 原始）：
- `docs/examples/监测对象_20260309.csv`（UTF-8）
- `docs/examples/监测对象_20260309_GBK原始.csv`（GBK，Excel 直出格式）
131 行、57 个唯一部件（部件编号+名称）、11 个通道、部件类型 = 齿轮/轴承/行星齿轮/轴。

## 目标（本卡只做「表 + 存储 + 导入导出 + 计算接续」，UI 在下一卡）
### 1. 表结构
按设计文档建 `component_freq`（字段以文档为准，至少含）：`id / object_id(监测对象) / part_code / part_name / part_type / channel_code / component_kind(gear|bearing|planet|shaft|other) / ref_rpm / hz_at_1rpm / scaling(speed|fixed) / freq_kind(om1|om2|omx|om2x|bpfi|bpfo|ftf|bsf) / value_hz(留痕原始值) / geom_d(附件 D 列) / note / created_at / updated_at`，唯一键 `(object_id, part_code, freq_kind)`（`deleted_at IS NULL` 部分唯一索引）。
**归一化口径照设计文档**：`hz_at_1rpm = value_hz / ref_rpm`，`scaling=speed` 随实际转速缩放，`fixed` 不缩放。

### 2. 附件 CSV 导入
- 列映射（中文表头，逐列写死）：`序号`→通道号（如 `Chan_1`）、`振动通道名称`→通道显示名、`部件类型`→part_type、`监测部件编号`→part_code、`监测部件名称`→part_name、`OM1/OM2/OMx/OM2x`→freq_kind、`外圈故障特征频率BPFI`→bpfi、`内圈故障特征频率BPF0`→bpfo（注意列名是 BPF0，语义按内圈）、`保持架故障特征频率FTF`→ftf、`滚动体故障特征频率BSF`→bsf、`D`→geom_d。
- **ref_rpm 推导**：表内无转速列，用该行 `OM1 × 60` 反推参考转速（`348.3 Hz → 20898 rpm`；`4.3043 Hz → 258.26 rpm`）。同部件多行 OM1 有精度差异（348.3 / 348.3333333）→ 取该部件出现次数最多的值（并列取最大精度者），并在报告里写清规则。
- 编码自动识别（GBK/UTF-8 都吃），空值跳过该 freq_kind。
- **幂等 upsert**：同 `(object_id, part_code, freq_kind)` 重复导入更新而非重复插入；同部件跨通道（如 Z1_R 同时挂 Chan_1/Chan_8）**只建一条部件记录**，通道关系记在关联表或用 `channel_code` 多值（自行设计并说明，「一个部件可被多个通道监测」必须支持）。
- 导入报告：新增 N / 更新 M / 跳过 K（含原因行号）/ 部件数。
- 错误行不中断整批（沿用 P1/P0.5 风格）。

### 3. 导出
- 导出**与附件同格式同列序的 CSV**（GBK 编码，Excel 双击可读），数值按 `value_hz` 原值输出；可导出全部或按对象/通道筛选。

### 4. 计算接续
- `resolve_rpm` 接上 `rated`（对象额定转速）与 `measured`（批次实测）两级，优先级：节点手填 > 批次实测 > 对象 rated > 无（无则不猜、门控）。
- 频率派生：`f = hz_at_1rpm × rpm_actual`（scaling=speed）；`fixed` 直接用 `value_hz`。提供 `resolve_component_freq(session, node_id, freq_kind, rpm_actual)` 之类函数，供标记线/主峰判据使用（本卡只做函数 + 测试，页面接线下一卡）。

## 验收（逐条写进 reports）
1. 导入附件：131 行 → 57 个部件、各 freq_kind 记录数符合表内非空计数（贴统计数字）；重复导入第二次 0 新增 M 更新。
2. 归一化正确：Z1_R 的 OM1 → `hz_at_1rpm ≈ 1/60`、`ref_rpm ≈ 20898`；OMx → 齿数 25（`hz_at_1rpm×ref_rpm/OM1 ≈ 25`）。
3. 4D 主旋翼轴轴承：ref_rpm ≈ 258.26，BPFI 归一化后阶次 ≈ 8.6（贴计算）。
4. 同部件挂 Chan_1 与 Chan_8 → 只 1 条部件记录，两通道都能查到。
5. 导出 CSV 与附件列序一致、GBK 可读、行数与导入部件×频率项一致；再导入导出文件 → 0 变化（往返幂等）。
6. `resolve_rpm` 三级优先级测试（节点手填 > measured > rated）；三者皆无 → 返回 None 且不猜。
7. 全量 pytest 全绿（≥752）。

## 完成定义补充
- 若你发现实现与设计文档口径冲突，**在报告里写明冲突点与你的取舍**，不要默默按自己想法改。
