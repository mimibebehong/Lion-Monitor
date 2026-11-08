# 任务：三处删除操作统一改为「二次点击确认」

项目：`/home/administrator/workspace/projects/Lion-Monitor`（**先 cd 到该目录**）

## 背景
马哥 2026-11-03 确认：coding-copilot 在 webui 会话里提出的建议——把**传感器页、阈值页、健康树**三处删除仍使用的「勾选框 + 禁用按钮」老写法，统一改成**二次点击确认**，与项目里已有的 `objects.py`、`datasets.py`、`metrics.py` 保持一致。

## 目标
1. **统一交互模式**（照抄 `app/pages/objects.py:275-315` 的 armed 模式）：
   - 第一次点击按钮 → 进入待确认态：按钮文案变「确认删除？」并变红（用已有 `danger_button_css` 辅助函数生成样式），`st.rerun()`
   - 第二次点击 → 真正执行删除，成功后清除 armed 状态
   - 删除按钮 `help` 写明「点两次删除：第一次进入待确认，第二次执行」
   - 切换选中行 / 切换页签 / 删除成功后必须重置对应 armed 状态，避免误删下一行
2. **传感器页 `app/pages/sensors.py`**：
   - 单台删除（现 :226 checkbox「确认删除这台传感器」+ 禁用按钮）
   - 批量删除（现 :257 checkbox「确认删除选中的 N 台传感器」+ 禁用按钮）→ 改为二次点击：第一次「删除选中的 N 台」→ 变红「确认删除 N 台？」→ 第二次执行；删除前仍显示「将删除：…」清单
3. **阈值页 `app/pages/thresholds.py`**：单行删除（现 :341 checkbox「确认删除该阈值」+ 禁用按钮）
4. **健康树 `app/pages/health_tree.py`**：删节点及子树（现 :849 checkbox「确认删除该节点及其子树」）→ 二次点击，第一次变红「确认删除该节点及子树？」，第二次执行
5. 三处都**移除**原 checkbox 及其 `disabled` 逻辑，不要保留两套。
6. 删除动作本身的业务逻辑**一行不改**（只改交互层），删除后的 store 调用、级联、留痕、flash 文案保持现状。

## 硬性要求
1. TDD：先写失败测试（AppTest 断言：未点击时无删除发生；第一次点击后出现「确认删除？」且未删除；第二次点击后记录消失；切换选中后 armed 复位），跑 RED 再实现 GREEN。
2. 一个页面一次提交，中文 commit：`fix(ui): 传感器页删除改二次点击确认（统一交互）` 之类。
3. `.venv/bin/python -m pytest`（不加 -q）必须全绿；**改动前先跑一次记录真实基线条数**，改完不低于该数。`ruff check` + `ruff format --check` 全过。
4. 只改上述三处交互，不动其他页面、不改业务逻辑、不放宽既有测试断言。
5. 证据写 `reports/2026-11-03-delete-confirm-unify-verification.txt`（含三次页面实跑/AppTest 真实输出、改动前后对比、pytest 条数）。

## 验收
1. 传感器页单台删除：点一次不变红不删，点两次才删（AppTest 断言）。
2. 传感器页批量删除：同上，且第一次点击后文案含「确认删除 N 台？」。
3. 阈值页：同上。
4. 健康树删节点及子树：同上，子树在第二次点击后才消失。
5. 三处均已无 checkbox 残留（`grep -n "确认删除" app/pages/{sensors,thresholds,health_tree}.py` 输出贴进报告）。
6. 全量测试全绿、ruff 全过、`git status` 干净。

## 汇报
中文汇报：改了哪些文件、提交 sha、pytest 条数（改前/改后）、三处验收逐条结果、未做项。
