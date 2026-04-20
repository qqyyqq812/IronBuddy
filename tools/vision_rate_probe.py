#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vision_rate_probe.py — readonly 探针, 测视觉帧率是否提升到位.

以 200Hz 轮询 /dev/shm/pose_data.json 的 mtime 变化, 统计 N 秒窗口的:
  - 有效更新 Hz
  - 最大两帧间隙 (ms)
  - 有效人体骨架帧率 (score > 0.05)

使用:
  ssh toybrick@10.18.76.224 python3 /home/toybrick/streamer_v3/tools/vision_rate_probe.py --seconds 5
  # 期望 avg_hz >= 22, max_gap_ms <= 80

Python 3.7 兼容.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

POSE_SHM = "/dev/shm/pose_data.json"
POLL_INTERVAL = 0.005  # 200Hz


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=5.0, help="统计窗口时长")
    ap.add_argument("--shm", default=POSE_SHM)
    args = ap.parse_args()

    if not os.path.exists(args.shm):
        print("[FATAL] 找不到 {}. 请先启动 vision 服务.".format(args.shm))
        return 2

    print("[PROBE] 监听 {} 共 {:.1f}s ...".format(args.shm, args.seconds))
    last_mtime = 0.0
    last_update_ts = time.time()
    gaps_ms = []
    update_count = 0
    valid_person_count = 0
    t0 = time.time()
    t_end = t0 + args.seconds

    while time.time() < t_end:
        try:
            st = os.stat(args.shm)
            mt = st.st_mtime
        except OSError:
            time.sleep(POLL_INTERVAL)
            continue
        if mt > last_mtime:
            now = time.time()
            if last_mtime > 0.0:
                gaps_ms.append((now - last_update_ts) * 1000.0)
            last_mtime = mt
            last_update_ts = now
            update_count += 1
            # 同步读一次内容判定人体是否有效
            try:
                with open(args.shm, "r") as f:
                    d = json.load(f)
                objs = d.get("objects", [])
                if objs and objs[0].get("score", 0.0) > 0.05:
                    valid_person_count += 1
            except (IOError, ValueError):
                pass
        time.sleep(POLL_INTERVAL)

    elapsed = max(time.time() - t0, 1e-3)
    avg_hz = update_count / elapsed
    valid_hz = valid_person_count / elapsed
    max_gap = max(gaps_ms) if gaps_ms else 0.0
    mean_gap = (sum(gaps_ms) / len(gaps_ms)) if gaps_ms else 0.0

    print("")
    print("=" * 50)
    print("  vision_rate_probe 结果 ({:.1f}s 窗口)".format(elapsed))
    print("=" * 50)
    print("  总更新次数     : {}".format(update_count))
    print("  平均更新 Hz    : {:.1f}".format(avg_hz))
    print("  有效人体 Hz    : {:.1f}".format(valid_hz))
    print("  最大两帧间隙   : {:.1f} ms".format(max_gap))
    print("  平均两帧间隙   : {:.1f} ms".format(mean_gap))
    print("=" * 50)

    if avg_hz >= 22.0 and max_gap <= 80.0:
        print("  ✅ 帧率达标 (avg_hz>=22, max_gap<=80)")
        return 0
    print("  ⚠️ 帧率未达目标. 建议检查 CLOUD_TARGET_FPS / NPU 热限.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
