#!/bin/bash
# run_openclaw_dev.sh — 开发机拉起 OpenClaw Daemon（规则引擎偏好学习模式）
#
# - 与板端 scripts/ironbuddy.service / start_openclaw_daemon.sh 互不干扰；
# - 默认强制 OPENCLAW_PREFLEARN_MODE=rule（不调 DeepSeek API）；
# - 日志: /tmp/openclaw_dev.log；pidfile: /tmp/openclaw.pid。
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || { echo "[openclaw_dev] 无法进入 $ROOT"; exit 1; }

LOGFILE="/tmp/openclaw_dev.log"
PIDFILE="/tmp/openclaw.pid"
DAEMON_PY="$ROOT/hardware_engine/cognitive/openclaw_daemon.py"

# 重复启动检测（bracket trick）
if pgrep -f "[o]penclaw_daemon.py" > /dev/null 2>&1; then
    echo "[openclaw_dev] 已在运行 (PID=$(pgrep -f '[o]penclaw_daemon.py'))，跳过启动"
    pgrep -f "[o]penclaw_daemon.py" > "$PIDFILE"
    exit 0
fi

# 开发机模式默认用规则引擎跑偏好学习（不调 LLM）
export OPENCLAW_PREFLEARN_MODE="${OPENCLAW_PREFLEARN_MODE:-rule}"
# 默认禁用飞书推送（避免误发），可通过 FEISHU_DRY_RUN=0 打开
export FEISHU_DRY_RUN="${FEISHU_DRY_RUN:-1}"

# 日志轮转：超过 2MB 备份一次
if [ -f "$LOGFILE" ] && [ "$(stat -c%s "$LOGFILE" 2>/dev/null || echo 0)" -gt 2097152 ]; then
    mv -f "$LOGFILE" "${LOGFILE}.1" 2>/dev/null
fi

nohup python3 "$DAEMON_PY" > "$LOGFILE" 2>&1 &
PID=$!
echo "$PID" > "$PIDFILE"
sleep 0.8

if ps -p "$PID" > /dev/null 2>&1; then
    echo "[openclaw_dev] 已启动 PID=$PID"
    echo "[openclaw_dev] 日志: $LOGFILE"
    echo "[openclaw_dev] PREFLEARN_MODE=$OPENCLAW_PREFLEARN_MODE  FEISHU_DRY_RUN=$FEISHU_DRY_RUN"
    echo "[openclaw_dev] 停止: bash scripts/stop_openclaw_dev.sh"
    echo "[openclaw_dev] 手动触发偏好学习:"
    echo "    touch /dev/shm/openclaw_trigger_preference_learning"
    exit 0
else
    echo "[openclaw_dev] 启动失败，查看 $LOGFILE"
    tail -n 40 "$LOGFILE" 2>/dev/null
    rm -f "$PIDFILE"
    exit 1
fi
