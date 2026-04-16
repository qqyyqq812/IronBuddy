#!/usr/bin/env python3
"""
IronBuddy — GRU Model Training Script
======================================
Loads all labeled CSV files from a data directory, trains the upgraded
CompensationGRU model, and saves the weights to extreme_fusion_gru.pt.

Works on a cloud GPU or the edge board (CPU-only fallback).

Usage
-----
    # Train on data in current directory
    python train_model.py

    # Specify data dir and output dir
    python train_model.py --data /data/training --out /models

    # Quick smoke-test run
    python train_model.py --epochs 5 --data ./sample_data
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader, random_split

try:
    from torch.utils.tensorboard import SummaryWriter
    HAS_TB = True
except ImportError:
    HAS_TB = False

# ---------------------------------------------------------------------------
# Locate the cognitive package relative to this script
# ---------------------------------------------------------------------------
_TOOLS_DIR  = Path(__file__).resolve().parent
_ENGINE_DIR = _TOOLS_DIR.parent / "hardware_engine"
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

from cognitive.fusion_model import (
    CLASS_BAD,
    CLASS_GOLDEN,
    CLASS_LAZY,
    CLASS_NAMES,
    FEATURES_4D,
    FEATURES_7D,
    CompensationGRU,
    SquatDataset,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_SEQ_LEN   = 30
DEFAULT_EPOCHS    = 25
DEFAULT_BATCH     = 64
DEFAULT_LR        = 0.005
DEFAULT_VAL_SPLIT = 0.15

LABEL_GLOB_MAP = {
    "golden": CLASS_GOLDEN,
    "lazy":   CLASS_LAZY,
    "bad":    CLASS_BAD,
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _detect_label(csv_path: str) -> int | None:
    """Infer label from filename convention: train_<exercise>_<label>*.csv"""
    name = Path(csv_path).name.lower()
    for keyword, label in LABEL_GLOB_MAP.items():
        if keyword in name:
            return label
    return None


def load_all_csvs(data_dir: str) -> list[tuple[pd.DataFrame, int]]:
    """
    Scans data_dir recursively for CSV files matching train_*_*.csv
    (supports both train_squat_*.csv and train_bicep_curl_*.csv).
    """
    pattern = os.path.join(data_dir, "**", "train_*_*.csv")
    paths   = glob.glob(pattern, recursive=True)

    if not paths:
        pattern2 = os.path.join(data_dir, "**", "*.csv")
        paths    = glob.glob(pattern2, recursive=True)

    data_list: list[tuple[pd.DataFrame, int]] = []
    for p in sorted(paths):
        label = _detect_label(p)
        if label is None:
            print(f"  [SKIP] Cannot determine label for: {p}")
            continue
        try:
            df = pd.read_csv(p)
            if len(df) < DEFAULT_SEQ_LEN + 1:
                print(f"  [SKIP] Too short ({len(df)} rows): {p}")
                continue
            data_list.append((df, label))
            print(f"  [OK]   {Path(p).name}  — {len(df)} rows, label={CLASS_NAMES[label]}")
        except Exception as e:
            print(f"  [ERR]  {p}: {e}")

    return data_list


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

def train(
    data_dir:   str,
    out_dir:    str,
    seq_len:    int   = DEFAULT_SEQ_LEN,
    epochs:     int   = DEFAULT_EPOCHS,
    batch_size: int   = DEFAULT_BATCH,
    lr:         float = DEFAULT_LR,
    val_split:  float = DEFAULT_VAL_SPLIT,
    device_str: str   = "auto",
) -> None:
    # --- device ---
    if device_str == "auto":
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        device = torch.device(device_str)
    print(f"\nDevice: {device}")

    # --- data ---
    print(f"\nScanning {data_dir} for training CSVs...")
    data_list = load_all_csvs(data_dir)
    if not data_list:
        print("No usable CSV files found. Exiting.")
        return

    full_dataset = SquatDataset(data_list, seq_len=seq_len)
    n_total = len(full_dataset)
    if n_total == 0:
        print("Dataset empty after windowing. Exiting.")
        return

    n_val   = max(1, int(n_total * val_split))
    n_train = n_total - n_val
    train_ds, val_ds = random_split(
        full_dataset, [n_train, n_val],
        generator=torch.Generator().manual_seed(42),
    )

    train_loader = DataLoader(train_ds, batch_size=batch_size, shuffle=True,  num_workers=0)
    val_loader   = DataLoader(val_ds,   batch_size=batch_size, shuffle=False, num_workers=0)

    print(f"\nDataset  : {n_total} windows  (train={n_train}, val={n_val})")

    # class distribution
    all_labels = [full_dataset.labels[i] for i in range(n_total)]
    for lbl, name in enumerate(CLASS_NAMES):
        cnt = all_labels.count(lbl)
        print(f"  {name:15s}: {cnt:5d} ({100*cnt/n_total:.1f}%)")

    # --- model ---
    model = CompensationGRU(input_size=7).to(device)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"\nModel params: {n_params:,}")

    # --- loss, optimizer ---
    cls_criterion = torch.nn.CrossEntropyLoss()
    optimizer     = torch.optim.Adam(model.parameters(), lr=lr)
    scheduler     = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=epochs, eta_min=lr * 0.05
    )

    # similarity targets per class
    SIM_TARGET = {
        CLASS_GOLDEN: 1.0,
        CLASS_LAZY:   0.5,
        CLASS_BAD:    0.2,
    }

    best_val_acc  = 0.0
    best_val_epoch = 0
    out_path = Path(out_dir) / "extreme_fusion_gru.pt"
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    # TensorBoard
    tb_dir = os.path.join(out_dir, "tb_logs", time.strftime("%Y%m%d_%H%M%S"))
    writer = SummaryWriter(tb_dir) if HAS_TB else None
    if writer:
        print(f"TensorBoard: tensorboard --logdir {os.path.dirname(tb_dir)}")

    print(f"\nTraining {epochs} epochs...")
    print(f"{'Epoch':>5}  {'Train Loss':>10}  {'Train Acc':>9}  {'Val Loss':>8}  {'Val Acc':>7}  LR")
    print("-" * 70)

    for epoch in range(1, epochs + 1):
        # ----- train -----
        model.train()
        t_loss = t_correct = t_total = 0

        for x, y_cls in train_loader:
            x      = x.to(device)
            y_cls  = y_cls.to(device)

            sim_tgt = torch.tensor(
                [SIM_TARGET[int(lbl)] for lbl in y_cls],
                dtype=torch.float32, device=device
            ).unsqueeze(1)

            optimizer.zero_grad()
            sim_pred, cls_logits, _ = model(x)

            loss = 0.4 * F.mse_loss(sim_pred, sim_tgt) + \
                   0.6 * cls_criterion(cls_logits, y_cls)
            loss.backward()
            optimizer.step()

            t_loss    += loss.item() * len(x)
            preds      = cls_logits.argmax(dim=1)
            t_correct += (preds == y_cls).sum().item()
            t_total   += len(x)

        scheduler.step()
        train_loss = t_loss / t_total
        train_acc  = t_correct / t_total

        # ----- validate -----
        model.eval()
        v_loss = v_correct = v_total = 0
        sim_sums = {name: [] for name in CLASS_NAMES}

        with torch.no_grad():
            for x, y_cls in val_loader:
                x     = x.to(device)
                y_cls = y_cls.to(device)

                sim_tgt = torch.tensor(
                    [SIM_TARGET[int(lbl)] for lbl in y_cls],
                    dtype=torch.float32, device=device
                ).unsqueeze(1)

                sim_pred, cls_logits, _ = model(x)
                loss = 0.4 * F.mse_loss(sim_pred, sim_tgt) + \
                       0.6 * cls_criterion(cls_logits, y_cls)

                v_loss    += loss.item() * len(x)
                preds      = cls_logits.argmax(dim=1)
                v_correct += (preds == y_cls).sum().item()
                v_total   += len(x)

                for i, lbl in enumerate(y_cls.cpu().numpy()):
                    sim_sums[CLASS_NAMES[int(lbl)]].append(float(sim_pred[i, 0].item()))

        val_loss = v_loss / v_total
        val_acc  = v_correct / v_total
        cur_lr   = scheduler.get_last_lr()[0]

        print(
            f"{epoch:5d}  {train_loss:10.4f}  {train_acc*100:8.1f}%  "
            f"{val_loss:8.4f}  {val_acc*100:6.1f}%  {cur_lr:.5f}"
        )

        if writer:
            writer.add_scalars("Loss", {"train": train_loss, "val": val_loss}, epoch)
            writer.add_scalars("Accuracy", {"train": train_acc, "val": val_acc}, epoch)
            writer.add_scalar("LR", cur_lr, epoch)
            for cls_name, sim_vals in sim_sums.items():
                if sim_vals:
                    writer.add_histogram(f"Similarity/{cls_name}", np.array(sim_vals), epoch)

        if val_acc >= best_val_acc:
            best_val_acc   = val_acc
            best_val_epoch = epoch
            torch.save(model.state_dict(), out_path)

    # ----- final report -----
    print(f"\nBest val acc: {best_val_acc*100:.1f}% at epoch {best_val_epoch}")

    # Similarity score distribution on validation set
    print("\nSimilarity score distribution (val set):")
    model.load_state_dict(torch.load(out_path, map_location=device))
    model.eval()
    all_sims = {name: [] for name in CLASS_NAMES}
    with torch.no_grad():
        for x, y_cls in val_loader:
            x = x.to(device)
            sim_pred, _, _ = model(x)
            for i, lbl in enumerate(y_cls.numpy()):
                all_sims[CLASS_NAMES[int(lbl)]].append(float(sim_pred[i, 0].item()))

    for name in CLASS_NAMES:
        sims = all_sims[name]
        if sims:
            arr = np.array(sims)
            print(f"  {name:15s}: mean={arr.mean():.3f}  std={arr.std():.3f}  "
                  f"min={arr.min():.3f}  max={arr.max():.3f}")

    # Confusion matrix + final TensorBoard logging
    if writer:
        try:
            from sklearn.metrics import confusion_matrix, classification_report
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            all_preds, all_labels = [], []
            with torch.no_grad():
                for x, y_cls in val_loader:
                    x = x.to(device)
                    _, cls_logits, _ = model(x)
                    all_preds.extend(cls_logits.argmax(dim=1).cpu().numpy())
                    all_labels.extend(y_cls.numpy())

            cm = confusion_matrix(all_labels, all_preds, labels=[0, 1, 2])
            fig, ax = plt.subplots(figsize=(5, 4))
            ax.imshow(cm, cmap="Blues")
            for i in range(3):
                for j in range(3):
                    ax.text(j, i, str(cm[i][j]), ha="center", va="center",
                            color="white" if cm[i][j] > cm.max()/2 else "black")
            ax.set_xticks([0, 1, 2]); ax.set_yticks([0, 1, 2])
            ax.set_xticklabels(CLASS_NAMES); ax.set_yticklabels(CLASS_NAMES)
            ax.set_xlabel("Predicted"); ax.set_ylabel("True")
            ax.set_title("Confusion Matrix")
            plt.tight_layout()
            writer.add_figure("ConfusionMatrix", fig, epochs)
            plt.close(fig)

            # Similarity distribution histograms
            fig2, axes = plt.subplots(1, 3, figsize=(12, 3))
            for idx, name in enumerate(CLASS_NAMES):
                sims = all_sims.get(name, [])
                if sims:
                    axes[idx].hist(sims, bins=20, alpha=0.7, color=["#22c55e", "#f59e0b", "#ef4444"][idx])
                axes[idx].set_title(name); axes[idx].set_xlim(0, 1)
                axes[idx].set_xlabel("Similarity")
            plt.tight_layout()
            writer.add_figure("SimilarityDistribution", fig2, epochs)
            plt.close(fig2)

            present_labels = sorted(set(all_labels))
            present_names = [CLASS_NAMES[i] for i in present_labels]
            report = classification_report(all_labels, all_preds, labels=present_labels, target_names=present_names)
            writer.add_text("ClassificationReport", f"```\n{report}\n```", epochs)
            print(f"\n{report}")
        except ImportError:
            print("[INFO] Install sklearn + matplotlib for confusion matrix in TensorBoard")

        writer.close()

    size_kb = os.path.getsize(out_path) / 1024
    print(f"\nSaved: {out_path}  ({size_kb:.1f} KB)")
    if size_kb > 100:
        print(f"[WARN] Model exceeds 100 KB budget ({size_kb:.1f} KB). "
              "Consider reducing hidden_size.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IronBuddy GRU model training script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--data",   default=".", help="Directory containing train_*_*.csv files")
    p.add_argument("--out",    default=".", help="Output directory for the trained model")
    p.add_argument("--epochs", type=int,   default=DEFAULT_EPOCHS)
    p.add_argument("--batch",  type=int,   default=DEFAULT_BATCH)
    p.add_argument("--lr",     type=float, default=DEFAULT_LR)
    p.add_argument("--seq",    type=int,   default=DEFAULT_SEQ_LEN, help="Sliding window length")
    p.add_argument("--device", default="auto", help="cpu | cuda | auto")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    train(
        data_dir   = args.data,
        out_dir    = args.out,
        seq_len    = args.seq,
        epochs     = args.epochs,
        batch_size = args.batch,
        lr         = args.lr,
        device_str = args.device,
    )
