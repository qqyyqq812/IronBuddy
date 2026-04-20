#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IronBuddy 视觉特征探测 V2 (FSM piggyback 版)

V1 问题：自己写独立 rep 状态机，把环境噪声(衣架/背景)当 rep，触发虚假记录。
V2 修复：不再自己判 rep 边界，**复用板端 FSM (main_claw_loop) 真正的 rep 计数**。

原理：
  FSM 每完成一次 rep，会 total_good/total_failed/total_comp 之一 +1 (pure_vision)
  或 _total_reps_count +1 (vision_sensor, 但未暴露到 fsm_state.json)。
  在 pure_vision 下 total_good + total_failed 即是 FSM 认可的 rep 数。
  本脚本监听这个总和变化，只有 FSM 计数的 rep 才视为有效, 期间收集视觉特征。

输出：
  - 每个 rep 写一行 JSON 到 /dev/shm/rep_features.jsonl (rolling 50 行)
  - 同时 print 可读行到 stdout (给 UI 或 log 看)
  - PID 写到 /dev/shm/probe_v2.pid 供 streamer API 杀进程

UI 集成：streamer_app.py 的 /api/probe/start 启动本脚本, /api/probe/stop 杀.

Python 3.7 兼容（板端）。仅用 math + json + time + os。
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

POLL_HZ = 50
POLL_INTERVAL = 1.0 / POLL_HZ
FSM_SHM = "/dev/shm/fsm_state.json"
POSE_SHM = "/dev/shm/pose_data.json"
FEATURES_JSONL = "/dev/shm/rep_features.jsonl"
PID_FILE = "/dev/shm/probe_v2.pid"
LABEL_FILE = "/dev/shm/probe_label.txt"  # 用户在 UI 点 6 按钮切换标签
MAX_ROLLING_LINES = 200  # 多类录制需要更大

# FSM state 词表 (V7.17+ main_claw_loop.py 实际使用值)
# 深蹲: NO_PERSON / IDLE / STAND(休息) / DESCENDING(下蹲) / BOTTOM(底停) / ASCENDING(起身)
# 弯举: NO_PERSON / IDLE / STAND / CURLING(向心) / [BOTTOM/ASCENDING 如果复用]
# REST_STATES = 休息/无人: 不累积 trace
REST_STATES = {"NO_PERSON", "IDLE", "STAND"}
# ACTIVE = 其他所有活跃状态 (即 DESCENDING/BOTTOM/ASCENDING/CURLING 等)
# 动态判定: state not in REST_STATES 即为活跃

# COCO 17 keypoints 索引
KP_RSHOULDER = 6
KP_LSHOULDER = 5
KP_RHIP = 12
KP_LHIP = 11
KP_RKNEE = 14


def _read_current_label():
    """读当前用户选中的标签 (6 按钮写入 /dev/shm/probe_label.txt). 未选返回 'unlabeled'."""
    try:
        if os.path.exists(LABEL_FILE):
            with open(LABEL_FILE, "r") as f:
                lab = f.read().strip()
            return lab if lab else "unlabeled"
    except (IOError, OSError):
        pass
    return "unlabeled"


def _safe_json(path):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return None


def _three_point_angle(a, b, c):
    """BA ∠ BC, 0-180 deg."""
    v1x, v1y = a[0] - b[0], a[1] - b[1]
    v2x, v2y = c[0] - b[0], c[1] - b[1]
    dot = v1x * v2x + v1y * v2y
    m1 = math.hypot(v1x, v1y) + 1e-6
    m2 = math.hypot(v2x, v2y) + 1e-6
    return math.degrees(math.acos(max(-1.0, min(1.0, dot / (m1 * m2)))))


def _get_trunk_angle(pose):
    """肩-髋-膝三点角. 越小 = 躯干前倾越重."""
    if not pose:
        return None
    objs = pose.get("objects", [])
    if not objs:
        return None
    kpts = objs[0].get("kpts", [])
    if len(kpts) < 17:
        return None
    r_sh, r_hip, r_knee = kpts[KP_RSHOULDER], kpts[KP_RHIP], kpts[KP_RKNEE]
    l_sh, l_hip = kpts[KP_LSHOULDER], kpts[KP_LHIP]
    l_knee = kpts[KP_RKNEE - 1]  # 左膝 13
    r_conf = r_sh[2] + r_hip[2] + r_knee[2]
    l_conf = l_sh[2] + l_hip[2] + l_knee[2]
    if r_conf >= l_conf and r_sh[2] > 0.05 and r_hip[2] > 0.05 and r_knee[2] > 0.05:
        return _three_point_angle(r_sh, r_hip, r_knee)
    if l_sh[2] > 0.05 and l_hip[2] > 0.05 and l_knee[2] > 0.05:
        return _three_point_angle(l_sh, l_hip, l_knee)
    return None


def _get_shoulder_xy(pose):
    if not pose:
        return None
    objs = pose.get("objects", [])
    if not objs:
        return None
    kpts = objs[0].get("kpts", [])
    if len(kpts) < 17:
        return None
    r_sh, l_sh = kpts[KP_RSHOULDER], kpts[KP_LSHOULDER]
    if r_sh[2] >= l_sh[2] and r_sh[2] > 0.05:
        return (r_sh[0], r_sh[1])
    if l_sh[2] > 0.05:
        return (l_sh[0], l_sh[1])
    return None


def _compute_features(trace, exercise):
    """从 [(t, angle, trunk, sh_xy), ...] 计算一次 rep 的视觉特征字典."""
    if len(trace) < 3:
        return None
    ts = [t for (t, _, _, _) in trace]
    angles = [a for (_, a, _, _) in trace]
    trunks = [tr for (_, _, tr, _) in trace if tr is not None]
    shs = [s for (_, _, _, s) in trace if s is not None]

    # 找 min_angle 索引 (底部/顶峰)
    min_i = 0
    min_a = angles[0]
    for i, a in enumerate(angles):
        if a < min_a:
            min_a = a
            min_i = i
    max_a = max(angles)

    # 角速度 (deg/s), 合理范围 clip 到 ±1500 防止单帧跳变被当真
    vel = [0.0]
    for i in range(1, len(angles)):
        dt = ts[i] - ts[i - 1]
        if dt > 5e-3:  # 只取 >5ms 的相邻帧（防止重复读同一帧）
            v = (angles[i] - angles[i - 1]) / dt
            if -1500.0 < v < 1500.0:  # 超过这个速度人类做不到，丢弃
                vel.append(v)
        else:
            vel.append(vel[-1] if vel else 0.0)

    # 分段 (下蹲 vs 起身)
    desc_vel = vel[:min_i] if min_i > 0 else []
    asc_vel = vel[min_i:] if min_i < len(vel) else []
    peak_desc = max((abs(v) for v in desc_vel), default=0.0)
    peak_asc = max((abs(v) for v in asc_vel), default=0.0)

    # 角加速度 — 50Hz 相邻帧差分噪声大, 改用 50ms 滑窗差分更稳
    # (窗口约 2-3 帧, 比相邻帧差分噪声小 5-10 倍)
    accels = []
    WINDOW_DT = 0.05  # 50ms 窗口
    for i in range(1, len(vel)):
        # 找最远 >= 50ms 的前一个 vel 索引
        t_now = ts[i]
        j = i - 1
        while j > 0 and (t_now - ts[j]) < WINDOW_DT:
            j -= 1
        dt_w = t_now - ts[j]
        if dt_w > 5e-3:
            a = abs((vel[i] - vel[j]) / dt_w)
            if a < 5000.0:  # 人类动作上限, 过滤异常
                accels.append(a)
    peak_accel = max(accels) if accels else 0.0

    duration = ts[-1] - ts[0]

    trunk_min = min(trunks) if trunks else None

    sh_disp = None
    if len(shs) >= 3:
        xs = [s[0] for s in shs]
        ys = [s[1] for s in shs]
        sh_disp = math.hypot(max(xs) - min(xs), max(ys) - min(ys))

    return {
        "ts": time.time(),
        "exercise": exercise,
        "min_angle": round(min_a, 1),
        "max_angle": round(max_a, 1),
        "peak_vel_desc": round(peak_desc, 1),
        "peak_vel_asc": round(peak_asc, 1),
        "peak_accel": round(peak_accel, 1),
        "duration": round(duration, 2),
        "trunk_min": round(trunk_min, 1) if trunk_min is not None else None,
        "shoulder_disp": round(sh_disp, 1) if sh_disp is not None else None,
        "sample_n": len(trace),
    }


def _append_rolling(path, new_line):
    """append line, cap at MAX_ROLLING_LINES."""
    try:
        existing = []
        if os.path.exists(path):
            with open(path, "r") as f:
                existing = f.readlines()
        existing.append(new_line + "\n")
        if len(existing) > MAX_ROLLING_LINES:
            existing = existing[-MAX_ROLLING_LINES:]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.writelines(existing)
        os.rename(tmp, path)
    except (IOError, OSError) as e:
        print("[probe_v2] append failed:", e, file=sys.stderr)


def _format_readable(feat, rep_idx):
    t_str = "{:5.1f}".format(feat["trunk_min"]) if feat["trunk_min"] is not None else "  --"
    s_str = "{:5.0f}px".format(feat["shoulder_disp"]) if feat["shoulder_disp"] is not None else "  --  "
    return (
        "[REP#{ri:02d} {ex:5s}] min={mn:5.1f}° max={mx:5.1f}° "
        "vel_desc={vd:6.1f}°/s vel_asc={va:6.1f}°/s accel={ac:6.1f}°/s² "
        "dur={du:4.1f}s trunk_min={t} sh_disp={s} n={n}"
    ).format(
        ri=rep_idx, ex=feat["exercise"][:5],
        mn=feat["min_angle"], mx=feat["max_angle"],
        vd=feat["peak_vel_desc"], va=feat["peak_vel_asc"],
        ac=feat["peak_accel"], du=feat["duration"],
        t=t_str, s=s_str, n=feat["sample_n"],
    )


def main():
    # 写 PID
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except IOError:
        pass
    # 清空历史
    try:
        if os.path.exists(FEATURES_JSONL):
            os.remove(FEATURES_JSONL)
    except OSError:
        pass

    print("=" * 78, flush=True)
    print("IronBuddy 视觉特征探测 V2 · 复用 FSM rep 边界", flush=True)
    print("=" * 78, flush=True)
    print("只在 FSM 真正计数的 rep 完成时才打印特征 (噪声/杂物不会触发)", flush=True)
    print("PID={}  特征文件={}".format(os.getpid(), FEATURES_JSONL), flush=True)
    print("=" * 78, flush=True)
    print("", flush=True)

    trace = []
    prev_rep_sum = None
    prev_state = None
    rep_counter = 0
    last_poll = 0.0

    try:
        while True:
            now = time.time()
            wait = (last_poll + POLL_INTERVAL) - now
            if wait > 0:
                time.sleep(wait)
                now = time.time()
            last_poll = now

            fsm = _safe_json(FSM_SHM)
            if fsm is None:
                continue
            state = fsm.get("state", "NO_PERSON")
            angle = fsm.get("angle")
            exercise = fsm.get("exercise", "squat")
            if not isinstance(angle, (int, float)):
                continue

            # FSM 真实 rep 计数 = good + failed + comp
            good = int(fsm.get("good", 0) or 0)
            failed = int(fsm.get("failed", 0) or 0)
            comp = int(fsm.get("comp", 0) or 0)
            rep_sum = good + failed + comp
            if prev_rep_sum is None:
                prev_rep_sum = rep_sum

            # 在 DESCENDING/CURLING 期间累积 trace
            in_active = state not in REST_STATES  # 非 STAND/NO_PERSON/IDLE 即活跃
            if in_active:
                pose = _safe_json(POSE_SHM)
                trunk = _get_trunk_angle(pose) if exercise == "squat" else None
                sh_xy = _get_shoulder_xy(pose) if exercise == "bicep_curl" else None
                trace.append((now, float(angle), trunk, sh_xy))

            # 退出 ACTIVE + rep_sum 有增长 → FSM 真正计数了一个 rep
            exited_active = (not in_active) and (prev_state is not None and prev_state not in REST_STATES)
            rep_incremented = rep_sum > prev_rep_sum

            if rep_incremented:
                # 计算特征
                feat = _compute_features(trace, exercise)
                rep_counter += 1
                if feat is not None:
                    label = _read_current_label()
                    feat["label"] = label
                    feat["rep_idx"] = rep_counter
                    feat["fsm_good"] = good
                    feat["fsm_failed"] = failed
                    feat["fsm_comp"] = comp
                    readable = _format_readable(feat, rep_counter) + " [{}]".format(label)
                    print(readable, flush=True)
                    _append_rolling(FEATURES_JSONL, json.dumps(feat, ensure_ascii=False))
                else:
                    print("[REP#{:02d} {}] 特征计算失败 (trace 太短 n={})".format(
                        rep_counter, exercise[:5], len(trace)), flush=True)
                trace = []
                prev_rep_sum = rep_sum
            elif exited_active and not rep_incremented:
                # 离开 ACTIVE 但 FSM 没计数 → FSM 自己判定此 rep 无效, 我们也丢弃 trace
                trace = []

            prev_state = state

    except KeyboardInterrupt:
        print("\n", flush=True)
        print("=" * 78, flush=True)
        print("探测结束. 共捕获 {} 个 FSM 认可的 rep.".format(rep_counter), flush=True)
        print("特征历史文件: {}".format(FEATURES_JSONL), flush=True)
        print("=" * 78, flush=True)
        try:
            if os.path.exists(PID_FILE):
                os.remove(PID_FILE)
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
