#!/bin/bash
# IronBuddy V4.4 · 弯举/深蹲权重切换脚本
# ---------------------------------------------------------
# 按视频镜头切换 GRU 权重（拍弯举镜头前 curl / 拍深蹲镜头前 squat）。
# main_claw_loop._load_gru_model() 只查 hardware_engine/extreme_fusion_gru.pt
# 和 hardware_engine/cognitive/extreme_fusion_gru.pt，
# 所以同一时刻只能加载一个动作模型。本脚本做 cp + pkill 重启 FSM。
#
# 用法：
#   bash scripts/switch_model.sh curl
#   bash scripts/switch_model.sh squat
#
# 源权重：
#   弯举：models/extreme_fusion_gru_curl.pt   （周一本地采集 + train_model 产）
#   深蹲：models/extreme_fusion_gru_squat.pt  （已由 MIA 数据训练产出）
#
# 板端部署步骤：
#   1. cp 对应 .pt → hardware_engine/extreme_fusion_gru.pt（本地）
#   2. rsync 到板端（通过 start_validation.sh，或直接 scp）
#   3. 板端 pkill main_claw_loop.py → 重启 FSM 以重载权重
# ---------------------------------------------------------
set -euo pipefail

MODE="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BOARD_KEY="${HOME}/.ssh/id_rsa_toybrick"
TARGET="toybrick@10.18.76.224"

if [[ "$MODE" != "curl" && "$MODE" != "squat" ]]; then
    echo "用法: bash scripts/switch_model.sh <curl|squat>"
    exit 1
fi

SRC="$REPO_ROOT/models/extreme_fusion_gru_${MODE}.pt"
DST_LOCAL="$REPO_ROOT/hardware_engine/extreme_fusion_gru.pt"

echo "============================================"
echo "  IronBuddy 权重切换: $MODE"
echo "============================================"

# [1/4] 源权重存在性检查
if [[ ! -f "$SRC" ]]; then
    echo "[错误] 源权重不存在: $SRC"
    echo "  若切换到 curl: 请周一采集数据后跑 tools/train_model.py"
    echo "  若切换到 squat: 请运行 python tools/mia_preprocess_squat.py + tools/train_model.py"
    exit 2
fi
echo "[1/4] 源权重: $SRC ($(du -h "$SRC" | cut -f1))"

# [2/4] 本地 cp 到 hardware_engine/
cp "$SRC" "$DST_LOCAL"
echo "[2/4] 本地部署: $DST_LOCAL"

# [3/4] rsync 到板端
if [[ ! -f "$BOARD_KEY" ]]; then
    echo "[3/4] 跳过板端同步 (未找到 $BOARD_KEY)"
    echo "       手动执行: scp $DST_LOCAL $TARGET:/home/toybrick/streamer_v3/hardware_engine/extreme_fusion_gru.pt"
else
    if ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=5 "$TARGET" "echo ok" >/dev/null 2>&1; then
        scp -i "$BOARD_KEY" -o StrictHostKeyChecking=no \
            "$DST_LOCAL" "$TARGET:/home/toybrick/streamer_v3/hardware_engine/extreme_fusion_gru.pt"
        echo "[3/4] 板端同步完成"

        # [4/4] 板端重启 FSM（pkill + nohup，不动其他 4 个服务）
        ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no "$TARGET" 'bash -s' <<'BOARD_RESTART'
set -e
# bracket trick 防 pgrep 匹配自身
PID=$(pgrep -f '[m]ain_claw_loop.py' || true)
if [[ -n "$PID" ]]; then
    echo "  板端 FSM PID=$PID，重启中..."
    kill -9 $PID 2>/dev/null || true
    sleep 0.8
fi
cd /home/toybrick/streamer_v3
# 以与 start_validation.sh 相同方式重启（不等同）
nohup python3 hardware_engine/main_claw_loop.py > /tmp/fsm.log 2>&1 &
echo "  新 FSM PID=$!"
BOARD_RESTART
        echo "[4/4] 板端 FSM 已重启，加载新权重"
    else
        echo "[3/4] 板端连接失败，跳过同步 (仅本地部署)"
    fi
fi

echo "============================================"
echo "  切换完成: 当前 hardware_engine/extreme_fusion_gru.pt → $MODE 版"
echo "============================================"
