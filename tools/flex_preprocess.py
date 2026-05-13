# coding=utf-8
"""
FLEX_AQA_Dataset → IronBuddy V4.2/4.3 13 列 rep CSV 预处理
===========================================================

plan §1 (flex-curl-only-pivot.md) 部分 1 · 网上公开数据预准备战略

- FLEX 数据集结构（git clone /tmp/flex_aqa 实读 + README/datasets/SevenPair.py 交叉验证）:
    <flex_root>/EMG/A0X/<rep:03d>/EMG.csv            4 通道 sEMG @ 200Hz，无 header
    <flex_root>/Skeleton/A0X/<rep:03d>/skeleton_points.csv   21 点 × 3 坐标
    <flex_root>/Split_4/split_4_train_list.mat       (class, rep, final_score, ...)

- 输出：data/flex/curl/{standard,compensation,bad_form}/rep_<gid:04d>.csv
    13 列完全对齐 V4.2 contract（V4.2 rep_*.csv 的 CSV header）。

- 强约束：不 import FLEX repo 任何模块，remap21_to_25 在本文件复制简化版本。
- 只依赖：numpy, pandas, scipy.io, scipy.signal, (stdlib)。
- 故障优雅：<flex-root> 不存在 → 清晰 exit 1 指向 data/flex/README.md。
- `--mock` 模式合成 30 fake rep 走完整链路，验证脚本本身无 bug。

用法:
    # (1) 通道相关性验证（前 5 rep 跑 4 种组合）
    python tools/flex_preprocess.py --flex-root <FLEX_ROOT> --validate-channels

    # (2) 全量预处理
    python tools/flex_preprocess.py --flex-root <FLEX_ROOT> --out data/flex

    # (3) 自定义阈值 + 自定义类别 id
    python tools/flex_preprocess.py --flex-root <FLEX_ROOT> \\
        --thresholds 75,45 --curl-class-ids 7,17,18,19

    # (4) 合成数据冒烟（不需 FLEX_ROOT）
    python tools/flex_preprocess.py --mock

作者：IronBuddy V4.3 Agent-1
"""
from __future__ import absolute_import, division, print_function

import argparse
import csv
import glob
import json
import os
import sys
import time
import warnings

import numpy as np

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

try:
    import scipy.io  # type: ignore
    import scipy.signal  # type: ignore
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


# =============================================================================
# 常量：plan §1.2 / §1.3 / §1.4 的硬编码决策
# =============================================================================
FLEX_SAMPLE_RATE = 200                         # sEMG 200Hz（FLEX models/emg_encoder.py sr=200）

# 通道映射（plan §1.2）。依据：SevenPair.py:84-99 将 [7,17,18,19] 归入 "Single" 单边臂组，
# 按 col[0]/col[1] 读 L_main/L_sub。弯举 = 单边上肢 biceps + forearm，故 col[0]=target, col[1]=comp。
FLEX_CH_TARGET_DEFAULT = 0   # L_main biceps brachii
FLEX_CH_COMP_DEFAULT = 1     # L_sub forearm flexor
FLEX_CH_CONTRA_L_DEFAULT = 2 # 对侧 R_main（用于 Symmetry_Score 若 4 通道齐全）

# 验证 4 种组合（plan §1.2）
VALIDATE_COMBOS = [(0, 1), (2, 3), (0, 2), (1, 3)]

# 类别 id 默认：SevenPair.py 的 Single 组，与 plan §1.2 占位一致
FLEX_CURL_CLASS_IDS_DEFAULT = [7, 17, 18, 19]

# score → 三类阈值（plan §1.3）
DEFAULT_THRESHOLDS = (80.0, 50.0)

# NTU 25 点 elbow 三点法（plan §1.4）
ELBOW_SHOULDER_IDX = 9
ELBOW_JOINT_IDX = 10
ELBOW_WRIST_IDX = 11

# RMS 滑窗（200ms 窗，50ms hop @ 200Hz）
RMS_WIN = 40   # 200 ms
RMS_HOP = 10   # 50 ms

# 归一化系数（V4.2 contract）
NORM_MDF = 100.0
NORM_MNF = 150.0
NORM_ZCR = 400.0
NORM_RAW_UNFILT = 2048.0

# V4.2 13 列 header（与 data/v42/user_*/curl/*/rep_*.csv 严格一致）
V42_CSV_HEADER = [
    'Timestamp', 'Ang_Vel', 'Angle', 'Ang_Accel',
    'Target_RMS_Norm', 'Comp_RMS_Norm', 'Symmetry_Score', 'Phase_Progress',
    'Target_MDF', 'Target_MNF', 'Target_ZCR', 'Target_Raw_Unfilt', 'label',
]

LABEL_NAMES = ['standard', 'compensation', 'bad_form']


# =============================================================================
# 21 → 25 点骨架 remap（从 /tmp/flex_aqa/utils/misc.py remap21_to_25 简化复制）
# =============================================================================
def remap21_to_25(data21):
    """21 点 × 3 坐标 → NTU 25 点 × 3 坐标（SevenPair.load_Skeleton 等价）。

    data21: np.ndarray shape (T, 21, 3)
    返回  : np.ndarray shape (T, 25, 3)
    """
    mapping_25 = {
        0: None, 1: None, 2: 1, 3: 0,
        4: 7, 5: 8, 6: 9, 7: 10,
        8: 3, 9: 4, 10: 5, 11: 6,
        12: 16, 13: 17, 14: 18, 15: None,
        16: 11, 17: 12, 18: 13, 19: None,
        20: 2, 21: 10, 22: 10, 23: 6, 24: 6,
    }
    T, _, C = data21.shape
    data25 = np.zeros((T, 25, C), dtype=data21.dtype)
    for i25, i21 in mapping_25.items():
        if i21 is not None:
            data25[:, i25, :] = data21[:, i21, :]
    spine_base = 0.5 * (data21[:, 11, :] + data21[:, 16, :])
    data25[:, 0, :] = spine_base
    data25[:, 1, :] = 0.5 * (spine_base + data21[:, 2, :])
    data25[:, 19, :] = 0.5 * (data21[:, 14, :] + data21[:, 15, :])
    data25[:, 15, :] = 0.5 * (data21[:, 19, :] + data21[:, 20, :])
    return data25


# =============================================================================
# 数值工具
# =============================================================================
def _angle_3pt(a, b, c):
    """a-b-c 三点，返回在 b 处的夹角（度）。a/b/c shape (T, 3)。"""
    ba = a - b
    bc = c - b
    num = np.sum(ba * bc, axis=-1)
    denom = (np.linalg.norm(ba, axis=-1) * np.linalg.norm(bc, axis=-1)) + 1e-9
    cos = np.clip(num / denom, -1.0, 1.0)
    return np.degrees(np.arccos(cos)).astype(np.float32)


def _rolling_rms(sig, win, hop):
    """滑动窗口 RMS，返回与 sig 同长度（重采样回 T 点）。"""
    T = len(sig)
    if T < win:
        # 太短：退化为整段 RMS 广播
        rms = np.sqrt(np.mean(sig.astype(np.float64) ** 2) + 1e-12)
        return np.full(T, rms, dtype=np.float32)
    n_win = max(1, (T - win) // hop + 1)
    rms_arr = np.zeros(n_win, dtype=np.float32)
    for i in range(n_win):
        seg = sig[i * hop:i * hop + win]
        rms_arr[i] = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2) + 1e-12))
    # 重采样回 T 点
    x_old = np.linspace(0.0, 1.0, n_win)
    x_new = np.linspace(0.0, 1.0, T)
    return np.interp(x_new, x_old, rms_arr).astype(np.float32)


def _welch_mdf_mnf(sig, fs, nperseg=200):
    """Welch 谱 → MDF（中位频率）+ MNF（均值频率）。不依赖 FLEX repo。"""
    if len(sig) < nperseg:
        nperseg = max(16, len(sig) // 2)
    if not _HAS_SCIPY:
        # 兜底：FFT
        freqs = np.fft.rfftfreq(len(sig), d=1.0 / fs)
        psd = np.abs(np.fft.rfft(sig)) ** 2
    else:
        freqs, psd = scipy.signal.welch(sig, fs=fs, nperseg=nperseg, noverlap=nperseg // 2)
    psd = psd.astype(np.float64)
    total = psd.sum() + 1e-12
    # MNF = ∑(f·P)/∑P
    mnf = float((freqs * psd).sum() / total)
    # MDF = 频率，使得累计功率 = 50%
    cum = np.cumsum(psd)
    half = 0.5 * cum[-1]
    idx = int(np.searchsorted(cum, half))
    idx = min(idx, len(freqs) - 1)
    mdf = float(freqs[idx])
    return mdf, mnf


def _zcr(sig):
    """过零率：每秒的 sign-change 数估计；按 FLEX 200Hz 换算。"""
    if len(sig) < 2:
        return 0.0
    signs = np.sign(sig - np.mean(sig))
    signs[signs == 0] = 1
    changes = np.sum(np.abs(np.diff(signs)) > 0)
    # 秒数
    secs = len(sig) / float(FLEX_SAMPLE_RATE)
    return float(changes / max(secs, 1e-6))


def score_to_label(score, thresholds):
    """score → 三类 (0=standard, 1=compensation, 2=bad_form)。"""
    hi, lo = thresholds
    if score >= hi:
        return 0
    if score >= lo:
        return 1
    return 2


# =============================================================================
# Split 加载（.mat）
# =============================================================================
def load_split_map(flex_root):
    """读 Split_4/split_4_{train,test}_list.mat，合并成 {(class_idx, rep_idx): score}。

    mat 格式（SevenPair.py:23）: `consolidated_train_list` / `consolidated_test_list`
    每行: [class_idx(1-20), rep_idx(1-based), final_score, ...]
    """
    if not _HAS_SCIPY:
        raise RuntimeError("scipy required (pip install scipy)")
    out = {}
    for fname, key in [
        ('split_4_train_list.mat', 'consolidated_train_list'),
        ('split_4_test_list.mat', 'consolidated_test_list'),
    ]:
        p = os.path.join(flex_root, 'Split_4', fname)
        if not os.path.isfile(p):
            warnings.warn("[flex_preprocess] split 文件缺失: %s" % p)
            continue
        try:
            mat = scipy.io.loadmat(p)
            arr = mat.get(key)
            if arr is None:
                warnings.warn("[flex_preprocess] %s 中无 key '%s'" % (p, key))
                continue
            for row in arr:
                try:
                    ci = int(row[0])
                    ri = int(row[1])
                    sc = float(row[2])
                    out[(ci, ri)] = sc
                except Exception:
                    continue
        except Exception as ex:
            warnings.warn("[flex_preprocess] 读 %s 失败: %s" % (p, ex))
    return out


# =============================================================================
# 单 rep 处理核心
# =============================================================================
def load_emg_rep(flex_root, class_idx, rep_idx):
    """读 EMG.csv，返回 (T, 4) float32 ndarray。无 header。"""
    p = os.path.join(flex_root, 'EMG', 'A%02d' % class_idx, '%03d' % rep_idx, 'EMG.csv')
    if not os.path.isfile(p):
        return None
    # FLEX EMG.csv 无 header（SevenPair.py:82 header=None）
    df = pd.read_csv(p, header=None)
    arr = df.to_numpy(dtype=np.float32)
    # 取前 4 列
    if arr.shape[1] < 4:
        warnings.warn("[flex_preprocess] EMG 通道不足 4: %s (cols=%d)" % (p, arr.shape[1]))
        return None
    return arr[:, :4]


def load_skeleton_rep(flex_root, class_idx, rep_idx):
    """读 skeleton_points.csv → (Ts, 25, 3)；Ts 通常 ~103 帧。"""
    p = os.path.join(flex_root, 'Skeleton', 'A%02d' % class_idx, '%03d' % rep_idx, 'skeleton_points.csv')
    if not os.path.isfile(p):
        return None
    try:
        raw = pd.read_csv(p, header=0).to_numpy(dtype=np.float32)
    except Exception as ex:
        warnings.warn("[flex_preprocess] 读 %s 失败: %s" % (p, ex))
        return None
    # 21 点 × 3 坐标 = 63 列
    if raw.shape[1] != 63:
        warnings.warn("[flex_preprocess] skeleton 列数 != 63: %s (got %d)" % (p, raw.shape[1]))
        return None
    raw_21 = raw.reshape(-1, 21, 3)
    raw_25 = remap21_to_25(raw_21)
    return raw_25


def compute_elbow_series(skel25):
    """从 (Ts, 25, 3) 骨架计算 elbow 角度 + 角速度 + 角加速度。

    使用 ELBOW_SHOULDER_IDX/ELBOW_JOINT_IDX/ELBOW_WRIST_IDX (plan §1.4)
    返回：angle (Ts,), ang_vel (Ts,), ang_accel (Ts,) 都是 float32。
    """
    sh = skel25[:, ELBOW_SHOULDER_IDX, :]
    el = skel25[:, ELBOW_JOINT_IDX, :]
    wr = skel25[:, ELBOW_WRIST_IDX, :]
    angle = _angle_3pt(sh, el, wr)
    # 骨架抽样率约 30Hz（5 视角视频）；以 1/30s 为步长
    fs_skel = 30.0
    ang_vel = np.diff(angle, prepend=angle[0]) * fs_skel
    ang_accel = np.diff(ang_vel, prepend=ang_vel[0]) * fs_skel
    return angle.astype(np.float32), ang_vel.astype(np.float32), ang_accel.astype(np.float32)


def _resample_1d(arr, target_len):
    if len(arr) == target_len:
        return arr.astype(np.float32)
    x_old = np.linspace(0.0, 1.0, len(arr))
    x_new = np.linspace(0.0, 1.0, target_len)
    return np.interp(x_new, x_old, arr).astype(np.float32)


def build_13col_row(emg4, skel25, ch_target, ch_comp, ch_contra, label, target_len=200):
    """把一 rep 的 EMG(T,4) + Skeleton(Ts,25,3) → (target_len, 13) ndarray。

    target_len=200 对齐 V4.2 EMG_SEQ_LEN。
    每列按 V42_CSV_HEADER 顺序。
    """
    T_emg = emg4.shape[0]
    if T_emg < 4 or skel25 is None or skel25.shape[0] < 3:
        return None

    # --- EMG rep-level 特征 ---
    sig_target = emg4[:, ch_target].astype(np.float32)
    sig_comp = emg4[:, ch_comp].astype(np.float32)
    sig_contra = emg4[:, ch_contra].astype(np.float32) if emg4.shape[1] > ch_contra else sig_target

    # 滑窗 RMS，并归一化到 per-rep peak（FLEX 无 MVC, plan §1.4）
    tgt_rms = _rolling_rms(np.abs(sig_target), RMS_WIN, RMS_HOP)
    comp_rms = _rolling_rms(np.abs(sig_comp), RMS_WIN, RMS_HOP)
    peak_t = float(tgt_rms.max()) + 1e-6
    peak_c = float(comp_rms.max()) + 1e-6
    tgt_rms_norm = np.clip(tgt_rms / peak_t, 0.0, 1.0)
    comp_rms_norm = np.clip(comp_rms / peak_c, 0.0, 1.0)

    # Symmetry_Score = min(L,R)/max(L,R) 用 target vs contra
    contra_rms = _rolling_rms(np.abs(sig_contra), RMS_WIN, RMS_HOP)
    sym = np.minimum(tgt_rms, contra_rms) / (np.maximum(tgt_rms, contra_rms) + 1e-6)
    sym = np.clip(sym, 0.0, 1.0)

    # 频域 4 列（rep-level 标量，广播成长度 T_emg）
    mdf, mnf = _welch_mdf_mnf(sig_target, FLEX_SAMPLE_RATE, nperseg=min(200, T_emg))
    zcr = _zcr(sig_target)
    raw_unfilt = float(np.sqrt(np.mean(sig_target.astype(np.float64) ** 2) + 1e-12))

    mdf_arr = np.full(T_emg, mdf / NORM_MDF, dtype=np.float32)
    mnf_arr = np.full(T_emg, mnf / NORM_MNF, dtype=np.float32)
    zcr_arr = np.full(T_emg, zcr / NORM_ZCR, dtype=np.float32)
    raw_arr = np.full(T_emg, raw_unfilt / NORM_RAW_UNFILT, dtype=np.float32)

    # --- Skeleton rep-level 特征（重采样到 T_emg）---
    angle, ang_vel, ang_accel = compute_elbow_series(skel25)
    angle_e = _resample_1d(angle, T_emg)
    vel_e = _resample_1d(ang_vel, T_emg)
    acc_e = _resample_1d(ang_accel, T_emg)

    # Phase_Progress 线性 [0, 1]
    phase = np.linspace(0.0, 1.0, T_emg, dtype=np.float32)

    # Timestamp = 相对时间
    ts = np.arange(T_emg, dtype=np.float32) / FLEX_SAMPLE_RATE

    # 最后重采样到 target_len（对齐 V4.2 EMG_SEQ_LEN=200 即可；此处 target_len 可等于 T_emg）
    if target_len != T_emg:
        ts = _resample_1d(ts, target_len)
        vel_e = _resample_1d(vel_e, target_len)
        angle_e = _resample_1d(angle_e, target_len)
        acc_e = _resample_1d(acc_e, target_len)
        tgt_rms_norm = _resample_1d(tgt_rms_norm, target_len)
        comp_rms_norm = _resample_1d(comp_rms_norm, target_len)
        sym = _resample_1d(sym, target_len)
        phase = _resample_1d(phase, target_len)
        mdf_arr = _resample_1d(mdf_arr, target_len)
        mnf_arr = _resample_1d(mnf_arr, target_len)
        zcr_arr = _resample_1d(zcr_arr, target_len)
        raw_arr = _resample_1d(raw_arr, target_len)

    N = len(ts)
    label_col = np.full(N, int(label), dtype=np.int32)
    out = np.stack([
        ts, vel_e, angle_e, acc_e,
        tgt_rms_norm, comp_rms_norm, sym, phase,
        mdf_arr, mnf_arr, zcr_arr, raw_arr,
        label_col.astype(np.float32),
    ], axis=-1)
    return out


def write_rep_csv(mat13, path):
    """把 (N, 13) ndarray 写 CSV，header 完全匹配 V42_CSV_HEADER。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(V42_CSV_HEADER)
        for row in mat13:
            row_out = list(row[:-1]) + [int(row[-1])]
            w.writerow(row_out)


# =============================================================================
# 通道验证：抽前 K rep 计算 4 组合的 RMS-elbow_angle Pearson |r|
# =============================================================================
def _pearson(a, b):
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    a = a - a.mean()
    b = b - b.mean()
    num = (a * b).sum()
    den = np.sqrt((a * a).sum() * (b * b).sum()) + 1e-12
    return float(num / den)


def validate_channels(flex_root, class_ids, split_map, n_reps=5):
    """抽每类前几 rep 跑 4 组合相关性；返回挑选的 (target, comp) 组合 + 决策表。"""
    scores = {combo: [] for combo in VALIDATE_COMBOS}
    sampled = 0
    for cid in class_ids:
        a_dir = os.path.join(flex_root, 'EMG', 'A%02d' % cid)
        if not os.path.isdir(a_dir):
            continue
        rep_dirs = sorted(os.listdir(a_dir))[:n_reps]
        for rd in rep_dirs:
            try:
                rid = int(rd)
            except ValueError:
                continue
            emg = load_emg_rep(flex_root, cid, rid)
            skel = load_skeleton_rep(flex_root, cid, rid)
            if emg is None or skel is None:
                continue
            angle, _, _ = compute_elbow_series(skel)
            angle_e = _resample_1d(angle, emg.shape[0])
            for (ct, _cc) in VALIDATE_COMBOS:
                if ct >= emg.shape[1]:
                    continue
                rms = _rolling_rms(np.abs(emg[:, ct]), RMS_WIN, RMS_HOP)
                r = abs(_pearson(rms, angle_e))
                scores[(ct, _cc)].append(r)
            sampled += 1
            if sampled >= n_reps * len(class_ids):
                break
        if sampled >= n_reps * len(class_ids):
            break

    print("\n[validate_channels] 4 组合 RMS-elbow_angle |Pearson r| 均值:")
    print("  %-10s  %-8s  %-8s  %-6s" % ('combo', 'mean|r|', 'n_samples', 'rank'))
    rows = []
    for combo in VALIDATE_COMBOS:
        vals = scores[combo]
        mean_r = float(np.mean(vals)) if vals else 0.0
        rows.append((combo, mean_r, len(vals)))
    rows.sort(key=lambda x: -x[1])
    for i, (combo, mean_r, n) in enumerate(rows):
        print("  (%d,%d)      %-8.3f  %-8d  %d" % (combo[0], combo[1], mean_r, n, i + 1))

    best = rows[0][0] if rows else (FLEX_CH_TARGET_DEFAULT, FLEX_CH_COMP_DEFAULT)
    best_r = rows[0][1] if rows else 0.0
    print("[validate_channels] 决策：ch_target=%d, ch_comp=%d (|r|=%.3f)" %
          (best[0], best[1], best_r))
    if best_r < 0.6:
        print("[validate_channels] ⚠ |r|=%.3f < 0.6：通道选择置信度低，建议人工核查论文 FKG 表" % best_r)
    return best, rows


# =============================================================================
# Mock 合成数据冒烟
# =============================================================================
def make_mock_rep(rng, n_samples=400, quality='good'):
    """合成一 rep：EMG 4 通道 + 21 点骨架 + score。"""
    T_emg = n_samples
    T_skel = 100
    t = np.linspace(0, 2 * np.pi, T_emg)
    # biceps (ch0) 发力波形
    base = np.sin(t) * 0.6 + 0.1
    emg = np.zeros((T_emg, 4), dtype=np.float32)
    emg[:, 0] = base * (1.0 if quality == 'good' else 0.7) + rng.randn(T_emg) * 0.05
    emg[:, 1] = base * (0.3 if quality == 'good' else 0.8) + rng.randn(T_emg) * 0.05  # comp
    emg[:, 2] = base * 0.2 + rng.randn(T_emg) * 0.05
    emg[:, 3] = base * 0.1 + rng.randn(T_emg) * 0.05

    # 21 点骨架：构造弯举动作（shoulder 固定、elbow 跟着抬、wrist 最顶）
    skel = np.zeros((T_skel, 21, 3), dtype=np.float32)
    t2 = np.linspace(0, np.pi, T_skel)
    # idx 3 L-shoulder (→ 25[8]=shoulder via mapping 8: 3)
    skel[:, 3, :] = np.array([0.0, 0.0, 1.5])
    skel[:, 4, :] = np.stack([0.3 * np.ones(T_skel), np.sin(t2) * 0.2, 1.2 * np.ones(T_skel)], axis=-1)
    skel[:, 5, :] = np.stack([0.6 * np.ones(T_skel),
                               np.sin(t2) * 0.4 + 0.5,
                               1.0 + np.cos(t2) * 0.3], axis=-1)
    skel[:, 6, :] = skel[:, 5, :] + np.array([0.1, 0.05, 0.0])
    # 必填其他关节以避 NaN
    skel[:, 11, :] = np.array([-0.3, -0.5, 0.0])
    skel[:, 16, :] = np.array([0.3, -0.5, 0.0])
    skel[:, 2, :] = np.array([0.0, 0.3, 1.0])
    return emg, skel


def run_mock(out_dir, n_reps=30):
    """合成 30 fake rep 走完整链路，验证脚本本身无 bug。"""
    print("[flex_preprocess --mock] 合成 %d fake rep → %s" % (n_reps, out_dir))
    rng = np.random.RandomState(42)
    counts = {0: 0, 1: 0, 2: 0}
    gid = 0
    t0 = time.time()
    for i in range(n_reps):
        quality = ['good', 'mid', 'bad'][i % 3]
        emg, skel21 = make_mock_rep(rng, n_samples=400, quality=quality)
        skel25 = remap21_to_25(skel21)
        label = {'good': 0, 'mid': 1, 'bad': 2}[quality]
        mat = build_13col_row(emg, skel25, 0, 1, 2, label, target_len=200)
        if mat is None:
            continue
        cls_name = LABEL_NAMES[label]
        path = os.path.join(out_dir, 'curl', cls_name, 'rep_%04d.csv' % gid)
        write_rep_csv(mat, path)
        counts[label] += 1
        gid += 1
    print("[flex_preprocess --mock] 输出统计:")
    for lid, n in counts.items():
        print("  class %d (%s): %d reps" % (lid, LABEL_NAMES[lid], n))
    print("[flex_preprocess --mock] 总计 %d reps, 用时 %.2fs" % (gid, time.time() - t0))
    # 抽样验证首个 CSV 的 header 与列数
    sample = os.path.join(out_dir, 'curl', LABEL_NAMES[0], 'rep_0000.csv')
    if os.path.isfile(sample):
        with open(sample) as f:
            hdr = f.readline().strip().split(',')
            n_data = len(f.readline().strip().split(','))
        print("[flex_preprocess --mock] sample header (%d cols): %s" % (len(hdr), ','.join(hdr)))
        assert len(hdr) == 13, "header must have 13 cols, got %d" % len(hdr)
        assert hdr == V42_CSV_HEADER, "header mismatch V4.2 contract"
        assert n_data == 13, "data row must have 13 cols, got %d" % n_data
        print("[flex_preprocess --mock] ✓ header + 13 列完全对齐 V4.2 contract")
    return gid


# =============================================================================
# main
# =============================================================================
def parse_thresholds(s):
    parts = s.split(',')
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--thresholds 需要 2 个数值，格式 '80,50'")
    hi = float(parts[0])
    lo = float(parts[1])
    if lo >= hi:
        raise argparse.ArgumentTypeError("thresholds 要求 hi > lo")
    return (hi, lo)


def parse_class_ids(s):
    return [int(x) for x in s.split(',') if x.strip()]


def main():
    ap = argparse.ArgumentParser(description="FLEX_AQA → IronBuddy 13 列 rep CSV 预处理")
    ap.add_argument('--flex-root', default=None,
                    help='FLEX 解压根目录（含 EMG/ Skeleton/ Split_4/）')
    ap.add_argument('--out', default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'flex'))
    ap.add_argument('--thresholds', type=parse_thresholds, default=DEFAULT_THRESHOLDS,
                    help='score 阈值 "hi,lo"；hi 以上 standard，lo 以上 compensation')
    ap.add_argument('--curl-class-ids', type=parse_class_ids,
                    default=FLEX_CURL_CLASS_IDS_DEFAULT,
                    help='FLEX A0X 中弯举类别 id（逗号分隔）')
    ap.add_argument('--validate-channels', action='store_true',
                    help='抽前 5 rep 跑 4 组合相关性并挑最高')
    ap.add_argument('--mock', action='store_true',
                    help='合成 30 fake rep 冒烟（不需 FLEX_ROOT）')
    ap.add_argument('--ch-target', type=int, default=FLEX_CH_TARGET_DEFAULT)
    ap.add_argument('--ch-comp', type=int, default=FLEX_CH_COMP_DEFAULT)
    ap.add_argument('--ch-contra', type=int, default=FLEX_CH_CONTRA_L_DEFAULT)
    ap.add_argument('--target-len', type=int, default=200,
                    help='每 rep 重采样到多少点（对齐 V4.2 EMG_SEQ_LEN=200）')
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()

    t_start = time.time()

    # ---- (A) Mock 路径 ----
    if args.mock:
        out_dir = args.out
        os.makedirs(out_dir, exist_ok=True)
        n = run_mock(out_dir, n_reps=30)
        print("[flex_preprocess] --mock 完成: %d reps 输出到 %s" % (n, out_dir))
        return 0

    # ---- (B) 常规路径 ----
    if args.flex_root is None:
        print("[flex_preprocess] ✗ 未提供 --flex-root，也未使用 --mock。")
        print("[flex_preprocess]   请先申请 + 下载 FLEX 数据集。详见 data/flex/README.md。")
        print("[flex_preprocess]   或用 --mock 跑合成数据冒烟。")
        return 1

    if not os.path.isdir(args.flex_root):
        print("[flex_preprocess] ✗ --flex-root 不存在: %s" % args.flex_root)
        print("[flex_preprocess]   申请流程详见 data/flex/README.md")
        return 1

    if not _HAS_PANDAS:
        print("[flex_preprocess] ✗ 需要 pandas（dev 机）")
        return 2
    if not _HAS_SCIPY:
        print("[flex_preprocess] ✗ 需要 scipy（pip install scipy）")
        return 2

    # 加载 split map
    split_map = load_split_map(args.flex_root)
    print("[flex_preprocess] 加载 split_map: %d (class, rep) pairs" % len(split_map))

    # ---- 通道验证 ----
    if args.validate_channels:
        (best_t, best_c), rows = validate_channels(args.flex_root, args.curl_class_ids, split_map, n_reps=5)
        mapping_path = os.path.join(args.out, '_channel_mapping.json')
        os.makedirs(args.out, exist_ok=True)
        with open(mapping_path, 'w', encoding='utf-8') as f:
            json.dump({
                'ch_target': int(best_t),
                'ch_comp': int(best_c),
                'combos_evaluated': [
                    {'combo': list(c), 'mean_abs_r': mr, 'n_samples': int(n)}
                    for (c, mr, n) in rows
                ],
            }, f, indent=2)
        print("[flex_preprocess] 写决策到 %s" % mapping_path)
        # 同时更新当前 run 的 ch_target / ch_comp
        args.ch_target = int(best_t)
        args.ch_comp = int(best_c)

    # ---- 全量预处理 ----
    print("[flex_preprocess] 预处理配置:")
    print("  flex_root      = %s" % args.flex_root)
    print("  out            = %s" % args.out)
    print("  thresholds     = %s" % (args.thresholds,))
    print("  curl_class_ids = %s" % args.curl_class_ids)
    print("  (ch_target, ch_comp, ch_contra) = (%d, %d, %d)"
          % (args.ch_target, args.ch_comp, args.ch_contra))
    print("  target_len     = %d" % args.target_len)

    counts = {0: 0, 1: 0, 2: 0}
    gid = 0
    missing = 0
    err = 0
    for cid in args.curl_class_ids:
        a_dir = os.path.join(args.flex_root, 'EMG', 'A%02d' % cid)
        if not os.path.isdir(a_dir):
            warnings.warn("[flex_preprocess] 跳过 class %d (目录缺失: %s)" % (cid, a_dir))
            continue
        rep_dirs = sorted(os.listdir(a_dir))
        for rd in rep_dirs:
            try:
                rid = int(rd)
            except ValueError:
                continue
            score = split_map.get((cid, rid))
            if score is None:
                missing += 1
                continue
            label = score_to_label(score, args.thresholds)

            emg = load_emg_rep(args.flex_root, cid, rid)
            skel = load_skeleton_rep(args.flex_root, cid, rid)
            if emg is None or skel is None:
                err += 1
                continue
            mat = build_13col_row(emg, skel, args.ch_target, args.ch_comp,
                                   args.ch_contra, label, target_len=args.target_len)
            if mat is None:
                err += 1
                continue
            cls_name = LABEL_NAMES[label]
            path = os.path.join(args.out, 'curl', cls_name, 'rep_%04d.csv' % gid)
            write_rep_csv(mat, path)
            counts[label] += 1
            gid += 1

    print("\n[flex_preprocess] === 汇总 ===")
    for lid in [0, 1, 2]:
        pct = 100.0 * counts[lid] / max(gid, 1)
        print("  %-13s (id=%d): %d reps (%.1f%%)" % (LABEL_NAMES[lid], lid, counts[lid], pct))
    print("  总 rep 数    : %d" % gid)
    print("  无 split 映射: %d" % missing)
    print("  读取/计算失败: %d" % err)
    print("  用时        : %.1fs" % (time.time() - t_start))

    # 不均衡警告
    if gid > 0:
        for lid, n in counts.items():
            if n < 0.10 * gid:
                print("[flex_preprocess] ⚠ 类 %s 占比 < 10%%，建议 --thresholds 调整"
                      % LABEL_NAMES[lid])
    return 0


if __name__ == '__main__':
    sys.exit(main())
