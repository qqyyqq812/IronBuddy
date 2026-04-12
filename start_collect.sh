#!/bin/bash
# IronBuddy 数据采集模式 — 仅启动视觉+推流+EMG（无语音、无DeepSeek、无OpenClaw）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="toybrick@10.105.245.224"
BOARD_KEY="$HOME/.ssh/id_rsa_toybrick"
CLOUD_KEY="$HOME/.ssh/id_cloud_autodl"
CLOUD_SSH="root@connect.westd.seetacloud.com"
CLOUD_PORT=14191
CLOUD_RTMPOSE_URL="https://u953119-ba4a-9dcd6a47.westd.seetacloud.com:8443/infer"

echo "============================================"
echo "  IronBuddy 数据采集模式"
echo "  仅启动: 视觉 + 推流 + EMG"
echo "  不启动: 语音 / DeepSeek / OpenClaw"
echo "============================================"
echo ""

# [1/4] SSH 密钥 + 云端 GPU 检查
echo "[1/4] 检查密钥和云端 GPU..."
if [ ! -f "$BOARD_KEY" ]; then
    if [ -f "/mnt/c/temp/id_rsa" ]; then
        cp "/mnt/c/temp/id_rsa" "$BOARD_KEY" && chmod 600 "$BOARD_KEY"
        echo "  -> 已从 Windows 复制板卡密钥"
    else
        echo "  [错误] 找不到板卡 SSH 密钥: $BOARD_KEY"
        echo "  请将密钥放到 ~/.ssh/id_rsa_toybrick"
        exit 1
    fi
fi

# 测试板卡连通性
if ! ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 $TARGET "echo ok" >/dev/null 2>&1; then
    echo "  [错误] 无法连接板卡 $TARGET"
    echo "  请检查板卡是否开机、网络是否通畅"
    exit 1
fi
echo "  -> 板卡连接正常"

if [ -f "$CLOUD_KEY" ]; then
    ALIVE=$(ssh -p $CLOUD_PORT -i "$CLOUD_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
        $CLOUD_SSH "curl -s http://localhost:6006/health 2>/dev/null" 2>/dev/null || echo "")
    if echo "$ALIVE" | grep -q '"ready"'; then
        echo "  -> 云端 GPU 在线"
    else
        echo "  -> 正在启动云端服务器..."
        ssh -p $CLOUD_PORT -i "$CLOUD_KEY" -o StrictHostKeyChecking=no \
            $CLOUD_SSH 'export PATH=/root/miniconda3/bin:$PATH && cd /root/ironbuddy_cloud && nohup python rtmpose_http_server.py > server.log 2>&1 &' 2>/dev/null || true
        echo "  -> 等待云端启动 (10s)..."
        sleep 10
    fi
else
    echo "  [警告] 未找到云端密钥 $CLOUD_KEY，跳过云端检查"
    echo "  请确保云端 RTMPose 已手动启动"
fi

# [2/4] 同步代码到板卡
echo "[2/4] 同步代码到板卡..."
rsync -az -e "ssh -i $BOARD_KEY -o StrictHostKeyChecking=no" \
    --exclude='.git' --exclude='*.tar.gz' --exclude='*.rar' \
    --exclude='docs/hardware_ref' --exclude='backups' --exclude='.agent_memory' \
    --exclude='data' \
    "$SCRIPT_DIR/" $TARGET:/home/toybrick/streamer_v3/ > /dev/null 2>&1
echo "  -> 同步完成"

# [3/4] 清理板卡旧进程
echo "[3/4] 清理板卡旧进程..."
ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no $TARGET 'bash -s' <<'CLEAN'
echo toybrick | sudo -S killall -9 python3 2>/dev/null || true
rm -f /tmp/ironbuddy_*.pid
sudo rm -f /dev/shm/*.json /dev/shm/*.txt /dev/shm/result.jpg /dev/shm/emg_heartbeat /dev/shm/record_mode 2>/dev/null || true
echo "  -> 已清理"
CLEAN

# [4/4] 仅启动 3 个服务: 视觉 + 推流 + EMG
echo "[4/4] 启动采集服务 (3个)..."
ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no $TARGET "bash -s -- '$CLOUD_RTMPOSE_URL'" <<'BOARD'
CLOUD_URL=$1
cd /home/toybrick/streamer_v3
sleep 1

echo "  -> [1/3] 视觉推理 (Cloud RTMPose HTTPS)"
nohup env CLOUD_RTMPOSE_URL="$CLOUD_URL" python3 -u hardware_engine/ai_sensory/cloud_rtmpose_client.py > /tmp/npu_main.log 2>&1 &
echo $! > /tmp/ironbuddy_vision.pid
sleep 3

echo "  -> [2/3] 推流网页"
nohup python3 streamer_app.py > /tmp/streamer.log 2>&1 &
echo $! > /tmp/ironbuddy_streamer.pid

echo "  -> [3/3] EMG 接收器"
nohup python3 hardware_engine/sensor/udp_emg_server.py > /tmp/udp_emg.log 2>&1 &
echo $! > /tmp/ironbuddy_emg.pid

sleep 1
echo "  -> 采集服务已就绪"
BOARD

echo ""
echo "============================================"
echo "  数据采集模式已就绪!"
echo ""
echo "  推流画面: http://10.105.245.224:5000/"
echo "  视觉推理: Cloud GPU HTTPS (~100ms)"
echo ""
echo "  采集数据:"
echo "    bash collect_one.sh squat golden 60"
echo "    bash collect_one.sh squat lazy 60"
echo "    bash collect_one.sh squat bad 60"
echo ""
echo "  停止采集:"
echo "    bash stop_collect.sh"
echo "============================================"
