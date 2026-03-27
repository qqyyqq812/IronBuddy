#!/usr/bin/env python3
"""
IronBuddy V2 — 3D 关节角度与角速度计算器
从 17 个 3D 关键点计算所需的关节角度、角速度。
"""
import numpy as np
from collections import deque

# COCO 17-point 关键点索引
KP = {
    'nose': 0, 'l_eye': 1, 'r_eye': 2, 'l_ear': 3, 'r_ear': 4,
    'l_shoulder': 5, 'r_shoulder': 6,
    'l_elbow': 7, 'r_elbow': 8,
    'l_wrist': 9, 'r_wrist': 10,
    'l_hip': 11, 'r_hip': 12,
    'l_knee': 13, 'r_knee': 14,
    'l_ankle': 15, 'r_ankle': 16,
}

# 需要计算的关节角度 — (上游点, 关节点, 下游点)
JOINT_TRIPLES = {
    'l_knee':     (KP['l_hip'],      KP['l_knee'],     KP['l_ankle']),
    'r_knee':     (KP['r_hip'],      KP['r_knee'],     KP['r_ankle']),
    'l_hip':      (KP['l_shoulder'], KP['l_hip'],      KP['l_knee']),
    'r_hip':      (KP['r_shoulder'], KP['r_hip'],      KP['r_knee']),
    'l_elbow':    (KP['l_shoulder'], KP['l_elbow'],    KP['l_wrist']),
    'r_elbow':    (KP['r_shoulder'], KP['r_elbow'],    KP['r_wrist']),
    'l_shoulder': (KP['l_hip'],      KP['l_shoulder'], KP['l_elbow']),
    'r_shoulder': (KP['r_hip'],      KP['r_shoulder'], KP['r_elbow']),
}


def _angle_3d(a, b, c):
    """计算三点 a-b-c 在 b 点处的 3D 夹角（度）"""
    ba = a - b
    bc = c - b
    dot = np.dot(ba, bc)
    mag = np.linalg.norm(ba) * np.linalg.norm(bc)
    if mag < 1e-8:
        return 180.0
    cos_a = np.clip(dot / mag, -1.0, 1.0)
    return float(np.degrees(np.arccos(cos_a)))


class JointCalculator:
    """3D 关节角度 + 角速度计算器"""

    def __init__(self, fps=15.0, smoothing=3):
        self.fps = fps
        self.dt = 1.0 / fps
        self.smoothing = smoothing
        self._prev_angles = {}     # 上一帧的关节角度
        self._angle_buffers = {}   # 每个关节的历史 (用于平滑)
        for name in JOINT_TRIPLES:
            self._angle_buffers[name] = deque(maxlen=smoothing)

    def compute(self, kpts_3d):
        """
        计算所有关节角度和角速度

        Args:
            kpts_3d: np.ndarray shape (17, 3)

        Returns:
            dict: {
                'angles': {'l_knee': 85.2, 'r_knee': 87.1, ...},
                'velocities': {'l_knee': -12.3, ...}  # 度/秒
            }
        """
        angles = {}
        velocities = {}

        for name, (i_a, i_b, i_c) in JOINT_TRIPLES.items():
            raw = _angle_3d(kpts_3d[i_a], kpts_3d[i_b], kpts_3d[i_c])

            # 平滑
            self._angle_buffers[name].append(raw)
            smoothed = float(np.mean(self._angle_buffers[name]))
            angles[name] = smoothed

            # 角速度 (度/秒)
            if name in self._prev_angles:
                velocities[name] = (smoothed - self._prev_angles[name]) / self.dt
            else:
                velocities[name] = 0.0
            self._prev_angles[name] = smoothed

        return {'angles': angles, 'velocities': velocities}
