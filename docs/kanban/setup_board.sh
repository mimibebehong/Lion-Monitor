#!/usr/bin/env bash
# 建立 Lion-Monitor 看板 + 11 个任务 + 顺序依赖链，输出任务 ID 映射
set -euo pipefail

BOARD=lion-monitor
PROJ=/home/administrator/workspace/projects/Lion-Monitor
BODIES=$PROJ/docs/kanban/bodies

if ! hermes kanban boards list 2>/dev/null | grep -q " $BOARD "; then
  hermes kanban boards create "$BOARD" \
    --name "Lion-Monitor 待办" \
    --description "锂电产线振动监测软件：数据分析计算 + 页面补齐 + 评审遗留" \
    --icon "🦁" \
    --default-workdir "$PROJ" 2>&1 | tail -2
else
  echo "board $BOARD 已存在，跳过创建"
fi

mk() {  # mk <title> <bodyfile> -> id
  hermes kanban --board "$BOARD" create "$1" \
    --body-file "$2" \
    --assignee coding-copilot \
    --workspace "dir:$PROJ" \
    --created-by mark \
    --max-retries 1 \
    --json
}

declare -A ID
ID[A1]=$(mk "A1 原始波形绘图（区间读取+最小/最大包络抽样+CSV）" "$BODIES/A1.md" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
ID[A2]=$(mk "A2 FFT 频谱（去均值+窗函数+Peak/RMS+主峰+标记线+CSV）" "$BODIES/A2.md" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
ID[A3]=$(mk "A3 包络谱（带通+希尔伯特+去均值+标记线+CSV）" "$BODIES/A3.md" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
ID[C1]=$(mk "C1 工作台接真实库（去掉 demo_data）" "$BODIES/C1.md" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
ID[C2]=$(mk "C2 回收站接 deletion_log（支持恢复）" "$BODIES/C2.md" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
ID[C3]=$(mk "C3 健康树导出模板按钮启用" "$BODIES/C3.md" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
ID[D1]=$(mk "D1 删除死代码 store/thresholds.py" "$BODIES/D1.md" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
ID[D2]=$(mk "D2 指标常量收敛到单一来源" "$BODIES/D2.md" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
ID[D3]=$(mk "D3 数据集删除级联下沉到 store 层" "$BODIES/D3.md" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
ID[D4]=$(mk "D4 数据接入默认扫描路径移入配置" "$BODIES/D4.md" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
ID[D5]=$(mk "D5 台账改名同步健康树节点显示名" "$BODIES/D5.md" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

# 顺序链：前一个完成，后一个才 ready
order=(A1 A2 A3 C1 C2 C3 D1 D2 D3 D4 D5)
for ((i = 0; i < ${#order[@]} - 1; i++)); do
  hermes kanban --board "$BOARD" link "${ID[${order[$i]}]}" "${ID[${order[$((i + 1))]}]}" >/dev/null
  echo "link ${order[$i]} -> ${order[$((i + 1))]}"
done

echo "=== 任务 ID ==="
for k in "${order[@]}"; do echo "$k=${ID[$k]}"; done
{
  echo "BOARD=$BOARD"
  for k in "${order[@]}"; do echo "$k=${ID[$k]}"; done
} > "$PROJ/docs/kanban/.task_ids"
