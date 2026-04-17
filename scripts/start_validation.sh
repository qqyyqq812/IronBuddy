#!/bin/bash
# IronBuddy V3.0 one-click start (WSL side: rsync + trigger board-side launcher)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET="toybrick@10.105.245.224"
BOARD_KEY="$HOME/.ssh/id_rsa_toybrick"
CLOUD_KEY="$HOME/.ssh/id_cloud_autodl"
CLOUD_SSH="root@connect.westd.seetacloud.com"
CLOUD_PORT=14191
CLOUD_RTMPOSE_URL="${CLOUD_RTMPOSE_URL:-https://u953119-ba4a-9dcd6a47.westd.seetacloud.com:8443/infer}"

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
rsync -az --checksum -e "ssh -i $BOARD_KEY -o StrictHostKeyChecking=no" \
    --exclude='.git' --exclude='*.tar.gz' --exclude='*.rar' \
    --exclude='docs/hardware_ref' --exclude='backups' --exclude='.agent_memory' \
    --exclude='data' \
    "$PROJECT_DIR/" $TARGET:/home/toybrick/streamer_v3/ > /dev/null 2>&1
# 确保模型文件强制同步（rsync可能因大小相同跳过）
if [ -f "$PROJECT_DIR/models/extreme_fusion_gru.pt" ]; then
    scp -i "$BOARD_KEY" -o StrictHostKeyChecking=no \
        "$PROJECT_DIR/models/extreme_fusion_gru.pt" \
        $TARGET:/home/toybrick/streamer_v3/hardware_engine/cognitive/extreme_fusion_gru.pt > /dev/null 2>&1
fi

# [3/3] Delegate launch to board-side script
echo "[3/3] starting board..."
ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no $TARGET \
    "chmod +x /home/toybrick/streamer_v3/scripts/start_all_services.sh /home/toybrick/streamer_v3/scripts/stop_all_services.sh 2>/dev/null; \
     export CLOUD_RTMPOSE_URL='$CLOUD_RTMPOSE_URL'; \
     bash /home/toybrick/streamer_v3/scripts/start_all_services.sh" || {
    echo "WARN: remote launcher returned non-zero; check /tmp/ironbuddy_startup.log on board"
}

echo ""
echo "==========================================================="
echo "  IronBuddy V3.0 online!"
echo "  Web:   http://10.105.245.224:5000/"
echo "  Vision: Cloud GPU direct HTTPS (~100ms, no tunnel)"
echo "==========================================================="
