#!/bin/bash
# IronBuddy 数据采集模式 (V7.23 收敛版)
#
# ⚠️ 唯一上板入口是 scripts/start_validation.sh —— 本脚本不做任何 rsync/scp。
# 前置依赖: 必须先跑 bash scripts/start_validation.sh 完成代码同步 + 启动 5 进程
# 本脚本职责: SSH 板端停掉 voice + FSM 两个进程, 进入纯采集态 (仅 vision+streamer+emg 继续运行)
#
# V7.22 及之前版本自带 115 行独立 rsync+ssh launch, 违反"单一入口"原则, V7.23 精简掉.

set -euo pipefail

TARGET="toybrick@10.18.76.224"
BOARD_KEY="$HOME/.ssh/id_rsa_toybrick"

echo "============================================"
echo "  IronBuddy 数据采集模式 (V7.23 收敛版)"
echo "  前置: 必须已执行 bash scripts/start_validation.sh"
echo "  职责: 仅关闭 voice + FSM (留 vision/streamer/emg)"
echo "============================================"
echo ""

# 板卡连通性检查
if [ ! -f "$BOARD_KEY" ]; then
    echo "  [错误] 找不到板卡 SSH 密钥: $BOARD_KEY"
    echo "  请先跑: bash scripts/start_validation.sh"
    exit 1
fi

if ! ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 "$TARGET" "echo ok" >/dev/null 2>&1; then
    echo "  [错误] 无法连接板卡 $TARGET"
    echo "  请先跑: bash scripts/start_validation.sh"
    exit 1
fi

# 停掉 voice_daemon 和 main_claw_loop (FSM)
ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no "$TARGET" 'bash -s' <<'CLEAN'
set +e
stop_pid() {
    local pidfile=$1
    local name=$2
    if [ -f "$pidfile" ]; then
        local pid=$(cat "$pidfile")
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            echo toybrick | sudo -S kill -9 "$pid" 2>/dev/null
            echo "  -> 已停止 $name (pid=$pid)"
        fi
        rm -f "$pidfile"
    fi
}
stop_pid /tmp/ironbuddy_voice.pid    voice_daemon
stop_pid /tmp/ironbuddy_mainloop.pid main_claw_loop
# 清理可能的语音残留信号
sudo rm -f /dev/shm/voice_speaking /dev/shm/llm_inflight /dev/shm/voice_interrupt /dev/shm/chat_active 2>/dev/null || true
echo "  -> voice + FSM 已停, 剩余 vision/streamer/emg 继续运行"
CLEAN

echo ""
echo "============================================"
echo "  数据采集模式就绪"
echo ""
echo "  推流画面: http://10.18.76.224:5000/"
echo ""
echo "  采集命令 (板端执行):"
echo "    ssh $TARGET"
echo "    cd /home/toybrick/streamer_v3"
echo "    bash collect_one.sh squat golden 60"
echo "    bash collect_one.sh squat lazy 60"
echo "    bash collect_one.sh squat bad 60"
echo ""
echo "  恢复完整 5 进程: bash scripts/start_validation.sh"
echo "============================================"
