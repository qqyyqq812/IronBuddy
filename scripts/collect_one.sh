#!/bin/bash
# IronBuddy 单标签数据采集
# 用法: bash collect_one.sh <exercise> <label> <seconds>
# 示例: bash collect_one.sh squat golden 60
#       bash collect_one.sh squat lazy 60
#       bash collect_one.sh bicep_curl bad 45
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOARD_KEY="$HOME/.ssh/id_rsa_toybrick"
TARGET="toybrick@10.18.76.224"

# ── 参数检查 ──
if [ $# -lt 3 ]; then
    echo ""
    echo "用法: bash collect_one.sh <exercise> <label> <seconds>"
    echo ""
    echo "参数:"
    echo "  exercise  动作类型: squat | bicep_curl"
    echo "  label     标签: golden | lazy | bad"
    echo "  seconds   采集时长(秒)"
    echo ""
    echo "示例:"
    echo "  bash collect_one.sh squat golden 60"
    echo "  bash collect_one.sh squat lazy 60"
    echo "  bash collect_one.sh bicep_curl bad 45"
    echo ""
    exit 1
fi

EXERCISE="$1"
LABEL="$2"
DURATION="$3"

# 验证参数
if [[ "$EXERCISE" != "squat" && "$EXERCISE" != "bicep_curl" ]]; then
    echo "[错误] 无效动作类型: $EXERCISE"
    echo "  支持: squat, bicep_curl"
    exit 1
fi

if [[ "$LABEL" != "golden" && "$LABEL" != "lazy" && "$LABEL" != "bad" ]]; then
    echo "[错误] 无效标签: $LABEL"
    echo "  支持: golden, lazy, bad"
    exit 1
fi

if ! [[ "$DURATION" =~ ^[0-9]+$ ]] || [ "$DURATION" -lt 5 ]; then
    echo "[错误] 采集时长必须是大于等于5的整数(秒)"
    exit 1
fi

BOARD_OUT_DIR="/home/toybrick/training_data/${EXERCISE}/${LABEL}"
LOCAL_OUT_DIR="${SCRIPT_DIR}/data/${EXERCISE}/${LABEL}"

echo ""
echo "============================================"
echo "  IronBuddy 数据采集"
echo "  动作: $EXERCISE"
echo "  标签: $LABEL"
echo "  时长: ${DURATION}秒"
echo "  板卡路径: $BOARD_OUT_DIR"
echo "  本地路径: $LOCAL_OUT_DIR"
echo "============================================"
echo ""

# ── 第1步: 在板卡上创建输出目录并运行采集 ──
echo "[1/3] 在板卡上启动采集 (${DURATION}秒)..."
echo "  请在摄像头前做 ${EXERCISE} 动作 (${LABEL} 模式)"
echo ""

ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no $TARGET "bash -s" <<COLLECT
set -e
mkdir -p "${BOARD_OUT_DIR}"
cd /home/toybrick/streamer_v3
python3 tools/collect_training_data.py \
    --exercise "${EXERCISE}" \
    --mode "${LABEL}" \
    --out "${BOARD_OUT_DIR}" \
    --auto "${DURATION}"
COLLECT

COLLECT_EXIT=$?
if [ $COLLECT_EXIT -ne 0 ]; then
    echo ""
    echo "[错误] 板卡采集失败 (退出码: $COLLECT_EXIT)"
    echo "  检查板卡日志: ssh -i $BOARD_KEY $TARGET 'tail -20 /tmp/npu_main.log'"
    exit 1
fi

# ── 第2步: 将 CSV 复制回本地 ──
echo ""
echo "[2/3] 下载采集数据到本地..."
mkdir -p "$LOCAL_OUT_DIR"

# 找到刚生成的 CSV（按时间最新）
REMOTE_CSV=$(ssh -i "$BOARD_KEY" -o StrictHostKeyChecking=no $TARGET \
    "ls -t ${BOARD_OUT_DIR}/train_*.csv 2>/dev/null | head -1")

if [ -z "$REMOTE_CSV" ]; then
    echo "[错误] 板卡上未找到生成的 CSV 文件"
    echo "  检查目录: ssh -i $BOARD_KEY $TARGET 'ls -la ${BOARD_OUT_DIR}/'"
    exit 1
fi

REMOTE_FILENAME=$(basename "$REMOTE_CSV")
scp -i "$BOARD_KEY" -o StrictHostKeyChecking=no \
    "${TARGET}:${REMOTE_CSV}" "${LOCAL_OUT_DIR}/${REMOTE_FILENAME}"

echo "  -> 已下载: ${LOCAL_OUT_DIR}/${REMOTE_FILENAME}"

# ── 第3步: 本地验证数据质量 ──
echo ""
echo "[3/3] 验证数据质量..."

if [ -f "${SCRIPT_DIR}/tools/validate_data.py" ]; then
    python3 "${SCRIPT_DIR}/tools/validate_data.py" "$LOCAL_OUT_DIR"
else
    echo "  [警告] 未找到 validate_data.py，跳过验证"
    echo "  文件: ${LOCAL_OUT_DIR}/${REMOTE_FILENAME}"
    # 简单统计
    LINES=$(wc -l < "${LOCAL_OUT_DIR}/${REMOTE_FILENAME}")
    FRAMES=$((LINES - 1))
    echo "  帧数: $FRAMES"
fi

echo ""
echo "============================================"
echo "  采集完成!"
echo "  数据: ${LOCAL_OUT_DIR}/${REMOTE_FILENAME}"
echo "============================================"
