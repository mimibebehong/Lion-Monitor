# Lion-Monitor

锂电池产线在线监测系统。

## 状态

- [x] 工程骨架初始化（2026-10-28）
- [x] 振动数据读取接口（`src/lion_monitor/ingest/vibration.py`）+ 现场样本集成测试
- [x] 存储层（SQLite `data/lion.db`）与 13 个页面全部接库；演示数据模块已清除
- [x] 数据接入 → 指标计算 → 阈值 → 趋势 → 健康评级 闭环跑通
- [x] 数据分析三页签（原始波形 / FFT 频谱 / 包络谱）接通，均可导出 CSV
- [ ] 6 项口径待拍板（见「需求 › 待确认」，清单与现状在
  `docs/plans/2026-10-30-剩余工作盘点.md`）

测试：Windows 便携运行时下 `python -m pytest` 全绿；`python -m ruff check ...` 全绿。

## 需求

### 业务目标

锂电池产线在线监测：实时采集 → 特征提取 → 异常 / 质量判定 → 报警与批次追溯。
当前只覆盖**已采集振动文件的离线分析**（实时采集与 PLC/OPC-UA 不在本期）。

- 监测对象：产线设备振动（示例为卷绕产线：隔膜过辊轴承、入片牵引电机、卷绕张力气缸）
- 产线工段范围（前段涂布 / 辊压 / 分切，中段卷绕 / 叠片，后段化成 / 分容 / 分选）**待马哥确认**

### 监测数据

- 现场数据目录（Windows）：`D:\hermes_wp\Project\锂电产线\envision\data`，
  数据接入页默认扫描 `…/data/四通道保存`；换机器/盘符用 `LION_SCAN_DIR` 覆盖，
  或在页面上点「浏览目录」重新选（选过会记住）
- 文件命名：`<日期> <时间>-<通道>_<任务>.dat`，如 `2026-10-10 14_51_51-CH3_Task1.dat`
- 文件结构：512 B 头部 + float32 little-endian 样本；采样率在 0x110、点数在 0x0F0
- 现场样本口径：32 kHz / 20 s / 640,000 点 / 传感器 500 mV/g
- 幅值单位：按文件头 `scale` 换算，**加速度统一用 g**；速度指标 `v_RMS` 输出 mm/s

### 功能需求（已实现）

| # | 需求 | 实现位置 |
|---|---|---|
| 1 | 测试任务：任务号 `TT-YYYYMMDD-NN`，任务下挂一棵健康树，可归档、软删 | 任务列表页 + `store/tasks.py` |
| 2 | 主数据台账：监测对象 / 通道 / 指标定义 / 传感器各自独立成台账，健康树只做引用装配 | 监测对象 / 传感器 / 指标配置页 + `store/*_reg.py` `metric_def.py` `sensors.py` |
| 3 | 健康树：任务 → 设备 → 监测对象 → 通道 → 监测指标；模板导出 / 导入（整树替换，含台账引用校验） | 健康树配置页 + `store/tree.py` |
| 4 | 数据接入：扫描目录 → 一个子目录 = 一个批次 = 一个数据集（多通道文件）→ 按健康树计算指标 | 数据接入页 + `features/pipeline.py` |
| 5 | 指标计算：`a_Peak = max\|x\|`、`a_RMS = sqrt(mean(x²))`、`v_RMS`（10–1000 Hz 带通 + 频域积分，mm/s） | `features/metrics.py` |
| 6 | 阈值：warn / alert 两级，配在「通道 × 指标」的指标节点上，读取时沿树向上继承 | 阈值设置页 + `store/limits.py` |
| 7 | 趋势与健康评级：每个指标值一个点（不聚合、不抽稀），越限点按 warn/alert 着色，节点状态取子树最差 | 趋势分析页 + `features/health.py` |
| 8 | 数据分析：①原始波形（区间读取，超上限按最小/最大包络抽样）②FFT（6 窗函数、Peak/RMS、主峰 ▲、目标频率与阶次竖线）③包络谱（巴特沃斯带通 + 希尔伯特 + 去均值），均可导出 CSV，支持叠加第二个数据集对比 | 数据分析页 + `features/waveform.py` `spectrum.py` `envelope.py` |
| 9 | 删除：全表软删除（`deleted_at`），写 `deletion_log` 留痕，回收站可恢复（节点带子树快照，保留期 7 天） | 回收站页 + `store/recycle.py` |
| 10 | 变更留痕：阈值 `warn/alert/方向`、指标参数覆盖与固化、指标定义默认值、节点转速的真改各写一条 `change_log`（实体/字段/旧值→新值/操作人/时间），值没变不记；阈值设置页「变更历史」可查 | 阈值设置页 + `store/audit.py` |

页面清单（13 页 / 4 组）：工作台｜任务列表、健康树配置、监测对象、传感器、指标配置、阈值设置｜数据接入、趋势分析、数据集管理、数据分析｜数据库备份、回收站。

### 非功能需求

- **单人单机**：SQLite 单文件 + `streamlit run` 本机启动，不引入用户 / 权限体系
- **TDD**：新增功能先写失败测试，再实现；结论以真实 pytest 输出为准
- **配置外置**：产线相关参数尽量落 `configs/`（数据接入扫描目录可用 `LION_SCAN_DIR` 覆盖）
- **可追溯**：任何判定都能回溯到原始 `.dat` 文件、所用指标参数与阈值

### 不在本期范围

实时在线采集（PLC / OPC-UA）、故障诊断与寿命预测模型、用户权限 / 多租户、
CSV / MAT 等其它数据格式（仅留接口）。

### 待确认（需马哥拍板，暂不实现）

| ID | 事项 |
|---|---|
| B3 | 指标计算参数（如 `v_RMS` 的 10–1000 Hz 带通）配置生效方式 |
| E1 | 有效性业务判据（直流偏置 / 饱和率 / 峭度区间）的阈值 |
| E2 | 传感器量程口径（方案已定按 g，待马哥点头确认） |
| E3 | 阈值告警是否外推（邮件 / 弹窗 / 微信） |
| E4 | 回收站保留期（是否 30 天自动清空） |
| E5 | 批次分组口径（同站点 + 时间戳近似） |
| E6 | 是否引入轴承特征频率库（BPFO / BPFI / BSF） |

## 快速开始（Windows 10 / 11，仅 Windows）

> 本项目已改为 Windows-only：路径、目录选择、进程启动时间、打包链路都按 Windows 实现。

### 1. 直接用便携运行时跑起来

```bat
:: 首次：解压便携 Python + 离线装依赖（全程不联网）
build.bat --no-installer

:: 之后：只换代码，秒级
build.bat --skip-runtime --skip-deps --no-installer

:: 启动（绿色包；内部是 run.py，Selector 事件循环 + 统一日志）
dist\LionMonitor.exe
```

浏览器自动打开 `http://localhost:8501`。

服务日志统一在 `logs\lion-monitor.log`（10 MB × 5 轮转）：启动信息、Streamlit
异常、页面未捕获异常都在这一个文件；`logs\launcher.log` 只记启动器，`logs\server.log`
只在 `run.py` 起不来时留兜底报错。

### 2. 跑测试 / 静态检查

```bat
dist\runtime\python.exe -m pip install pytest pytest-cov ruff
dist\runtime\python.exe -m pytest tests -q
dist\runtime\python.exe -m ruff check src app tests tools build.py conftest.py
```

### 3. 打 Inno 安装包

安装 [Inno Setup 6](https://jrsoftware.org/isdl.php) 后：

```bat
build.bat
:: 产物：dist\（绿色包）
::       installer\output\LionMonitor-Setup-<版本>.exe（安装包）
```

安装包会：允许用户选安装目录 → 放行防火墙 8501（专用网络）→ 环境自检 →
可选注册 NSSM 开机自启服务 → 创建桌面/开始菜单快捷方式（含「用户使用手册」PDF）。卸载保留 `data\`、`backup\`、`logs\`。

### 4. 重新生成用户使用手册

手册源文件 `docs/用户使用手册.md`，安装包里带的是 `docs/manual/用户使用手册.pdf`：

```bat
dist\runtime\python.exe -m pip install markdown      :: 只构建机需要，不进发行包
dist\runtime\python.exe tools\build_manual.py         :: 出 .build\manual.html + docs\manual\用户使用手册.pdf
```

PDF 由 Windows 自带 Edge 的 headless 打印生成，不引入额外依赖。

### 5. 图标

- 母图 `installer/lion-icon.png`（768×768 透明 PNG）
- `installer/lion.ico`：16/24/32/48/64/128/256 七种尺寸，`build.py` 用 `/win32icon` 编进启动器，
  `.iss` 用 `SetupIconFile=lion.ico` 给安装包
- 网页标签页图标 `app/assets/lion_icon.png`（128 px，由 `app/main.py` 的 `page_icon` 引用）

## 目录结构

```
Lion-Monitor/
├── src/lion_monitor/     # 主包
│   ├── ingest/           # 数据接入（envis .dat 读取与目录索引）
│   ├── features/         # 特征与业务逻辑（指标 / 波形 / 频谱 / 包络 / 健康 / 导入管线 / 概览）
│   ├── store/            # SQLite 存储层（任务 / 健康树 / 台账 / 阈值 / 数据集 / 指标值 / 回收站）
│   ├── viz/              # 绘图（Plotly 波形与幅值谱、matplotlib 四联概览）
│   ├── detect/ alert/ storage/ api/   # 骨架，待实现
│   └── config.py         # 配置模型（pydantic）+ 路径默认值
├── app/                  # Streamlit 前端（main.py + 13 个页面 + 共用 UI/树渲染 + assets 图标）
├── tests/                # 单元 / 集成测试
├── configs/              # YAML 配置文件（产线、阈值、资产清单）
├── data/                 # lion.db + raw → interim → processed 三级数据区
├── scripts/              # 运维/批处理脚本（起服务、巡检、出图、装 venv）
├── notebooks/            # 探索性分析
└── docs/                 # 方案、接口说明、代码评审、看板；manual/ 是对外用户手册
```

## 振动数据读取接口

现场采集文件格式：**512 B 头部 + float32 little-endian 样本**，字段映射见
`docs/plans/2026-10-28-振动数据读取接口.md`（由实测样本反推，非文档推测）。

```python
from lion_monitor.ingest import read_vibration, read_vibration_header, scan_vibration_dir

meta = read_vibration_header(path)   # 只读 512 B 头部，批量索引用
record = read_vibration(path)        # 元数据 + 样本
record.samples                       # ndarray[float32]，只读视图
record.time_axis()                   # 相对时间轴（秒）
record.duration_s                    # 时长（秒）
scan_vibration_dir(directory)        # 目录索引，按起始时间升序
```

命令行巡检：

```bash
.venv/bin/python scripts/inspect_vibration.py "D:\hermes_wp\Project\锂电产线\envision\data"
```

现场样本实测（`2026-10-10 14_51_51-CH3_Task1.dat`）：CH3、32 kHz、320,000 点、10.0 s、
幅值 −0.868…+0.876、RMS 0.618；幅值单位按头部 `scale` 换算（导入后统一为 g）。

## 振动数据绘图

四联概览图：全时峰值包络（含 RMS 曲线、放大区间标记）/ 峰值附近局部放大 / 幅值谱（前 3 峰标注）/ STFT 时频图。

```python
from lion_monitor.ingest import read_vibration
from lion_monitor.viz import plot_vibration_overview, save_vibration_overview

record = read_vibration(path)
fig = plot_vibration_overview(record)              # 返回 Figure，可继续嵌入报告
save_vibration_overview(record, "reports/figures/x.png")  # 直接落盘，返回路径
```

只取频谱与峰值（用于阈值/特征）：

```python
from lion_monitor.viz import amplitude_spectrum, stft_db, top_peaks

freqs, amp = amplitude_spectrum(record.samples, record.meta.sample_rate_hz)
top_peaks(freqs, amp, count=3)      # [(频率 Hz, 幅值), ...]，按幅值降序
times, s_freqs, db = stft_db(record.samples, record.meta.sample_rate_hz)
```

命令行批量出图（PNG 不入库，可随时重生成）：

```bash
.venv/bin/python scripts/plot_vibration.py "D:\hermes_wp\Project\锂电产线\envision\data"
```

现场样本实测结论：CH3 通道 10 s 稳态信号，主峰 **3001 Hz**（幅值 0.564），
谐波 6002 Hz / 9004 Hz 依次衰减，RMS 全程平直在 0.618 → 稳态运行、无冲击或调幅迹象。

## 运行监控前端

```bash
.venv/bin/python run.py run app/main.py
# 默认 http://localhost:8501；run.py 会统一切事件循环策略并初始化日志
```

13 个页面全部读 `data/lion.db`：工作台、任务列表、健康树配置、监测对象、传感器、
指标配置、阈值设置、数据接入、趋势分析、数据集管理、数据分析、数据库备份、回收站。
空库首次启动**不播种**示例任务（要示例树在健康树页点「恢复内置示例树」，空库时该页直接给这个按钮）；
页面在库里没有对应数据时给空态提示，不回落演示数据。

页面清单与各页设计见 `docs/plans/2026-10-28-监测软件方案与UI设计.md`，
剩余工作与待拍板事项见 `docs/plans/2026-10-30-剩余工作盘点.md`。

## 运维：数据库备份与恢复

整库只有一个文件 `data/lion.db`（WAL 模式），备份/恢复就是对这个文件做在线快照。
**不要用 `cp` 直接复制**：WAL 模式下新数据还在 `lion.db-wal` 里，复制主体文件会拿到半截库。
统一走 `sqlite3.Connection.backup()`（与 SQLite CLI 的 `.backup` 同一个 API，读写并发时也能拿到一致快照）。

```bash
# 手动备份（默认写到 data/backups/，保留最近 10 份）
.venv/bin/python scripts/backup_db.py

# 指定库/目录/保留份数
.venv/bin/python scripts/backup_db.py --db data/lion.db --backup-dir /mnt/d/lion-backups --keep 3
```

文件名 `lion-YYYYmmdd-HHMMSS.db`（同一秒内多次备份自动加 `-2` `-3` 序号，不覆盖已有文件），
命令会打印备份路径、字节数与可读大小；失败时非零退出并把中文原因写到 stderr。

备份口径：

- **目录**：默认 `<库文件所在目录>/backups/`，可用环境变量 `LION_BACKUP_DIR` 覆盖
  （现场建议指到 `D:\` 等非系统盘，`data/backups/` 不进版本库）。
- **保留**：默认 10 份，`--keep N` 或页面「保留份数」可改；超出的按文件时间删最旧的。
- **迁移前自动备份**：`store/db.py::init_db` 每次启动先探测「这次是否要改结构」
  （在内存影子库预演同一份迁移，真库只读），要改就先备份整库再迁移；
  **备份失败则中止迁移并抛中文错误**（`迁移中止：迁移前自动备份失败（…）`），结构一个字节都不会改。
  结构已是最新时不会重复备份。
- **页面入口**：系统 › 数据库备份——「立即备份数据库」按钮 + 最近一次备份的时间/大小/路径 + 备份文件列表。
- **恢复**：停掉 `streamlit`（以及任何在写库的进程），把要恢复的
  `data/backups/lion-…db` 复制成 `data/lion.db`，删掉同名 `lion.db-wal` / `lion.db-shm` 再启动
  （启动时 `init_db` 会按需迁移，迁移前还会再自动备份一份）。

## 约定

- 代码风格：`ruff`（line-length=100）
- 测试：`pytest`，新增功能先写测试（TDD）
- 配置：外部化到 `configs/*.yaml`，不在代码里硬编码产线与阈值
