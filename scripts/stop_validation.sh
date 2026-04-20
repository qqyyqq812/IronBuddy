#!/bin/bash
# IronBuddy V3.0 one-click stop (Board + SSH tunnel)

BOARD_KEY="$HOME/.ssh/id_rsa_toybrick"
TARGET="toybrick@10.18.76.224"

echo "[1/3] stopping board..."
ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 $TARGET 'bash -s' <<'STOP'
for f in /tmp/ironbuddy_*.pid; do
    [ -f "$f" ] && kill -9 $(cat "$f") 2>/dev/null && rm -f "$f"
done
pkill -f "ssh.*-L.*6006:localhost:6006" 2>/dev/null
killall -9 python3 2>/dev/null
sudo rm -f /dev/shm/*.json /dev/shm/*.txt /dev/shm/result.jpg 2>/dev/null
echo "  -> board cleaned"
STOP

echo "[2/3] closing SSH tunnel..."
if [ -f /tmp/ironbuddy_tunnel.pid ]; then
    kill $(cat /tmp/ironbuddy_tunnel.pid) 2>/dev/null
    rm -f /tmp/ironbuddy_tunnel.pid
fi
pkill -f "ssh.*-L.*6006:localhost:6006" 2>/dev/null || true
echo "  -> tunnel closed"

echo "[3/3] ==========================================================="
echo "  IronBuddy V3.0 stopped"
echo "==========================================================="
