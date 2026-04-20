#!/bin/bash
# IronBuddy 数据采集模式 — 停止所有采集服务
set -euo pipefail

BOARD_KEY="$HOME/.ssh/id_rsa_toybrick"
TARGET="toybrick@10.18.76.224"

echo "============================================"
echo "  IronBuddy 停止数据采集"
echo "============================================"
echo ""

echo "[1/2] 停止板卡采集进程..."
ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 $TARGET 'bash -s' <<'STOP'
# 先用 PID 文件精确停止
for f in /tmp/ironbuddy_vision.pid /tmp/ironbuddy_streamer.pid /tmp/ironbuddy_emg.pid; do
    if [ -f "$f" ]; then
        PID=$(cat "$f")
        kill -9 "$PID" 2>/dev/null && echo "  -> 已停止 PID $PID ($f)"
        rm -f "$f"
    fi
done

# 兜底: 杀掉所有 python3
echo toybrick | sudo -S killall -9 python3 2>/dev/null || true

# 清理共享内存
sudo rm -f /dev/shm/*.json /dev/shm/*.txt /dev/shm/result.jpg /dev/shm/emg_heartbeat /dev/shm/record_mode 2>/dev/null || true
echo "  -> 板卡已清理"
STOP

echo "[2/2] 完成"
echo ""
echo "============================================"
echo "  数据采集已停止"
echo "============================================"
