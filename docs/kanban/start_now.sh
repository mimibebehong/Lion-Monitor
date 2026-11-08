#!/usr/bin/env bash
# 18:00 之后启动 Lion-Monitor 看板（解除 A1 的 scheduled 停放）
# 手动启动也可以直接执行本脚本。
set -uo pipefail

BOARD=lion-monitor
FIRST=t_3f510981   # A1 原始波形绘图
LOG=/home/administrator/workspace/projects/Lion-Monitor/docs/kanban/start.log

echo "=== $(date '+%F %T') 解除停放 $FIRST ===" >> "$LOG"
hermes kanban --board "$BOARD" unblock "$FIRST" 2>&1 | tee -a "$LOG"
echo "=== 看板状态 ===" >> "$LOG"
hermes kanban --board "$BOARD" list 2>&1 | head -14 >> "$LOG"
