#!/bin/bash
# 一键停止所有 IronBuddy 服务（WSL + 靶机）

TARGET="toybrick@10.28.134.224"
WSL_KEY="$HOME/.ssh/id_rsa_toybrick"

echo "=== 停止 IronBuddy V1 所有服务 ==="

# 1. WSL 端：停 OpenClaw Gateway
echo "[1/3] 停止 WSL 端 OpenClaw Gateway..."
pkill -f "openclaw gateway" 2>/dev/null && echo "  -> Gateway 已停止" || echo "  -> Gateway 未运行"

# 2. WSL 端：停 SSH 隧道
echo "[2/3] 停止 SSH 反向隧道..."
pkill -f "ssh -i.*-R 18789" 2>/dev/null && echo "  -> 隧道已断开" || echo "  -> 隧道未运行"

# 3. 靶机端：停所有服务
echo "[3/3] 停止靶机端所有服务..."
ssh -i "$WSL_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 $TARGET 2>/dev/null << 'EOF'
  killall -9 main 2>/dev/null
  killall -9 python3 2>/dev/null
  pkill -f peripheral_daemon 2>/dev/null
  pkill -f tts_daemon 2>/dev/null
  pkill -f streamer_app 2>/dev/null
  pkill -f main_claw_loop 2>/dev/null
  # 释放 SSH 端口转发占用（sshd root 进程需 sudo）
  echo toybrick | sudo -S fuser -k 18789/tcp 2>/dev/null
  echo "  -> 靶机服务已全部停止"
EOF

echo "=== 全部停止完毕 ==="
