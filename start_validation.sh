#!/bin/bash
# IronBuddy V3.0 one-click start
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="toybrick@10.105.245.224"
BOARD_KEY="$HOME/.ssh/id_rsa_toybrick"
CLOUD_KEY="$HOME/.ssh/id_cloud_autodl"
CLOUD_SSH="root@connect.westd.seetacloud.com"
CLOUD_PORT=14191
# AutoDL direct HTTPS (no SSH tunnel needed!)
CLOUD_RTMPOSE_URL="https://u953119-ba4a-9dcd6a47.westd.seetacloud.com:8443/infer"

# [1/3] Keys + cloud check
echo "[1/3] setup..."
if [ ! -f "$BOARD_KEY" ]; then
    [ -f "/mnt/c/temp/id_rsa" ] && cp "/mnt/c/temp/id_rsa" "$BOARD_KEY" && chmod 600 "$BOARD_KEY" || { echo "ERROR: no key"; exit 1; }
fi

if [ -f "$CLOUD_KEY" ]; then
    ALIVE=$(ssh -p $CLOUD_PORT -i "$CLOUD_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
        $CLOUD_SSH "curl -s http://localhost:6006/health 2>/dev/null" 2>/dev/null || echo "")
    if echo "$ALIVE" | grep -q '"ready"'; then
        echo "  -> cloud GPU online"
    else
        echo "  -> starting cloud server..."
        ssh -p $CLOUD_PORT -i "$CLOUD_KEY" -o StrictHostKeyChecking=no \
            $CLOUD_SSH 'export PATH=/root/miniconda3/bin:$PATH && cd /root/ironbuddy_cloud && nohup python rtmpose_http_server.py > server.log 2>&1 &' 2>/dev/null || true
        sleep 10
    fi
fi

# [2/3] Deploy
echo "[2/3] rsync..."
rsync -az -e "ssh -i $BOARD_KEY -o StrictHostKeyChecking=no" \
    --exclude='.git' --exclude='*.tar.gz' --exclude='*.rar' \
    --exclude='docs/hardware_ref' --exclude='backups' --exclude='.agent_memory' \
    "$SCRIPT_DIR/" $TARGET:/home/toybrick/streamer_v3/ > /dev/null 2>&1

# [3/3] Start board (direct HTTPS, no SSH tunnel!)
echo "[3/3] starting..."
ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no $TARGET "bash -s -- '$CLOUD_RTMPOSE_URL'" <<'BOARD'
CLOUD_URL=$1
echo toybrick | sudo -S killall -9 python3 2>/dev/null
pkill -f "ssh.*-L.*6006" 2>/dev/null
sleep 1
rm -f /tmp/ironbuddy_*.pid
sudo rm -f /dev/shm/*.json /dev/shm/*.txt /dev/shm/result.jpg /dev/shm/emg_heartbeat 2>/dev/null

cd /home/toybrick/streamer_v3
WSL_IP=$(echo $SSH_CLIENT | awk '{print $1}')

echo "  -> [1/5] vision (Cloud GPU direct HTTPS)"
nohup env CLOUD_RTMPOSE_URL="$CLOUD_URL" python3 -u hardware_engine/ai_sensory/cloud_rtmpose_client.py > /tmp/npu_main.log 2>&1 &
echo $! > /tmp/ironbuddy_vision.pid
sleep 3

echo "  -> [2/5] streamer"
nohup python3 streamer_app.py > /tmp/streamer.log 2>&1 &
echo $! > /tmp/ironbuddy_streamer.pid

echo "  -> [3/5] FSM"
nohup env OPENCLAW_URL="ws://${WSL_IP}:18789" python3 hardware_engine/main_claw_loop.py > /tmp/main_loop.log 2>&1 &
echo $! > /tmp/ironbuddy_mainloop.pid

echo "  -> [4/5] EMG"
nohup python3 hardware_engine/sensor/udp_emg_server.py > /tmp/udp_emg.log 2>&1 &
echo $! > /tmp/ironbuddy_emg.pid

echo "  -> [5/5] voice"
nohup python3 hardware_engine/voice_daemon.py > /tmp/voice_daemon.log 2>&1 &
echo $! > /tmp/ironbuddy_voice.pid

echo "  -> done"
BOARD

echo ""
echo "==========================================================="
echo "  IronBuddy V3.0 online!"
echo "  Web:   http://10.105.245.224:5000/"
echo "  Vision: Cloud GPU direct HTTPS (~100ms, no tunnel)"
echo "==========================================================="
