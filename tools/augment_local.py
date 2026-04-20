# coding=utf-8
"""
V4.4 本地数据 10x 增强器（Local Data Augmenter）
==================================================

在 data/v42/<user>/curl/<label>/rep_*.csv 上原地派生 N 个 augmented 副本，
供 pretrain_encoders.py / train_fusion_head.py 做 base pretrain 替代。

CSV 契约（13 列，严格保持顺序）：
    Timestamp, Ang_Vel, Angle, Ang_Accel,
    Target_RMS_Norm, Comp_RMS_Norm, Symmetry_Score, Phase_Progress,
    Target_MDF, Target_MNF, Target_ZCR, Target_Raw_Unfilt,
    label

归一化系数（已应用于源 CSV，无需再除）：
    Target_MDF/100, Target_MNF/150, Target_ZCR/400, Target_Raw_Unfilt/2048

五种增强手法（每副本随机挑 2-3 种叠加）：
    a. 时间扭曲：resample 压缩/拉伸到 0.85x..1.15x，再 resample 回原长
    b. 幅度抖动：每数值列 * U(0.92, 1.08)
    c. 相位偏移：np.roll 偏移 +/-(5%-10%) 时序长度
    d. 高频白噪声：6 肌电派生列加 N(0, 0.02)，clip 到 [0, 1.0]
    e. HSBI 噪声谱叠加（条件启用，需 --with-hsbi-noise）

严格边界：
    - 不改 Timestamp / label 列
    - 不重复增强已有 rep_*_aug*.csv
    - --multiplier 0 等于纯清理模式（删除所有 aug 副本）
    - --holdout-user 完全跳过（保持测试集纯净）

依赖：numpy + scipy + pandas（本地训练机均可用；非板端脚本）。

用法：
    python tools/augment_local.py --data-root data/v42 --multiplier 10 --holdout-user user_04
    python tools/augment_local.py --data-root data/v42 --multiplier 0 --holdout-user user_04  # 清理
    python tools/augment_local.py --data-root data/v42 --with-hsbi-noise --multiplier 10
"""
from __future__ import annotations

import argparse
import glob
import os
import random
import sys
import time
from typing import List, Tuple

import numpy as np
import pandas as pd
from scipy import signal


# --------- CSV 契约 ---------
CSV_HEADER = [
    'Timestamp', 'Ang_Vel', 'Angle', 'Ang_Accel',
    'Target_RMS_Norm', 'Comp_RMS_Norm', 'Symmetry_Score', 'Phase_Progress',
    'Target_MDF', 'Target_MNF', 'Target_ZCR', 'Target_Raw_Unfilt',
    'label',
]

# 受增强影响的数值列（不含 Timestamp / label）
NUMERIC_COLS = [c for c in CSV_HEADER if c not in ('Timestamp', 'label')]

# 高斯噪声仅加到这 6 肌电派生列
EMG_NOISE_COLS = [
    'Target_RMS_Norm', 'Comp_RMS_Norm',
    'Target_MDF', 'Target_MNF', 'Target_ZCR', 'Target_Raw_Unfilt',
]

# HSBI 噪声仅加到 Raw_Unfilt 列
HSBI_NOISE_COL = 'Target_Raw_Unfilt'


# --------- 五种增强函数 ---------
def augment_time_warp(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """a. 时间扭曲：先压缩或拉伸到 factor*N，再 resample 回原长。"""
    n = len(df)
    if n < 4:
        return df
    factor = rng.uniform(0.85, 1.15)
    new_len = max(4, int(round(n * factor)))
    out = df.copy()
    for col in NUMERIC_COLS:
        stretched = signal.resample(df[col].to_numpy(dtype=np.float64), new_len)
        restored = signal.resample(stretched, n)
        out[col] = restored
    return out


def augment_amplitude_jitter(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """b. 幅度抖动：每数值列独立 * U(0.92, 1.08)。"""
    out = df.copy()
    for col in NUMERIC_COLS:
        scale = rng.uniform(0.92, 1.08)
        out[col] = df[col].to_numpy() * scale
    return out


def augment_phase_shift(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """c. 相位偏移：np.roll 偏移 ±(5%-10%) 时序长度（所有数值列一起滚）。"""
    n = len(df)
    if n < 4:
        return df
    frac = rng.uniform(0.05, 0.10)
    shift = int(round(n * frac)) * (1 if rng.random() >= 0.5 else -1)
    out = df.copy()
    for col in NUMERIC_COLS:
        out[col] = np.roll(df[col].to_numpy(), shift)
    return out


def augment_white_noise(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """d. 高频白噪声：6 EMG 派生列加 N(0, 0.02)，clip 到 [0, 1.0]。"""
    out = df.copy()
    n = len(df)
    for col in EMG_NOISE_COLS:
        noise = rng.normal(0.0, 0.02, size=n)
        out[col] = np.clip(df[col].to_numpy() + noise, 0.0, 1.0)
    return out


def _load_hsbi_noise_spectrum(hsbi_root: str) -> np.ndarray | None:
    """从 HSBI 数据（.csv 或 .edf）抽 100-450Hz 高频谱。无则返 None。"""
    if not os.path.isdir(hsbi_root):
        return None
    candidates = glob.glob(os.path.join(hsbi_root, '*.csv'))
    candidates += glob.glob(os.path.join(hsbi_root, '*.edf'))
    if not candidates:
        return None
    # KISS：首个 CSV 抽单列做 bandpass filter，若失败则返 None
    try:
        path = sorted(candidates)[0]
        if path.endswith('.csv'):
            raw = pd.read_csv(path).select_dtypes(include=[np.number]).to_numpy()
            if raw.size == 0:
                return None
            # 取第一列，归一到 [0, 1]
            sig = raw[:, 0].astype(np.float64)
        else:
            # .edf 需要 mne/pyedflib；若未装，跳过
            return None
        sig = (sig - sig.min()) / (sig.max() - sig.min() + 1e-9)
        # 假设 HSBI 1000-2000Hz 采样，100-450Hz 带通
        try:
            sos = signal.butter(4, [100, 450], btype='band', fs=1000, output='sos')
            hf = signal.sosfilt(sos, sig)
        except Exception:
            hf = sig
        return hf.astype(np.float32)
    except Exception:
        return None


def augment_hsbi_noise(
    df: pd.DataFrame, rng: np.random.Generator, hsbi_spectrum: np.ndarray | None,
) -> pd.DataFrame:
    """e. HSBI 噪声谱叠加（条件启用）：从 HSBI 高频谱合成噪声加到 Raw_Unfilt。"""
    if hsbi_spectrum is None or hsbi_spectrum.size == 0:
        return df  # 静默跳过（--with-hsbi-noise 开启但 HSBI 空时 warn 已在主流程）
    n = len(df)
    start = int(rng.integers(0, max(1, hsbi_spectrum.size - n)))
    chunk = hsbi_spectrum[start:start + n]
    if chunk.size < n:
        chunk = np.pad(chunk, (0, n - chunk.size), mode='wrap')
    out = df.copy()
    scale = 0.03  # 叠加强度：不过度污染原信号
    noised = df[HSBI_NOISE_COL].to_numpy() + scale * chunk
    out[HSBI_NOISE_COL] = np.clip(noised, 0.0, 1.0)
    return out


AUG_FUNCS = {
    'time_warp': augment_time_warp,
    'amp_jitter': augment_amplitude_jitter,
    'phase_shift': augment_phase_shift,
    'white_noise': augment_white_noise,
    # hsbi 单独处理（需要 spectrum 参数）
}


# --------- 主流程 ---------
def find_original_reps(data_root: str, exercise: str, holdout_user: str) -> List[str]:
    """扫 data/v42/<user>/<exercise>/<label>/rep_*.csv，排除 holdout 和已 aug 副本。"""
    pattern = os.path.join(data_root, '*', exercise, '*', 'rep_*.csv')
    all_files = glob.glob(pattern)
    originals = []
    for f in all_files:
        basename = os.path.basename(f)
        # 排除已 augmented 副本
        if '_aug' in basename:
            continue
        # 排除 holdout user
        parts = os.path.normpath(f).split(os.sep)
        # 结构：.../data/v42/<user>/<exercise>/<label>/rep_NNN.csv
        try:
            user_dir = parts[-4]
        except IndexError:
            continue
        if user_dir == holdout_user:
            continue
        originals.append(f)
    return sorted(originals)


def find_aug_copies(data_root: str, exercise: str) -> List[str]:
    """返回全部 rep_*_aug*.csv 副本路径（清理模式用）。"""
    pattern = os.path.join(data_root, '*', exercise, '*', 'rep_*_aug*.csv')
    return sorted(glob.glob(pattern))


def label_of_rep(path: str) -> str:
    """从路径推断 label 文件夹名：.../<label>/rep_NNN.csv → label。"""
    return os.path.basename(os.path.dirname(path))


def apply_augmentations(
    df: pd.DataFrame,
    rng: np.random.Generator,
    hsbi_spectrum: np.ndarray | None,
    with_hsbi: bool,
) -> Tuple[pd.DataFrame, List[str]]:
    """随机挑 2-3 种增强叠加。返回 (增强后 df, 使用的手法名)。"""
    pool = list(AUG_FUNCS.keys())
    if with_hsbi and hsbi_spectrum is not None:
        pool = pool + ['hsbi_noise']
    k = int(rng.integers(2, 4))  # 2 或 3
    k = min(k, len(pool))
    chosen = rng.choice(pool, size=k, replace=False).tolist()
    out = df
    for name in chosen:
        if name == 'hsbi_noise':
            out = augment_hsbi_noise(out, rng, hsbi_spectrum)
        else:
            out = AUG_FUNCS[name](out, rng)
    return out, chosen


def augment_one_rep(
    src_path: str,
    k_index: int,
    rng: np.random.Generator,
    hsbi_spectrum: np.ndarray | None,
    with_hsbi: bool,
) -> Tuple[str, List[str]]:
    """对单个 rep 派生第 K 个 augmented 副本。返回 (目标路径, 手法名列表)。"""
    df = pd.read_csv(src_path)
    # 保证列顺序严格一致
    missing = [c for c in CSV_HEADER if c not in df.columns]
    if missing:
        raise ValueError(f'{src_path} 缺失列 {missing}')
    df = df[CSV_HEADER].copy()

    # 备份 Timestamp 和 label（不参与增强）
    ts = df['Timestamp'].to_numpy().copy()
    lb = df['label'].to_numpy().copy()

    aug_df, methods = apply_augmentations(df, rng, hsbi_spectrum, with_hsbi)

    # 还原 Timestamp 和 label
    aug_df['Timestamp'] = ts
    aug_df['label'] = lb
    # 严格列顺序
    aug_df = aug_df[CSV_HEADER]

    # 文件名：rep_NNN.csv → rep_NNN_augK.csv
    src_dir = os.path.dirname(src_path)
    base = os.path.basename(src_path).replace('.csv', '')
    dst = os.path.join(src_dir, f'{base}_aug{k_index}.csv')
    aug_df.to_csv(dst, index=False, float_format='%.6f')
    return dst, methods


def cleanup_aug_copies(data_root: str, exercise: str) -> int:
    """清理所有 rep_*_aug*.csv 副本。返回删除数量。"""
    aug_files = find_aug_copies(data_root, exercise)
    for f in aug_files:
        try:
            os.remove(f)
        except OSError as e:
            print(f'[WARN] 无法删除 {f}: {e}', file=sys.stderr)
    return len(aug_files)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description='V4.4 本地 sEMG 数据 10x augment 工具',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument('--data-root', default='data/v42', help='v42 数据根目录')
    p.add_argument('--multiplier', type=int, default=10,
                   help='每 rep 派生的副本数量；0 等于清理模式')
    p.add_argument('--with-hsbi-noise', action='store_true',
                   help='启用 HSBI 高频噪声谱叠加')
    p.add_argument('--hsbi-root', default='data/external/hsbi_biceps',
                   help='HSBI 数据根目录（仅 --with-hsbi-noise 时读取）')
    p.add_argument('--out-suffix', default='_aug',
                   help='预留：副本文件名后缀标签（当前固定 _augK，保留参数兼容）')
    p.add_argument('--holdout-user', default='user_04',
                   help='完全跳过的 holdout 用户目录名')
    p.add_argument('--exercise', default='curl',
                   help='动作名（curl / squat），仅处理对应目录')
    p.add_argument('--seed', type=int, default=42, help='随机种子')
    return p


def main(argv: List[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    t0 = time.time()

    data_root = args.data_root
    if not os.path.isdir(data_root):
        print(f'[ERROR] 数据根目录不存在：{data_root}', file=sys.stderr)
        return 2

    # 清理模式
    if args.multiplier <= 0:
        removed = cleanup_aug_copies(data_root, args.exercise)
        elapsed = time.time() - t0
        print(f'[CLEANUP] 已删除 {removed} 个 aug 副本；耗时 {elapsed:.2f}s')
        return 0

    # 收集原 rep
    originals = find_original_reps(data_root, args.exercise, args.holdout_user)
    if not originals:
        print(f'[ERROR] 在 {data_root}/*/{args.exercise}/*/rep_*.csv 未找到原 rep '
              f'（排除 holdout={args.holdout_user}）', file=sys.stderr)
        return 3

    # HSBI 噪声谱（按需）
    hsbi_spectrum = None
    if args.with_hsbi_noise:
        hsbi_spectrum = _load_hsbi_noise_spectrum(args.hsbi_root)
        if hsbi_spectrum is None:
            print(f'[WARN] --with-hsbi-noise 启用但 {args.hsbi_root} 空或不可读；'
                  f'本次跳过 HSBI 噪声叠加（不阻塞）', file=sys.stderr)

    # 按 label 统计
    orig_by_label: dict = {}
    for f in originals:
        orig_by_label.setdefault(label_of_rep(f), []).append(f)

    rng = np.random.default_rng(args.seed)
    total_new = 0
    method_counter: dict = {}
    aug_by_label: dict = {lab: 0 for lab in orig_by_label}

    for src in originals:
        lab = label_of_rep(src)
        for k in range(args.multiplier):
            # 为每 rep 分配独立 RNG 避免跨 rep 相关
            sub_rng = np.random.default_rng(rng.integers(0, 2**31 - 1))
            _, methods = augment_one_rep(
                src, k, sub_rng, hsbi_spectrum, args.with_hsbi_noise,
            )
            total_new += 1
            aug_by_label[lab] = aug_by_label.get(lab, 0) + 1
            for m in methods:
                method_counter[m] = method_counter.get(m, 0) + 1

    elapsed = time.time() - t0
    print('-' * 60)
    print(f'[DONE] 原 rep 数：{len(originals)}')
    print(f'[DONE] 生成 augmented 副本数：{total_new}')
    print(f'[DONE] 倍率：{args.multiplier}x  holdout={args.holdout_user}  exercise={args.exercise}')
    print(f'[DONE] HSBI 噪声：{"ON" if (args.with_hsbi_noise and hsbi_spectrum is not None) else "OFF"}')
    print('[DONE] 每类前后对比：')
    for lab in sorted(orig_by_label.keys()):
        before = len(orig_by_label[lab])
        added = aug_by_label.get(lab, 0)
        print(f'        {lab:15s}  原 {before:4d}  +aug {added:4d}  =合计 {before + added:4d}')
    print('[DONE] 手法使用频次：')
    for m in sorted(method_counter.keys()):
        print(f'        {m:15s}  {method_counter[m]} 次')
    print(f'[DONE] 耗时：{elapsed:.2f}s')
    print('-' * 60)
    return 0


if __name__ == '__main__':
    sys.exit(main())
