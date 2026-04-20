#!/usr/bin/env python3
"""
IronBuddy 弯举数据增强器（V4.7）
================================
将 data/bicep_curl/{golden,lazy,bad}/*.csv（仅 3 份自采）10× 扩展为
data/bicep_curl_augmented/{golden,lazy,bad}/*.csv（3 seed + 30 aug = 33 份）。

约束：
- 仅依赖 numpy + csv + random 标准库（板端 Python 3.7 兼容，**不引入 pandas**）
- 仅扰动 Target_RMS / Comp_RMS 两列（肌电幅值域内抖动）
- 不改 Angle / Ang_Vel / Ang_Accel / Symmetry_Score / Phase_Progress 等几何/节律列
- 保留原 seed 到 augmented 目录（命名 `<原名>_seed.csv`），避免训练时丢掉原信号

策略：
- 幅值扰动：乘以 uniform(0.85, 1.15) —— 模拟个体肌力差异
- 高斯噪声：加 N(0, 1.5) 再 clip [0, 100] —— 模拟 ESP32 噪声
- （可选）时间扭曲：uniform(0.9, 1.1) 因子对整段做 1D 线性插值（保持原长）

使用：
    python3 tools/augment_curl_data.py
    python3 tools/augment_curl_data.py --factor 10 --seed 42
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import sys
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _PROJECT_ROOT / "data" / "bicep_curl"
_DST_DIR = _PROJECT_ROOT / "data" / "bicep_curl_augmented"
_LABELS = ("golden", "lazy", "bad")

# 扰动参数
_AMP_MIN, _AMP_MAX = 0.85, 1.15          # 幅值 ±15%
_NOISE_SIGMA = 1.5                       # 高斯 σ（RMS 单位 0-100）
_WARP_MIN, _WARP_MAX = 0.9, 1.1          # 时间扭曲因子 ±10%
_RMS_COLS = ("Target_RMS", "Comp_RMS")   # 只扰动这两列


def _read_csv(path):
    """读取 CSV → (header, rows)。rows 是 list of dict（按列名访问）。"""
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        rows = list(reader)
    return header, rows


def _write_csv(path, header, rows):
    """写 CSV。rows 是 list of dict，按 header 顺序输出。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _time_warp_1d(arr, factor):
    """对 1D 数组做时间扭曲（factor<1 → 压缩后重采样回原长；factor>1 → 拉伸后重采样回原长）。
    使用 numpy 原生线性插值，避免 scipy 依赖。"""
    n = len(arr)
    if n < 2 or abs(factor - 1.0) < 1e-6:
        return arr.copy()
    # 源索引：在 [0, n-1] 上均匀 n 点
    src_idx = np.linspace(0, n - 1, n)
    # 扭曲：因子 > 1 → 从较短的原段里插出长信号；等价于采样点集中前段
    # 实现：把源索引 scale 再 clip → 从原 arr 线性取值
    scaled = src_idx * factor
    scaled = np.clip(scaled, 0, n - 1)
    return np.interp(scaled, src_idx, arr)


def augment_rows(rows, amp_mul, noise_sigma, warp_factor, rng):
    """对一个 rep 序列做一次增强，返回新 rows（同长度同 header）。"""
    # 提取 RMS 两列到 numpy
    rms_cols_np = {}
    for col in _RMS_COLS:
        try:
            rms_cols_np[col] = np.asarray([float(r[col]) for r in rows], dtype=np.float32)
        except (KeyError, ValueError):
            # 若 CSV 列缺失（极端兜底），直接返回原始 rows
            return [dict(r) for r in rows]

    # 1) 时间扭曲（可选，保持长度）
    for col in _RMS_COLS:
        rms_cols_np[col] = _time_warp_1d(rms_cols_np[col], warp_factor)

    # 2) 幅值扰动 × 3) 高斯噪声 → clip [0, 100]
    for col in _RMS_COLS:
        arr = rms_cols_np[col] * amp_mul
        arr = arr + rng.normal(0.0, noise_sigma, size=arr.shape).astype(np.float32)
        arr = np.clip(arr, 0.0, 100.0)
        rms_cols_np[col] = arr

    # 写回 rows（深拷贝原字典，仅改扰动列）
    new_rows = []
    for i, r in enumerate(rows):
        nr = dict(r)
        for col in _RMS_COLS:
            # 保留 4 位小数（与原 CSV 一致）
            nr[col] = "{:.4f}".format(float(rms_cols_np[col][i]))
        new_rows.append(nr)
    return new_rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--factor", type=int, default=10, help="每份 CSV 生成多少增强副本")
    parser.add_argument("--seed", type=int, default=42, help="随机种子（可复现）")
    parser.add_argument("--src", type=str, default=str(_SRC_DIR), help="源 CSV 目录")
    parser.add_argument("--dst", type=str, default=str(_DST_DIR), help="输出目录")
    parser.add_argument("--no-warp", action="store_true", help="禁用时间扭曲（仅做幅值扰动+噪声）")
    args = parser.parse_args()

    rng = np.random.RandomState(args.seed)
    random.seed(args.seed)

    src_dir = Path(args.src)
    dst_dir = Path(args.dst)
    if not src_dir.exists():
        print(f"[ERR] 源目录不存在: {src_dir}")
        sys.exit(1)

    # 清空旧输出（避免污染 seed 重名）
    if dst_dir.exists():
        import shutil
        shutil.rmtree(dst_dir)

    total_in_rows = 0
    total_out_rows = 0
    total_in_files = 0
    total_out_files = 0

    for label in _LABELS:
        label_src = src_dir / label
        label_dst = dst_dir / label
        if not label_src.exists():
            print(f"[SKIP] {label_src} 不存在")
            continue

        for csv_path in sorted(label_src.glob("*.csv")):
            total_in_files += 1
            header, rows = _read_csv(csv_path)
            total_in_rows += len(rows)
            stem = csv_path.stem

            # 1) 原始 seed 复制一份到 augmented 目录（训练时一起喂）
            seed_path = label_dst / (stem + "_seed.csv")
            _write_csv(seed_path, header, rows)
            total_out_files += 1
            total_out_rows += len(rows)

            # 2) 生成 factor 份增强变体
            for i in range(args.factor):
                amp_mul = float(rng.uniform(_AMP_MIN, _AMP_MAX))
                warp_factor = 1.0 if args.no_warp else float(rng.uniform(_WARP_MIN, _WARP_MAX))
                new_rows = augment_rows(rows, amp_mul, _NOISE_SIGMA, warp_factor, rng)
                aug_path = label_dst / "{}_aug{}.csv".format(stem, i + 1)
                _write_csv(aug_path, header, new_rows)
                total_out_files += 1
                total_out_rows += len(new_rows)

            print("  [OK] {:6s}  {} → {} 个变体 × {} 行".format(
                label, csv_path.name, args.factor, len(rows)))

    print("\n===== 增强摘要 =====")
    print("  输入：{} 份 CSV / {} 行".format(total_in_files, total_in_rows))
    print("  输出：{} 份 CSV / {} 行".format(total_out_files, total_out_rows))
    print("  放大倍率：{:.1f}×".format(total_out_files / max(1, total_in_files)))
    print("  输出目录：{}".format(dst_dir))


if __name__ == "__main__":
    main()
