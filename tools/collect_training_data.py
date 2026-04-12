#!/usr/bin/env python3
"""
IronBuddy — Training Data Collector
=====================================
Real-time CLI tool for collecting labeled training data from the live
hardware pipeline.  Reads pose keypoints and EMG activations from shared
memory, computes the full 7-D feature vector, validates data quality,
and writes labeled CSV files ready for model training.

Usage
-----
    python collect_training_data.py --mode golden
    python collect_training_data.py --mode lazy   --out /data/training
    python collect_training_data.py --mode bad    --out /data/training

Interactive controls (non-blocking, reads stdin)
    s  — start / resume recording
    p  — pause recording
    q  — quit and save
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import termios
import time
import tty
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
POSE_SHM        = "/dev/shm/pose_data.json"
EMG_SHM         = "/dev/shm/muscle_activation.json"
RECORD_MODE_SHM = "/dev/shm/record_mode"

POLL_HZ          = 20          # frames per second to poll
POLL_INTERVAL    = 1.0 / POLL_HZ

# Validity thresholds
MAX_TIMESTAMP_DIFF_S = 0.100  # 100 ms coherence window
MIN_POSE_SCORE       = 0.10
MIN_ANGLE_RANGE_DEG  = 20.0   # min angle excursion within the window
WINDOW_SIZE_FOR_RANGE = 30    # look-back window for amplitude check

CSV_HEADER = [
    "Timestamp",
    "Ang_Vel", "Angle", "Ang_Accel",
    "Target_RMS", "Comp_RMS",
    "Symmetry_Score", "Phase_Progress",
    "pose_score", "label",
]

# ---------------------------------------------------------------------------
# Terminal helpers (raw single-key read without Enter)
# ---------------------------------------------------------------------------

class _RawTerm:
    """Context manager: puts stdin into raw mode for single-char reads."""

    def __enter__(self):
        self.fd   = sys.stdin.fileno()
        self.old  = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        return self

    def __exit__(self, *_):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


def _try_read_key() -> Optional[str]:
    """Non-blocking single-char read; returns None if no key pressed."""
    import select
    if select.select([sys.stdin], [], [], 0)[0]:
        return sys.stdin.read(1)
    return None


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _angle_3pts(a, b, c) -> float:
    """Knee angle from three (x, y) keypoints."""
    ba = [a[0] - b[0], a[1] - b[1]]
    bc = [c[0] - b[0], c[1] - b[1]]
    dot  = ba[0]*bc[0] + ba[1]*bc[1]
    mag  = (ba[0]**2 + ba[1]**2)**0.5 * (bc[0]**2 + bc[1]**2)**0.5
    if mag < 1e-9:
        return 180.0
    cos_a = max(-1.0, min(1.0, dot / mag))
    import math
    return math.degrees(math.acos(cos_a))


def _symmetry_score(kpts: list) -> float:
    """
    Rough left/right symmetry: ratio of confidence-weighted keypoint
    visibility between left and right sides.  Returns value in [0, 1].
    """
    if len(kpts) < 17:
        return 1.0
    left_conf  = sum(kpts[i][2] for i in [11, 13, 15])  # hip, knee, ankle L
    right_conf = sum(kpts[i][2] for i in [12, 14, 16])  # hip, knee, ankle R
    total      = left_conf + right_conf
    if total < 1e-6:
        return 1.0
    return 1.0 - abs(left_conf - right_conf) / total


def _extract_angle_and_pose_score(pose_data: dict, exercise: str = "squat"):
    """
    Returns (angle_deg, pose_score, symmetry) or (None, 0.0) if person not detected.
    Supports squat (knee angle) and bicep_curl (elbow angle).
    """
    objects = pose_data.get("objects", [])
    if not objects:
        return None, 0.0

    obj   = objects[0]
    score = obj.get("score", 0.0)
    if score < MIN_POSE_SCORE:
        return None, score

    kpts = obj.get("kpts", [])
    if len(kpts) < 17:
        return None, score

    if exercise == "bicep_curl":
        # Elbow angle: shoulder(5/6) - elbow(7/8) - wrist(9/10)
        l_score = kpts[5][2] + kpts[7][2] + kpts[9][2]
        r_score = kpts[6][2] + kpts[8][2] + kpts[10][2]
        if l_score > r_score:
            a, b, c = kpts[5], kpts[7], kpts[9]
        else:
            a, b, c = kpts[6], kpts[8], kpts[10]
    else:
        # Knee angle: hip(11/12) - knee(13/14) - ankle(15/16)
        l_score = kpts[11][2] + kpts[13][2] + kpts[15][2]
        r_score = kpts[12][2] + kpts[14][2] + kpts[16][2]
        if l_score > r_score:
            a, b, c = kpts[11], kpts[13], kpts[15]
        else:
            a, b, c = kpts[12], kpts[14], kpts[16]

    angle = _angle_3pts(a[:2], b[:2], c[:2])
    sym   = _symmetry_score(kpts)
    return angle, score, sym


def _extract_emg(emg_data: dict, exercise: str = "squat"):
    """Returns (target_rms, comp_rms). Target muscle depends on exercise."""
    acts = emg_data.get("activations", {})
    if exercise == "bicep_curl":
        return acts.get("biceps", 0.0), acts.get("glutes", 0.0)
    return acts.get("glutes", 0.0), acts.get("biceps", 0.0)


# ---------------------------------------------------------------------------
# Validity checks
# ---------------------------------------------------------------------------

def _temporal_coherent(pose_ts: float, emg_ts: float) -> bool:
    return abs(pose_ts - emg_ts) <= MAX_TIMESTAMP_DIFF_S


def _amplitude_ok(angle_history: list) -> bool:
    if len(angle_history) < WINDOW_SIZE_FOR_RANGE:
        return True   # not enough data yet — don't block early frames
    window = angle_history[-WINDOW_SIZE_FOR_RANGE:]
    return (max(window) - min(window)) >= MIN_ANGLE_RANGE_DEG


# ---------------------------------------------------------------------------
# Main collector
# ---------------------------------------------------------------------------

class DataCollector:
    def __init__(self, mode: str, out_dir: str, exercise: str = "squat"):
        self.mode     = mode
        self.exercise = exercise
        self.out_dir  = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        ts_str   = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.out_path = self.out_dir / f"train_{exercise}_{mode}_{ts_str}.csv"

        self.rows: list[list] = []

        self._angle_history: list[float] = []
        self._prev_ang_vel:  float       = 0.0
        self._prev_angle:    float       = 180.0

        self.recording = False
        self.dropped   = 0
        self.accepted  = 0

    # ------------------------------------------------------------------
    def _compute_phase_progress(self, angle: float) -> float:
        """Estimate normalized phase progress (0=standing, 1=bottom)."""
        if len(self._angle_history) < 5:
            return 0.0
        a_min = min(self._angle_history)
        a_max = max(self._angle_history)
        span  = max(a_max - a_min, 1.0)
        return float(np.clip(1.0 - (angle - a_min) / span, 0.0, 1.0))

    # ------------------------------------------------------------------
    def _set_shm_mode(self):
        """Signal main loop that we are recording in this mode."""
        try:
            with open(RECORD_MODE_SHM, "w") as f:
                f.write(self.mode)
        except OSError:
            pass

    def _clear_shm_mode(self):
        try:
            os.remove(RECORD_MODE_SHM)
        except OSError:
            pass

    # ------------------------------------------------------------------
    def _read_shm(self):
        """
        Returns (pose_data, emg_data, pose_ts, emg_ts) or raises on failure.
        """
        pose_ts = emg_ts = 0.0

        try:
            pose_ts = os.path.getmtime(POSE_SHM)
            with open(POSE_SHM, "r") as f:
                pose_data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise RuntimeError(f"pose_data read error: {e}")

        try:
            emg_ts = os.path.getmtime(EMG_SHM)
            with open(EMG_SHM, "r") as f:
                emg_data = json.load(f)
        except (OSError, json.JSONDecodeError):
            emg_data = {}
            emg_ts   = pose_ts  # treat as coherent if EMG absent

        return pose_data, emg_data, pose_ts, emg_ts

    # ------------------------------------------------------------------
    def _print_status(self, validity_msg: str = ""):
        state_str = "[RECORDING]" if self.recording else "[ PAUSED  ]"
        print(
            f"\r{state_str} mode={self.mode:10s} "
            f"accepted={self.accepted:5d} dropped={self.dropped:5d}  "
            f"{validity_msg:<40s}",
            end="",
            flush=True,
        )

    # ------------------------------------------------------------------
    def run(self):
        print(f"\nIronBuddy Data Collector")
        print(f"  Exercise: {self.exercise}")
        print(f"  Mode    : {self.mode}")
        print(f"  Output  : {self.out_path}")
        print(f"\nControls:  [s] start/resume   [p] pause   [q] quit & save\n")

        self._clear_shm_mode()

        with _RawTerm():
            try:
                while True:
                    t0 = time.monotonic()

                    # --- key input ---
                    key = _try_read_key()
                    if key == 's':
                        self.recording = True
                        self._set_shm_mode()
                        print(f"\n[INFO] Recording started.")
                    elif key == 'p':
                        self.recording = False
                        self._clear_shm_mode()
                        print(f"\n[INFO] Paused.")
                    elif key == 'q':
                        print(f"\n[INFO] Quitting...")
                        break

                    # --- read sensors ---
                    try:
                        pose_data, emg_data, pose_ts, emg_ts = self._read_shm()
                    except RuntimeError as e:
                        self._print_status(f"SKIP: {e}")
                        time.sleep(POLL_INTERVAL)
                        continue

                    # --- validity checks ---
                    validity_msg = ""

                    # Temporal coherence
                    if not _temporal_coherent(pose_ts, emg_ts):
                        validity_msg = f"SKIP: ts_diff={abs(pose_ts-emg_ts)*1000:.0f}ms"
                        self.dropped += 1
                        self._print_status(validity_msg)
                        time.sleep(POLL_INTERVAL)
                        continue

                    # Person detection & angle
                    result = _extract_angle_and_pose_score(pose_data, self.exercise)
                    if result[0] is None:
                        score = result[1]
                        validity_msg = f"SKIP: no_person (score={score:.2f})"
                        self.dropped += 1
                        self._print_status(validity_msg)
                        time.sleep(POLL_INTERVAL)
                        continue

                    angle, pose_score, sym = result

                    self._angle_history.append(angle)
                    if len(self._angle_history) > 120:
                        self._angle_history.pop(0)

                    # Amplitude check
                    if not _amplitude_ok(self._angle_history):
                        validity_msg = (
                            f"LOW-AMP {max(self._angle_history[-30:], default=0):.0f}-"
                            f"{min(self._angle_history[-30:], default=0):.0f}deg"
                        )
                        # don't drop — still show user, just don't record yet
                        self._print_status(validity_msg)
                        time.sleep(POLL_INTERVAL)
                        continue

                    # --- feature computation ---
                    target_rms, comp_rms = _extract_emg(emg_data, self.exercise)

                    ang_vel   = angle - self._prev_angle
                    ang_accel = ang_vel - self._prev_ang_vel
                    self._prev_ang_vel = ang_vel
                    self._prev_angle   = angle

                    phase_prog = self._compute_phase_progress(angle)

                    now = time.time()
                    self._print_status(
                        f"angle={angle:.1f} vel={ang_vel:.2f} sym={sym:.2f} "
                        f"phase={phase_prog:.2f}"
                    )

                    if self.recording:
                        row = [
                            f"{now:.3f}",
                            f"{ang_vel:.4f}",
                            f"{angle:.4f}",
                            f"{ang_accel:.4f}",
                            f"{target_rms:.4f}",
                            f"{comp_rms:.4f}",
                            f"{sym:.4f}",
                            f"{phase_prog:.4f}",
                            f"{pose_score:.4f}",
                            self.mode,
                        ]
                        self.rows.append(row)
                        self.accepted += 1

                    # --- rate limit ---
                    elapsed = time.monotonic() - t0
                    sleep_t = max(0.0, POLL_INTERVAL - elapsed)
                    time.sleep(sleep_t)

            except KeyboardInterrupt:
                print("\n[INFO] Interrupted.")

        self._clear_shm_mode()
        self._save()

    # ------------------------------------------------------------------
    def _save(self):
        if not self.rows:
            print("[WARN] No data collected — nothing to save.")
            return

        with open(self.out_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_HEADER)
            writer.writerows(self.rows)

        print(f"[OK] Saved {len(self.rows)} frames to {self.out_path}")

    # ------------------------------------------------------------------
    def run_auto(self, duration_sec):
        """Non-interactive auto-record mode. No TTY required."""
        print(f"\nIronBuddy Data Collector (AUTO MODE)")
        print(f"  Exercise : {self.exercise}")
        print(f"  Mode     : {self.mode}")
        print(f"  Duration : {duration_sec}s")
        print(f"  Output   : {self.out_path}")

        self._clear_shm_mode()
        self.recording = True
        self._set_shm_mode()

        t_start = time.monotonic()
        print(f"\n[AUTO] Recording started...")

        try:
            while time.monotonic() - t_start < duration_sec:
                try:
                    pose_data, emg_data, pose_ts, emg_ts = self._read_shm()
                except RuntimeError:
                    time.sleep(POLL_INTERVAL)
                    continue

                if not _temporal_coherent(pose_ts, emg_ts):
                    self.dropped += 1
                    time.sleep(POLL_INTERVAL)
                    continue

                result = _extract_angle_and_pose_score(pose_data, self.exercise)
                if result[0] is None:
                    self.dropped += 1
                    time.sleep(POLL_INTERVAL)
                    continue

                angle, pose_score, sym = result
                self._angle_history.append(angle)
                if len(self._angle_history) > 120:
                    self._angle_history.pop(0)

                target_rms, comp_rms = _extract_emg(emg_data, self.exercise)

                ang_vel   = angle - self._prev_angle
                ang_accel = ang_vel - self._prev_ang_vel
                self._prev_ang_vel = ang_vel
                self._prev_angle   = angle

                phase_prog = self._compute_phase_progress(angle)
                now = time.time()

                row = [
                    f"{now:.3f}",
                    f"{ang_vel:.4f}",
                    f"{angle:.4f}",
                    f"{ang_accel:.4f}",
                    f"{target_rms:.4f}",
                    f"{comp_rms:.4f}",
                    f"{sym:.4f}",
                    f"{phase_prog:.4f}",
                    f"{pose_score:.4f}",
                    self.mode,
                ]
                self.rows.append(row)
                self.accepted += 1

                elapsed = time.monotonic() - t_start
                remaining = duration_sec - elapsed
                print(
                    f"\r[AUTO] {self.accepted} frames  dropped={self.dropped}  "
                    f"remaining={remaining:.0f}s  angle={angle:.0f}°",
                    end="", flush=True,
                )
                time.sleep(POLL_INTERVAL)
        finally:
            self._clear_shm_mode()

        print(f"\n[AUTO] Recording finished.")
        self._save()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="IronBuddy real-time training data collector",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--mode",
        choices=["golden", "lazy", "bad"],
        required=True,
        help="Label for the collected data",
    )
    p.add_argument(
        "--exercise",
        choices=["squat", "bicep_curl"],
        default="squat",
        help="Exercise type (default: squat)",
    )
    p.add_argument(
        "--out",
        default=".",
        help="Output directory for the CSV file (default: current dir)",
    )
    p.add_argument(
        "--auto",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Auto-record for N seconds then save (no TTY needed)",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    collector = DataCollector(mode=args.mode, out_dir=args.out, exercise=args.exercise)
    if args.auto > 0:
        collector.run_auto(args.auto)
    else:
        collector.run()
