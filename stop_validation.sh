#!/bin/bash
# 一键停止环境 V3.0 (联动 WSL 宿主机与 RK3399ProX 靶机)

TARGET="toybrick@10.105.245.224"
WSL_KEY="$HOME/.ssh/id_rsa_toybrick"

echo "[1/3] 连入板载大脑，摧毁所有 V3 运行进程与共享内存..."
ssh -i "$WSL_KEY" -o "StrictHostKeyChecking=no" $TARGET << 'EOF'
  killall -9 main 2>/dev/null
  killall -9 python3 2>/dev/null
  sleep 1
  sudo rm -f /dev/shm/*.json /dev/shm/*.txt /dev/shm/result.jpg /dev/shm/emg_heartbeat 2>/dev/null
  echo "  -> 板载深层进程已全部阻断释放！"
EOF

echo "[2/3] 清理本地(WSL端)测试模拟发包残余..."
pkill -9 -f "mock_teammate_esp32.py" 2>/dev/null

echo "[2/2] ==========================================================="
echo "🎯 IronBuddy V3.0 实验环境已安全关闭！"
echo "================================================================="
