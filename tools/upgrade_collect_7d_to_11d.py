# coding=utf-8
#!/usr/bin/env python3
"""
IronBuddy V4.3 - 7D -> 11D Collect Upgrader (CLI-only)
======================================================

从板端用 ssh/scp 拉取旧脚本（collect_one.sh / start_collect.sh）产出的 7D CSV
(+ 可选 raw EMG 文件)，补齐 4 个新特征 (MDF/MNF/ZCR/Raw_Unfilt)，按 Angle 谷底
切 rep，最终输出 V4.2 数据契约的 13 列 rep_NNN.csv 到
    data/v42/<user>/<exercise>/<label>/

主路径（推荐）：板端有 raw 二进制 → scipy.signal.welch 算频谱特征
兜底路径：无 raw → 从 Target_RMS 包络 FFT 近似（精度差但能跑通流程）

严禁改动 dual_branch_fusion.py / fusion_model.py / main_claw_loop.py / streamer_app.py。

Usage
-----
    python tools/upgrade_collect_7d_to_11d.py \\
        --board-host toybrick@10.105.245.224 \\
        --user user_01 --label standard --exercise curl

参数全部可覆盖 glob / 输出路径，方便适配板端真实文件名。

待 ssh 拿到后修订的顶部常量
---------------------------
- OLD_COLUMNS          : 旧 7D CSV 的列名（现按 V4.2 13 列前 8 列推断）
- BOARD_CSV_GLOB_TPL   : 板端 CSV 文件匹配模板
- BOARD_RAW_GLOB_TPL   : 板端 raw 二进制文件匹配模板
- BOARD_RAW_DTYPE      : 默认 int16 little-endian 2 通道交错
- BOARD_RAW_SAMPLE_HZ  : 默认 1000 Hz（板端 udp_emg_server 输入率）
"""

from __future__ import absolute_import, division, print_function

import argparse
import glob as _glob
import json
import os
import subprocess
import sys
import tempfile
import time

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:  # 开发机必有，板端不会跑这个脚本
    _HAS_NUMPY = False

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

try:
    from scipy import signal as _sps
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False


# ---------------------------------------------------------------------------
# 顶部常量（待 ssh 拿到板端真实脚本后 fine-tune）
# ---------------------------------------------------------------------------
# 推断：旧 collect_one.sh 输出的 7D CSV 含 Timestamp + 7D 特征 = 8 列
# 真实列名要看 start_collect.sh / collect_one.sh 才能确认
OLD_COLUMNS = [
    "Timestamp", "Ang_Vel", "Angle", "Ang_Accel",
    "Target_RMS", "Comp_RMS", "Symmetry_Score", "Phase_Progress",
]

BOARD_CSV_GLOB_TPL = "/home/toybrick/streamer_v3/output/{user}_{label}_*.csv"
BOARD_RAW_GLOB_TPL = "/home/toybrick/streamer_v3/output/{user}_{label}_*.raw"

# raw EMG 二进制格式（int16 LE, 2 通道交错），1000 Hz 采样（板端 udp_emg_server）
BOARD_RAW_DTYPE = "<i2"
BOARD_RAW_CHANNELS = 2
BOARD_RAW_SAMPLE_HZ = 1000

# V4.2 归一化常数（与 collect_training_data_v42.py / dual_branch_fusion 一致）
NORM_MDF = 100.0
NORM_MNF = 150.0
NORM_ZCR = 400.0
NORM_RAW_UNFILT = 2048.0

CSV_HEADER = [
    "Timestamp",
    "Ang_Vel", "Angle", "Ang_Accel",
    "Target_RMS_Norm", "Comp_RMS_Norm", "Symmetry_Score", "Phase_Progress",
    "Target_MDF", "Target_MNF", "Target_ZCR", "Target_Raw_Unfilt",
    "label",
]
LABEL_INT = {"standard": 0, "compensation": 1, "bad_form": 2}

SSH_TIMEOUT_S = 30
LOCAL_STAGE_DIR = "/tmp/ironbuddy_upgrade"

# rep 切分参数
MIN_REP_DURATION_S = 1.5
MAX_REP_DURATION_S = 3.0


# ---------------------------------------------------------------------------
# SSH / SCP helpers
# ---------------------------------------------------------------------------
def _run(cmd, timeout=SSH_TIMEOUT_S):
    """封装 subprocess.run，capture_output + timeout + 友好错误。"""
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        print("[ERROR] timeout after %ds: %s" % (timeout, " ".join(cmd)),
              file=sys.stderr)
        return None
    except FileNotFoundError:
        print("[ERROR] command not found: %s" % cmd[0], file=sys.stderr)
        return None
    return result


def _ssh_ls(board_host, pattern):
    """ssh 到板端 ls 匹配的文件路径。返回 list[str]（可能为空）。"""
    cmd = ["ssh", "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=10", board_host,
           "ls -1 %s 2>/dev/null" % pattern]
    r = _run(cmd)
    if r is None or r.returncode != 0:
        return []
    return [ln.strip() for ln in r.stdout.decode("utf-8", "ignore").splitlines()
            if ln.strip()]


def _scp_pull(board_host, remote_path, local_path):
    """scp 拉文件。失败返回 False。"""
    cmd = ["scp", "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=10",
           "%s:%s" % (board_host, remote_path), local_path]
    r = _run(cmd)
    if r is None or r.returncode != 0:
        err = (r.stderr.decode("utf-8", "ignore") if r else "").strip()
        print("[ERROR] scp failed %s -> %s (%s)" % (remote_path, local_path, err),
              file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# 4 个新特征算法
# ---------------------------------------------------------------------------
def _compute_spectral_from_raw(raw_segment, fs):
    """
    给一段 raw EMG (1D float ndarray)，算 (MDF, MNF, ZCR, Raw_Unfilt_RMS)。
    主路径：scipy.signal.welch, nperseg=200。
    """
    if not _HAS_SCIPY or not _HAS_NUMPY or raw_segment.size < 64:
        return 0.0, 0.0, 0.0, 0.0

    nperseg = min(200, raw_segment.size)
    f, psd = _sps.welch(raw_segment, fs=fs, nperseg=nperseg)
    total = float(psd.sum())
    if total < 1e-12:
        mdf = 0.0
        mnf = 0.0
    else:
        # MNF = sum(f * psd) / sum(psd)
        mnf = float((f * psd).sum() / total)
        # MDF = 中位频率（累计功率 = 总功率一半的 f）
        cum = np.cumsum(psd)
        half = cum[-1] / 2.0
        idx = int(np.searchsorted(cum, half))
        idx = min(idx, len(f) - 1)
        mdf = float(f[idx])

    # ZCR: 过零率 (counts / total samples) * fs → Hz 量纲
    sign = np.sign(raw_segment)
    sign[sign == 0] = 1.0
    zc = int((sign[1:] != sign[:-1]).sum())
    zcr = float(zc) / raw_segment.size * fs

    raw_rms = float(np.sqrt(np.mean(raw_segment.astype(np.float64) ** 2)))
    return mdf, mnf, zcr, raw_rms


def _fallback_spectral_from_rms(target_rms_series, win_len=16):
    """
    兜底路径（无 raw）：
    - ZCR: Target_RMS 滑动窗口的局部方差变化率近似
    - MDF/MNF: RMS 包络 FFT 的中心/中位频率（精度极差，仅做特征存在性兜底）
    - Raw_Unfilt ≈ Target_RMS * 0.85（经验系数）
    返回：per-sample 列 (mdf, mnf, zcr, raw_unfilt)，长度 == len(target_rms_series)
    """
    n = len(target_rms_series)
    if n == 0 or not _HAS_NUMPY:
        return np.zeros((0, 4), dtype=np.float32)

    arr = np.asarray(target_rms_series, dtype=np.float64)
    # ZCR 近似：滑动窗口方向变化
    diffs = np.sign(np.diff(arr, prepend=arr[0]))
    diffs[diffs == 0] = 1.0
    zcr_series = np.zeros(n, dtype=np.float32)
    for i in range(n):
        lo = max(0, i - win_len // 2)
        hi = min(n, i + win_len // 2)
        seg = diffs[lo:hi]
        if seg.size > 1:
            zcr_series[i] = float((seg[1:] != seg[:-1]).sum()) / (seg.size - 1) * 400.0

    # MDF/MNF 从整段 RMS 包络估计（为该 rep 的常量），用 FFT
    if n >= 8:
        fft = np.abs(np.fft.rfft(arr - arr.mean()))
        freqs = np.fft.rfftfreq(n, d=1.0 / 200.0)  # 假设 200 Hz 采样率（V4.2 poll_hz）
        if fft.sum() > 1e-9:
            mnf_val = float((freqs * fft).sum() / fft.sum())
            cum = np.cumsum(fft)
            half = cum[-1] / 2.0
            idx = int(np.searchsorted(cum, half))
            idx = min(idx, len(freqs) - 1)
            mdf_val = float(freqs[idx])
        else:
            mnf_val = 0.0
            mdf_val = 0.0
    else:
        mnf_val = 0.0
        mdf_val = 0.0

    raw_unfilt = arr * 0.85

    out = np.zeros((n, 4), dtype=np.float32)
    out[:, 0] = mdf_val
    out[:, 1] = mnf_val
    out[:, 2] = zcr_series
    out[:, 3] = raw_unfilt.astype(np.float32)
    return out


# ---------------------------------------------------------------------------
# rep 切分
# ---------------------------------------------------------------------------
def _split_into_reps(df, ts_col="Timestamp", angle_col="Angle"):
    """
    用 scipy.signal.find_peaks 在 Angle 列找局部最小值（弯举顶部=角度最小）。
    返回 list of (start_idx, end_idx)。要求 rep_len ∈ [MIN, MAX] 秒。
    """
    if not _HAS_SCIPY or not _HAS_NUMPY:
        return []
    angle = df[angle_col].to_numpy(dtype=np.float64)
    ts = df[ts_col].to_numpy(dtype=np.float64)
    if angle.size < 20:
        return []

    # find_peaks on -angle 找谷底
    # distance 以样本数估算：假设 200 Hz → 1.5s = 300 samples 最小间隔
    avg_dt = float(np.mean(np.diff(ts))) if ts.size > 1 else 0.005
    if avg_dt <= 0:
        avg_dt = 0.005
    min_dist = max(10, int(MIN_REP_DURATION_S / avg_dt))

    valleys, _ = _sps.find_peaks(-angle, distance=min_dist)
    if valleys.size < 2:
        return []

    reps = []
    for i in range(valleys.size - 1):
        s = int(valleys[i])
        e = int(valleys[i + 1])
        dur = ts[e] - ts[s]
        if MIN_REP_DURATION_S <= dur <= MAX_REP_DURATION_S:
            reps.append((s, e))
    return reps


# ---------------------------------------------------------------------------
# MVC 加载
# ---------------------------------------------------------------------------
def _load_peak_mvc(project_root, user):
    path = os.path.join(project_root, "data", "v42", user, "mvc_calibration.json")
    if not os.path.isfile(path):
        print("[WARN] MVC 校准缺失: %s -> 归一化分母用 1.0 兜底" % path)
        return {"ch0": 1.0, "ch1": 1.0}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        peak = data.get("peak_mvc", {})
        return {
            "ch0": float(peak.get("ch0", 1.0) or 1.0),
            "ch1": float(peak.get("ch1", 1.0) or 1.0),
        }
    except (OSError, ValueError) as ex:
        print("[WARN] 解析 MVC 失败 (%s) -> 用 1.0 兜底" % ex)
        return {"ch0": 1.0, "ch1": 1.0}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(args):
    if not _HAS_NUMPY or not _HAS_PANDAS or not _HAS_SCIPY:
        print("[ERROR] 需要 numpy + pandas + scipy（开发机环境）", file=sys.stderr)
        sys.exit(2)

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = args.out or os.path.join(
        project_root, "data", "v42", args.user, args.exercise, args.label,
    )
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    if not os.path.isdir(LOCAL_STAGE_DIR):
        os.makedirs(LOCAL_STAGE_DIR)

    csv_glob = args.board_csv_glob.format(user=args.user, label=args.label)
    raw_glob = args.board_raw_glob.format(user=args.user, label=args.label)

    print("[INFO] 查找板端 CSV: %s" % csv_glob)
    csv_remotes = _ssh_ls(args.board_host, csv_glob)
    if not csv_remotes:
        print("[ERROR] 板端未找到匹配 CSV，检查 --board-csv-glob 和 ssh 连通性",
              file=sys.stderr)
        sys.exit(2)
    csv_remote = csv_remotes[-1]  # 最新
    csv_local = os.path.join(LOCAL_STAGE_DIR, os.path.basename(csv_remote))
    print("[INFO] scp <- %s" % csv_remote)
    if not _scp_pull(args.board_host, csv_remote, csv_local):
        sys.exit(2)

    print("[INFO] 查找板端 raw: %s" % raw_glob)
    raw_remotes = _ssh_ls(args.board_host, raw_glob)
    raw_local = None
    if raw_remotes:
        raw_remote = raw_remotes[-1]
        raw_local = os.path.join(LOCAL_STAGE_DIR, os.path.basename(raw_remote))
        print("[INFO] scp <- %s" % raw_remote)
        if not _scp_pull(args.board_host, raw_remote, raw_local):
            raw_local = None
    else:
        print("[WARN] 无 raw 文件 -> 启用兜底频谱近似")

    # 读 CSV
    df = pd.read_csv(csv_local)
    if len(df.columns) != len(OLD_COLUMNS):
        print("[WARN] 列数 %d != OLD_COLUMNS %d，按实际列名处理"
              % (len(df.columns), len(OLD_COLUMNS)))
    else:
        df.columns = OLD_COLUMNS

    required = {"Timestamp", "Angle", "Target_RMS"}
    if not required.issubset(df.columns):
        print("[ERROR] 缺关键列 %s (实际=%s)" % (required, list(df.columns)),
              file=sys.stderr)
        sys.exit(2)

    # 4 个新特征
    if raw_local is not None:
        raw_bytes = np.fromfile(raw_local, dtype=BOARD_RAW_DTYPE)
        if raw_bytes.size % BOARD_RAW_CHANNELS != 0:
            raw_bytes = raw_bytes[: raw_bytes.size // BOARD_RAW_CHANNELS * BOARD_RAW_CHANNELS]
        raw_matrix = raw_bytes.reshape(-1, BOARD_RAW_CHANNELS).astype(np.float32)
        target_raw = raw_matrix[:, 0]
        # 对齐 CSV 时间：按 CSV 行数重采样为每行一段切片
        n_rows = len(df)
        samples_per_row = max(1, target_raw.size // max(1, n_rows))
        spec_cols = np.zeros((n_rows, 4), dtype=np.float32)
        for i in range(n_rows):
            lo = i * samples_per_row
            hi = min(target_raw.size, (i + 1) * samples_per_row)
            seg = target_raw[lo:hi]
            mdf, mnf, zcr, raw_rms = _compute_spectral_from_raw(seg, BOARD_RAW_SAMPLE_HZ)
            spec_cols[i] = [mdf, mnf, zcr, raw_rms]
    else:
        spec_cols = _fallback_spectral_from_rms(df["Target_RMS"].to_numpy())

    # MVC 归一化
    peak_mvc = _load_peak_mvc(project_root, args.user)
    mvc0 = peak_mvc["ch0"] if peak_mvc["ch0"] > 1e-6 else 1.0
    mvc1 = peak_mvc["ch1"] if peak_mvc["ch1"] > 1e-6 else 1.0

    df_out = pd.DataFrame()
    df_out["Timestamp"] = df["Timestamp"].astype(float)
    df_out["Ang_Vel"] = df["Ang_Vel"].astype(float)
    df_out["Angle"] = df["Angle"].astype(float)
    df_out["Ang_Accel"] = df["Ang_Accel"].astype(float)
    df_out["Target_RMS_Norm"] = df["Target_RMS"].astype(float) / mvc0
    df_out["Comp_RMS_Norm"] = df["Comp_RMS"].astype(float) / mvc1
    df_out["Symmetry_Score"] = df["Symmetry_Score"].astype(float)
    df_out["Phase_Progress"] = df["Phase_Progress"].astype(float)
    df_out["Target_MDF"] = spec_cols[:, 0] / NORM_MDF
    df_out["Target_MNF"] = spec_cols[:, 1] / NORM_MNF
    df_out["Target_ZCR"] = spec_cols[:, 2] / NORM_ZCR
    df_out["Target_Raw_Unfilt"] = spec_cols[:, 3] / NORM_RAW_UNFILT
    df_out["label"] = LABEL_INT[args.label]

    # 切 rep
    reps = _split_into_reps(df_out)
    print("[INFO] 检测到 %d 个 rep" % len(reps))

    session_log = os.path.join(os.path.dirname(out_dir), "session.log")
    log_lines = []
    n_written = 0
    for i, (s, e) in enumerate(reps, start=1):
        rep = df_out.iloc[s:e + 1].copy()
        if len(rep) < 50:
            print("[WARN] rep %d 太短 (%d 行) 丢弃" % (i, len(rep)))
            continue
        if len(rep) > 500:
            rep = rep.iloc[:500].copy()
        rep_path = os.path.join(out_dir, "rep_%03d.csv" % (n_written + 1))
        rep.to_csv(rep_path, index=False, float_format="%.4f")
        n_written += 1
        dur = float(rep["Timestamp"].iloc[-1] - rep["Timestamp"].iloc[0])
        log_lines.append("%s\t%s\t%s\t%.2fs\t%drows\trep_%03d.csv" % (
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            args.exercise, args.label, dur, len(rep), n_written,
        ))

    if log_lines:
        with open(session_log, "a") as f:
            f.write("\n".join(log_lines) + "\n")

    print("[DONE] 写出 %d 个 rep 到 %s" % (n_written, out_dir))
    if n_written == 0:
        sys.exit(3)


def _parse_args():
    p = argparse.ArgumentParser(
        description="V4.3 7D -> 11D CLI-only upgrader (ssh + scipy)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--board-host", required=True,
                   help="ssh host, e.g. toybrick@10.105.245.224")
    p.add_argument("--user", required=True, help="user id, e.g. user_01")
    p.add_argument("--label", required=True,
                   choices=["standard", "compensation", "bad_form"])
    p.add_argument("--exercise", default="curl", choices=["curl", "squat"])
    p.add_argument("--segment-len", type=int, default=60,
                   help="(预留) 板端采集总时长秒（主脚本暂不用）")
    p.add_argument("--rep-len", type=float, default=2.0,
                   help="(预留) 期望每 rep 约多长；实际切分用 find_peaks + [1.5, 3.0]s")
    p.add_argument("--board-csv-glob", default=BOARD_CSV_GLOB_TPL,
                   help="板端 CSV glob 模板，{user}/{label} 占位")
    p.add_argument("--board-raw-glob", default=BOARD_RAW_GLOB_TPL,
                   help="板端 raw glob 模板，{user}/{label} 占位")
    p.add_argument("--out", default=None,
                   help="输出目录；默认 data/v42/<user>/<exercise>/<label>/")
    return p.parse_args()


def main():
    args = _parse_args()
    run(args)


if __name__ == "__main__":
    main()
