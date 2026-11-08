# 贡献指南

感谢参与 Lion-Monitor。这个项目是 Windows 单机应用（Streamlit + SQLite），下面是最小必要的开发约定。

## 环境

- Windows 10 / 11
- Python 3.11
- 可选：Inno Setup 6（只在打安装包时需要）

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-win.txt
```

## 启动

```bat
python run.py run app\main.py
```

浏览器打开 <http://localhost:8501>。

> `run.py` 是唯一服务入口：它负责 Windows 事件循环策略和统一日志。不要再用
> `python -m streamlit run` 作为启动命令，否则可能触发 Windows Proactor 的 accept 断链问题。

## 测试与静态检查

```bat
pip install pytest pytest-cov ruff
python -m pytest tests -q
python -m ruff check src app tests tools build.py conftest.py run.py
```

CI 会在 Windows 上自动跑同样的 pytest + ruff。

## 代码约定

- Python 3.11，类型标注可用 `from __future__ import annotations`。
- 启动器 `launcher/LionMonitor.cs` 只能写 **C# 5 语法**（`csc.exe` v4.0.30319 不支持
  `$"..."`、`nameof`、`?.`、`out var`）。
- 批处理/`.iss`/`.cs` 保持 CRLF，Python 保持 LF（见 `.gitattributes`）。
- 日志统一走 `lion_monitor.logging_setup`，主日志是 `logs/lion-monitor.log`；
  新增第三方输出时先确认不会造成重复写或递归。
- 产线相关参数放 `configs/`，数据接入目录用 `LION_SCAN_DIR` 覆盖。

## 不要提交的内容

以下内容已在 `.gitignore` 中，不要强行加入：

- `dist/`、`wheels/`、`.build/`、`installer/output/`（构建产物/离线依赖）
- `logs/`、`data/lion.db*`、`data/backups/`、演示原始数据
- `docs/`、`reports/`（内部文档与评审产物，仅本地保留）

## 提交前检查清单

- [ ] `python -m pytest tests -q` 通过
- [ ] `python -m ruff check ...` 通过
- [ ] 改运行入口：`health` + 首页 + 真实浏览器会话都正常
- [ ] 改日志：真实启动一次，确认无重复、无半行、无英文长警告
- [ ] 改安装/启动器：在临时目录静默安装 → 启动 → 卸载验证，不碰生产目录
- [ ] 移除内部目录后：模拟干净克隆（临时改名 `docs/`、`reports/`）再跑全量测试
- [ ] 更新了相关 README/测试，而不是只改代码

## 提交信息

建议使用中文、动词开头的短标题（与现有历史一致），例如：

```text
修复 Windows accept 断链导致的服务假死
docs: 精简公开仓库并补充界面截图
```

PR 描述里写清楚：**改了什么、为什么改、怎么验证的**（命令/截图/日志片段）。
