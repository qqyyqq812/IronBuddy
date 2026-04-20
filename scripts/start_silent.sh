#!/bin/bash
# IronBuddy V3.0 静音版一键启动 (WSL 端)
# 仿 start_validation.sh, 但调用板端 start_silent_services.sh 而非 start_all_services.sh
# 完全独立, 不修改原脚本
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET="toybrick@10.18.76.224"
BOARD_KEY="$HOME/.ssh/id_rsa_toybrick"
CLOUD_KEY="$HOME/.ssh/id_cloud_autodl"
CLOUD_SSH="root@connect.westd.seetacloud.com"
CLOUD_PORT=42924
CLOUD_RTMPOSE_URL="${CLOUD_RTMPOSE_URL:-https://u953119-ba4a-9dcd6a47.westd.seetacloud.com:8443/infer}"

# [1/3] Keys + cloud check
echo "[1/3] setup (silent-mode)..."
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
# 静音版与 start_validation.sh 的区别: 不再把 rsync 输出吞掉, 加进度 + 超时护栏
# (--checksum 要遍历全项目, 大仓 30-90s, 无输出容易误判为卡死)
echo "[2/3] rsync (显示进度, 全量对比需 30-90s, 请耐心)..."
timeout 300 rsync -az --checksum --info=progress2 --no-inc-recursive \
    -e "ssh -i $BOARD_KEY -o StrictHostKeyChecking=no -o ConnectTimeout=10" \
    --exclude='.git' --exclude='*.tar.gz' --exclude='*.rar' \
    --exclude='docs/hardware_ref' --exclude='backups' --exclude='.agent_memory' \
    --exclude='data' \
    "$PROJECT_DIR/" $TARGET:/home/toybrick/streamer_v3/ || {
    echo "  ❌ rsync 失败或超时 (>300s). 检查: ssh toybrick@10.18.76.224 是否通畅"
    exit 1
}
if [ -f "$PROJECT_DIR/models/extreme_fusion_gru.pt" ]; then
    echo "  -> 同步 GRU 模型..."
    timeout 60 scp -i "$BOARD_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 \
        "$PROJECT_DIR/models/extreme_fusion_gru.pt" \
        $TARGET:/home/toybrick/streamer_v3/hardware_engine/cognitive/extreme_fusion_gru.pt > /dev/null 2>&1 || \
        echo "  ⚠️  GRU 模型同步失败或超时(非致命)"
fi
echo "  ✅ rsync 完成"

# [3/3] 板端静音启动
echo "[3/3] starting board in SILENT mode (voice skipped, amixer muted)..."
ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no $TARGET \
    "chmod +x /home/toybrick/streamer_v3/scripts/start_silent_services.sh /home/toybrick/streamer_v3/scripts/stop_all_services.sh 2>/dev/null; \
     export CLOUD_RTMPOSE_URL='$CLOUD_RTMPOSE_URL'; \
     bash /home/toybrick/streamer_v3/scripts/start_silent_services.sh" || {
    echo "WARN: remote launcher returned non-zero; check /tmp/ironbuddy_silent_startup.log on board"
}

echo ""
echo "==========================================================="
echo "  IronBuddy V3.0 SILENT mode online! (图书馆 demo)"
echo "  Web:        http://10.18.76.224:5000/"
echo "  Vision:     Cloud GPU direct HTTPS (~100ms)"
echo "  Voice:      [DISABLED] - 语音模块未启动"
echo "  Audio HW:   [MUTED]    - amixer Speaker mute"
echo "  Mute flag:  [SET]      - mute_signal.json=true"
echo "  停止方法:   bash scripts/stop_validation.sh"
echo "==========================================================="
