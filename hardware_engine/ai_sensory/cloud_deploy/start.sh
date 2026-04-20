#!/usr/bin/env bash
# IronBuddy Cloud RTMPose Server - Startup Script (ONNX Runtime GPU)
# Runs on AutoDL RTX 5090, port 6006 (only exposed HTTP port)
# Usage: bash start.sh [--skip-install]

set -e
export PATH=/root/miniconda3/bin:$PATH
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="/root/ironbuddy_cloud/server.log"
PID_FILE="/root/ironbuddy_cloud/server.pid"
PORT=6006

echo "============================================================"
echo " IronBuddy RTMPose Cloud Server (ONNX Runtime GPU)"
echo " Port: $PORT"
echo "============================================================"

# ── Stop any existing instance ─────────────────────────────────────────────────
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[Start] Stopping existing server (PID $OLD_PID)..."
        kill "$OLD_PID" && sleep 2
    fi
    rm -f "$PID_FILE"
fi

# ── Install dependencies if needed ─────────────────────────────────────────────
if [ "$1" != "--skip-install" ]; then
    bash "$SCRIPT_DIR/install_deps.sh"
fi

# ── Copy server code to cloud working directory ───────────────────────────────
mkdir -p /root/ironbuddy_cloud
cp "$SCRIPT_DIR/rtmpose_http_server.py" /root/ironbuddy_cloud/

# ── Launch server ──────────────────────────────────────────────────────────────
echo "[Start] Launching RTMPose HTTP server on 0.0.0.0:$PORT ..."
cd /root/ironbuddy_cloud
nohup python rtmpose_http_server.py >> "$LOG" 2>&1 &
SERVER_PID=$!
echo "$SERVER_PID" > "$PID_FILE"
echo "[Start] Server PID: $SERVER_PID  (log -> $LOG)"

# ── Wait and health check ──────────────────────────────────────────────────────
echo "[Start] Waiting for model to load..."
MAX_WAIT=60
ELAPSED=0
while [ $ELAPSED -lt $MAX_WAIT ]; do
    sleep 3
    ELAPSED=$((ELAPSED + 3))
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "[Error] Server process died! Last log:"
        tail -20 "$LOG"
        exit 1
    fi
    STATUS=$(curl -s "http://127.0.0.1:$PORT/health" 2>/dev/null || echo "")
    if echo "$STATUS" | grep -q '"ready"'; then
        echo "[Start] Server READY! $STATUS"
        echo ""
        echo "  Inference: POST http://localhost:$PORT/infer"
        echo "  Health:    GET  http://localhost:$PORT/health"
        echo "  Stop:      kill \$(cat $PID_FILE)"
        echo "  Logs:      tail -f $LOG"
        echo ""
        echo "  NOTE: Access from board via SSH tunnel:"
        echo "    ssh -p 42924 -N -L $PORT:localhost:$PORT root@connect.westd.seetacloud.com  # V4.5 2026-04-18 新实例端口"
        exit 0
    fi
    echo "[Wait] ${ELAPSED}s ..."
done

echo "[Warning] Timeout after ${MAX_WAIT}s. Check: tail -f $LOG"
tail -20 "$LOG"
