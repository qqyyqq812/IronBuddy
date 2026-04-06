#!/bin/bash
# 一键启动推流验证环境 (联动 WSL 宿主机与 RK3399ProX 靶机)
# 解决端口霸占与各个模块启动时序带来的死锁问题

TARGET="toybrick@10.28.134.224"
WIN_KEY="/mnt/c/temp/id_rsa"
WSL_KEY="$HOME/.ssh/id_rsa_toybrick"

# 1. 自动修复跨系统带来的 SSH 私钥权限拒绝 (Permissions are too open) 问题
if [ ! -f "$WSL_KEY" ]; then
    if [ -f "$WIN_KEY" ]; then
        echo "[步骤 0] 从 Windows 同步 SSH 密钥并实施 600 权限保护..."
        cp "$WIN_KEY" "$WSL_KEY"
        chmod 600 "$WSL_KEY"
    else
        echo "❌ 找不到 $WIN_KEY，请确保密钥存在！"
        exit 1
    fi
fi

echo "[步骤 1] 净化本地 OpenClaw 网关残留..."
pkill -f "openclaw gateway" 2>/dev/null
sleep 1

echo "[步骤 2] 在 WSL 宿主机后台启动 OpenClaw (18789)..."
nohup /home/qq/.nvm/versions/node/v24.13.0/bin/openclaw gateway --port 18789 > /tmp/openclaw_gateway.log 2>&1 &
echo $! > /tmp/openclaw_gateway.pid

echo "[步骤 3] 建立稳定的后台 SSH 反向隧道..."
pkill -f "ssh -i $WSL_KEY -N -f -R 18789:127.0.0.1:18789" 2>/dev/null

# 彻底释放板端 18789 端口（旧 SSH -R 转发由 sshd root 进程持有，必须 sudo 才能杀掉）
echo "  -> 释放板端 18789 端口占用..."
ssh -i "$WSL_KEY" -o StrictHostKeyChecking=no $TARGET \
    "echo toybrick | sudo -S fuser -k 18789/tcp 2>/dev/null; \
     echo toybrick | sudo -S ss -tlnp 2>/dev/null | grep 18789 | grep -oP 'pid=\K[0-9]+' | xargs -r sudo kill -9 2>/dev/null; \
     echo 'PORT_RELEASED'" 2>/dev/null
sleep 2

TUNNEL_OK=0
for i in 1 2 3; do
    ssh -i "$WSL_KEY" -N -f -R 18789:127.0.0.1:18789 \
        -o "StrictHostKeyChecking=no" \
        -o "ServerAliveInterval=30" \
        -o "ServerAliveCountMax=3" \
        -o "ExitOnForwardFailure=yes" \
        $TARGET && { TUNNEL_OK=1; break; }
    echo "  ⚠️ 隧道建立失败 (尝试 $i/3)，2 秒后重试..."
    # 再次强制释放端口
    ssh -i "$WSL_KEY" -o StrictHostKeyChecking=no $TARGET \
        "echo toybrick | sudo -S fuser -k 18789/tcp 2>/dev/null" 2>/dev/null
    sleep 2
done
if [ "$TUNNEL_OK" -eq 0 ]; then
    echo "  ❌ SSH 反向隧道建立失败！DeepSeek 教练点评将不可用。"
    echo "  💡 手动排查: ssh 到板端执行 sudo ss -tlnp | grep 18789 查看占用情况"
else
    echo "  ✅ SSH 反向隧道建立成功"
fi

SRC_DIR="$HOME/projects/embedded-fullstack"
echo "[步骤 3.5] 同步最新代码到靶机..."
# 注意：scp -r dir/ 到 dir/ 会嵌套，必须用 rsync 或先清理
ssh -i "$WSL_KEY" -o StrictHostKeyChecking=no $TARGET "rm -rf /home/toybrick/hardware_engine/__pycache__ /home/toybrick/hardware_engine/cognitive/__pycache__" 2>/dev/null
scp -i "$WSL_KEY" -o StrictHostKeyChecking=no -r "$SRC_DIR/hardware_engine" $TARGET:/home/toybrick/ 2>/dev/null
scp -i "$WSL_KEY" -o StrictHostKeyChecking=no "$SRC_DIR/streamer_app.py" $TARGET:/home/toybrick/ 2>/dev/null
scp -i "$WSL_KEY" -o StrictHostKeyChecking=no "$SRC_DIR/templates/index.html" $TARGET:/home/toybrick/templates/ 2>/dev/null
scp -i "$WSL_KEY" -o StrictHostKeyChecking=no -r "$SRC_DIR/agent_memory" $TARGET:/home/toybrick/ 2>/dev/null
# biomechanics: 排除 checkpoints（65MB ONNX 模型不需要每次重传）
rsync -az --exclude='checkpoints' -e "ssh -i $WSL_KEY -o StrictHostKeyChecking=no" "$SRC_DIR/biomechanics/" $TARGET:/home/toybrick/biomechanics/ 2>/dev/null
echo "  -> 代码同步完毕"

echo "[步骤 4] 发送指令至靶机，清理僵尸进程并按时序拉起服务..."
ssh -i "$WSL_KEY" -o "StrictHostKeyChecking=no" $TARGET << 'EOF'
  echo "  -> 物理超度旧的底层内核与引擎进程..."
  killall -9 main 2>/dev/null
  killall -9 python3 2>/dev/null
  pkill -f peripheral_daemon 2>/dev/null
  pkill -f tts_daemon 2>/dev/null
  pkill -f voice_daemon 2>/dev/null
  sleep 2

  echo "  -> 清除旧 LLM 回复数据..."
  rm -f /dev/shm/llm_reply.txt /dev/shm/chat_input.txt /dev/shm/chat_reply.txt /dev/shm/trigger_deepseek

  echo "  -> [Agent1] 初始化蜂鸣器 GPIO 并切换音箱路由..."
  bash /home/toybrick/hardware_engine/peripherals/buzzer_init.sh
  amixer -c 0 sset 'Playback Path' SPK 2>/dev/null
  
  echo "  -> 启动高能耗 NPU C++ 推理网关与 Python 桥接层..."
  echo toybrick | sudo -S nohup /home/toybrick/yolo_test/build/main 2 /home/toybrick/yolo_test/data/weights/pose-5s6-640-uint8.rknn /dev/video5 0.5 > /tmp/npu_main.log 2>&1 &
  nohup python3 /home/toybrick/hardware_engine/ai_sensory/vision/pose_subscriber.py > /tmp/subscriber.log 2>&1 &
  sleep 2
  
  echo "  -> 启动 Streamer 推流中台 (后台)..."
  cd /home/toybrick
  nohup python3 streamer_app.py > /tmp/streamer.log 2>&1 &
  sleep 2
  
  echo "  -> 启动 NPU 深蹲 FSM 计算核心 (后台)..."
  cd /home/toybrick/hardware_engine
  # ========================================================
  # 【飞书通知群机器人】将下面这行取消注释，并填入你的机器人 Webhook URL
  # export FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxx"
  # ========================================================
  nohup python3 main_claw_loop.py > /tmp/main_loop.log 2>&1 &
  
  echo "  -> [Agent1] 启动外设旁路监听守护精灵..."
  nohup bash /home/toybrick/hardware_engine/peripherals/peripheral_daemon.sh > /tmp/peripheral_daemon.log 2>&1 &
  nohup bash /home/toybrick/hardware_engine/peripherals/tts_daemon.sh > /tmp/tts_daemon.log 2>&1 &
  echo "  -> [Agent1] 启动唤醒式语音守护进程..."
  cd /home/toybrick/hardware_engine
  nohup python3 voice_daemon.py > /tmp/voice_daemon.log 2>&1 &

  echo "  -> 靶机环境就绪！"
EOF

echo "============================================================"
echo "✅ 所有系统均已连通且进入工作流！"
echo "🌐 请在浏览器访问 http://10.28.134.224:5000/"
echo "🛑 需要停止测试时，请务必执行：./stop_validation.sh"
echo "============================================================"
