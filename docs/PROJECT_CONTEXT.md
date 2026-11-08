# Lion-Monitor 项目上下文

锂电池产线在线监测系统（工程建于 2026-10-28）。

> 说明：项目级 `AGENTS.md` 需马哥授权才能写入，暂以本文件承载上下文。

## 领域背景

- 监测对象：锂电池生产线（具体工段待确认：前段涂布/辊压/分切、中段卷绕/叠片、后段化成/分容/分选等）
- 典型监测量：面密度、厚度、辊压压力、张力、温度、电压、电流、内阻、容量
- 目标能力：实时采集 → 特征提取 → 异常/质量判定 → 报警与批次追溯

## 数据来源

- 现场数据目录（Windows）：`D:\hermes_wp\Project\锂电产线\envision\data`
- WSL 路径：`/mnt/d/hermes_wp/Project/锂电产线/envision/data`
- 文件命名：`<日期> <时间>-<通道>_<任务>.dat`，如 `2026-10-10 14_51_51-CH3_Task1.dat`
- 文件结构：512 B 头部 + float32 LE 样本；采样率与点数在头部内（0x110 / 0x0F0）
- 读取接口：`lion_monitor.ingest.read_vibration` / `read_vibration_header` / `scan_vibration_dir`

## 技术栈

- Python 3.11+，依赖管理 `uv`，源码在 `src/lion_monitor/`
- 数据处理：pandas / numpy；配置：pydantic v2 + YAML
- 测试：pytest（`testpaths=tests`, `pythonpath=src`）；代码风格：ruff（line-length 100）

## 必须遵守的工程约定

1. **先计划后编码**：涉及多文件改动，先写方案再动手。
2. **TDD**：新增功能先写失败测试，再实现，最后重构。
3. **配置外置**：产线编号、采样频率、报警阈值、资产清单一律放 `configs/`，禁止硬编码进业务代码。
4. **数据分层**：`data/raw/` 只读原始数据；`data/interim/` 清洗中间结果；`data/processed/` 建模数据。
5. **可追溯**：任何报警/判定结果必须能回溯到原始数据与所用阈值配置版本。
6. **真实执行**：结论必须来自实际运行输出，禁止编造指标。

## 常用命令

```bash
uv venv && uv pip install -e ".[dev]"   # 首次安装
uv run pytest                            # 测试
uv run ruff check src tests              # 静态检查
uv run ruff format src tests             # 格式化
```

## 待确认事项（阻塞后续设计）

- [ ] 产线工段范围（前段 / 中段 / 后段 / 全流程？）
- [ ] 数据来源与接入方式（PLC OPC-UA / MES 数据库 / CSV 导出 / 现场总线？）
- [ ] 采样频率与数据规模（点数/天、通道数）
- [ ] 输出形态（Web 实时看板 / 报警推送 / 报表 / 离线批量分析？）
- [ ] 报警判定规则是否已有企业标准或经验阈值
- [ ] 是否需要与现有 MES / QMS / PDM 系统对接
