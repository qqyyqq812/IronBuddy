#!/bin/bash
# stop_openclaw_dev.sh — 停止开发机 OpenClaw Daemon
# - 优先读 /tmp/openclaw.pid；兜底 pgrep bracket trick
# - 双重判定: SIGTERM 0.8s → SIGKILL 残留
set -u

PIDFILE="/tmp/openclaw.pid"
TARGETS=""

if [ -f "$PIDFILE" ]; then
    P="$(cat "$PIDFILE" 2>/dev/null | tr -d '[:space:]')"
    if [ -n "$P" ] && ps -p "$P" > /dev/null 2>&1; then
        TARGETS="$P"
    fi
fi

# 兜底：按进程名匹配（bracket trick 避免自身）
BK=$(pgrep -f "[o]penclaw_daemon.py" 2>/dev/null || true)
if [ -n "$BK" ]; then
    TARGETS="$TARGETS $BK"
fi

TARGETS=$(echo "$TARGETS" | tr ' ' '\n' | awk 'NF' | sort -u | tr '\n' ' ')

if [ -z "$TARGETS" ]; then
    echo "[openclaw_dev] 未发现运行中的 daemon"
    rm -f "$PIDFILE"
    exit 0
fi

echo "[openclaw_dev] SIGTERM -> $TARGETS"
for p in $TARGETS; do kill "$p" 2>/dev/null || true; done
sleep 0.8

STILL=""
for p in $TARGETS; do
    if ps -p "$p" > /dev/null 2>&1; then STILL="$STILL $p"; fi
done
if [ -n "$STILL" ]; then
    echo "[openclaw_dev] SIGKILL 残留:$STILL"
    for p in $STILL; do kill -9 "$p" 2>/dev/null || true; done
fi

rm -f "$PIDFILE"
echo "[openclaw_dev] 已停止"
