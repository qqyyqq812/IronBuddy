#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IronBuddy 视觉特征探测脚本（Phase 0）

目的：不注入 EMG，仅监听板端 FSM + 骨架，对每个 rep 提取关键视觉特征：
  - min_angle / max_angle
  - peak_ang_vel_descend / peak_ang_vel_ascend  (deg/sec, abs)
  - peak_ang_accel (deg/sec^2, abs)
  - duration_sec
  - 深蹲: trunk_angle_min (肩-髋-膝三点法, 单位 deg)
  - 弯举: shoulder_disp (肩关节 x/y 位移, 单位 px)  ← 识别"躯干后仰"

用户按顺序做 6 种动作 × 3 rep，脚本逐 rep 打印表格行。结束后用户把输出粘回给
AI，AI 定 if-else 阈值，生成智能 EMG 注入脚本。

实时读取：
  /dev/shm/fsm_state.json   — state, angle, exercise
  /dev/shm/pose_data.json   — objects[0].kpts[17]  (COCO 17 keypoints)

rep 检测：
  state 从 {DESCENDING|CURLING} → STAND 的跃迁即为一次 rep 完成

Python 3.7 兼容（板端直接跑）。无 pandas / 仅用 math+json+time。
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

POLL_HZ = 100
POLL_INTERVAL = 1.0 / POLL_HZ
FSM_SHM = "/dev/shm/fsm_state.json"
POSE_SHM = "/dev/shm/pose_data.json"

# COCO 17 keypoints 索引
KP_RSHOULDER = 6
KP_RHIP = 12
KP_RKNEE = 14
KP_RANKLE = 16
KP_RELBOW = 8
KP_RWRIST = 10
KP_LSHOULDER = 5
KP_LHIP = 11

# FSM state 词表
ACTIVE_STATES = {"DESCENDING", "CURLING"}
REST_STATE = "STAND"


def _safe_json(path):
    """原子读 JSON, 读不到返回 None."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return None


def _three_point_angle(a, b, c):
    """三点角 (向量 BA vs BC 夹角), 返回 0-180 deg. 输入是 (x, y) 或 (x,y,c)."""
    ax, ay = a[0], a[1]
    bx, by = b[0], b[1]
    cx, cy = c[0], c[1]
    v1x, v1y = ax - bx, ay - by
    v2x, v2y = cx - bx, cy - by
    dot = v1x * v2x + v1y * v2y
    m1 = math.hypot(v1x, v1y) + 1e-6
    m2 = math.hypot(v2x, v2y) + 1e-6
    cos_a = max(-1.0, min(1.0, dot / (m1 * m2)))
    return math.degrees(math.acos(cos_a))


def _get_trunk_angle():
    """从 pose_data.json 读关键点算躯干角 (肩-髋-膝). 无人返回 None."""
    d = _safe_json(POSE_SHM)
    if not d:
        return None
    objs = d.get("objects", [])
    if not objs:
        return None
    kpts = objs[0].get("kpts", [])
    if len(kpts) < 17:
        return None
    # 右侧优先: 用置信度选择
    r_sh, r_hip, r_knee = kpts[KP_RSHOULDER], kpts[KP_RHIP], kpts[KP_RKNEE]
    l_sh, l_hip, l_knee = kpts[KP_LSHOULDER], kpts[KP_LHIP], kpts[KP_LHIP + 2]
    r_conf = r_sh[2] + r_hip[2] + r_knee[2]
    l_conf = l_sh[2] + l_hip[2] + l_knee[2]
    if r_conf >= l_conf and r_sh[2] > 0.05:
        return _three_point_angle(r_sh, r_hip, r_knee)
    if l_sh[2] > 0.05:
        return _three_point_angle(l_sh, l_hip, l_knee)
    return None


def _get_shoulder_xy():
    """从 pose_data.json 读肩关节 (x,y). 用于弯举"躯干后仰"识别."""
    d = _safe_json(POSE_SHM)
    if not d:
        return None
    objs = d.get("objects", [])
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


class RepTracker:
    """维护 rep 状态机 + 累积 (t, angle, trunk, shoulder) 序列."""

    def __init__(self):
        self.prev_state = None
        self.rep_count = 0
        self.trace = []  # list of (t, angle, trunk_angle_or_None, (sh_x, sh_y)_or_None)
        self.rep_start_ts = None
        self.last_exercise = "squat"

    def push_sample(self, now, state, angle, exercise):
        """每次 poll 调用. 返回 (rep_complete_dict_or_None)."""
        in_active = state in ACTIVE_STATES
        # 进入活跃: 新 rep 起点
        if in_active and (self.prev_state not in ACTIVE_STATES):
            self.trace = []
            self.rep_start_ts = now
            self.last_exercise = exercise
        # 活跃期累积
        if in_active:
            trunk = _get_trunk_angle() if exercise == "squat" else None
            sh_xy = _get_shoulder_xy() if exercise == "bicep_curl" else None
            self.trace.append((now, angle, trunk, sh_xy))
        # 退出活跃: rep 完成
        result = None
        if (not in_active) and (self.prev_state in ACTIVE_STATES):
            if len(self.trace) >= 3:
                self.rep_count += 1
                result = self._finalize_rep()
            self.trace = []
            self.rep_start_ts = None
        self.prev_state = state
        return result

    def _finalize_rep(self):
        """对当前 trace 计算特征, 返回 dict."""
        ts = [t for (t, _, _, _) in self.trace]
        angles = [a for (_, a, _, _) in self.trace]
        trunks = [tr for (_, _, tr, _) in self.trace if tr is not None]
        shs = [sh for (_, _, _, sh) in self.trace if sh is not None]

        # 找 min_angle index (底部/顶峰)
        min_i = 0
        min_a = angles[0]
        for i, a in enumerate(angles):
            if a < min_a:
                min_a = a
                min_i = i
        max_a = max(angles)

        # 角速度: 前后差分除时间
        vel = []
        for i in range(1, len(angles)):
            dt = ts[i] - ts[i - 1]
            if dt > 1e-4:
                vel.append((angles[i] - angles[i - 1]) / dt)
            else:
                vel.append(0.0)

        if not vel:
            vel = [0.0]

        # 下蹲/下放阶段 (index 0 到 min_i) - 角度下降, vel 应为负
        desc_vel = vel[:min_i] if min_i > 0 else []
        asc_vel = vel[min_i:] if min_i < len(vel) else []
        peak_desc = max((abs(v) for v in desc_vel), default=0.0)
        peak_asc = max((abs(v) for v in asc_vel), default=0.0)

        # 角加速度
        accels = []
        for i in range(1, len(vel)):
            dt = ts[i] - ts[i - 1]  # 注意 vel index 对应 ts[1:], 近似 dt
            if dt > 1e-4:
                accels.append(abs((vel[i] - vel[i - 1]) / dt))
        peak_accel = max(accels) if accels else 0.0

        duration = ts[-1] - ts[0]

        # 躯干角最小值 (深蹲用) - 越小代表上身越前倾
        trunk_min = min(trunks) if trunks else None

        # 肩部位移范围 (弯举用) - 后仰动作肩会向后/向上移动
        sh_disp = None
        if len(shs) >= 2:
            xs = [s[0] for s in shs]
            ys = [s[1] for s in shs]
            sh_disp = math.hypot(max(xs) - min(xs), max(ys) - min(ys))

        return {
            "rep": self.rep_count,
            "exercise": self.last_exercise,
            "min_angle": min_a,
            "max_angle": max_a,
            "peak_vel_desc": peak_desc,
            "peak_vel_asc": peak_asc,
            "peak_accel": peak_accel,
            "duration": duration,
            "trunk_min": trunk_min,
            "shoulder_disp": sh_disp,
            "sample_n": len(self.trace),
        }


def _format_rep(r):
    """单行紧凑输出."""
    ex = r["exercise"][:5]
    t_str = "--"
    if r["trunk_min"] is not None:
        t_str = "{:5.1f}".format(r["trunk_min"])
    s_str = "--"
    if r["shoulder_disp"] is not None:
        s_str = "{:4.0f}px".format(r["shoulder_disp"])
    return (
        "[REP#{rep:02d} {ex}] "
        "min={mn:5.1f}° max={mx:5.1f}° "
        "vel_desc={vd:6.1f}°/s vel_asc={va:6.1f}°/s "
        "accel={ac:6.1f}°/s² "
        "dur={du:4.1f}s trunk_min={t} sh_disp={s} n={n}"
    ).format(
        rep=r["rep"], ex=ex,
        mn=r["min_angle"], mx=r["max_angle"],
        vd=r["peak_vel_desc"], va=r["peak_vel_asc"],
        ac=r["peak_accel"], du=r["duration"],
        t=t_str, s=s_str, n=r["sample_n"],
    )


def main():
    print("=" * 78)
    print("IronBuddy 视觉特征探测 · Phase 0")
    print("=" * 78)
    print("请按以下顺序做 6 种动作 × 3 rep (共 18 rep):")
    print("  [1-3]  3 个标准深蹲")
    print("  [4-6]  3 个弹震式代偿深蹲 (底部立即爆起)")
    print("  [7-9]  3 个半蹲 (只蹲到 100-130° 就起)")
    print("  [10]   语音切弯举")
    print("  [10-12] 3 个标准弯举 (肘贴肋慢举到 40-50°)")
    print("  [13-15] 3 个躯干后仰甩臂 (肚子前顶+肩上耸+前臂爆甩)")
    print("  [16-18] 3 个半程弯举 (只弯到 60-80°)")
    print()
    print("每完成 1 rep, 本脚本会立刻打印该 rep 的 9 项视觉特征.")
    print("做完全部 18 rep 后按 Ctrl+C, 把输出截屏或复制粘回给 AI.")
    print("=" * 78)
    print()

    tracker = RepTracker()
    last_fsm_ts = 0.0
    last_rep_counters = None

    try:
        while True:
            now = time.time()
            # 限频 100Hz
            wait = (last_fsm_ts + POLL_INTERVAL) - now
            if wait > 0:
                time.sleep(wait)
                now = time.time()
            last_fsm_ts = now

            fsm = _safe_json(FSM_SHM)
            if fsm is None:
                continue
            state = fsm.get("state", "NO_PERSON")
            angle = fsm.get("angle")
            exercise = fsm.get("exercise", "squat")
            if not isinstance(angle, (int, float)):
                continue

            # 边界兜底: 监听 good+failed+comp 总和变化 (若 state-based 漏掉)
            cur_sum = (fsm.get("good", 0) or 0) + (fsm.get("failed", 0) or 0) \
                + (fsm.get("comp", 0) or 0)
            if last_rep_counters is None:
                last_rep_counters = cur_sum

            result = tracker.push_sample(now, state, float(angle), exercise)
            if result:
                print(_format_rep(result))
                sys.stdout.flush()

    except KeyboardInterrupt:
        print("\n")
        print("=" * 78)
        print("探测结束. 共检测到 {} 个 rep.".format(tracker.rep_count))
        print("把上面的全部 [REP#xx ...] 行复制回给 AI, 用来定智能脚本的 if-else 阈值.")
        print("=" * 78)
        return 0


if __name__ == "__main__":
    sys.exit(main())
