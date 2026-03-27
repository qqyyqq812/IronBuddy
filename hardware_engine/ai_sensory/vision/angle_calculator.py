"""
关节角度计算与动作计数模块。

核心逻辑：
- 用向量点积公式计算三点构成的夹角
- 用状态机追踪动作周期（站立→下蹲→站立 = 1次）
"""

import numpy as np
from enum import Enum


class RepState(Enum):
    """动作计数状态机的状态定义"""
    IDLE = "idle"           # 等待开始
    DESCENDING = "down"     # 正在下降（如深蹲的下蹲阶段）
    BOTTOM = "bottom"       # 到位保持
    ASCENDING = "up"        # 正在上升（如深蹲的站起阶段）


def calc_angle(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> float:
    """
    计算三点构成的夹角（以 b 为顶点）。

    利用向量 BA 和 BC 的点积公式：
    angle = arccos( (BA · BC) / (|BA| * |BC|) )

    Args:
        a, b, c: 形如 (x, y) 的关键点坐标，b 是角的顶点
    Returns:
        角度值（0-180度）
    """
    ba = a - b
    bc = c - b

    # 避免零向量导致除零
    norm_ba = np.linalg.norm(ba)
    norm_bc = np.linalg.norm(bc)
    if norm_ba < 1e-6 or norm_bc < 1e-6:
        return 0.0

    cosine = np.dot(ba, bc) / (norm_ba * norm_bc)
    # 数值稳定性：clamp 到 [-1, 1]
    cosine = np.clip(cosine, -1.0, 1.0)
    angle = np.degrees(np.arccos(cosine))
    return angle


class RepCounter:
    """
    基于关节角度的动作计数器（状态机模式）。

    核心思路：追踪关节角度从大→小→大的一个完整周期 = 1次有效动作。
    举例（深蹲）：站立(膝盖角度~170°) → 下蹲(~80°) → 站立(~170°) = 完成1次
    """

    def __init__(self, threshold_down: float = 90, threshold_up: float = 160,
                 hold_frames: int = 3):
        """
        Args:
            threshold_down: 到位角度阈值（低于此值认为动作到位）
            threshold_up: 回位角度阈值（高于此值认为已站起/回位）
            hold_frames: 角度需保持多少帧才算真正到位（防抖动）
        """
        self.threshold_down = threshold_down
        self.threshold_up = threshold_up
        self.hold_frames = hold_frames

        self.state = RepState.IDLE
        self.count = 0
        self.hold_count = 0  # 当前到位保持了多少帧
        self.is_good_form = False  # 上一次动作是否标准

    def update(self, angle: float) -> dict:
        """
        每帧更新一次角度，返回当前状态。

        Args:
            angle: 当前帧的关节角度
        Returns:
            {
                "count": 当前完成次数,
                "state": 当前状态,
                "is_good_form": 动作是否标准（角度达标）,
                "just_completed": 本帧是否刚好完成了一次动作
            }
        """
        just_completed = False

        if self.state == RepState.IDLE:
            # 等待用户开始下降
            if angle < self.threshold_up:
                self.state = RepState.DESCENDING

        elif self.state == RepState.DESCENDING:
            if angle <= self.threshold_down:
                self.hold_count += 1
                if self.hold_count >= self.hold_frames:
                    # 到位了！
                    self.state = RepState.BOTTOM
                    self.is_good_form = True
                    self.hold_count = 0
            else:
                self.hold_count = 0
                # 如果角度又回去了，说明没蹲到位
                if angle >= self.threshold_up:
                    self.state = RepState.IDLE
                    self.is_good_form = False

        elif self.state == RepState.BOTTOM:
            # 等待用户站起来
            if angle > self.threshold_down + 20:
                self.state = RepState.ASCENDING

        elif self.state == RepState.ASCENDING:
            if angle >= self.threshold_up:
                # 完成一次有效动作！
                self.count += 1
                just_completed = True
                self.state = RepState.IDLE

        return {
            "count": self.count,
            "state": self.state.value,
            "is_good_form": self.is_good_form,
            "angle": round(angle, 1),
            "just_completed": just_completed,
        }

    def reset(self):
        """重置计数器"""
        self.state = RepState.IDLE
        self.count = 0
        self.hold_count = 0
        self.is_good_form = False
