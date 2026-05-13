#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
train_gru_three_class_bicep.py — 弯举 (bicep_curl) 三分类 CompensationGRU 训练.

镜像 tools/train_gru_three_class.py (深蹲侧), 核心差异:
  - 数据源: 本地真录 (data/bicep_curl/{golden,lazy,bad}/)
           + 增强副本 (data/bicep_curl_augmented/{golden,lazy,bad}/)
  - 标签映射 (用户确认):
      golden → standard     (CLASS_GOLDEN = 0)
      bad    → compensating (CLASS_LAZY   = 1)   ← 弯举 bad = 摆臂借力 = 代偿
      lazy   → non_standard (CLASS_BAD    = 2)   ← 弯举 lazy = 收缩幅度不足

  - 输出: hardware_engine/extreme_fusion_gru_bicep.pt (与深蹲独立)

归一化 + Self-test + CompensationGRU 架构完全复用深蹲侧.

用法:
    python3 tools/train_gru_three_class_bicep.py --epochs 20
    python3 tools/train_gru_three_class_bicep.py --selftest-only
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import random
import sys
import time
from collections import Counter

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hardware_engine.cognitive.fusion_model import (  # noqa: E402
    CompensationGRU,
    CLASS_GOLDEN,   # 0 = standard
    CLASS_LAZY,     # 1 = compensating
    CLASS_BAD,      # 2 = non_standard
    CLASS_NAMES,    # ['standard', 'compensating', 'non_standard']
)

# 弯举用户标签 (CSV 里 label 列) → 类别索引
BICEP_LABEL_TO_CLASS = {
    "golden": CLASS_GOLDEN,   # standard
    "bad":    CLASS_LAZY,     # compensating (弯举 bad = 摆臂借力)
    "lazy":   CLASS_BAD,      # non_standard (弯举 lazy = 收缩不足)
}

DEFAULT_DATA_DIRS = [
    os.path.join(ROOT, "data", "bicep_curl"),
    os.path.join(ROOT, "data", "bicep_curl_augmented"),
]
DEFAULT_OUT = os.path.join(ROOT, "hardware_engine", "extreme_fusion_gru_bicep.pt")
SEQ_LEN = 30
STRIDE = 10   # 滑窗步长, 平衡样本数与信息冗余


# ---------------------------------------------------------------------- 数据加载
def _load_csv_rows(path):
    """读 CSV → (rows, label_str).  label_str 来自最后一列 'label'."""
    rows = []
    label_str = None
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    rows.append({
                        "Ang_Vel":        float(row["Ang_Vel"]),
                        "Angle":          float(row["Angle"]),
                        "Ang_Accel":      float(row["Ang_Accel"]),
                        "Target_RMS":     float(row["Target_RMS"]),
                        "Comp_RMS":       float(row["Comp_RMS"]),
                        "Symmetry_Score": float(row["Symmetry_Score"]),
                        "Phase_Progress": float(row["Phase_Progress"]),
                    })
                    if label_str is None and "label" in row:
                        label_str = row["label"].strip()
                except (KeyError, ValueError):
                    continue
    except IOError:
        pass
    return rows, label_str


def _rows_to_array(rows):
    """list[dict] → ndarray(n, 7)  列序: Ang_Vel, Angle, Ang_Accel, Target_RMS, Comp_RMS, Sym, Phase."""
    arr = np.zeros((len(rows), 7), dtype=np.float32)
    for i, r in enumerate(rows):
        arr[i] = [
            r["Ang_Vel"], r["Angle"], r["Ang_Accel"],
            r["Target_RMS"], r["Comp_RMS"],
            r["Symmetry_Score"], r["Phase_Progress"],
        ]
    return arr


def _normalize_inplace(features):
    """V7.15 归一化 (与深蹲训练 + 板端推理严格一致).

    训练分布 0-1 与推理 muscle_activation.pct / 100 对齐, 避免模型见过 >1 推理永远 <1.
    """
    features[:, 0] = np.clip(features[:, 0] / 30.0, -3.0, 3.0)   # Ang_Vel
    features[:, 1] = features[:, 1] / 180.0                      # Angle
    features[:, 2] = np.clip(features[:, 2] / 10.0, -1.0, 1.0)   # Ang_Accel
    features[:, 3] = np.clip(features[:, 3], 0.0, 100.0) / 100.0 # Target_RMS
    features[:, 4] = np.clip(features[:, 4], 0.0, 100.0) / 100.0 # Comp_RMS
    # Symmetry / Phase 本身 0-1, 不动
    return features


def _gather_csvs(data_dirs):
    """遍历给定目录列表, 返回 [(path, label_class_idx), ...]."""
    out = []
    for root in data_dirs:
        if not os.path.isdir(root):
            continue
        for user_label, class_idx in BICEP_LABEL_TO_CLASS.items():
            subdir = os.path.join(root, user_label)
            if not os.path.isdir(subdir):
                continue
            for fp in sorted(glob.glob(os.path.join(subdir, "*.csv"))):
                out.append((fp, class_idx, user_label))
    return out


class _BicepCurlDataset(Dataset):
    """每条 CSV 按 stride 切滑窗, label 来自目录映射."""

    def __init__(self, csv_list, seq_len=SEQ_LEN, stride=STRIDE):
        self.samples = []
        self.labels = []
        self.seq_len = seq_len
        dropped = 0
        for fp, class_idx, _user_label in csv_list:
            rows, file_label = _load_csv_rows(fp)
            if len(rows) < seq_len:
                dropped += 1
                continue
            arr = _rows_to_array(rows)
            _normalize_inplace(arr)
            # stride 滑窗
            for start in range(0, len(arr) - seq_len + 1, stride):
                self.samples.append(arr[start:start + seq_len])
                self.labels.append(class_idx)
        if dropped:
            print(f"[DATA] 丢弃 {dropped} 条 CSV (行数 < {seq_len})")

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        return (torch.tensor(self.samples[idx], dtype=torch.float32),
                self.labels[idx])


# ---------------------------------------------------------------------- 训练
def train(data_dirs, out_path, epochs=20, batch_size=32, lr=5e-3, seed=42):
    torch.manual_seed(seed); np.random.seed(seed); random.seed(seed)

    print("=" * 60)
    print("  弯举 3-class GRU 训练")
    print("=" * 60)

    csv_list = _gather_csvs(data_dirs)
    if not csv_list:
        print(f"[FATAL] 数据目录为空: {data_dirs}")
        return False
    print(f"[DATA] CSV 文件数: {len(csv_list)}")
    cnt_file = Counter(lb for _, _, lb in csv_list)
    for k in ("golden", "bad", "lazy"):
        print(f"        {k:7s}: {cnt_file.get(k, 0)} CSV")

    dataset = _BicepCurlDataset(csv_list, seq_len=SEQ_LEN, stride=STRIDE)
    print(f"[DATA] 滑窗样本数: {len(dataset)}  (seq_len={SEQ_LEN}, stride={STRIDE})")
    if len(dataset) < 100:
        print(f"[FATAL] 窗口数 {len(dataset)} < 100, 建议先跑 tools/augment_curl_data.py 扩增数据")
        return False

    # 类别权重平衡
    y_cnt = Counter(dataset.labels)
    total = sum(y_cnt.values())
    weights = torch.tensor([
        total / (3.0 * max(y_cnt.get(CLASS_GOLDEN, 1), 1)),
        total / (3.0 * max(y_cnt.get(CLASS_LAZY,   1), 1)),
        total / (3.0 * max(y_cnt.get(CLASS_BAD,    1), 1)),
    ], dtype=torch.float32)
    print(f"[DATA] 按类样本数: "
          f"standard={y_cnt.get(CLASS_GOLDEN, 0)}, "
          f"compensating={y_cnt.get(CLASS_LAZY, 0)}, "
          f"non_standard={y_cnt.get(CLASS_BAD, 0)}")
    print(f"[DATA] class weights: {[round(w, 3) for w in weights.tolist()]}")

    dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=0)
    model = CompensationGRU(input_size=7, hidden_size=16)

    cls_criterion = nn.CrossEntropyLoss(weight=weights)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=8, gamma=0.5)

    sim_target_by_cls = {CLASS_GOLDEN: 1.0, CLASS_LAZY: 0.6, CLASS_BAD: 0.3}

    print(f"\n[TRAIN] {epochs} epochs, batch={batch_size}, lr={lr}")
    t0 = time.time()
    conf_mat = np.zeros((3, 3), dtype=np.int64)
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
        per_cls_acc = [conf_mat[i, i] / max(conf_mat[i].sum(), 1) for i in range(3)]
        print(f"  Ep {ep:02d}/{epochs}  loss={total_loss/len(dataloader):.4f}  "
              f"acc={acc*100:.1f}%  per-cls "
              f"[{per_cls_acc[0]*100:.0f}/{per_cls_acc[1]*100:.0f}/{per_cls_acc[2]*100:.0f}]%")

    print(f"\n[TRAIN] 用时 {time.time() - t0:.1f}s")
    print("\n[TRAIN] 最终混淆矩阵 (行=真实, 列=预测):")
    print("             pred_std  pred_comp  pred_non")
    for i, name in enumerate(CLASS_NAMES):
        print(f"  true_{name:12s} {conf_mat[i].tolist()}")

    ok = selftest(model, data_dirs)
    if not ok:
        print("\n❌ [SELFTEST] 失败, 不保存 .pt")
        return False

    tmp = out_path + ".tmp"
    torch.save(model.state_dict(), tmp)
    os.rename(tmp, out_path)
    print(f"\n✅ 模型保存: {out_path}  ({os.path.getsize(out_path)/1024:.1f} KB)")
    return True


# ---------------------------------------------------------------------- Self-test
def _pick_csv_for(user_label, data_dirs):
    """优先从 augmented 目录抽一条该标签的 CSV."""
    for root in reversed(data_dirs):    # 后者优先: augmented 排在 DEFAULT_DATA_DIRS 后, 先尝试
        pool = sorted(glob.glob(os.path.join(root, user_label, "*.csv")))
        if pool:
            rng = np.random.RandomState(hash(user_label) & 0xffff)
            return pool[rng.randint(0, len(pool))]
    raise RuntimeError(f"无 {user_label} CSV (in {data_dirs})")


def _make_case(user_label, data_dirs):
    fp = _pick_csv_for(user_label, data_dirs)
    rows, _ = _load_csv_rows(fp)
    if len(rows) < SEQ_LEN:
        raise RuntimeError(f"{fp} 行数 {len(rows)} < {SEQ_LEN}")
    arr = _rows_to_array(rows)
    # 抽中间 30 帧, 避开首尾边界
    mid = len(arr) // 2
    start = max(0, mid - SEQ_LEN // 2)
    arr = arr[start:start + SEQ_LEN]
    _normalize_inplace(arr)
    return arr.astype(np.float32), fp


def selftest(model, data_dirs=None):
    if data_dirs is None:
        data_dirs = DEFAULT_DATA_DIRS
    model.eval()
    cases = [
        ("golden", CLASS_GOLDEN, "standard"),
        ("bad",    CLASS_LAZY,   "compensating"),
        ("lazy",   CLASS_BAD,    "non_standard"),
    ]
    print("\n" + "-" * 60)
    print("  自检 (offline, 从真实 CSV 抽样)")
    print("-" * 60)
    all_ok = True
    with torch.no_grad():
        for user_label, expected_cls, expected_name in cases:
            try:
                x, fp = _make_case(user_label, data_dirs)
            except Exception as e:
                print(f"  ⚠️  {user_label}: 无法构造 case ({e})")
                all_ok = False
                continue
            out = model.infer(x)
            actual = out["classification"]
            sim = out["similarity"]
            conf = out["confidence"]
            ok = (actual == expected_name)
            mark = "✅" if ok else "❌"
            print(f"  {mark} user_label={user_label:7s} → pred={actual:13s} "
                  f"(expected {expected_name}) sim={sim:.2f} conf={conf:.2f}")
            print(f"       src: {os.path.basename(fp)}")
            if not ok:
                all_ok = False
    print("-" * 60)
    print("  🟢 SELFTEST PASS" if all_ok else "  🔴 SELFTEST FAIL")
    return all_ok


# ---------------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="弯举 3 分类 CompensationGRU 训练")
    ap.add_argument("--data-dirs", nargs="+", default=DEFAULT_DATA_DIRS,
                    help="数据根目录列表 (包含 golden/lazy/bad 子目录)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=5e-3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--selftest-only", action="store_true",
                    help="不训练, 加载现有 .pt 跑自检")
    args = ap.parse_args()

    if args.selftest_only:
        if not os.path.exists(args.out):
            print(f"[FATAL] {args.out} 不存在")
            return 2
        model = CompensationGRU(input_size=7, hidden_size=16)
        model.load_state_dict(torch.load(args.out, map_location="cpu"))
        ok = selftest(model, args.data_dirs)
        return 0 if ok else 1

    ok = train(args.data_dirs, args.out, args.epochs,
               args.batch_size, args.lr, args.seed)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
