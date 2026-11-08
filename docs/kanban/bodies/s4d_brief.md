# 任务：S4 带通改频域滤波（替代 butter 阶数设置）

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**所有命令先 cd 到该目录**）
先读：`docs/plans/2026-10-31-指标参数体系与频率参数设计.md`、`docs/reviews/2026-10-31-总体复查.md`、`docs/kanban/待办看板.md`

## 硬性要求（违反即返工）
1. **TDD**：先写失败测试并跑出 RED（贴真实输出）→ 实现 → GREEN。禁止编造任何测试结果。
2. 一个逻辑改动一次提交，中文 commit message，格式 `fix(scope): 说明（批次·编号）`。
3. `.venv/bin/python -m pytest`（**不要加 -q/-qq**）必须全绿；当前基线 **752 passed**，改动后条数只能持平或增加。`.venv/bin/ruff check src app tests` 与 `ruff format --check` 必须过。
4. 只改本任务相关文件；不要删现有功能，不要为省事改动无关测试断言。
5. 证据（命令 + 真实输出 + 代码位置 + 前后对比）写 `reports/2026-11-02-s4-freq-band-filter-verification.txt` 并提交。
6. 不实现需要现场给信息的项（E1 有效性业务判据、E3 阈值推送、设备使用信息字段）。

## 完成定义
- 验收项逐条有真实命令输出；`git status --porcelain` 为空；summary 用中文写清改了哪些文件、提交 sha、pytest 条数、未做项。

## 背景（马哥 2026-11-02 定）
带通滤波改为**频域滤波**：不再用 `scipy.signal.butter` + `sosfiltfilt`，避免阶数、零相位、边缘效应等一堆设置问题。

## 目标
1. 把 `src/lion_monitor/features/metrics.py::_band_filter` 改为频域实现：
   - 对输入序列做 `np.fft.rfft` → 带外频点置零 → `np.fft.irfft(n=原长度)`。
   - 保留现有 `effective_params` 夹紧逻辑（`band_high ≤ 0.95×fs/2`、`band_low ≥ 2 Hz`、`band_low < band_high`、样本量下限）。
   - 频域滤波是零相位（无延迟），返回长度与输入一致。
   - 过渡带：默认可硬截断（置零）；若你判断硬截断导致明显振铃，可加 1~2 个 bin 的余弦过渡，但**必须在报告里说明**并给出对比数值。
2. 所有调用方统一走新实现：`v_RMS`、`velocity_series`、包络谱（`features/envelope.py` 的带通，如使用同一函数则自动跟随）。
3. **口径文档（G13 同步）**：更新 `docs/plans/2026-10-28-监测软件方案与UI设计.md` 的计算口径小节 + v_RMS 页面 help，写清：频域带通（带外置零）、上限 0.95×Nyquist 夹紧、频域积分转速度、RMS 为去均值后 AC 口径；删掉「4 阶 Butterworth」表述。
4. 现有 golden/数值测试会变：按新口径更新期望值，并在报告里列出「改了哪些测试、为什么」；**不允许直接放宽断言**（如把 assert 改成 approximate 大容差）。

## 验收（逐条写进 reports）
1. 合成正弦 145 Hz / 0.8 gpk / fs=32000 / 20 s → v_RMS 与理论值对比（相对误差 < 2%，贴数值）。
2. 频域滤波零相位：构造 100 Hz + 500 Hz 双音，滤波后峰值时间位置与原始一致（无延迟）。
3. 带外能量被压制：1200 Hz 单音在 10–1000 带通后残差 < -40 dB（贴数值）。
4. `velocity_series` 长度与输入一致、数值有限。
5. 全量 pytest 全绿（≥752）；ruff 过。

## 完成定义补充
- 若你发现实现与设计文档口径冲突，**在报告里写明冲突点与你的取舍**，不要默默按自己想法改。
