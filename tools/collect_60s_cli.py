# coding=utf-8
#!/usr/bin/env python3
"""
IronBuddy V4.3 - 极简 60s CLI 采集（板端旧脚本不可用时的兜底）
============================================================

流程：
1) 检查 MVC 已校准 (data/v42/<user>/mvc_calibration.json)
2) ssh 板端启动 udp_emg_server（若未运行）
3) 倒计时 3-2-1
4) ssh 板端 timeout N cat /dev/shm/emg_raw_buffer > /tmp/<user>_<label>_<ts>.raw
5) scp 回本地，再复用 upgrade_collect_7d_to_11d 的频谱 + rep 切分算法
6) 写 rep_NNN.csv 到 data/v42/<user>/<exercise>/<label>/

注意：
- 主路径仍然是板端旧 collect_one.sh + tools/upgrade_collect_7d_to_11d.py
- 本脚本为兜底：若旧脚本完全不可用，直接走 raw 提取 + 算特征

待 ssh 拿到后修订的顶部常量
---------------------------
- BOARD_UDP_EMG_PATH   : 板端 udp_emg_server.py 的绝对路径
- BOARD_RAW_BUFFER     : 板端 raw EMG 缓冲文件（推断 /dev/shm/emg_raw_buffer）
- BOARD_STAGE_DIR      : 板端落盘目录（推断 /tmp/）
- 本脚本暂不产出 Angle 列（需要 pose_data 辅助），写入 NaN 然后靠下游拒收
  → 真实接入时要么板端同步写 pose CSV，要么本地合成时用视觉替身
"""

from __future__ import absolute_import, division, print_function

import argparse
import json
import os
import subprocess
import sys
import time

# 复用 upgrade 脚本内的算法函数（同目录 import）
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

try:
    import pandas as pd
    _HAS_PANDAS = True
except ImportError:
    _HAS_PANDAS = False

from upgrade_collect_7d_to_11d import (  # noqa: E402
    _compute_spectral_from_raw,
    _split_into_reps,
    _load_peak_mvc,
    CSV_HEADER,
    LABEL_INT,
    NORM_MDF, NORM_MNF, NORM_ZCR, NORM_RAW_UNFILT,
    BOARD_RAW_SAMPLE_HZ,
    BOARD_RAW_CHANNELS,
    BOARD_RAW_DTYPE,
)

# ---------------------------------------------------------------------------
# 顶部常量（待 ssh 拿到后修订）
# ---------------------------------------------------------------------------
BOARD_UDP_EMG_PATH = "/home/toybrick/embedded-fullstack/hardware_engine/sensor/udp_emg_server.py"
BOARD_RAW_BUFFER = "/dev/shm/emg_raw_buffer"
BOARD_STAGE_DIR = "/tmp"
DEFAULT_BOARD = "toybrick@10.105.245.224"
LOCAL_STAGE = "/tmp/ironbuddy_collect60"
SSH_TIMEOUT_S = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _run(cmd, timeout=SSH_TIMEOUT_S):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        print("[ERROR] timeout: %s" % " ".join(cmd), file=sys.stderr)
        return None
    except FileNotFoundError:
        print("[ERROR] command not found: %s" % cmd[0], file=sys.stderr)
        return None


def _ensure_udp_server(board_host):
    """若 udp_emg_server 未运行则 nohup 起一个。pgrep 用 bracket trick。"""
    check = _run(["ssh", "-o", "BatchMode=yes",
                  "-o", "ConnectTimeout=10", board_host,
                  "pgrep -af '[u]dp_emg_server' >/dev/null && echo RUNNING || echo STOPPED"])
    if check is None:
        return False
    state = check.stdout.decode("utf-8", "ignore").strip()
    if "RUNNING" in state:
        print("[INFO] udp_emg_server 已在运行")
        return True
    print("[INFO] 启动板端 udp_emg_server ...")
    start_cmd = ("nohup python3 %s > /tmp/emg_server.log 2>&1 &" % BOARD_UDP_EMG_PATH)
    r = _run(["ssh", "-o", "BatchMode=yes", board_host, start_cmd])
    if r is None or r.returncode != 0:
        print("[WARN] 启动 udp_emg_server 失败（继续尝试采集）")
        return False
    time.sleep(1.0)
    return True


def _check_mvc(project_root, user):
    mvc = os.path.join(project_root, "data", "v42", user, "mvc_calibration.json")
    if not os.path.isfile(mvc):
        print("[ERROR] MVC 未校准：%s\n  先跑 python tools/mvc_cli.py --user %s"
              % (mvc, user), file=sys.stderr)
        sys.exit(2)
    try:
        with open(mvc, "r") as f:
            data = json.load(f)
        if data.get("protocol") != "SENIAM-2000":
            print("[ERROR] MVC 协议错误: %s" % data.get("protocol"), file=sys.stderr)
            sys.exit(2)
    except (OSError, ValueError) as ex:
        print("[ERROR] MVC 解析失败: %s" % ex, file=sys.stderr)
        sys.exit(2)
    return mvc


def _grab_60s_raw(board_host, duration, user, label):
    """板端用 timeout N cat 写 raw 到 /tmp/, 再 scp 回本地."""
    ts = int(time.time())
    remote_path = "%s/%s_%s_%d.raw" % (BOARD_STAGE_DIR, user, label, ts)
    cmd = ["ssh", "-o", "BatchMode=yes", board_host,
           "timeout %d cat %s > %s" % (duration + 1, BOARD_RAW_BUFFER, remote_path)]
    r = _run(cmd, timeout=duration + SSH_TIMEOUT_S)
    if r is None or r.returncode not in (0, 124):  # timeout -> 124 is expected
        err = (r.stderr.decode("utf-8", "ignore") if r else "").strip()
        print("[ERROR] 板端采集失败: %s" % err, file=sys.stderr)
        return None

    if not os.path.isdir(LOCAL_STAGE):
        os.makedirs(LOCAL_STAGE)
    local_path = os.path.join(LOCAL_STAGE, os.path.basename(remote_path))
    cmd = ["scp", "-o", "BatchMode=yes",
           "%s:%s" % (board_host, remote_path), local_path]
    r = _run(cmd)
    if r is None or r.returncode != 0:
        print("[ERROR] scp 回本地失败", file=sys.stderr)
        return None
    return local_path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(args):
    if not _HAS_NUMPY or not _HAS_PANDAS:
        print("[ERROR] 需要 numpy + pandas", file=sys.stderr)
        sys.exit(2)

    project_root = os.path.dirname(_THIS_DIR)
    _check_mvc(project_root, args.user)

    out_dir = os.path.join(project_root, "data", "v42",
                           args.user, args.exercise, args.label)
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)

    _ensure_udp_server(args.board)

    print("\n=== 准备采集 %s / %s ===" % (args.exercise, args.label))
    print("请连续做 %d 秒（高频弯举，10-15 rep）" % args.duration)
    for k in (3, 2, 1):
        print("  %d..." % k, flush=True)
        time.sleep(1)
    print("  GO ! (采集中...)")

    raw_path = _grab_60s_raw(args.board, args.duration, args.user, args.label)
    if raw_path is None:
        sys.exit(2)
    print("[INFO] raw 文件: %s" % raw_path)

    # 读 raw
    raw = np.fromfile(raw_path, dtype=BOARD_RAW_DTYPE)
    if raw.size % BOARD_RAW_CHANNELS != 0:
        raw = raw[: raw.size // BOARD_RAW_CHANNELS * BOARD_RAW_CHANNELS]
    mat = raw.reshape(-1, BOARD_RAW_CHANNELS).astype(np.float32)
    target = mat[:, 0]
    comp = mat[:, 1]

    peak_mvc = _load_peak_mvc(project_root, args.user)
    mvc0 = peak_mvc["ch0"] if peak_mvc["ch0"] > 1e-6 else 1.0
    mvc1 = peak_mvc["ch1"] if peak_mvc["ch1"] > 1e-6 else 1.0

    # 以 100 Hz 降采成"行"（类比 main_claw_loop 的 frame rate）
    row_hz = 100
    step = max(1, BOARD_RAW_SAMPLE_HZ // row_hz)
    n_rows = target.size // step
    target_rms_row = np.zeros(n_rows, dtype=np.float32)
    comp_rms_row = np.zeros(n_rows, dtype=np.float32)
    mdf_row = np.zeros(n_rows, dtype=np.float32)
    mnf_row = np.zeros(n_rows, dtype=np.float32)
    zcr_row = np.zeros(n_rows, dtype=np.float32)
    raw_row = np.zeros(n_rows, dtype=np.float32)
    for i in range(n_rows):
        lo = i * step
        hi = lo + step
        t_seg = target[lo:hi]
        c_seg = comp[lo:hi]
        target_rms_row[i] = float(np.sqrt(np.mean(t_seg.astype(np.float64) ** 2)))
        comp_rms_row[i] = float(np.sqrt(np.mean(c_seg.astype(np.float64) ** 2)))
        mdf, mnf, zcr, raw_rms = _compute_spectral_from_raw(
            target[max(0, lo - 100):hi + 100], BOARD_RAW_SAMPLE_HZ,
        )
        mdf_row[i] = mdf
        mnf_row[i] = mnf
        zcr_row[i] = zcr
        raw_row[i] = raw_rms

    # Angle/Ang_Vel/Ang_Accel/Phase/Sym 本脚本无视觉数据 → 写 0；下游训练会跳过
    ts_row = np.arange(n_rows, dtype=np.float64) / row_hz + time.time()
    df = pd.DataFrame({
        "Timestamp": ts_row,
        "Ang_Vel": np.zeros(n_rows, dtype=np.float32),
        "Angle": np.zeros(n_rows, dtype=np.float32),
        "Ang_Accel": np.zeros(n_rows, dtype=np.float32),
        "Target_RMS_Norm": target_rms_row / mvc0,
        "Comp_RMS_Norm": comp_rms_row / mvc1,
        "Symmetry_Score": np.ones(n_rows, dtype=np.float32),
        "Phase_Progress": np.linspace(0.0, 1.0, n_rows, dtype=np.float32),
        "Target_MDF": mdf_row / NORM_MDF,
        "Target_MNF": mnf_row / NORM_MNF,
        "Target_ZCR": zcr_row / NORM_ZCR,
        "Target_Raw_Unfilt": raw_row / NORM_RAW_UNFILT,
        "label": [LABEL_INT[args.label]] * n_rows,
    })

    # 无 Angle 真值 → 按等长窗口切（每 2s 一 rep），非严格 rep 边界
    win_rows = int(row_hz * 2.0)
    reps = []
    for s in range(0, n_rows - 100, win_rows):
        reps.append((s, min(n_rows - 1, s + win_rows)))
    print("[INFO] 等长窗口切分 -> %d 个 rep" % len(reps))

    n_written = 0
    for i, (s, e) in enumerate(reps, start=1):
        rep = df.iloc[s:e + 1]
        if len(rep) < 50:
            continue
        rep_path = os.path.join(out_dir, "rep_%03d.csv" % (n_written + 1))
        rep.to_csv(rep_path, index=False, float_format="%.4f")
        n_written += 1
    print("[DONE] 写出 %d 个 rep 到 %s" % (n_written, out_dir))
    print("[WARN] 本脚本无视觉源，Angle/Ang_Vel/Ang_Accel=0；主路径请用 upgrade_collect_7d_to_11d.py")


def _parse_args():
    p = argparse.ArgumentParser(description="V4.3 60s CLI 兜底采集")
    p.add_argument("--user", required=True)
    p.add_argument("--exercise", required=True, choices=["curl", "squat"])
    p.add_argument("--label", required=True,
                   choices=["standard", "compensation", "bad_form"])
    p.add_argument("--duration", type=int, default=60)
    p.add_argument("--board", default=DEFAULT_BOARD)
    return p.parse_args()


def main():
    args = _parse_args()
    run(args)


if __name__ == "__main__":
    main()
