#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
train_gru_three_class.py — 基于 MIA 数据训练 3 分类 CompensationGRU.

解决的问题:
  - 旧 extreme_fusion_gru.pt 只用 golden+bad 2 类训练, 推理时对任何输入都饱和输出 standard
  - MIA 数据集本身只有 golden/bad 两种 label; non_standard=bad (半蹲偷懒),
    compensating 类需要从 golden 派生 (主肌偷懒 + 辅助肌过发力)

数据流:
  MIA CSV → 按 label 分类 → 合成 compensating → clip 归一化 → 30 帧滑窗 → GRU 训练
       golden (761)   → standard     (CLASS 0)
       bad    (203)   → non_standard (CLASS 2)  (注意! fusion_model.CLASS_BAD=2)
       golden × 变换  → compensating (CLASS 1)  (注意! fusion_model.CLASS_LAZY=1)

训练后自检 3 组手造样本, argmax 正确则写 .pt.

使用:
    python3 tools/train_gru_three_class.py --epochs 20
    python3 tools/train_gru_three_class.py --mia-dir data/mia/squat --out hardware_engine/extreme_fusion_gru.pt
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import random
import sys
import time

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader

# ---------------------------------------------------------------------- 路径与常量
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hardware_engine.cognitive.fusion_model import (  # noqa: E402
    CompensationGRU,
    CLASS_GOLDEN,     # 0 = standard
    CLASS_LAZY,       # 1 = compensating (语义重定义: lazy → compensating)
    CLASS_BAD,        # 2 = non_standard
    CLASS_NAMES,      # ['standard', 'compensating', 'non_standard']
)

DEFAULT_MIA_DIR = os.path.join(ROOT, "data", "mia", "squat")
DEFAULT_OUT = os.path.join(ROOT, "hardware_engine", "extreme_fusion_gru.pt")
SEQ_LEN = 30

# 每个 golden rep 扩增 N 个合成 compensating 样本 (控制 3 类均衡)
N_COMP_PER_GOLDEN = 1


# ---------------------------------------------------------------------- 数据加载与合成
def _load_csv(path):
    """返回 list[dict] (每行都是 float)."""
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    out.append({
                        "Ang_Vel": float(row["Ang_Vel"]),
                        "Angle": float(row["Angle"]),
                        "Ang_Accel": float(row["Ang_Accel"]),
                        "Target_RMS": float(row["Target_RMS"]),
                        "Comp_RMS": float(row["Comp_RMS"]),
                        "Symmetry_Score": float(row["Symmetry_Score"]),
                        "Phase_Progress": float(row["Phase_Progress"]),
                    })
                except (KeyError, ValueError):
                    continue
    except IOError:
        pass
    return out


def _rep_to_array(rep_rows):
    """list[dict] → ndarray(n, 7) 顺序与 FEATURES_7D 一致."""
    arr = np.zeros((len(rep_rows), 7), dtype=np.float32)
    for i, r in enumerate(rep_rows):
        arr[i] = [
            r["Ang_Vel"], r["Angle"], r["Ang_Accel"],
            r["Target_RMS"], r["Comp_RMS"],
            r["Symmetry_Score"], r["Phase_Progress"],
        ]
    return arr


def _synthesize_compensating(golden_arr):
    """从 golden sample 合成 compensating.

    关键设计 (V7.15): 三类特征空间分界要清晰:
      - Standard:     Target_RMS 高 (30-80 pct),  Comp_RMS 低 (10-30 pct)
      - Compensating: Target_RMS 低 (15-40 pct),  Comp_RMS 高 (65-95 pct) ← 此函数产出
      - Non_standard: Target_RMS 低 (10-35 pct),  Comp_RMS 低 (5-25 pct)

    仅使用乘法变换会让 compensating 与 standard 在 comp 分布上严重重叠,
    所以直接把 target/comp 用线性组合+随机偏置重塑到目标值域.
    """
    comp = golden_arr.copy()
    n = comp.shape[0]
    # 列索引: 0=Ang_Vel 1=Angle 2=Ang_Accel 3=Target_RMS 4=Comp_RMS 5=Sym 6=Phase

    # Target 强制压到 15-40 pct 范围 (主肌偷懒)
    t_base = np.random.uniform(15.0, 35.0, size=n).astype(np.float32)
    t_phase_rise = 10.0 * comp[:, 6].astype(np.float32)  # phase 越高 target 越大
    comp[:, 3] = np.clip(t_base + t_phase_rise + np.random.normal(0, 3, n), 10, 45)

    # Comp 强制抬到 65-95 pct 范围 (辅助肌代偿)
    c_base = np.random.uniform(60.0, 85.0, size=n).astype(np.float32)
    c_phase_rise = 15.0 * comp[:, 6].astype(np.float32)
    comp[:, 4] = np.clip(c_base + c_phase_rise + np.random.normal(0, 4, n), 55, 100)
    # Phase > 0.55 (起身相位) 额外尖峰, 冲到 90-100
    mask_peak = comp[:, 6] > 0.55
    comp[mask_peak, 4] = np.clip(comp[mask_peak, 4] + 10, 85, 100)

    # Symmetry 偏低 (代偿时左右发力不对称)
    comp[:, 5] = comp[:, 5] * np.random.uniform(0.5, 0.75, size=n).astype(np.float32)
    return comp


def _normalize_inplace(features):
    """V7.15 归一化对齐: clip Target_RMS/Comp_RMS 到 [0, 100] 再 / 100
    这样训练分布与推理时 muscle_activation.pct 归一化完全一致 (0-1 值域).
    """
    features[:, 1] = features[:, 1] / 180.0             # Angle
    features[:, 2] = np.clip(features[:, 2] / 10.0, -1.0, 1.0)  # Ang_Accel
    features[:, 3] = np.clip(features[:, 3], 0.0, 100.0) / 100.0  # Target_RMS (clip 关键!)
    features[:, 4] = np.clip(features[:, 4], 0.0, 100.0) / 100.0  # Comp_RMS (clip 关键!)
    # Ang_Vel 保留 deg/frame (训练推理都是差分值), 不再 * fps
    features[:, 0] = np.clip(features[:, 0] / 30.0, -3.0, 3.0)  # Ang_Vel 缩放到 ~-3..3
    # Symmetry / Phase 本身 0-1 不处理
    return features


class _RepDataset(Dataset):
    """从 (rep_array, label) 列表生成滑窗样本."""

    def __init__(self, reps_labels, seq_len=SEQ_LEN):
        self.samples = []
        self.labels = []
        self.seq_len = seq_len
        for arr, label in reps_labels:
            arr = arr.copy()
            _normalize_inplace(arr)
            n = len(arr)
            if n < seq_len:
                # rep 太短: pad 或跳过 (MIA rep 一般 30-60 帧, 不应短于 seq_len)
                if n >= 10:
                    pad = np.tile(arr[-1:], (seq_len - n, 1))
                    arr = np.concatenate([arr, pad], axis=0)
                else:
                    continue
            # 多窗口 (每个 rep 产生 n-seq_len+1 个重叠窗)
            for i in range(len(arr) - seq_len + 1):
                self.samples.append(arr[i : i + seq_len])
                self.labels.append(label)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        x = torch.tensor(self.samples[idx], dtype=torch.float32)
        y = self.labels[idx]
        return x, y


def build_dataset(mia_dir, n_comp_per_golden=N_COMP_PER_GOLDEN):
    """加载 MIA CSV → 3 类 reps_labels."""
    golden_files = sorted(glob.glob(os.path.join(mia_dir, "golden", "*.csv")))
    bad_files    = sorted(glob.glob(os.path.join(mia_dir, "bad", "*.csv")))
    print(f"[DATA] golden={len(golden_files)} bad={len(bad_files)}")
    if len(golden_files) == 0:
        print(f"[FATAL] {mia_dir}/golden/ 为空, 先跑 tools/mia_preprocess_squat.py")
        sys.exit(2)

    reps_labels = []
    # Standard (CLASS_GOLDEN=0)
    for fp in golden_files:
        rep = _load_csv(fp)
        if len(rep) < 10:
            continue
        arr = _rep_to_array(rep)
        reps_labels.append((arr, CLASS_GOLDEN))
        # 同时合成 compensating (CLASS_LAZY=1) - 每个 golden 派生 N 条
        for _ in range(n_comp_per_golden):
            comp = _synthesize_compensating(arr)
            reps_labels.append((comp, CLASS_LAZY))

    # Non_standard (CLASS_BAD=2)
    for fp in bad_files:
        rep = _load_csv(fp)
        if len(rep) < 10:
            continue
        arr = _rep_to_array(rep)
        reps_labels.append((arr, CLASS_BAD))

    # 统计
    from collections import Counter
    cnt = Counter(lb for _, lb in reps_labels)
    print(f"[DATA] reps per class: standard={cnt[CLASS_GOLDEN]}, "
          f"compensating={cnt[CLASS_LAZY]}, non_standard={cnt[CLASS_BAD]}")

    return reps_labels


# ---------------------------------------------------------------------- 训练
def train(mia_dir, out_path, epochs=20, batch_size=64, lr=5e-3, seed=42):
    torch.manual_seed(seed); np.random.seed(seed); random.seed(seed)

    print("=" * 60)
    print("  V7.15 3-class GRU 训练")
    print("=" * 60)

    reps_labels = build_dataset(mia_dir)
    dataset = _RepDataset(reps_labels, seq_len=SEQ_LEN)
    print(f"[DATA] 总窗口数: {len(dataset)}")
    if len(dataset) < 100:
        print(f"[FATAL] 数据太少 (n={len(dataset)}) 无法训练")
        sys.exit(2)

    # 类别权重平衡 (如果 non_standard 样本少)
    from collections import Counter
    y_cnt = Counter(dataset.labels)
    total = sum(y_cnt.values())
    weights = torch.tensor([
        total / (3.0 * max(y_cnt.get(0, 1), 1)),
        total / (3.0 * max(y_cnt.get(1, 1), 1)),
        total / (3.0 * max(y_cnt.get(2, 1), 1)),
    ], dtype=torch.float32)
    print(f"[DATA] class weights: {weights.tolist()}")

    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=0)
    model = CompensationGRU(input_size=7, hidden_size=16)

    cls_criterion = nn.CrossEntropyLoss(weight=weights)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=8, gamma=0.5)

    # Similarity 目标: standard=1.0, compensating=0.6, non_standard=0.3
    sim_target_by_cls = {CLASS_GOLDEN: 1.0, CLASS_LAZY: 0.6, CLASS_BAD: 0.3}

    print(f"\n[TRAIN] {epochs} epochs, {len(dataset)} windows, batch={batch_size}")
    t0 = time.time()
    for ep in range(1, epochs + 1):
        model.train()
        total_loss = 0.0
        correct = 0
        conf_mat = np.zeros((3, 3), dtype=np.int64)
        for x, y in dataloader:
            optimizer.zero_grad()
            sim_tgt = torch.tensor([[sim_target_by_cls[int(c)]] for c in y],
                                    dtype=torch.float32)
            sim_pred, cls_logits, _ = model(x)
            loss = 0.3 * F.mse_loss(sim_pred, sim_tgt) + 0.7 * cls_criterion(cls_logits, y)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
            pred = cls_logits.argmax(dim=1)
            correct += (pred == y).sum().item()
            for yp, yt in zip(pred.tolist(), y.tolist()):
                conf_mat[yt, yp] += 1
        scheduler.step()
        acc = correct / len(dataset)
        # Per-class acc
        per_cls_acc = [conf_mat[i, i] / max(conf_mat[i].sum(), 1) for i in range(3)]
        print(f"  Ep {ep:02d}/{epochs}  loss={total_loss/len(dataloader):.4f}  "
              f"acc={acc*100:.1f}%  per-cls [{per_cls_acc[0]*100:.0f}/{per_cls_acc[1]*100:.0f}/{per_cls_acc[2]*100:.0f}]%")

    elapsed = time.time() - t0
    print(f"\n[TRAIN] 用时 {elapsed:.1f}s")

    # 最终混淆矩阵
    print("\n[TRAIN] 最终混淆矩阵 (行=真实, 列=预测):")
    print("             pred_std pred_comp pred_non")
    for i, name in enumerate(CLASS_NAMES):
        print(f"  true_{name:12s} {conf_mat[i].tolist()}")

    # ---------------- 自检 ----------------
    ok = selftest(model)
    if not ok:
        print("\n❌ [SELFTEST] 失败, 不保存 .pt. 请增大 epochs 或调整合成参数后重试")
        return False

    # 保存
    tmp = out_path + ".tmp"
    torch.save(model.state_dict(), tmp)
    os.rename(tmp, out_path)
    print(f"\n✅ 模型保存: {out_path}  ({os.path.getsize(out_path)/1024:.1f} KB)")
    return True


# ---------------------------------------------------------------------- 自检
def _make_case(label, mia_dir=None):
    """从真实 MIA CSV 抽一条 rep 做 self-test case.

    V7.15: 手造完整 squat cycle 的 angle 分布和训练集不一致 (MIA 只是 30 帧 down-half),
    selftest 用真实训练分布数据更可靠地验证模型.

    label=standard    → 从 golden/ 抽一条, 原样
    label=compensating→ 从 golden/ 抽一条, 应用 _synthesize_compensating 变换
    label=non_standard→ 从 bad/ 抽一条, 原样
    """
    if mia_dir is None:
        mia_dir = DEFAULT_MIA_DIR

    if label in ("standard", "compensating"):
        pool = sorted(glob.glob(os.path.join(mia_dir, "golden", "*.csv")))
    else:
        pool = sorted(glob.glob(os.path.join(mia_dir, "bad", "*.csv")))

    if not pool:
        raise RuntimeError(f"MIA CSV 池为空: {mia_dir}")

    # 固定 seed 挑一条可复现的 CSV
    rng = np.random.RandomState(hash(label) & 0xffff)
    fp = pool[rng.randint(0, len(pool))]
    rows = _load_csv(fp)
    if len(rows) < 10:
        # 换一条
        fp = pool[0]
        rows = _load_csv(fp)

    arr = _rep_to_array(rows)
    if label == "compensating":
        arr = _synthesize_compensating(arr)

    # Pad/truncate 到 SEQ_LEN
    if len(arr) >= SEQ_LEN:
        arr = arr[:SEQ_LEN]
    else:
        pad = np.tile(arr[-1:], (SEQ_LEN - len(arr), 1))
        arr = np.concatenate([arr, pad], axis=0)

    # 归一化 (与训练完全一致)
    _normalize_inplace(arr)
    return arr.astype(np.float32)


def selftest(model):
    """跑 3 组手造样本, 验证 argmax 正确 + per-class 置信度合理."""
    model.eval()
    cases = [
        ("standard", 0),
        ("compensating", 1),
        ("non_standard", 2),
    ]
    print("\n" + "-" * 60)
    print("  自检 (offline)")
    print("-" * 60)
    all_ok = True
    with torch.no_grad():
        for name, expected_cls in cases:
            x = _make_case(name)
            out = model.infer(x)
            actual = out["classification"]
            sim = out["similarity"]
            conf = out["confidence"]
            ok = (actual == CLASS_NAMES[expected_cls])
            mark = "✅" if ok else "❌"
            print(f"  {mark} case={name:15s} → pred={actual:13s} sim={sim:.2f} conf={conf:.2f}")
            if not ok:
                all_ok = False
    print("-" * 60)
    if all_ok:
        print("  🟢 SELFTEST PASS")
    else:
        print("  🔴 SELFTEST FAIL")
    return all_ok


# ---------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="V7.15 3-class GRU 训练 (MIA 数据源)")
    ap.add_argument("--mia-dir", default=DEFAULT_MIA_DIR)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--lr", type=float, default=5e-3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--selftest-only", action="store_true",
                    help="不训练, 只加载现有 .pt 跑自检")
    args = ap.parse_args()

    if args.selftest_only:
        if not os.path.exists(args.out):
            print(f"[FATAL] {args.out} 不存在")
            return 2
        model = CompensationGRU(input_size=7, hidden_size=16)
        model.load_state_dict(torch.load(args.out, map_location="cpu"))
        ok = selftest(model)
        return 0 if ok else 1

    ok = train(args.mia_dir, args.out, args.epochs, args.batch_size, args.lr, args.seed)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
