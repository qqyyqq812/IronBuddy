# coding=utf-8
"""
V4.2 沙盒数据合成器（Mock Dataset Generator）
==============================================

生成 data/v42/user_XX/{curl,squat}/{standard,compensation,bad_form}/rep_NNN.csv
供 pretrain_encoders.py / train_fusion_head.py / validate_v42_dataset.py 冒烟。

CSV 契约（13 列 + header，见 .claude/plans/distributed-puzzling-wilkinson.md 数据契约）:
    Timestamp, Ang_Vel, Angle, Ang_Accel,
    Target_RMS_Norm, Comp_RMS_Norm, Symmetry_Score, Phase_Progress,
    Target_MDF, Target_MNF, Target_ZCR, Target_Raw_Unfilt,
    label

依赖：仅 numpy + stdlib（不依赖 torch/pandas，以便在板端也能跑）。

用法：
    python tools/sandbox_data_mock.py --out data/v42 [--users 3] [--reps-per-class 15] [--force]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time

import numpy as np


# CSV 契约
CSV_HEADER = [
    'Timestamp', 'Ang_Vel', 'Angle', 'Ang_Accel',
    'Target_RMS_Norm', 'Comp_RMS_Norm', 'Symmetry_Score', 'Phase_Progress',
    'Target_MDF', 'Target_MNF', 'Target_ZCR', 'Target_Raw_Unfilt',
    'label',
]

# 每 rep 200 行 (~1s @ 200Hz)
N_SAMPLES = 200
SAMPLE_HZ = 200.0

EXERCISES = ['curl', 'squat']
LABELS = ['standard', 'compensation', 'bad_form']
LABEL_TO_INT = {'standard': 0, 'compensation': 1, 'bad_form': 2}

# 角度范围
CURL_RANGE = (170.0, 45.0)     # 弯举（elbow）170 → 45
SQUAT_RANGE = (170.0, 80.0)    # 深蹲（knee）170 → 80

NOISE_STD = 0.02


def _clip01(x):
    return np.clip(x, 0.0, 1.0)


def synth_angle(exercise: str, label: str, rng: np.random.RandomState) -> np.ndarray:
    """合成 angle 序列（单位：度）
    - 标准/代偿: 全幅度正弦
    - bad_form: 幅度缩到 60%
    """
    t = np.linspace(0, np.pi, N_SAMPLES)
    if exercise == 'curl':
        hi, lo = CURL_RANGE
    else:
        hi, lo = SQUAT_RANGE
    full_amp = hi - lo
    if label == 'bad_form':
        amp = 0.6 * full_amp
    else:
        amp = full_amp
    # 正弦下潜再回升: hi → (hi - amp) → hi
    angle = hi - amp * np.sin(t)
    # 小幅高斯噪声（角度尺度内 σ≈0.5°）
    angle = angle + rng.normal(0.0, 0.5, size=N_SAMPLES)
    return angle


def synth_phase_progress(label: str, rng: np.random.RandomState) -> np.ndarray:
    """Phase_Progress ∈ [0,1]，bad_form 截断到 0.6。"""
    if label == 'bad_form':
        p = np.linspace(0.0, 0.6, N_SAMPLES)
    else:
        p = np.linspace(0.0, 1.0, N_SAMPLES)
    p = p + rng.normal(0.0, NOISE_STD, size=N_SAMPLES)
    return _clip01(p)


def synth_emg(label: str, rng: np.random.RandomState) -> dict:
    """合成 EMG 7 列的序列（已归一化到 [0,1]）。"""
    t = np.linspace(0, np.pi, N_SAMPLES)
    # Target_RMS_Norm 基线
    if label == 'standard':
        target = 0.55 + 0.25 * np.sin(t)
    elif label == 'compensation':
        target = 0.35 + 0.10 * np.sin(t)
    else:  # bad_form
        target = 0.30 + 0.08 * np.sin(t)
    target = target + rng.normal(0.0, NOISE_STD, size=N_SAMPLES)

    # Comp_RMS_Norm
    comp = np.full(N_SAMPLES, 0.15, dtype=float)
    if label == 'standard':
        comp = 0.15 + 0.02 * np.sin(t)
    elif label == 'compensation':
        # 前 30% 尖峰 0.8
        spike_end = int(N_SAMPLES * 0.30)
        spike_shape = np.zeros(N_SAMPLES)
        spike_shape[:spike_end] = 0.65 * np.sin(np.linspace(0, np.pi, spike_end))
        comp = 0.20 + spike_shape
    else:  # bad_form
        comp = 0.25 + 0.03 * np.sin(t)
    comp = comp + rng.normal(0.0, NOISE_STD, size=N_SAMPLES)

    # Symmetry_Score
    if label == 'standard':
        sym = 0.95 + rng.normal(0.0, NOISE_STD, N_SAMPLES)
    elif label == 'compensation':
        sym = 0.80 + rng.normal(0.0, NOISE_STD, N_SAMPLES)
    else:
        sym = 0.90 + rng.normal(0.0, NOISE_STD, N_SAMPLES)

    # Target_MDF（归一化 MDF/100Hz）基线 ~0.87
    mdf = 0.87 + rng.normal(0.0, NOISE_STD, N_SAMPLES)
    # Target_MNF（归一化）基线 ~0.95
    mnf = 0.95 + rng.normal(0.0, NOISE_STD, N_SAMPLES)
    # Target_ZCR 基线 ~0.5
    zcr = 0.50 + rng.normal(0.0, NOISE_STD, N_SAMPLES)
    # Target_Raw_Unfilt (归一化 /2048) 与 RMS 正相关
    raw = 0.40 + 0.6 * (target - 0.35) + rng.normal(0.0, NOISE_STD, N_SAMPLES)

    return {
        'target_rms': _clip01(target),
        'comp_rms': _clip01(comp),
        'symmetry': _clip01(sym),
        'mdf': _clip01(mdf),
        'mnf': _clip01(mnf),
        'zcr': _clip01(zcr),
        'raw_unfilt': _clip01(raw),
    }


def synth_rep(exercise: str, label: str, rng: np.random.RandomState) -> list:
    """合成单个 rep 的 (N_SAMPLES, 13) 行数据。"""
    angle = synth_angle(exercise, label, rng)
    dt = 1.0 / SAMPLE_HZ
    ang_vel = np.gradient(angle, dt)
    ang_accel = np.gradient(ang_vel, dt)

    phase = synth_phase_progress(label, rng)
    emg = synth_emg(label, rng)

    label_int = LABEL_TO_INT[label]
    ts0 = time.time()
    rows = []
    for i in range(N_SAMPLES):
        rows.append([
            '%.6f' % (ts0 + i * dt),
            '%.4f' % ang_vel[i],
            '%.4f' % angle[i],
            '%.4f' % ang_accel[i],
            '%.6f' % emg['target_rms'][i],
            '%.6f' % emg['comp_rms'][i],
            '%.6f' % emg['symmetry'][i],
            '%.6f' % phase[i],
            '%.6f' % emg['mdf'][i],
            '%.6f' % emg['mnf'][i],
            '%.6f' % emg['zcr'][i],
            '%.6f' % emg['raw_unfilt'][i],
            label_int,
        ])
    return rows


def write_rep_csv(path: str, rows: list):
    # 使用 newline='' 以避免 Windows 换行问题
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        w.writerows(rows)


def user_anthropometry(user_id: str, rng: np.random.RandomState) -> dict:
    return {
        'user_id': user_id,
        'height_cm': float(round(170.0 + rng.normal(0.0, 5.0), 1)),
        'upper_arm_cm': float(round(28.0 + rng.normal(0.0, 1.5), 1)),
        'thigh_cm': float(round(50.0 + rng.normal(0.0, 2.5), 1)),
        'generated': 'mock',
        'ts': time.time(),
    }


def user_mvc(user_id: str, exercise: str, rng: np.random.RandomState) -> dict:
    return {
        'peak_mvc': {
            'ch0': float(round(800.0 + rng.normal(0.0, 40.0), 1)),
            'ch1': float(round(500.0 + rng.normal(0.0, 30.0), 1)),
        },
        'protocol': 'SENIAM-2000',
        'exercise': exercise,
        'std_pct': 0.08,
        'ts': time.time(),
        'source': 'mock',
        'user_id': user_id,
    }


def directory_nonempty(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    for _root, dirs, files in os.walk(path):
        # 忽略隐藏文件
        non_hidden = [f for f in files if not f.startswith('.')]
        if non_hidden or [d for d in dirs if not d.startswith('.')]:
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description='V4.2 mock data generator (no torch/pandas).')
    ap.add_argument('--out', default='data/v42', help='输出根目录')
    ap.add_argument('--users', type=int, default=3, help='虚拟用户数，默认 3')
    ap.add_argument('--reps-per-class', type=int, default=15, help='每类 rep 数，默认 15')
    ap.add_argument('--force', action='store_true', help='非空目录强制覆盖')
    ap.add_argument('--seed', type=int, default=42, help='全局随机种子')
    args = ap.parse_args()

    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)

    # 1) 先做非空检查
    for u in range(1, args.users + 1):
        user_dir = os.path.join(out_root, 'user_%02d' % u)
        if directory_nonempty(user_dir) and not args.force:
            print('[sandbox_mock] REFUSE: %s 非空且未给 --force。先备份或加 --force 覆盖。' % user_dir)
            sys.exit(2)

    master_rng = np.random.RandomState(args.seed)
    total_reps = 0
    total_files = 0

    for u in range(1, args.users + 1):
        user_id = 'user_%02d' % u
        user_dir = os.path.join(out_root, user_id)
        os.makedirs(user_dir, exist_ok=True)

        user_seed = int(master_rng.randint(0, 2 ** 31 - 1))
        user_rng = np.random.RandomState(user_seed)

        # anthropometry.json
        with open(os.path.join(user_dir, 'anthropometry.json'), 'w') as f:
            json.dump(user_anthropometry(user_id, user_rng), f, indent=2, ensure_ascii=False)

        # mvc_calibration.json — 写 curl 版本（深蹲共用一份作为 MVC，因实际二者通道不同，但 mock 通用足够）
        mvc = user_mvc(user_id, 'curl', user_rng)
        with open(os.path.join(user_dir, 'mvc_calibration.json'), 'w') as f:
            json.dump(mvc, f, indent=2, ensure_ascii=False)

        for exercise in EXERCISES:
            for label in LABELS:
                class_dir = os.path.join(user_dir, exercise, label)
                os.makedirs(class_dir, exist_ok=True)
                for rep in range(1, args.reps_per_class + 1):
                    rep_seed = int(user_rng.randint(0, 2 ** 31 - 1))
                    rep_rng = np.random.RandomState(rep_seed)
                    rows = synth_rep(exercise, label, rep_rng)
                    path = os.path.join(class_dir, 'rep_%03d.csv' % rep)
                    write_rep_csv(path, rows)
                    total_reps += 1
                    total_files += 1

    # 粗略磁盘占用
    total_bytes = 0
    for root, _d, files in os.walk(out_root):
        for fn in files:
            total_bytes += os.path.getsize(os.path.join(root, fn))
    total_kb = total_bytes / 1024.0

    print('[sandbox_mock] 完成:')
    print('  输出目录     : %s' % out_root)
    print('  用户数       : %d' % args.users)
    print('  总 rep 数    : %d (= users × exercises × labels × reps_per_class = %d)' %
          (total_reps, args.users * len(EXERCISES) * len(LABELS) * args.reps_per_class))
    print('  总文件数     : %d' % total_files)
    print('  磁盘占用     : %.1f KB' % total_kb)
    print('  CSV 头 (13)  : %s' % ','.join(CSV_HEADER))


if __name__ == '__main__':
    main()
