#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
simulate_mvc_burst.py — 独立 MVC 爆发模拟脚本 (5s), 与视觉解耦.

彩排流程用法:
  1. 用户语音: "教练" → "开始 MVC 测试"
  2. voice_daemon 播报: "正在测量, 请用最大力量收缩" (开始倒计时 3-2-1)
  3. 用户立刻在 ssh 终端运行: python3 tools/simulate_mvc_burst.py
  4. 脚本立即 (<50ms) 写 /dev/shm/emg_calibration.json 的 peak_mvc 字段
  5. 脚本同步发 5s 高幅度 UDP 波形, 让 udp_emg_server 的 muscle_activation.json
     显示最大发力 (UI 视觉反馈)
  6. voice_daemon 倒计时 + 1.5s buffer 后读 emg_calibration.json 播报峰值

关键: 启动即写 emg_calibration.json (立即生效, 不等 5s 结束), 保证 voice 播报能读到.

参考 v42 训练数据集 (data/v42/user_*/mvc_calibration.json):
  ch0 (biceps 主肌): 763-818, 取 800 为默认
  ch1 (comp/forearm): 443-513, 取 470 为默认

Python 3.7 兼容. 无 walrus, 无 X|None, 无 pandas.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import socket
import sys
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_DURATION = 5.0
DEFAULT_RATE_HZ = 1000
DEFAULT_TARGET_PEAK = 800.0   # ch0 RMS peak, v42 range 763-818
DEFAULT_COMP_PEAK = 470.0     # ch1 RMS peak, v42 range 443-513
EMG_CAL_PATH = "/dev/shm/emg_calibration.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [MVC_BURST] - %(message)s",
)


def _write_calibration(target_peak, comp_peak, exercise):
    # type: (float, float, str) -> bool
    """立即写 /dev/shm/emg_calibration.json (voice_daemon 读取的 legacy 格式)."""
    payload = {
        "peak_mvc": {
            "ch0": float(target_peak),
            "ch1": float(comp_peak),
        },
        "protocol": "SIM-5s",
        "exercise": exercise,
        "std_pct": 0.08,
        "ts": time.time(),
        "source": "simulate_mvc_burst",
    }
    try:
        tmp = EMG_CAL_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.rename(tmp, EMG_CAL_PATH)
        return True
    except (IOError, OSError) as e:
        logging.error("写 %s 失败: %s", EMG_CAL_PATH, e)
        return False


def _envelope(elapsed, duration, ramp=0.5):
    # type: (float, float, float) -> float
    """生理包络: 前 ramp 秒爬坡, 中段恒定最大, 最后 ramp 秒回落."""
    if elapsed < ramp:
        return elapsed / ramp
    if elapsed > duration - ramp:
        return max(0.0, (duration - elapsed) / ramp)
    return 1.0


def _synth_raw_sample(rms_target, t_sec):
    # type: (float, float) -> float
    """生成单个原始 EMG ADC 采样, 使后续 DSP RMS ≈ rms_target."""
    # 与 simulate_emg_from_* 同构: K_AMP=2.0 补偿 HP+Notch+LP 衰减
    amp = rms_target * 2.0 * 1.414  # sine peak ≈ RMS * sqrt(2)
    carrier = amp * math.sin(2.0 * math.pi * 80.0 * t_sec)
    noise = amp * 0.3 * random.gauss(0.0, 1.0)
    return carrier + noise


def main():
    ap = argparse.ArgumentParser(description="Standalone 5s MVC burst simulator")
    ap.add_argument("--host", default=DEFAULT_HOST, help="udp_emg_server IP (板端 127.0.0.1)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--duration", type=float, default=DEFAULT_DURATION, help="爆发时长 (秒)")
    ap.add_argument("--rate-hz", type=int, default=DEFAULT_RATE_HZ)
    ap.add_argument("--target-peak", type=float, default=DEFAULT_TARGET_PEAK,
                    help="ch0 RMS 峰值 (v42 范围 763-818)")
    ap.add_argument("--comp-peak", type=float, default=DEFAULT_COMP_PEAK,
                    help="ch1 RMS 峰值 (v42 范围 443-513)")
    ap.add_argument("--exercise", default="curl", choices=("curl", "squat"))
    args = ap.parse_args()

    # ───────── STEP 1: 立即写 emg_calibration.json ─────────
    # 这是最关键一步, voice_daemon 会在倒计时+1.5s 后读这个文件
    ok = _write_calibration(args.target_peak, args.comp_peak, args.exercise)
    if ok:
        logging.info("✅ 已写 %s: ch0=%.1f ch1=%.1f exercise=%s",
                     EMG_CAL_PATH, args.target_peak, args.comp_peak, args.exercise)
    else:
        logging.warning("⚠️  emg_calibration.json 写入失败, voice 可能读不到峰值")

    # ───────── STEP 2: 5s UDP 高幅度爆发 ─────────
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 15)
    dest = (args.host, args.port)
    logging.info("🚀 爆发 %gs → %s:%d @ %dHz (target=%g comp=%g)",
                 args.duration, args.host, args.port, args.rate_hz,
                 args.target_peak, args.comp_peak)

    interval = 1.0 / float(args.rate_hz)
    t0 = time.time()
    t_end = t0 + args.duration
    last_log = t0
    pkt_count = 0

    try:
        while True:
            now = time.time()
            if now >= t_end:
                break
            elapsed = now - t0
            env = _envelope(elapsed, args.duration, ramp=0.5)
            rms_t = args.target_peak * env
            rms_c = args.comp_peak * env
            t_raw = _synth_raw_sample(rms_t, now)
            c_raw = _synth_raw_sample(rms_c, now)
            msg = "{:.1f} {:.1f}".format(t_raw, c_raw).encode("ascii")
            try:
                sock.sendto(msg, dest)
            except (OSError, socket.error) as e:
                logging.warning("UDP 发送失败: %s", e)
            pkt_count += 1

            if now - last_log > 1.0:
                logging.info("📡 t=%.1fs env=%.2f rms_t=%.0f rms_c=%.0f @ %d pkts",
                             elapsed, env, rms_t, rms_c, pkt_count)
                last_log = now

            dt_tick = time.time() - now
            if dt_tick < interval:
                time.sleep(interval - dt_tick)
    except KeyboardInterrupt:
        logging.info("⏹ 手动中断")

    sock.close()
    logging.info("✅ MVC 爆发结束, 共发 %d 包", pkt_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
