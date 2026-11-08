#!/usr/bin/env bash
# 起一个可复用的 Streamlit 服务（幂等：先停旧的，再起新的），供看板 worker 做页面实测。
# 用法： bash scripts/dev_serve.sh [端口，默认 8599]
# 退出码 0 = 服务已就绪；非 0 = 起不来（输出原因）
set -uo pipefail

PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8599}"
PIDFILE="$PROJ/.dev_serve.pid"
LOG="$PROJ/.dev_serve.log"

# 1) 停掉占用该端口的旧进程（精确到 PID，不用 pkill -f 全量匹配）
OLD="$(ss -ltnp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p {print $0}' | grep -o 'pid=[0-9]*' | cut -d= -f2 | head -1)"
if [ -n "${OLD:-}" ]; then
  kill "$OLD" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$OLD" 2>/dev/null || break
    sleep 0.3
  done
  kill -9 "$OLD" 2>/dev/null
fi
# 2) 停掉上一次本脚本起的进程
if [ -f "$PIDFILE" ]; then
  P="$(cat "$PIDFILE" 2>/dev/null)"
  [ -n "${P:-}" ] && kill "$P" 2>/dev/null
  rm -f "$PIDFILE"
fi

# 3) 起服务（setsid 脱离当前会话，worker 退出后依然在）
cd "$PROJ" || exit 2
setsid nohup .venv/bin/streamlit run app/main.py \
  --server.headless true --server.port "$PORT" --server.runOnSave false \
  --browser.gatherUsageStats false > "$LOG" 2>&1 < /dev/null &
echo $! > "$PIDFILE"

# 4) 等健康检查通过（最多 30 秒）
for i in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$PORT/_stcore/health" > /dev/null 2>&1; then
    echo "READY pid=$(cat "$PIDFILE") url=http://127.0.0.1:$PORT log=$LOG"
    exit 0
  fi
  sleep 0.5
done

echo "FAILED 服务 30 秒内未就绪，日志尾部："
tail -20 "$LOG"
exit 1
