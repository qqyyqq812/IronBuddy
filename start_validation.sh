#!/bin/bash
# 一键启动推流验证环境 V3.0 (联动 WSL 宿主机与 RK3399ProX 靶机)

TARGET="toybrick@10.105.245.224"
WIN_KEY="/mnt/c/temp/id_rsa"
WSL_KEY="$HOME/.ssh/id_rsa_toybrick"

# 1. 自动修复跨系统带来的 SSH 私钥权限拒绝问题
if [ ! -f "$WSL_KEY" ]; then
    if [ -f "$WIN_KEY" ]; then
        echo "[1/4] 从 Windows 挂载点抓取私钥并施加 600 权限..."
        cp "$WIN_KEY" "$WSL_KEY"
        chmod 600 "$WSL_KEY"
    else
        echo "❌ 找不到 $WIN_KEY，请确保密钥存在！"
        exit 1
    fi
fi

echo "[2/4] 从 WSL2 将最新高潜 V3 代码推入板载神经中枢..."
SRC_DIR="$HOME/projects/embedded-fullstack"
# 精确投送代码，避开一切冗杂依赖与历史包袱，彻底清爽化
rsync -avz -e "ssh -i $WSL_KEY -o StrictHostKeyChecking=no" \
    --exclude='.git' --exclude='*.tar.gz' --exclude='*.rar' --exclude='docs/hardware_ref' --exclude='backups' \
    "$SRC_DIR/" $TARGET:/home/toybrick/streamer_v3/ > /dev/null 2>&1
echo "  -> 代码重载完毕"

echo "[3/4] 连入板载大脑，摧毁所有僵尸进程，清洗内存盘毒瘤..."
ssh -i "$WSL_KEY" -o "StrictHostKeyChecking=no" $TARGET << 'EOF'
  echo "  -> 物理超度旧的底层内核与引擎进程..."
  killall -9 main 2>/dev/null
  killall -9 python3 2>/dev/null
  sleep 1

  echo "  -> 清洗内存盘脏数据..."
  sudo rm -f /dev/shm/*.json /dev/shm/*.txt /dev/shm/result.jpg /dev/shm/emg_heartbeat 2>/dev/null

  echo "  -> [V3 - 视觉系] 全核点亮 NPU C++ 推理管线..."
  echo toybrick | sudo -S nohup /home/toybrick/yolo_test/build/main 2 /home/toybrick/yolo_test/data/weights/pose-5s6-640-uint8.rknn /dev/video5 0.5 > /tmp/npu_main.log 2>&1 &
  # Wait for NPU init
  sleep 2

  echo "  -> [V3 - 表现层] 启动毫秒级 Streamer 中继站..."
  cd /home/toybrick/streamer_v3
  nohup python3 streamer_app.py > /tmp/streamer.log 2>&1 &

  echo "  -> [V3 - 调度层] 拉起深蹲 FSM 状态机总控..."
  nohup python3 hardware_engine/main_claw_loop.py > /tmp/main_loop.log 2>&1 &

  echo "  -> [V3 - 收信系] 部署唤醒式录音守护..."
  nohup python3 hardware_engine/voice_daemon.py > /tmp/voice_daemon.log 2>&1 &

  echo "  -> [V3 - 肌电链] 启动射频中转器与全息假体..."
  cd /home/toybrick/streamer_v3/hardware_engine/sensor
  nohup python3 udp_emg_server.py > /tmp/udp_emg.log 2>&1 &

  echo "  -> 板载深层架构全开！"
EOF

echo "[4/4] ==========================================================="
echo "✅ IronBuddy V3.0 已全面重构上线运行！"
echo "🌐 前端访问地址: http://10.105.245.224:5000/"
echo "🎤 说出“教练”即可唤醒对话！"
echo "================================================================="
