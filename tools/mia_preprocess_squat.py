#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MIA 数据集 Squat 子集 → IronBuddy V3 7D CSV 预处理脚本
=====================================================

版本      : 1.0 (2026-04-18)
作者      : Agent-2 (IronBuddy pipeline helper)
用途      : 将 MIA Dataset 中 Squat 相关 clip 的 (emgvalues.npy + joints3d.npy)
            转成 IronBuddy V3 7D CSV 格式（10 列），作为 GRU 训练数据。

MIA 数据集引用:
  Chiquier & Vondrick, "Muscles in Action", ICCV 2023
  Repo: https://github.com/cvlab-columbia/musclesinaction

关键约定:
  - MIA EMG 8 通道顺序（来自 musclesinaction/dataloader/data.py:138）:
        ['rightquad','leftquad','rightham','leftham',
         'rightglutt','leftglutt','leftbicep','rightbicep']
  - 对 IronBuddy 2 贴片硬件映射:
        CH0 (Target) = rightquad   idx=0  （深蹲发力主肌）
        CH1 (Comp)   = rightglutt  idx=4  （深蹲代偿监测肌）
  - 对称性 Symmetry_Score = min(R,L)/(max(R,L)+eps)

输出 CSV 10 列 header（必须严格对齐 data/bicep_curl/golden/train_*.csv）:
    Timestamp, Ang_Vel, Angle, Ang_Accel,
    Target_RMS, Comp_RMS, Symmetry_Score, Phase_Progress,
    pose_score, label

label ∈ {'golden', 'bad'}  ← MIA 不产出 'lazy'

使用示例:
    python tools/mia_preprocess_squat.py \
        --mia-root data/mia_squat_raw/MIADatasetOfficial \
        --out      data/mia/squat \
        --fps 30 --rep-min-duration 1.0 --rep-max-duration 4.0 \
        --max-reps-per-clip 5
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
import warnings
from collections import Counter
from typing import Dict, List, Optional, Tuple

import numpy as np

try:
    from scipy.signal import find_peaks
except ImportError as e:
    print('[FATAL] scipy 未安装 (pip install scipy)', file=sys.stderr)
    raise

# ------------------------------------------------------------------ 常量
# 10 列 V3 7D 契约
CSV_HEADER = [
    'Timestamp', 'Ang_Vel', 'Angle', 'Ang_Accel',
    'Target_RMS', 'Comp_RMS', 'Symmetry_Score', 'Phase_Progress',
    'pose_score', 'label',
]

# MIA EMG 通道索引
EMG_RIGHTQUAD = 0   # Target
EMG_LEFTQUAD = 1
EMG_RIGHTHAM = 2
EMG_LEFTHAM = 3
EMG_RIGHTGLUTT = 4  # Comp
EMG_LEFTGLUTT = 5

# joints3d 25 关节索引（NTU/MIA 风格）
# MIA joints3d 是 SMPL-49 格式（非 NTU 25）：右腿 hip=1 / knee=4 / ankle=7
JOINT_RHIP = 1
JOINT_RKNEE = 4
JOINT_RANKLE = 7

# 默认参数
DEFAULT_FPS = 30.0
DEFAULT_REP_MIN = 1.0
DEFAULT_REP_MAX = 4.0
DEFAULT_MAX_REPS = 5
RMS_WINDOW_MS = 100.0  # 100ms 滑窗
EPS = 1e-6


# ------------------------------------------------------------------ 工具函数
def infer_label_from_path(path: str) -> Optional[str]:
    """从路径中推断 label。含 Good→golden, Bad→bad, 纯 Squat→golden。"""
    p = path.replace('\\', '/').lower()
    # 先匹配 good/bad 变体（优先级高）
    if 'goodsquat' in p or 'squatgood' in p:
        return 'golden'
    if 'badsquat' in p or 'squatbad' in p:
        return 'bad'
    # 其它 squat 变体（RonddeJambeGood/Bad, SideLunge 等）
    if 'good' in p and 'squat' in p:
        return 'golden'
    if 'bad' in p and 'squat' in p:
        return 'bad'
    # 纯 Squat 目录：延后按 knee_angle 启发式判定
    if '/squat/' in p or p.endswith('/squat'):
        return 'pending_angle'
    return None


def infer_label_from_angle(min_angle_deg):
    # type: (float) -> str
    """
    MIA 实际目录只有统一的 `Squat`（无 Good/Bad 区分）。
    按膝关节最小角深度启发：
        60° ≤ min ≤ 75° → golden  （标准深蹲，大腿与地面近似平行）
        min < 60°        → bad    （过深，膝过脚尖风险）
        min > 75°        → bad    （半蹲，深度不够）
    与 SquatStateMachine.ANGLE_STANDARD=100 语义互补（FSM 粗判，模型细判）。
    """
    if min_angle_deg < 60.0:
        return 'bad'
    if min_angle_deg <= 75.0:
        return 'golden'
    return 'bad'


def compute_knee_angle(joints3d: np.ndarray) -> np.ndarray:
    """
    计算右膝关节角 (hip-knee-ankle 三点法)。
    joints3d shape (T, 25, 3) → (T,) degrees.
    """
    hip = joints3d[:, JOINT_RHIP, :]     # (T,3)
    knee = joints3d[:, JOINT_RKNEE, :]
    ankle = joints3d[:, JOINT_RANKLE, :]

    v1 = hip - knee
    v2 = ankle - knee

    # 归一化
    n1 = np.linalg.norm(v1, axis=1, keepdims=True) + EPS
    n2 = np.linalg.norm(v2, axis=1, keepdims=True) + EPS
    u1 = v1 / n1
    u2 = v2 / n2

    cos_a = np.sum(u1 * u2, axis=1)
    cos_a = np.clip(cos_a, -1.0, 1.0)
    angle_deg = np.degrees(np.arccos(cos_a))
    return angle_deg


def rolling_rms(x: np.ndarray, window: int) -> np.ndarray:
    """对 1D 信号做滑窗 RMS，同长度输出。"""
    window = max(3, int(window))
    x = np.asarray(x, dtype=np.float64)
    x2 = x * x
    # 用 cumsum 近似等价于 np.convolve(mode='same')
    kernel = np.ones(window, dtype=np.float64) / window
    mean_sq = np.convolve(x2, kernel, mode='same')
    return np.sqrt(np.maximum(mean_sq, 0.0))


def resample_to_emg_length(signal: np.ndarray, target_len: int) -> np.ndarray:
    """将 signal (T,) 线性插值到 target_len 长度。"""
    if len(signal) == target_len:
        return signal.astype(np.float64)
    src_t = np.linspace(0.0, 1.0, len(signal), endpoint=True)
    dst_t = np.linspace(0.0, 1.0, target_len, endpoint=True)
    return np.interp(dst_t, src_t, signal.astype(np.float64))


def detect_rep_boundaries(knee_angle: np.ndarray, fps: float,
                          rep_min: float, rep_max: float) -> List[Tuple[int, int]]:
    """
    MIA 每 clip 固定 30 帧（约 1 秒），已是单个 rep 的有效片段，**不再二次切分**。
    直接整段返回一个 (0, T) 区间。
    （保留原参数签名，rep_min/rep_max 本函数内部不再使用。）
    """
    T = int(len(knee_angle))
    if T < 5:
        return []
    return [(0, T)]


# ------------------------------------------------------------------ 单 clip 处理
def process_clip(clip_dir: str, fps: float, rep_min: float, rep_max: float,
                 max_reps: int) -> Tuple[List[Dict], str]:
    """
    处理一个 clip（含 emgvalues.npy + joints3d.npy）。
    返回 (rep_records, skip_reason)；成功时 skip_reason=''。
    每个 rep_record = {label, subject, clip_id, rep_idx, data: DataFrame-like dict}
    """
    emg_path = os.path.join(clip_dir, 'emgvalues.npy')
    j3d_path = os.path.join(clip_dir, 'joints3d.npy')

    if not os.path.isfile(emg_path):
        return [], 'missing_emg'
    if not os.path.isfile(j3d_path):
        return [], 'missing_joints3d'

    label = infer_label_from_path(clip_dir)
    if label is None:
        return [], 'label_unclear'

    try:
        emg = np.load(emg_path)          # (T, 8)
        j3d = np.load(j3d_path)          # (Tv, 25, 3)
    except Exception as ex:
        return [], 'npy_load_fail:{}'.format(ex)

    if emg.ndim != 2 or emg.shape[1] < 6:
        return [], 'emg_shape_bad:{}'.format(emg.shape)
    if j3d.ndim != 3 or j3d.shape[1] < 15 or j3d.shape[2] != 3:
        return [], 'j3d_shape_bad:{}'.format(j3d.shape)

    T_emg = emg.shape[0]
    T_vid = j3d.shape[0]

    # 数据质量校验
    target_raw = emg[:, EMG_RIGHTQUAD].astype(np.float64)
    comp_raw = emg[:, EMG_RIGHTGLUTT].astype(np.float64)
    if not np.isfinite(target_raw).all() or not np.isfinite(comp_raw).all():
        return [], 'emg_has_nan_inf'
    if np.abs(target_raw).max() < EPS:
        return [], 'emg_rightquad_all_zero'

    # 将 joints3d 重采样到 emg 时间轴（选右膝角）
    knee_angle_vid = compute_knee_angle(j3d)   # (Tv,)
    if not np.isfinite(knee_angle_vid).all():
        return [], 'knee_angle_nan'
    knee_angle = resample_to_emg_length(knee_angle_vid, T_emg)

    # 若路径推断为 pending_angle（MIA 实际全是 `Squat` 统一名），按膝角最小值分层
    if label == 'pending_angle':
        label = infer_label_from_angle(float(np.nanmin(knee_angle)))

    # 检测 reps
    reps = detect_rep_boundaries(knee_angle, fps, rep_min, rep_max)
    if not reps:
        return [], 'no_valid_reps'
    if len(reps) > max_reps:
        reps = reps[:max_reps]

    # 全局特征（逐样本）
    ang_vel = np.gradient(knee_angle) * fps          # deg/s
    ang_accel = np.gradient(ang_vel) * fps           # deg/s^2

    rms_window = max(3, int(fps * (RMS_WINDOW_MS / 1000.0)))
    target_rms = rolling_rms(np.abs(target_raw), rms_window)
    comp_rms = rolling_rms(np.abs(comp_raw), rms_window)

    # 左右 symmetry（右 quad vs 左 quad）
    r_quad = emg[:, EMG_RIGHTQUAD].astype(np.float64)
    l_quad = emg[:, EMG_LEFTQUAD].astype(np.float64)
    r_abs = np.abs(r_quad)
    l_abs = np.abs(l_quad)
    num = np.minimum(r_abs, l_abs)
    den = np.maximum(r_abs, l_abs) + EPS
    symm = num / den  # 0~1

    # 时间戳基准（秒）
    t_start_wall = time.time()
    dt = 1.0 / fps

    # 从 clip_dir 推断 subject + clip_id
    # 典型路径 .../train/Subject3/Squat/005/  → subject=Subject3, clip_id=005
    parts = clip_dir.replace('\\', '/').rstrip('/').split('/')
    subject = 'unknown'
    clip_id = parts[-1]
    for p in parts:
        if p.startswith('Subject'):
            subject = p
            break

    records: List[Dict] = []
    for k, (s, e) in enumerate(reps):
        if e - s < 5:
            continue
        seg_len = e - s
        phase = np.linspace(0.0, 1.0, seg_len, endpoint=True)
        ts = t_start_wall + np.arange(seg_len) * dt

        data = {
            'Timestamp':       ts,
            'Ang_Vel':         ang_vel[s:e],
            'Angle':           knee_angle[s:e],
            'Ang_Accel':       ang_accel[s:e],
            'Target_RMS':      target_rms[s:e],
            'Comp_RMS':        comp_rms[s:e],
            'Symmetry_Score':  symm[s:e],
            'Phase_Progress':  phase,
            'pose_score':      np.ones(seg_len, dtype=np.float64),
            'label':           [label] * seg_len,
        }
        records.append({
            'label': label,
            'subject': subject,
            'clip_id': clip_id,
            'rep_idx': k,
            'data': data,
        })

    if not records:
        return [], 'all_reps_too_short'
    return records, ''


# ------------------------------------------------------------------ 主流程
def find_clip_dirs(mia_root: str) -> List[str]:
    """
    扫描 mia_root 下所有含 emgvalues.npy 的 Squat 相关目录。
    兼容两种层级:
      - .../SubjectN/Squat/clip_id/emgvalues.npy  （clip_id 层）
      - .../SubjectN/Squat/emgvalues.npy          （直接挂载）
    """
    patterns = [
        os.path.join(mia_root, '*', 'Subject*', '*Squat*', '*', 'emgvalues.npy'),
        os.path.join(mia_root, '*', 'Subject*', '*Squat*', 'emgvalues.npy'),
    ]
    found = set()
    for pat in patterns:
        for emg_file in glob.glob(pat):
            found.add(os.path.dirname(emg_file))
    return sorted(found)


def write_csv(filepath: str, data: Dict, precision: int = 4) -> None:
    """手写 CSV 避免依赖 pandas dtype 细节，严格 10 列精度匹配 bicep_curl 样本。"""
    cols = CSV_HEADER
    n = len(data['Timestamp'])

    fmt_map = {
        'Timestamp':      '{:.3f}',
        'Ang_Vel':        '{:.4f}',
        'Angle':          '{:.4f}',
        'Ang_Accel':      '{:.4f}',
        'Target_RMS':     '{:.4f}',
        'Comp_RMS':       '{:.4f}',
        'Symmetry_Score': '{:.4f}',
        'Phase_Progress': '{:.4f}',
        'pose_score':     '{:.4f}',
    }

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(','.join(cols) + '\n')
        for i in range(n):
            row = []
            for c in cols:
                if c == 'label':
                    row.append(str(data[c][i]))
                else:
                    v = data[c][i]
                    row.append(fmt_map[c].format(float(v)))
            f.write(','.join(row) + '\n')


def main() -> int:
    ap = argparse.ArgumentParser(description='MIA Squat → IronBuddy V3 7D CSV')
    ap.add_argument('--mia-root', required=True,
                    help='MIA 解压根目录（含 train/test/Subject*）')
    ap.add_argument('--out', required=True, help='输出目录（会产生 golden/ bad/）')
    ap.add_argument('--fps', type=float, default=DEFAULT_FPS)
    ap.add_argument('--rep-min-duration', type=float, default=DEFAULT_REP_MIN)
    ap.add_argument('--rep-max-duration', type=float, default=DEFAULT_REP_MAX)
    ap.add_argument('--max-reps-per-clip', type=int, default=DEFAULT_MAX_REPS)
    args = ap.parse_args()

    mia_root = os.path.abspath(args.mia_root)
    out_root = os.path.abspath(args.out)
    if not os.path.isdir(mia_root):
        print('[FATAL] --mia-root not a directory:', mia_root, file=sys.stderr)
        return 2

    os.makedirs(os.path.join(out_root, 'golden'), exist_ok=True)
    os.makedirs(os.path.join(out_root, 'bad'), exist_ok=True)

    t0 = time.time()
    clip_dirs = find_clip_dirs(mia_root)
    print('[INFO] found clip dirs: {}'.format(len(clip_dirs)))
    if not clip_dirs:
        print('[WARN] 无 clip 目录；检查 --mia-root 与解压结构')
        return 1

    skip_reasons: Counter = Counter()
    label_counts: Counter = Counter()
    total_reps = 0
    written_files: List[str] = []

    for i, cd in enumerate(clip_dirs):
        records, skip = process_clip(
            cd, args.fps, args.rep_min_duration,
            args.rep_max_duration, args.max_reps_per_clip,
        )
        if skip:
            skip_reasons[skip] += 1
            continue
        for rec in records:
            label = rec['label']
            label_counts[label] += 1
            fname = 'mia_{subj}_{cid}_rep{k}.csv'.format(
                subj=rec['subject'], cid=rec['clip_id'], k=rec['rep_idx'])
            fp = os.path.join(out_root, label, fname)
            write_csv(fp, rec['data'])
            written_files.append(fp)
            total_reps += 1

        if (i + 1) % 50 == 0:
            print('[INFO] processed {}/{} clips, total reps so far: {}'.format(
                i + 1, len(clip_dirs), total_reps))

    elapsed = time.time() - t0
    report = {
        'mia_root':        mia_root,
        'out_root':        out_root,
        'num_clip_dirs':   len(clip_dirs),
        'num_reps_total':  total_reps,
        'label_counts':    dict(label_counts),
        'skip_reasons':    dict(skip_reasons),
        'elapsed_sec':     round(elapsed, 2),
        'params': {
            'fps':               args.fps,
            'rep_min_duration':  args.rep_min_duration,
            'rep_max_duration':  args.rep_max_duration,
            'max_reps_per_clip': args.max_reps_per_clip,
            'rms_window_ms':     RMS_WINDOW_MS,
        },
        'csv_header': CSV_HEADER,
    }
    with open(os.path.join(out_root, '_conversion_report.json'), 'w',
              encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print('')
    print('=' * 60)
    print('[DONE] MIA Squat preprocessing complete')
    print('  elapsed      : {:.1f}s'.format(elapsed))
    print('  clip dirs    : {}'.format(len(clip_dirs)))
    print('  reps written : {}'.format(total_reps))
    print('  label dist   : {}'.format(dict(label_counts)))
    print('  skip reasons : {}'.format(dict(skip_reasons)))
    print('')
    print('Next step:')
    print('  python tools/train_model.py --data {} --out models/ --epochs 20'
          .format(out_root))
    print('=' * 60)
    return 0


if __name__ == '__main__':
    sys.exit(main())
