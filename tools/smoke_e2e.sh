#!/usr/bin/env bash
# V4.2 端到端冒烟：mock data → pretrain → fusion_head × 2（curl/squat）
# .claude/plans/distributed-puzzling-wilkinson.md Agent-C 定义
#
# 用法：
#   cd /home/qq/projects/embedded-fullstack
#   bash tools/smoke_e2e.sh
#
# 约束：若 train_fusion_head.py 仍是 NotImplementedError，会在 STEP 5/6 失败——这是预期。
#      前 4 步（mock→validate→pretrain）必须独立可跑通。

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"

echo "==============================================================="
echo "[smoke] 工作目录 : $ROOT"
echo "[smoke] 时间戳    : $(date '+%Y-%m-%d %H:%M:%S')"
echo "==============================================================="

on_exit() {
    ec=$?
    echo ""
    echo "[smoke] TRAP exit_code=$ec at $(date '+%H:%M:%S')"
}
trap on_exit EXIT

# ---------- STEP 1: 备份现有真实数据（如有） ----------
echo ""
echo "=== STEP 1: 备份 data/v42（如存在非空）==="
if [[ -d "data/v42" ]] && [[ -n "$(ls -A data/v42 2>/dev/null || true)" ]]; then
    ts=$(date +%s)
    cp -r data/v42 "data/v42_real_backup_${ts}"
    rm -rf data/v42
    echo "[smoke] 已备份到 data/v42_real_backup_${ts}"
else
    echo "[smoke] data/v42 不存在或为空，跳过备份"
fi

# ---------- STEP 2: 生成 mock 数据 ----------
echo ""
echo "=== STEP 2: 生成 mock 数据集 (3 users × 2 exercises × 3 classes × 15 reps = 270) ==="
python tools/sandbox_data_mock.py --out data/v42 --force \
    || { echo "[smoke] STEP 2 FAILED"; exit 1; }

# ---------- STEP 3: 校验数据集（允许非零退出：270 可能 < 阈值） ----------
echo ""
echo "=== STEP 3: 校验 V4.2 数据集 ==="
if [[ -f tools/validate_v42_dataset.py ]]; then
    python tools/validate_v42_dataset.py --data-root data/v42 --min-reps-per-class 10 \
        || echo "[smoke] validate 非零退出（可能因为 Agent-A 尚未完成，或 min-reps 不足，冒烟容忍）"
else
    echo "[smoke] tools/validate_v42_dataset.py 不存在（Agent-A 尚未提交），跳过"
fi

# ---------- STEP 4: 预训练 Encoder（smoke epochs=5） ----------
echo ""
echo "=== STEP 4: 预训练 Vision + EMG Encoder (epochs=5 smoke) ==="
python tools/pretrain_encoders.py --exercise both --data-root data/v42 --epochs 5 \
    || { echo "[smoke] STEP 4 FAILED (pretrain_encoders)"; exit 1; }

# ---------- STEP 5/6: Fusion Head（Agent-B 可能未完成，失败容忍） ----------
echo ""
echo "=== STEP 5: 融合头训练 curl (epochs=10) ==="
python tools/train_fusion_head.py --exercise curl --data-root data/v42 --epochs 10 \
    || echo "[smoke] STEP 5 非零退出（Agent-B 未完成 train_fusion_head.py 属预期）"

echo ""
echo "=== STEP 6: 融合头训练 squat (epochs=10) ==="
python tools/train_fusion_head.py --exercise squat --data-root data/v42 --epochs 10 \
    || echo "[smoke] STEP 6 非零退出（Agent-B 未完成 train_fusion_head.py 属预期）"

# ---------- STEP 7: 汇报产物 ----------
echo ""
echo "=== STEP 7: 产物一览 ==="
echo "-- hardware_engine/cognitive/weights/ --"
ls -lh hardware_engine/cognitive/weights/ 2>/dev/null || echo "(weights 目录不存在)"
echo ""
echo "-- git status --"
git status --short || true

echo ""
echo "==============================================================="
echo "[smoke] 完成 at $(date '+%H:%M:%S')"
echo "==============================================================="
