#!/bin/bash
# status_openclaw_dev.sh — 查询开发机 OpenClaw Daemon 状态
set -u

PIDFILE="/tmp/openclaw.pid"
LOGFILE="/tmp/openclaw_dev.log"

PIDS=$(pgrep -f "[o]penclaw_daemon.py" 2>/dev/null || true)
if [ -z "$PIDS" ]; then
    echo "[openclaw_dev] NOT RUNNING"
    exit 1
fi

echo "[openclaw_dev] RUNNING  PIDs=$PIDS"
if [ -f "$PIDFILE" ]; then
    echo "[openclaw_dev] pidfile $PIDFILE -> $(cat "$PIDFILE")"
fi

if [ -f "$LOGFILE" ]; then
    echo "[openclaw_dev] 日志尾部 ($LOGFILE):"
    tail -n 12 "$LOGFILE"
fi
exit 0
