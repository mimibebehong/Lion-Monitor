# Windows 端开发说明（Windows 10 / 11 only）

> 2026-11-05 完成 Windows 适配与打包验证：便携 Python 3.11.9 + C# 启动器 +
> Inno Setup 6 安装包已在 Win10 上编译、安装、启动、卸载跑通。
> 本目录不再依赖 WSL / `/mnt` / `/proc` / PowerShell 构建。

## 一、哪些能改，哪些别动

| 路径 | 说明 |
|---|---|
| `app/` `src/` `tools/` `tests/` `configs/` | 代码 |
| `launcher/LionMonitor.cs` | 启动器源码。只能写 **C# 5 语法**（csc.exe v4.0.30319 不支持 `$"..."`、`nameof`、`?.`、`out var`） |
| `installer/LionMonitor.iss` + `ChineseSimplified.isl` | Inno Setup 6 安装脚本与中文语言文件（6.7 默认不带中文） |
| `build.py` / `build.bat` | 构建入口（纯 Python + 批处理，不依赖 PowerShell） |
| **`dist/`** | 绿色包：便携运行时 + 代码 + `LionMonitor.exe` |
| **`wheels/`** | 60 个 Windows 离线 wheel，装依赖全程不联网 |
| **`.build/`** | 便携 Python 压缩包、get-pip.py、NSSM 压缩包、构建日志 |

## 二、跑起来看界面

```bat
:: 绿色包（推荐，和装完后的行为一致）
dist\LionMonitor.exe

:: 开发时直接跑源码（用便携运行时）
dist\runtime\python.exe -m streamlit run app\main.py --server.port 8501
```

浏览器打开 `http://localhost:8501`。第二次双击 `LionMonitor.exe` 只会再开一个浏览器标签，
不会再起第二个服务（单实例判断已改为 `/_stcore/health`）。

## 三、跑测试

便携运行时带 `python311._pth`，会忽略 `PYTHONPATH`；仓库根的 `conftest.py` 已把
`src/`、仓库根显式加进 `sys.path`，所以直接这样跑：

```bat
dist\runtime\python.exe -m pip install pytest pytest-cov ruff
dist\runtime\python.exe -m pytest tests -q
dist\runtime\python.exe -m ruff check src app tests tools build.py conftest.py
```

> 打包前记得把 pytest/ruff 卸掉，避免它们被 `pip freeze` 写进 `deps.lock`。

## 四、构建

```bat
build.bat                             :: 完整构建（离线 wheels + 便携运行时，空库安装包）
build.bat --skip-runtime --skip-deps  :: 只换代码 + 编启动器，约 1 分钟
build.bat --no-installer              :: 只到 dist\，不打安装包

:: 演示版安装包（带三批现场数据，别人装完打开就是有数据的工作台）
build.bat --skip-runtime --skip-deps --demo-data "D:\code_python\Project\Lion-Monitor\data\四通道保存"
```

- 日志：`.build\build-<时间戳>.log`
- 绿色包：`dist\`（含 `.streamlit\config.toml`、`runtime\`、`app\`、`src\`）
- 安装包：`installer\output\LionMonitor-Setup-0.1.0.exe`
- 打安装包需要 Inno Setup 6（winget 装在
  `%LOCALAPPDATA%\Programs\Inno Setup 6`，`build.py` 会自动找到）
- 构建最后会跑 12 项环境自检；核心依赖缺失会**直接中止**，不会再打出半成品
- `--demo-data` 会把三批 `.dat` 复制到 `dist\data\四通道保存`，并调用
  `tools\relocate_demo.py` 把库里的绝对路径改到绿色包目录；安装时安装器会再改一次到
  `{app}\data\四通道保存`，所以装到任何目录，原始波形/数据分析页都能读到文件
- 演示版安装包会把 `data\lion.db` 和三批数据一起压进去；要正式空库安装包就再跑一次
  不带 `--demo-data` 的构建（会覆盖同名 `installer\output\LionMonitor-Setup-0.1.0.exe`，
  想留两份的话先把演示版改名）
- 安装向导：欢迎 → 安装说明 → 选择目标位置 → 附加任务 → 确认 → 完成；
  `UsePreviousAppDir=no` 保证每次都从默认目录开始，路径框可直接输入或点「浏览...」；
  `InitializeWizard` + `CurPageChanged` 把窗口固定到主屏 (100,100)，
  避免多显示器/远程桌面下向导跑到副屏或屏幕外，导致「浏览」「下一步」点不到
- 卸载向导会提示「程序文件删除，data/logs/backup 默认保留」

## 五、这次修掉的 Windows 问题

| 问题 | 修法 |
|---|---|
| `DEFAULT_SCAN_DIR` 是 `/mnt/d/...` WSL 路径 | 改为 Windows 路径 `D:\hermes_wp\Project\锂电产线\envision\data\四通道保存`，可用 `LION_SCAN_DIR` 覆盖 |
| `ui_staleness` 只读 `/proc/<pid>/stat` | Windows 改用 `GetProcessTimes`（ctypes），代码比进程新时能正常提示重启 |
| `check_port` 在 Windows 用 `SO_REUSEADDR` 会误判空闲 | Windows 改用 `SO_EXCLUSIVEADDRUSE` |
| 启动器用首页 HTML 判断单实例（Streamlit 1.66 SPA 空壳） | 只看 `/_stcore/health` 返回 200 |
| `win_picker` 写死 WSL `/mnt/c` 路径 | Windows 原生 csc / `%PUBLIC%` 分支；网页端仍用 `dir_browser` |
| `build.py` 没拷 `.streamlit` | 已加入复制清单（否则主题变默认亮色） |
| Inno 6.7 不带 `ChineseSimplified.isl` | 语言文件随 `installer\` 一起走 |
| 安装器每次装完误报「自检有问题」 | 自检写 `logs\selfcheck.status`，只有退出码 1 才弹窗 |
| `.iss` 的 `[Code]` 里用 `;` 注释、`String` 接 `LoadStringFromFile` | 改为 Pascal 的 `//` 与 `AnsiString` |
| NSSM 服务参数里带绝对路径引号，安装目录有空格会起不来 | 用 `AppDirectory` + 相对 `app\main.py` |

## 六、验收记录（2026-11-05，本机 Win10）

- `pytest`：全绿（含真实四通道数据用例）
- `ruff`：全绿
- 源码界面：工作台 / 数据接入 / 目录浏览（C:、D: 盘符与三个批次目录）正常
- 现场数据：3 个批次 × 4 通道导入成功，生成 3 个数据集、360 个指标值；
  CH2 `a_Peak` 约 2.26 g（输入 2.20 gpk @235 Hz，符合）
- 启动器：首次启动 6 秒左右出健康检查；**第二次双击不再起第二个服务**
- 安装包：静默安装到 `D:\...\_install_test` 成功；防火墙规则、自检、卸载均通过；
  卸载后 `data\lion.db` 保留
- 演示版安装包（最终版 2026-11-06 10:20 构建，165.2 MB，SHA256
  `0891C2D2B4AE73EA70BD57F1D95B1D8719F594E056587BBB9C956A479907535A`）：
  已作为 v0.1.0 发布版复制到 `D:\code_python\Project\Lion-Monitor\`（同目录附
  `LionMonitor-0.1.0-发布说明.txt`、`LionMonitor-用户使用手册-v0.1.0.pdf/.md`）；
  含新图标（EXE / 安装包 / 快捷方式 / 浏览器标签页）、用户手册（装进 `{app}\docs`
  并加开始菜单快捷方式），以及健康树状态修复、首屏提速、树片段刷新、批量工具按需渲染、
  趋势页签按需渲染、换页回到默认状态和自检改进；编译日志里 `data\lion.db` +
  `data\四通道保存`（13 个文件）+ `docs\用户使用手册.pdf` + `tools\relocate_demo.py`
  都在打包清单内
- 自检改进（20:31 后）：8501 上跑的就是 Lion-Monitor 自己时判「通过」而不是
  「端口被占用」；控制台 GBK 也不会因 ✅/❌ 抛 UnicodeEncodeError；
  装到 `D:\software\LionMonitor` 实测自检 **11 项通过 / 0 失败 / 1 跳过**
- 安装/卸载向导：`UI Automation` 读取目标位置页 —— 路径输入框 (198,456,709x40)、
  「浏览(R)...」(928,454,160x49)、「下一步(N)」(811,856,160x49)，全部落在 1536x960
  主屏内，可输入也可浏览；窗口位置修复版本已用同一份 `.iss` 编译验证
  （最后一版带演示数据的安装包在 18:41 编译，唯一差异是 `[Code]` 的窗口定位，
  已验证通过；带演示数据的 E2E 静默安装在 18:21 那版上跑通）
- NSSM 服务：注册后 `sc query LionMonitor` = RUNNING，`/_stcore/health` = 200；
  停止/注销正常

## 七、性能实测与热点（2026-11-05 晚）

### 7.1 这次修的两个问题

| 问题 | 根因 | 修法 |
|---|---|---|
| 阈值都配好了，健康树节点还是显示「未设阈值」 | `app/pages/health_tree.py` 把树快照丢给 `tree_ui.render_tree` 时**没有注入 `state`**，`tree_ui.state_of` 只能回落成 `unset`（趋势页注入了，所以那边正常） | 新增 `_apply_health()`：用 `features.health.node_health` 算真实状态后递归写进快照；指标节点按最新值判级，设备/对象/通道沿子树汇总 |
| 点页面要等好几秒 | 首屏白等在 `app/ui.py` 顶层 `import pandas`（1.1 s），而 `ui` 被每个页面 import | 改成 `TYPE_CHECKING` 只做类型标注；实测首屏脚本耗时 **3132 ms → 1236 ms** |

修复后演示库（任务 `TT-20261029-01`）实测：22 个节点 = **10 正常 / 7 严重 / 5 未设阈值**
（CH4 三个指标本来就没配阈值，显示「未设阈值」是对的）。

### 7.2 分页面耗时（本机 Win10 / 1536×960，演示库）

服务端脚本耗时来自 `LION_PROFILE=1` 时 `logs/lion-monitor.log` 的 profile 行；
浏览器耗时是点了导航到页面标题出现（点第二遍，热态）。

| 页面 | 服务端脚本（热） | 浏览器点击到出内容（热） | 主要热点 |
|---|---|---|---|
| 工作台 | 91–184 ms | 244–478 ms | 5 段 KPI + 两张卡片，SQL 56 条 |
| 任务列表 | 73–274 ms | 333–685 ms | 表格 + 页签 |
| 健康树配置 | 517–802 ms | 533–655 ms | 树 22 行 × 4 列 = 88 列 / 66 按钮 / 22 复选框，DOM 731 个节点 |
| 阈值设置 | 64–119 ms | 425 ms | 表单 + 表 |
| 数据接入 | 352–787 ms | 964–1661 ms | 3 批次 × 4 通道展开块 |
| 趋势分析 | 938–1537 ms | 952 ms（首次 14.2 s） | 健康树渲染 ≈210 ms + 4 条曲线取数/建图；**首次要下载并初始化 plotly.js** |
| 数据分析 | 184 ms（首次 4.3 s） | 961 ms | 首次导入 matplotlib + 读 .dat |
| 数据集 / 备份 / 回收站 | 79–128 ms | 386–1269 ms | 表格 |

### 7.3 结论：慢在哪一层

1. **架构层（占大头，0.3–0.8 s/次）**：Streamlit 每次点导航都整页重跑
   （`main.py` 的侧边栏 + 整页脚本），前端再把整棵元素树重建一遍。
   健康树/趋势这类页面一次要建 731 个 DOM 节点，重建成本就摆在那。
2. **页面脚本层（0.06–1.5 s）**：趋势分析（树 + 取数 + 4 张 plotly 图）、
   健康树（22 行 × 4 列的控件）、数据接入（批次展开块）最重。
   数据库不是瓶颈：12 个指标取数 18 ms、全树算健康 29 ms、全任务树快照 1 ms；
   趋势页一次刷新 158 条 SQL 大部分是逐节点 `resolve_limit`（N+1），合计也就几十 ms。
3. **首次访问层（最高 14 s）**：`pandas` 1.1 s、`matplotlib`/`scipy` 若干秒、
   plotly.js 前端包首次加载，都只在各自页面**第一次**打开时付一次。

### 7.4 第二轮优化（2026-11-05 21:26，已进发布包）

| 改法 | 为什么 | 实测 |
|---|---|---|
| 健康树包进 `st.fragment`（`tree_ui.render_tree(fragment=True)`） | 展开/折叠、勾设备只重画树，不再整页重建（右侧面板、KPI、侧边栏都不动） | 树 DOM 210–265 个节点 vs 整页 705；折叠/展开 0.4–1.0 s |
| 换页清掉页面级状态（`app/nav_state.py`） | Streamlit 会把控件状态一直留着，从健康树点走再点回来还停在上次选中的节点/页签上，看着像没刷新 | 实测：选 CH2 → 去工作台 → 回健康树，选中节点回到根、树全展开；当前任务保留 |
| 健康树「批量工具」从 `st.expander` 改成开关 | **Streamlit 的 expander 折叠时也会执行里面全部代码**：导出模板要查库 + 十几个控件，每点一次树白算一次 | 页面脚本 190 ms → 157 ms（AppTest 同口径），DOM 少约 40 个节点 |
| 趋势分析两个页签加 `on_change="rerun"` | 打开「按需渲染」，不选中的「健康评级」不再每次重跑都算全任务超限记录 | 少一次全量 `metric_series` + 逐节点 `resolve_limit` |
| 首屏 `pandas` 懒加载 | `ui.py` 被每页 import，顶层 import pandas 要 1.1 s | 首屏 3132 ms → 1236 ms |

页面整体更轻：健康树展开态 `main` 里 705 个 `data-testid` 节点（上一版 893）。

### 7.5 还能继续压的方向（按性价比）
1. 健康树改成「表格式树」（`st.dataframe` + 单行选中）：一个控件替掉 33 个按钮，
   DOM 能再降一个数量级，代价是外观不再是经典文件树；
2. 右侧详情面板也做成独立 fragment，用 `st.rerun(scope="<fragment key>")`
   只刷「树 + 详情」两块；
3. 逐节点 `resolve_limit` 改批量 `limits.resolve_limits`（趋势页还没用上）；
4. 默认只展开前两层（首屏树行 22 → 5），要点开才看细节；
5. 启动器里预热 `import pandas`，首屏再省约 1 s。

### 7.6 品牌与文档（2026-11-06）

| 改动 | 说明 |
|---|---|
| 软件图标 | 新增 `installer/lion-icon.png`（母图）+ `installer/lion.ico`（16/24/32/48/64/128/256 七种尺寸）；`build.py` 用 `/win32icon` 编进 `LionMonitor.exe`，`.iss` 用 `SetupIconFile=lion.ico` 给安装包/卸载项，`app/assets/lion_icon.png` 作浏览器标签页图标 |
| 用户手册 | `docs/用户使用手册.md`（源）+ `docs/manual/用户使用手册.pdf`（15 页，headless Edge 打印）；`build.py` 把 `docs\manual` 拷到 `dist\docs`，`.iss` 加「用户使用手册」开始菜单快捷方式 |
| 向导补充 | `安装说明.txt` / `安装完成.txt` 里加了「手册在哪」的提示 |

生成手册（改了 md 之后重跑这一条）：

```bat
dist\runtime\python.exe -m pip install markdown   :: 只构建机器需要，不进发行包
dist\runtime\python.exe tools\build_manual.py     :: 出 .build\manual.html + docs\manual\用户使用手册.pdf
```

PDF 由 Windows 自带 Edge 的 headless 打印，不额外引入依赖；只想看排版加 `--html-only`。

## 八、数据（两个 data 目录别搞混）

| 位置 | 内容 | 什么时候用 |
|---|---|---|
| **根 `data\lion.db`** | 开发调试库（本次验收导入过 3 个批次） | 在根目录跑源码时用 |
| `dist\data\` | 绿色包自己的库；**打安装包前只允许留 `lion.db`**（`clean_build_artifacts()` 会清、`_assert_dist_data_clean()` 会拦），残留的东西会被安装器记录、卸载时删掉 | 绿色包演示用 |
| `dist\demo_data\` | 演示数据 + 演示库，**安装包唯一的演示数据来源**；装完由 `tools\seed_demo.py` 新建到 `{app}\data\` | 安装包演示用 |
| `installer\output\LionMonitor-Setup-*.exe` | 安装包；分发版带演示数据（`--demo-data`），装完 `data\` 有 3 数据集 / 12 通道 / 30 条超限 | 演示 / 直接分发 |
| `D:\code_python\Project\Lion-Monitor\data\四通道保存` | **现场测试数据**（3 个批次，12 个 .dat） | 验收 / 演示用，`LION_SCAN_DIR` 指到这里 |

两个库是独立的：在根目录导入的数据不会出现在 `dist\` 里。发布前想重置绿色包，
删掉 `dist\data\lion.db*` 即可；想 100% 还原你原来的工作台，把 WSL/开发机上的
`data/lion.db` 覆盖到 `dist\data\lion.db` 再重启即可。

## 九、v0.1.1 故障复盘：同事机器「Internal Server Error」

### 9.1 现场与根因

同事机器装 11-06 10:20 那版，打开是 Streamlit 的 `Internal Server Error`，
但 selfcheck 能连上 8501（health 200）——进程活着、页面渲染不出来。两个根因：

1. **`Excludes: "data\*"` 误伤 plotly（致命）**
   Inno 的 `Excludes` 按「路径里任意一段叫这个名字」匹配，不是只匹配顶层 `data\`。
   `data\*` 把 `plotly\graph_objs\layout\template\data\` 下 79 个文件一起排掉了，
   装完 `import plotly` 直接
   `AttributeError: module 'plotly.graph_objs.layout.template.data' has no attribute 'Candlestick'`
   → 服务起不来 / 首页 500。11-05 21:51 那版没有这条 Excludes，所以正常。
2. **装/升级时没停旧实例**
   旧实例（含开机自启服务）还在跑就覆盖 `app\`、`src\`、`runtime\`、`data\lion.db`，
   文件变成一半，症状一样。现场日志的「8501 已有 Lion-Monitor 在运行」就是这条。

同时修掉两个隐性问题：

3. **演示数据没装进去**：`demo_data` 被主 `[Files]` 排除，却没补独立条目 →
   装完空库（工作台一片空，看着像「界面不一样」）。现在有单独一条
   `Source: "..\dist\demo_data\*"; DestDir: "{app}\demo_data"`。
4. **卸载删数据**：`dist\data` 里残留的演示 `.dat` 被安装器记了一笔，卸载照删。

### 9.2 加固清单（都在 v0.1.1 里）

| 位置 | 做了什么 |
|---|---|
| `installer\LionMonitor.iss` | `PrepareToInstall` → `StopRunningInstance()`：升级用旧运行时跑 `tools\stop_app.py`；首次装新目录用纯 Inno 的 `StopByPort()`（netstat → tasklist 核对映像名 → taskkill）。`InitializeUninstall` 同样先停实例 |
| `tools\stop_app.py` | 只用标准库 + 系统命令（netstat/tasklist/taskkill/sc）；**不碰 nssm**（其清单 requireAdministrator，非管理员会弹 UAC 卡死静默安装），不调 PowerShell/WMI（现场这两种都失灵） |
| `src\lion_monitor\deploy\selfcheck.py` | `--expect-stopped` 安装期口径；`check_program_files()` 10 个关键文件；`check_runtime_import()` 子进程**真 import** streamlit+plotly（`find_spec` 抓不到「装了一半」） |
| `launcher\LionMonitor.cs` | 启动后不只查 health，还查首页 `/`；非 200 给出「程序文件可能被覆盖坏，请重装」；写 `logs\server.pid`（无 BOM） |
| `tools\collect_logs.py` + 开始菜单「收集诊断日志」 | 一个 txt 汇总全部日志（见 9.4） |
| `build.py` | `clean_build_artifacts()` 清空 `dist\data`（演示版只留 `lion.db`）；`_assert_dist_data_clean()` 编译前拦截残留，宁可构建失败也不打「卸载会丢数据」的包 |

### 9.3 验收记录（2026-11-06 18:38–19:02，本机 Win10）

| 场景 | 结果 |
|---|---|
| 单测 / 静态检查 | `1378 passed, 5 skipped`；`ruff All checks passed` |
| 首次装到新目录，旧实例从 `dist\` 占着 8501 | `install_stop.log`：`结束占用端口 8501 的进程 pid=…（python.exe）` → 端口腾出；selfcheck 12 通过 / 1 警告（per-user 无防火墙）/ 0 失败 |
| 升级覆盖**运行中**的安装目录 | 按 `logs\server.pid` 结束进程 → 端口释放 → selfcheck 0 失败；`data\` 原样保留 |
| 装完启动 | health 200 + 首页 200；工作台 任务 1 / 对象 3 / 超限 30；`data\` 有 13 个 `.dat` + 演示库 |
| plotly 完整性 | 安装目录 `runtime\...\template\data\` 48 个 `.py` 齐全 |
| 卸载 | `data\` 16 个文件全在，卸载日志 0 条 `data\` 删除记录；程序目录清空 |

### 9.4 一键收日志

开始菜单 → Lion-Monitor → 「收集诊断日志（发给技术支持）」→
桌面生成 `LionMonitor-诊断-<日期>-<时间>.txt`，内含：系统信息、health/首页状态码、
netstat + tasklist + `server.pid`、关键文件完整性、`config.toml`、`selfcheck.status`、
6 个日志的末尾 400 行。示例见发布目录 `LionMonitor-诊断日志-示例-v0.1.1.txt`。
