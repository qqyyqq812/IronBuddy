#!/bin/bash
# ============================================================
# IronBuddy 数据采集批处理脚本
# 6组数据: 2运动 × 3质量等级
# 板端运行: bash ~/tools/batch_collect.sh
# ============================================================
set -e

DATA_DIR="${HOME}/training_data/$(date +%Y%m%d)"
TOOL="${HOME}/tools/collect_training_data.py"
PYTHON="python3"

mkdir -p "$DATA_DIR"

echo "============================================"
echo "  IronBuddy 数据采集 — $(date +%Y-%m-%d)"
echo "  输出目录: $DATA_DIR"
echo "============================================"
echo ""

# 采集列表: exercise mode 中文提示
TASKS=(
    "squat golden 深蹲-标准动作"
    "squat lazy 深蹲-偷懒动作(幅度不够)"
    "squat bad 深蹲-错误动作(膝盖内扣/重心偏移)"
    "bicep_curl golden 弯举-标准动作"
    "bicep_curl lazy 弯举-偷懒动作(幅度不够)"
    "bicep_curl bad 弯举-错误动作(借力耸肩/身体晃动)"
)

TOTAL=${#TASKS[@]}
CURRENT=0

for task in "${TASKS[@]}"; do
    read -r exercise mode desc <<< "$task"
    CURRENT=$((CURRENT + 1))

    echo ""
    echo "========================================"
    echo "  [$CURRENT/$TOTAL] $desc"
    echo "  exercise=$exercise  mode=$mode"
    echo "========================================"
    echo ""
    echo "  准备好后按回车开始采集..."
    echo "  (采集中: [s]开始 [p]暂停 [q]结束保存)"
    read -r

    $PYTHON "$TOOL" --exercise "$exercise" --mode "$mode" --out "$DATA_DIR"

    echo ""
    echo "  ✅ $desc 采集完成"
    echo ""
done

# 统计
echo ""
echo "============================================"
echo "  全部采集完成！"
echo "============================================"
echo ""
echo "文件列表:"
ls -lh "$DATA_DIR"/train_*.csv 2>/dev/null || echo "  (无文件)"
echo ""
echo "每个文件行数:"
wc -l "$DATA_DIR"/train_*.csv 2>/dev/null || echo "  (无文件)"
echo ""
echo "下一步: 将 $DATA_DIR 传回 WSL 进行训练"
echo "  scp -r toybrick@10.105.245.224:$DATA_DIR ~/projects/embedded-fullstack/data/"
