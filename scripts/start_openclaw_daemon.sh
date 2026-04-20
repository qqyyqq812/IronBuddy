#!/bin/bash
# OpenClaw 后端常驻 Daemon 启动脚本（IronBuddy V4.7 主线 A.4）
# 默认不自动启动，由用户手动拉起；遵守板端红线（nohup + bracket trick pgrep）。
set -u

# 定位项目根目录（本脚本位于 scripts/ 下）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT" || { echo "[openclaw_daemon] 无法进入项目根目录 $ROOT"; exit 1; }

# 1) 重复启动检测（bracket trick 避免匹配自身）
if pgrep -f "[o]penclaw_daemon.py" > /dev/null 2>&1; then
    echo "[openclaw_daemon] 已在运行 (PID=$(pgrep -f '[o]penclaw_daemon.py'))，跳过启动"
    exit 0
fi

# 2) 加载 API 配置（飞书 webhook / OpenClaw token 可在这里注入）
if [ -f "$ROOT/.api_config.json" ]; then
    eval "$(python3 -c "import json;c=json.load(open('$ROOT/.api_config.json'));[print(f'export {k}={v!r}') for k,v in c.items() if isinstance(v,str)]" 2>/dev/null)"
fi

# 3) nohup 拉起 daemon（stdout/stderr 合并到日志）
LOGFILE="/tmp/openclaw_daemon.log"
nohup python3 "$ROOT/hardware_engine/cognitive/openclaw_daemon.py" \
    > "$LOGFILE" 2>&1 &
DAEMON_PID=$!
sleep 0.5

# 4) 启动结果回显
if ps -p "$DAEMON_PID" > /dev/null 2>&1; then
    echo "[openclaw_daemon] 已启动 PID=$DAEMON_PID, 日志: $LOGFILE"
    echo "[openclaw_daemon] 停止: pkill -f '[o]penclaw_daemon'"
    echo "[openclaw_daemon] 手动触发示例:"
    echo "    touch /dev/shm/openclaw_trigger_daily_plan"
    echo "    touch /dev/shm/openclaw_trigger_weekly_report"
    echo "    touch /dev/shm/openclaw_trigger_preference_learning"
else
    echo "[openclaw_daemon] 启动失败，查看日志: $LOGFILE"
    exit 1
fi
