#!/usr/bin/env python3
"""
Quick data quality checker — run after collection to verify datasets.
Usage: python validate_data.py /path/to/training_data/
"""
import csv
import sys
import os
from pathlib import Path

MIN_FRAMES = 60    # 3 seconds at 20Hz
MIN_ANGLE_RANGE = 15.0

def check_file(path):
    issues = []
    with open(path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    n = len(rows)
    if n < MIN_FRAMES:
        issues.append(f"太少: {n} 帧 (最少 {MIN_FRAMES})")

    if n == 0:
        return n, issues

    angles = [float(r["Angle"]) for r in rows if r.get("Angle")]
    if angles:
        rng = max(angles) - min(angles)
        if rng < MIN_ANGLE_RANGE:
            issues.append(f"角度范围仅 {rng:.1f}° (最少 {MIN_ANGLE_RANGE}°)")

    emg_vals = [float(r["Target_RMS"]) for r in rows if r.get("Target_RMS")]
    if emg_vals and max(emg_vals) < 1.0:
        issues.append("EMG 全零 — 传感器可能未连接")

    labels = set(r.get("label", "") for r in rows)
    if len(labels) > 1:
        issues.append(f"混合标签: {labels}")

    return n, issues


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    csvs = sorted(Path(data_dir).glob("train_*.csv"))

    if not csvs:
        print(f"在 {data_dir} 中未找到 train_*.csv 文件")
        sys.exit(1)

    print(f"\n{'文件':<50} {'帧数':>6}  状态")
    print("-" * 75)

    all_ok = True
    total_frames = 0
    for p in csvs:
        n, issues = check_file(p)
        total_frames += n
        name = p.name
        if issues:
            all_ok = False
            print(f"{name:<50} {n:>6}  ⚠ {'; '.join(issues)}")
        else:
            print(f"{name:<50} {n:>6}  ✅")

    print("-" * 75)
    print(f"{'合计':<50} {total_frames:>6}  {'全部通过 ✅' if all_ok else '有问题 ⚠'}")
    print()

    if all_ok:
        print("可以开始训练:")
        print(f"  python train_model.py --data {data_dir} --out ./models --epochs 25")


if __name__ == "__main__":
    main()
