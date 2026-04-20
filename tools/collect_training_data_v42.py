# coding=utf-8
#!/usr/bin/env python3
"""
IronBuddy V4.2 — Training Data Collector (11D + 3-class labels)
================================================================
实时采集 11D 特征 CSV，用于 V4.2 DualBranchFusionModel 训练。

参考：
- 计划 `.claude/plans/home-qq-projects-embedded-fullstack-ind-velvet-ember.md` §2.3/§2.2/§4.4/§3.1
- 执行细节 `.claude/plans/distributed-puzzling-wilkinson.md` Agent-A 部分

目录协议：`data/v42/<user>/<exercise>/<label>/rep_NNN.csv`
13 列 header：
    Timestamp, Ang_Vel, Angle, Ang_Accel,
    Target_RMS_Norm, Comp_RMS_Norm, Symmetry_Score, Phase_Progress,
    Target_MDF, Target_MNF, Target_ZCR, Target_Raw_Unfilt,
    label

启动前必须通过 MVC 校准闸门：`data/v42/<user>/mvc_calibration.json`
    {"protocol": "SENIAM-2000", "peak_mvc": {"ch0": <float>, "ch1": <float>}, ...}

Usage
-----
    python tools/collect_training_data_v42.py \\
        --user user_01 --exercise curl --label standard --reps 15

    # 开发机无硬件：
    python tools/collect_training_data_v42.py \\
        --user user_01 --exercise curl --label standard --reps 3 \\
        --manual --synthetic --skip-mvc-check

Interactive controls (raw TTY)
    s  — 开始 / 继续
    p  — 暂停
    q  — 结束
    space — 手动模式下：标记一个 rep 结束（同时启动下一个）

黄金代码保护：
    - 本脚本新增，不修改旧 tools/collect_training_data.py
    - 不 import hardware_engine，保证开发机独立跑
"""

from __future__ import absolute_import, division, print_function

import argparse
import csv
import json
import math
import os
import select
import sys
import termios
import time
import tty
from datetime import datetime

import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
POSE_SHM = "/dev/shm/pose_data.json"
EMG_SHM = "/dev/shm/muscle_activation.json"
EMG_SPECTRAL_SHM = "/dev/shm/emg_spectral.json"
FSM_SHM = "/dev/shm/fsm_state.json"

# 归一化常数（计划 §2.3 / distributed-puzzling-wilkinson.md）
NORM_MDF = 100.0    # Hz
NORM_MNF = 150.0    # Hz
NORM_ZCR = 400.0
NORM_RAW_UNFILT = 2048.0  # 12-bit ADC

# 质量阈值（沿用旧脚本阈值；板上 NPU 置信度低，用 0.05）
MIN_ANGLE_DEG = 20.0
MAX_ANGLE_DEG = 175.0
MIN_KPT_CONF = 0.05

# CSV 13 列 header（计划数据契约）
CSV_HEADER = [
    "Timestamp",
    "Ang_Vel", "Angle", "Ang_Accel",
    "Target_RMS_Norm", "Comp_RMS_Norm", "Symmetry_Score", "Phase_Progress",
    "Target_MDF", "Target_MNF", "Target_ZCR", "Target_Raw_Unfilt",
    "label",
]

LABEL_INT = {"standard": 0, "compensation": 1, "bad_form": 2}


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------
class _RawTerm(object):
    """raw-mode TTY context manager（从 collect_training_data.py 复用模式）。"""

    def __enter__(self):
        self.fd = sys.stdin.fileno()
        try:
            self.old = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
            self.active = True
        except (termios.error, OSError):
            # 非交互式 stdin（管道 / ssh 无 tty），降级为 no-op
            self.old = None
            self.active = False
        return self

    def __exit__(self, *_):
        if self.active and self.old is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


def _try_read_key():
    """非阻塞读一个字符；无输入返回 None。"""
    try:
        if select.select([sys.stdin], [], [], 0)[0]:
            return sys.stdin.read(1)
    except (OSError, ValueError):
        pass
    return None


# ---------------------------------------------------------------------------
# Geometry helpers（独立于 main_claw_loop.py，按三点法重抄）
# ---------------------------------------------------------------------------
def _angle_3pts(a, b, c):
    """三点夹角（度）。a/b/c 为 (x, y)。"""
    bax = a[0] - b[0]
    bay = a[1] - b[1]
    bcx = c[0] - b[0]
    bcy = c[1] - b[1]
    dot = bax * bcx + bay * bcy
    mag = math.sqrt(bax * bax + bay * bay) * math.sqrt(bcx * bcx + bcy * bcy)
    if mag < 1e-9:
        return 180.0
    cos_a = max(-1.0, min(1.0, dot / mag))
    return math.degrees(math.acos(cos_a))


def _symmetry_score(kpts):
    """左右侧置信度对称性 ∈ [0, 1]。"""
    if len(kpts) < 17:
        return 1.0
    left = sum(kpts[i][2] for i in (11, 13, 15))
    right = sum(kpts[i][2] for i in (12, 14, 16))
    total = left + right
    if total < 1e-6:
        return 1.0
    return 1.0 - abs(left - right) / total


def _extract_joint_angle(pose_data, exercise):
    """
    从 pose_data 提取目标关节角度 + 对称性 + 置信度。

    exercise='curl' -> shoulder-elbow-wrist
    exercise='squat' -> hip-knee-ankle
    返回 (angle_deg, confidence, symmetry) 或 (None, 0.0, 1.0) 如果无人/置信度太低。
    """
    objects = pose_data.get("objects", [])
    if not objects:
        return None, 0.0, 1.0
    obj = objects[0]
    score = obj.get("score", 0.0)
    kpts = obj.get("kpts", [])
    if len(kpts) < 17:
        return None, score, 1.0

    if exercise == "curl":
        l_c = kpts[5][2] + kpts[7][2] + kpts[9][2]
        r_c = kpts[6][2] + kpts[8][2] + kpts[10][2]
        if l_c > r_c:
            a, b, c = kpts[5], kpts[7], kpts[9]
            trio_conf = l_c / 3.0
        else:
            a, b, c = kpts[6], kpts[8], kpts[10]
            trio_conf = r_c / 3.0
    else:  # squat
        l_c = kpts[11][2] + kpts[13][2] + kpts[15][2]
        r_c = kpts[12][2] + kpts[14][2] + kpts[16][2]
        if l_c > r_c:
            a, b, c = kpts[11], kpts[13], kpts[15]
            trio_conf = l_c / 3.0
        else:
            a, b, c = kpts[12], kpts[14], kpts[16]
            trio_conf = r_c / 3.0

    if trio_conf < MIN_KPT_CONF:
        return None, trio_conf, 1.0

    angle = _angle_3pts(a[:2], b[:2], c[:2])
    sym = _symmetry_score(kpts)
    return angle, trio_conf, sym


# ---------------------------------------------------------------------------
# Shared-mem readers（带容错）
# ---------------------------------------------------------------------------
def _read_json_safe(path):
    try:
        with open(path, "r") as f:
            return json.load(f), True
    except (OSError, IOError, ValueError):
        return None, False


# ---------------------------------------------------------------------------
# MVC / anthropometry gate
# ---------------------------------------------------------------------------
def load_mvc_or_fail(user_root, skip):
    """读 mvc_calibration.json，校验协议 + 峰值。skip=True 跳过并返回默认值。"""
    mvc_path = os.path.join(user_root, "mvc_calibration.json")
    if skip:
        print("[WARN] --skip-mvc-check: 使用 fallback peak_mvc={ch0:800, ch1:500}",
              file=sys.stderr)
        return {"ch0": 800.0, "ch1": 500.0}, mvc_path
    if not os.path.isfile(mvc_path):
        print("[ERROR] MVC 校准文件不存在: {0}".format(mvc_path), file=sys.stderr)
        print("        请先完成 SENIAM-2000 校准后再采集。", file=sys.stderr)
        sys.exit(2)
    try:
        with open(mvc_path, "r") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print("[ERROR] 读取 MVC 校准失败: {0}".format(e), file=sys.stderr)
        sys.exit(2)
    protocol = data.get("protocol")
    if protocol != "SENIAM-2000":
        print("[ERROR] MVC protocol != 'SENIAM-2000' (得到 {0})".format(protocol),
              file=sys.stderr)
        sys.exit(2)
    peak = data.get("peak_mvc") or {}
    ch0 = peak.get("ch0")
    ch1 = peak.get("ch1")
    if ch0 is None or ch1 is None or ch0 <= 0 or ch1 <= 0:
        print("[ERROR] peak_mvc.ch0/ch1 缺失或非正: {0}".format(peak), file=sys.stderr)
        sys.exit(2)
    return {"ch0": float(ch0), "ch1": float(ch1)}, mvc_path


# ---------------------------------------------------------------------------
# Synthetic mock frame（开发机冒烟用，--synthetic flag 显式启用）
# ---------------------------------------------------------------------------
class SyntheticSource(object):
    """无硬件时用 sin 合成 pose + EMG + spectral，供 --manual --synthetic 开发机跑。"""

    def __init__(self, exercise):
        self.exercise = exercise
        self.t0 = time.monotonic()

    def frame(self):
        t = time.monotonic() - self.t0
        # 1.5 秒一个周期的 rep 样波形
        phase = (t % 1.5) / 1.5
        if self.exercise == "curl":
            angle = 170.0 - 125.0 * math.sin(math.pi * phase)  # 170 -> 45 -> 170
        else:
            angle = 170.0 - 100.0 * math.sin(math.pi * phase)  # 170 -> 70 -> 170
        conf = 0.85
        sym = 0.9 + 0.05 * math.sin(2 * math.pi * t)
        emg_target = 600.0 * (0.3 + 0.7 * math.sin(math.pi * phase) ** 2)
        emg_comp = 150.0 * (0.2 + 0.3 * math.sin(math.pi * phase + 0.4) ** 2)
        mdf = 85.0 + 10.0 * math.sin(0.5 * t)
        mnf = 110.0 + 12.0 * math.sin(0.5 * t + 0.3)
        zcr = 260.0 + 40.0 * math.sin(0.7 * t)
        raw = 1200.0 + 400.0 * math.sin(2 * math.pi * 6 * t)
        return dict(
            angle=angle, conf=conf, sym=sym,
            emg_target=emg_target, emg_comp=emg_comp,
            mdf=mdf, mnf=mnf, zcr=zcr, raw=raw,
            phase=phase,
        )


# ---------------------------------------------------------------------------
# Core collector
# ---------------------------------------------------------------------------
class V42Collector(object):
    def __init__(self, args):
        self.args = args
        self.user = args.user
        self.exercise = args.exercise
        self.label_str = args.label
        self.label_int = LABEL_INT[self.label_str]
        self.reps_target = args.reps
        self.poll_hz = max(1, int(args.poll_hz))
        self.poll_interval = 1.0 / self.poll_hz

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.user_root = os.path.join(project_root, "data", "v42", self.user)
        self.out_dir = os.path.join(self.user_root, self.exercise, self.label_str)
        if not os.path.isdir(self.out_dir):
            os.makedirs(self.out_dir)
        self.session_log = os.path.join(self.user_root, self.exercise, "session.log")
        if not os.path.isdir(os.path.dirname(self.session_log)):
            os.makedirs(os.path.dirname(self.session_log))

        # MVC 闸门
        self.peak_mvc, mvc_path = load_mvc_or_fail(self.user_root, args.skip_mvc_check)
        print("[INFO] MVC OK: ch0={0:.1f}, ch1={1:.1f}  ({2})".format(
            self.peak_mvc["ch0"], self.peak_mvc["ch1"], mvc_path))

        # 状态
        self.recording = False
        self.rep_count = 0
        self.rep_buffer = []           # 本 rep 累积行
        self.t_rep_start = None
        self._prev_angle = 180.0
        self._prev_ang_vel = 0.0
        self._angle_history = []
        self._fsm_state_last = None    # 监听 state 变化做切分
        self._spectral_warned = False

        # synthetic fallback
        self.synthetic = None
        if args.synthetic:
            self.synthetic = SyntheticSource(self.exercise)
            print("[WARN] synthetic 模式启用：忽略 /dev/shm，用 sin 合成数据。")

    # ------------------------------------------------------------------
    def _sanitize_denominator(self, ch):
        v = self.peak_mvc.get(ch, 0.0)
        return v if v > 1e-6 else 1.0

    def _phase_progress(self, angle):
        """按角度区间估计 0~1 相位。最小 5 帧历史后启用。"""
        if len(self._angle_history) < 5:
            return 0.0
        a_min = min(self._angle_history)
        a_max = max(self._angle_history)
        span = max(a_max - a_min, 1.0)
        prog = 1.0 - (angle - a_min) / span
        if prog < 0.0:
            return 0.0
        if prog > 1.0:
            return 1.0
        return float(prog)

    def _read_once(self):
        """
        一次轮询：返回 dict 或 None（帧被质量过滤）。
        synthetic 优先；否则从 /dev/shm 读。
        """
        if self.synthetic is not None:
            s = self.synthetic.frame()
            angle = s["angle"]
            if angle < MIN_ANGLE_DEG or angle > MAX_ANGLE_DEG:
                return None
            return dict(
                angle=angle, conf=s["conf"], sym=s["sym"],
                emg_target=s["emg_target"], emg_comp=s["emg_comp"],
                mdf=s["mdf"], mnf=s["mnf"], zcr=s["zcr"], raw=s["raw"],
                phase_hint=s["phase"],
            )

        pose_data, ok = _read_json_safe(POSE_SHM)
        if not ok:
            return None
        emg_data, emg_ok = _read_json_safe(EMG_SHM)
        if not emg_ok:
            emg_data = {}
        spec_data, spec_ok = _read_json_safe(EMG_SPECTRAL_SHM)
        if not spec_ok:
            if not self._spectral_warned:
                print("\n[WARN] emg_spectral.json 不存在，MDF/MNF/ZCR/Raw 填 0。")
                self._spectral_warned = True
            spec_data = {}

        angle_info = _extract_joint_angle(pose_data, self.exercise)
        if angle_info[0] is None:
            return None
        angle, conf, sym = angle_info
        if angle < MIN_ANGLE_DEG or angle > MAX_ANGLE_DEG:
            return None

        acts = emg_data.get("activations", {}) if isinstance(emg_data, dict) else {}
        if self.exercise == "curl":
            emg_target = float(acts.get("biceps", 0.0))
            emg_comp = float(acts.get("glutes", 0.0))
        else:
            emg_target = float(acts.get("glutes", 0.0))
            emg_comp = float(acts.get("biceps", 0.0))

        mdf = float(spec_data.get("target_mdf", 0.0))
        mnf = float(spec_data.get("target_mnf", 0.0))
        zcr = float(spec_data.get("target_zcr", 0.0))
        raw = float(spec_data.get("target_raw_unfilt", 0.0))

        return dict(
            angle=angle, conf=conf, sym=sym,
            emg_target=emg_target, emg_comp=emg_comp,
            mdf=mdf, mnf=mnf, zcr=zcr, raw=raw,
            phase_hint=None,
        )

    def _check_fsm_rep_boundary(self):
        """监听 fsm_state.json.state：非 ascending → ascending 触发 rep 结束。"""
        if self.synthetic is not None:
            return False
        fsm, ok = _read_json_safe(FSM_SHM)
        if not ok or not isinstance(fsm, dict):
            return False
        state = fsm.get("state")
        boundary = (self._fsm_state_last is not None
                    and self._fsm_state_last != "ascending"
                    and state == "ascending")
        self._fsm_state_last = state
        return boundary

    def _append_row(self, frame):
        """写一行到 rep_buffer。frame 来自 _read_once。"""
        angle = frame["angle"]
        self._angle_history.append(angle)
        if len(self._angle_history) > 300:
            self._angle_history.pop(0)
        ang_vel = angle - self._prev_angle
        ang_accel = ang_vel - self._prev_ang_vel
        self._prev_angle = angle
        self._prev_ang_vel = ang_vel

        target_norm = frame["emg_target"] / self._sanitize_denominator("ch0")
        comp_norm = frame["emg_comp"] / self._sanitize_denominator("ch1")

        if frame["phase_hint"] is not None:
            phase = float(max(0.0, min(1.0, frame["phase_hint"])))
        else:
            phase = self._phase_progress(angle)

        row = [
            "{0:.3f}".format(time.time()),
            "{0:.4f}".format(ang_vel),
            "{0:.4f}".format(angle),
            "{0:.4f}".format(ang_accel),
            "{0:.4f}".format(target_norm),
            "{0:.4f}".format(comp_norm),
            "{0:.4f}".format(frame["sym"]),
            "{0:.4f}".format(phase),
            "{0:.4f}".format(frame["mdf"] / NORM_MDF),
            "{0:.4f}".format(frame["mnf"] / NORM_MNF),
            "{0:.4f}".format(frame["zcr"] / NORM_ZCR),
            "{0:.4f}".format(frame["raw"] / NORM_RAW_UNFILT),
            str(self.label_int),
        ]
        self.rep_buffer.append(row)
        if self.t_rep_start is None:
            self.t_rep_start = time.monotonic()

    def _flush_rep(self):
        """把当前 rep_buffer 写 CSV，重置 buffer，递增计数。"""
        if not self.rep_buffer:
            return
        rows = len(self.rep_buffer)
        # 行数合理性：< 50 丢弃（太短），> 500 截断
        if rows < 50:
            print("\n[WARN] rep 行数 {0} < 50，丢弃。".format(rows))
            self.rep_buffer = []
            self.t_rep_start = None
            return
        if rows > 500:
            self.rep_buffer = self.rep_buffer[:500]
            rows = 500

        self.rep_count += 1
        rep_name = "rep_{0:03d}.csv".format(self.rep_count)
        out_path = os.path.join(self.out_dir, rep_name)
        tmp_path = out_path + ".tmp"
        with open(tmp_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            w.writerows(self.rep_buffer)
        os.rename(tmp_path, out_path)  # 原子写

        duration = 0.0
        if self.t_rep_start is not None:
            duration = time.monotonic() - self.t_rep_start

        ts_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        log_line = "{0}\t{1}\t{2}\t{3:.2f}s\t{4}rows\t{5}\n".format(
            ts_iso, self.exercise, self.label_str, duration, rows, rep_name
        )
        with open(self.session_log, "a") as f:
            f.write(log_line)

        print("\n[OK] rep_{0:03d} 已保存  ({1} 行, {2:.2f}s)  {3}/{4}".format(
            self.rep_count, rows, duration, self.rep_count, self.reps_target
        ))
        self.rep_buffer = []
        self.t_rep_start = None

    # ------------------------------------------------------------------
    def run(self):
        print("IronBuddy V4.2 Collector")
        print("  user     : {0}".format(self.user))
        print("  exercise : {0}".format(self.exercise))
        print("  label    : {0}  (int={1})".format(self.label_str, self.label_int))
        print("  reps     : {0}".format(self.reps_target))
        print("  out_dir  : {0}".format(self.out_dir))
        print("  poll_hz  : {0}".format(self.poll_hz))
        print("  mode     : {0}  {1}".format(
            "MANUAL" if self.args.manual else "FSM",
            "SYNTH" if self.synthetic else "LIVE",
        ))
        print("\nControls: [s] start  [p] pause  [q] quit"
              + ("  [space] rep-end" if self.args.manual else ""))
        print("")

        with _RawTerm():
            try:
                while True:
                    t0 = time.monotonic()

                    key = _try_read_key()
                    if key == 's':
                        self.recording = True
                        print("\n[INFO] 开始记录。")
                    elif key == 'p':
                        self.recording = False
                        print("\n[INFO] 暂停。")
                    elif key == 'q':
                        print("\n[INFO] 主动退出。")
                        break
                    elif key == ' ' and self.args.manual and self.recording:
                        self._flush_rep()
                        if self.rep_count >= self.reps_target:
                            print("\n[INFO] 达到目标 rep 数。")
                            break

                    frame = self._read_once()
                    if frame is None:
                        self._status("  skip(no-frame)")
                        time.sleep(self.poll_interval)
                        continue

                    if self.recording:
                        self._append_row(frame)

                        # FSM 模式：监听状态跳变
                        if not self.args.manual:
                            if self._check_fsm_rep_boundary():
                                self._flush_rep()
                                if self.rep_count >= self.reps_target:
                                    print("\n[INFO] 达到目标 rep 数。")
                                    break

                    self._status("  angle={0:.1f}  buf={1}".format(
                        frame["angle"], len(self.rep_buffer)
                    ))
                    elapsed = time.monotonic() - t0
                    time.sleep(max(0.0, self.poll_interval - elapsed))
            except KeyboardInterrupt:
                print("\n[INFO] Ctrl+C 中断。")

        # 退出前若还有 buffer 且足够长，做最后一次 flush
        if len(self.rep_buffer) >= 50:
            self._flush_rep()

        print("\n[DONE] 共 {0} rep  -> {1}".format(self.rep_count, self.out_dir))
        print("[LOG]  session log: {0}".format(self.session_log))

    def _status(self, extra):
        rec = "[REC]" if self.recording else "[---]"
        sys.stdout.write("\r{0} rep={1}/{2} {3}   ".format(
            rec, self.rep_count, self.reps_target, extra
        ))
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _parse_args():
    p = argparse.ArgumentParser(
        description="IronBuddy V4.2 训练数据采集器（11D + 3 类标签）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--user", required=True,
                   help="用户标识，如 user_01（对应 data/v42/<user>/）")
    p.add_argument("--exercise", required=True, choices=["curl", "squat"],
                   help="动作类型")
    p.add_argument("--label", required=True,
                   choices=["standard", "compensation", "bad_form"],
                   help="动作质量标签")
    p.add_argument("--reps", type=int, default=15,
                   help="本次目标 rep 数（默认 15）")
    p.add_argument("--poll-hz", type=int, default=200,
                   help="采样轮询频率（默认 200 Hz）")
    p.add_argument("--manual", action="store_true",
                   help="手动模式：空格键标记 rep 结束（开发机 / FSM 不可用时）")
    p.add_argument("--synthetic", action="store_true",
                   help="开发机冒烟：忽略 /dev/shm，用 sin 合成数据（必须 --manual）")
    p.add_argument("--skip-mvc-check", action="store_true",
                   help="绕过 MVC 闸门（仅 smoke test 用，会 warn）")
    return p.parse_args()


def main():
    args = _parse_args()
    if args.synthetic and not args.manual:
        print("[ERROR] --synthetic 必须同时指定 --manual", file=sys.stderr)
        sys.exit(2)
    collector = V42Collector(args)
    collector.run()


if __name__ == "__main__":
    main()
