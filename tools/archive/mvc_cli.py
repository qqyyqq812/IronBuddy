# coding=utf-8
#!/usr/bin/env python3
"""
IronBuddy V4.3 - MVC CLI 校准工具（SENIAM-2000 协议）
====================================================

用 ssh 到板端抓 raw EMG 3 次，算每次 200ms 滑动 RMS 峰值，取 3 次峰值的最大
作为 peak_mvc；std_pct > 0.20 提示重测；最终写
    data/v42/<user>/mvc_calibration.json

Usage
-----
    python tools/mvc_cli.py --user user_01
    python tools/mvc_cli.py --user test_user --dry-run --no-sleep

待 ssh 拿到后修订的顶部常量
---------------------------
- BOARD_RAW_GRAB_CMD  : 板端抓 raw EMG 的命令（现推断 /dev/shm/emg_raw_latest.bin）
- BOARD_RAW_DTYPE     : raw 二进制格式（int16 LE, 2 通道交错）
- BOARD_RAW_SAMPLE_HZ : 板端 EMG 原始采样率（1000 Hz）
- RMS_WINDOW_MS       : MVC 峰值检测的滑动 RMS 窗口（200 ms, SENIAM 推荐）
- REST_BETWEEN_REPS_S : SENIAM 协议要求 2 分钟充分休息
"""

from __future__ import absolute_import, division, print_function

import argparse
import json
import os
import subprocess
import sys
import time

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


# ---------------------------------------------------------------------------
# 顶部常量（待 ssh 核对）
# ---------------------------------------------------------------------------
BOARD_RAW_GRAB_CMD = "cat /dev/shm/emg_raw_latest.bin"  # 推断；真实脚本可能写在别处
BOARD_RAW_DTYPE = "<i2"
BOARD_RAW_CHANNELS = 2
BOARD_RAW_SAMPLE_HZ = 1000

RMS_WINDOW_MS = 200
REST_BETWEEN_REPS_S = 120
DEFAULT_BOARD = "toybrick@10.105.245.224"

SSH_TIMEOUT_S = 30
MVC_STD_WARN_PCT = 0.20  # > 20% 提示重测


# ---------------------------------------------------------------------------
# Board grab + RMS
# ---------------------------------------------------------------------------
def _grab_raw_from_board(board_host, duration_s):
    """ssh 抓 raw EMG 二进制流。返回 bytes，或 None 失败。"""
    # 用 dd 读定长样本数更稳；这里先用 timeout + cat 作骨架
    n_samples = int(BOARD_RAW_SAMPLE_HZ * duration_s)
    n_bytes = n_samples * BOARD_RAW_CHANNELS * 2  # int16
    cmd = ["ssh", "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=10", board_host,
           "timeout %d %s | head -c %d" % (duration_s + 2, BOARD_RAW_GRAB_CMD, n_bytes)]
    try:
        r = subprocess.run(cmd, capture_output=True,
                           timeout=duration_s + SSH_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        print("[ERROR] ssh 抓 raw 超时", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("[ERROR] ssh 命令未找到", file=sys.stderr)
        return None
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "ignore").strip()
        print("[ERROR] ssh 抓 raw 失败: %s" % err, file=sys.stderr)
        return None
    return r.stdout


def _bytes_to_channels(raw_bytes):
    """int16 LE 2 通道交错 -> (ch0, ch1) ndarray。"""
    arr = np.frombuffer(raw_bytes, dtype=BOARD_RAW_DTYPE)
    if arr.size % BOARD_RAW_CHANNELS != 0:
        arr = arr[: arr.size // BOARD_RAW_CHANNELS * BOARD_RAW_CHANNELS]
    mat = arr.reshape(-1, BOARD_RAW_CHANNELS).astype(np.float32)
    return mat[:, 0], mat[:, 1]


def _rms_peak(signal, window_samples):
    """滑动 RMS 最大值。"""
    if signal.size < window_samples:
        return float(np.sqrt(np.mean(signal.astype(np.float64) ** 2)))
    sq = signal.astype(np.float64) ** 2
    # 滑动均值用 cumsum trick
    csum = np.cumsum(sq)
    window_sum = csum[window_samples - 1:] - np.concatenate([[0.0], csum[:-window_samples]])
    rms = np.sqrt(window_sum / window_samples)
    return float(rms.max())


def _mock_raw_bytes(duration_s, peak_amp):
    """开发机 dry-run 用：sin + 噪声合成 raw."""
    n = int(BOARD_RAW_SAMPLE_HZ * duration_s)
    t = np.arange(n) / float(BOARD_RAW_SAMPLE_HZ)
    ch0 = peak_amp * np.sin(2 * np.pi * 15 * t) + 50 * np.random.randn(n)
    ch1 = (peak_amp * 0.6) * np.sin(2 * np.pi * 18 * t) + 40 * np.random.randn(n)
    mat = np.stack([ch0, ch1], axis=1).astype(np.int16)
    return mat.tobytes()


# ---------------------------------------------------------------------------
# 单次 MVC 测试
# ---------------------------------------------------------------------------
def _one_mvc_trial(idx, total, args):
    print("\n=== MVC 第 %d/%d 次 ===" % (idx, total))
    print("请准备做最大等长收缩（用尽全力 %.0f 秒）..." % args.duration)
    for k in (3, 2, 1):
        print("  %d..." % k, flush=True)
        time.sleep(0 if args.no_sleep else 1)
    print("  GO !")

    if args.dry_run:
        raw = _mock_raw_bytes(args.duration, peak_amp=800.0 + idx * 20.0)
    else:
        raw = _grab_raw_from_board(args.board, args.duration)
        if raw is None or len(raw) == 0:
            print("[ERROR] 未获取到 raw 数据", file=sys.stderr)
            return None

    ch0, ch1 = _bytes_to_channels(raw)
    win_samples = int(BOARD_RAW_SAMPLE_HZ * RMS_WINDOW_MS / 1000.0)
    peak_ch0 = _rms_peak(ch0, win_samples)
    peak_ch1 = _rms_peak(ch1, win_samples)
    print("[OK] trial %d: peak_ch0=%.1f  peak_ch1=%.1f" % (idx, peak_ch0, peak_ch1))
    return peak_ch0, peak_ch1


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(args):
    if not _HAS_NUMPY:
        print("[ERROR] 需要 numpy", file=sys.stderr)
        sys.exit(2)

    if args.dry_run:
        print("[WARN] --dry-run：跳过 ssh，用 mock raw 数据")

    trials = []
    for i in range(1, args.reps + 1):
        result = _one_mvc_trial(i, args.reps, args)
        if result is None:
            sys.exit(2)
        trials.append(result)
        if i < args.reps and not args.no_sleep:
            print("\n[REST] 请休息 %d 秒（SENIAM 协议）..." % REST_BETWEEN_REPS_S)
            time.sleep(REST_BETWEEN_REPS_S)

    ch0_peaks = np.asarray([t[0] for t in trials], dtype=np.float64)
    ch1_peaks = np.asarray([t[1] for t in trials], dtype=np.float64)

    peak_mvc = {
        "ch0": float(ch0_peaks.max()),
        "ch1": float(ch1_peaks.max()),
    }
    mean_ch0 = float(ch0_peaks.mean())
    mean_ch1 = float(ch1_peaks.mean())
    std_pct_ch0 = float(ch0_peaks.std() / mean_ch0) if mean_ch0 > 1e-6 else 0.0
    std_pct_ch1 = float(ch1_peaks.std() / mean_ch1) if mean_ch1 > 1e-6 else 0.0
    std_pct = max(std_pct_ch0, std_pct_ch1)

    if std_pct > MVC_STD_WARN_PCT:
        print("\n[WARN] std_pct=%.2f > %.2f，建议重测（疲劳/姿势不稳）"
              % (std_pct, MVC_STD_WARN_PCT))

    payload = {
        "protocol": "SENIAM-2000",
        "peak_mvc": peak_mvc,
        "exercise": args.exercise,
        "std_pct": round(std_pct, 4),
        "ts": time.time(),
        "source": "mvc_cli",
        "user_id": args.user,
    }

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    user_root = os.path.join(project_root, "data", "v42", args.user)
    if not os.path.isdir(user_root):
        os.makedirs(user_root)
    out_path = os.path.join(user_root, "mvc_calibration.json")
    if args.dry_run:
        out_path = "/tmp/test_mvc_%s.json" % args.user

    with open(out_path, "w") as f:
        json.dump(payload, f, indent=2)
    print("\n[DONE] MVC 校准已写入: %s" % out_path)
    print("       peak_mvc = ch0=%.1f  ch1=%.1f  std_pct=%.3f"
          % (peak_mvc["ch0"], peak_mvc["ch1"], std_pct))


def _parse_args():
    p = argparse.ArgumentParser(description="V4.3 MVC CLI 校准工具")
    p.add_argument("--user", required=True)
    p.add_argument("--exercise", default="curl", choices=["curl", "squat"])
    p.add_argument("--duration", type=float, default=5.0,
                   help="每次 MVC 持续秒数（默认 5s）")
    p.add_argument("--reps", type=int, default=3, help="MVC 测试次数（默认 3）")
    p.add_argument("--board", default=DEFAULT_BOARD)
    p.add_argument("--dry-run", action="store_true",
                   help="跳过 ssh，用 mock raw；输出到 /tmp/test_mvc_<user>.json")
    p.add_argument("--no-sleep", action="store_true",
                   help="跳过倒计时与 2 分钟休息（debug 用）")
    return p.parse_args()


def main():
    args = _parse_args()
    run(args)


if __name__ == "__main__":
    main()
