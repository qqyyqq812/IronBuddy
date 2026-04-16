#!/usr/bin/env python3
"""
IronBuddy V2.1 — 肌肉激活估计引擎（累积模式）

核心逻辑变更（V2.1）：
  旧：每帧独立计算激活值 → 数值跳变
  新：累积模式 — 每完成一次标准动作，主动肌/协同肌/稳定肌
      按不同权重累积颜色深度。组间手动重置归零。
"""
import json
import os
import time
import numpy as np
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [MUSCLE] - %(message)s')

# 肌肉激活系数映射（相对激活权重比例）
_DEFAULT_ACTIVATION_WEIGHTS = {
    'primary': 1.0,
    'synergist': 0.5,
    'stabilizer': 0.2,
}

# 全身肌肉列表（前端渲染用, 每个肌肉的显示名 + 默认 0 激活）
ALL_MUSCLES = [
    'quadriceps', 'glutes', 'hamstrings', 'calves',
    'erector_spinae', 'abs', 'hip_adductors',
    'biceps', 'brachialis', 'brachioradialis',
    'anterior_deltoid', 'trapezius', 'forearm_flexors',
]


class MuscleModel:
    """肌肉激活百分比估计器（V2.1 累积模式）"""

    # 每次合格动作的累积增量（按角色）
    _REP_WEIGHTS_GOOD = {'primary': 8, 'synergist': 5, 'stabilizer': 3}
    # 违规动作也给少量累积（毕竟做了动作）
    _REP_WEIGHTS_BAD  = {'primary': 3, 'synergist': 2, 'stabilizer': 1}

    def __init__(self, data_dir=None):
        if data_dir is None:
            data_dir = os.path.dirname(os.path.abspath(__file__))

        with open(os.path.join(data_dir, 'exercise_profiles.json'), 'r') as f:
            self.profiles = json.load(f)
        with open(os.path.join(data_dir, 'moment_arm_tables.json'), 'r') as f:
            self.moment_arms = json.load(f)

        self.body_ratios = self.moment_arms.get('body_segment_ratios', {})
        self._current_exercise = None
        self._user_params = {}
        self._compensation_warnings = []

        # V2.1 累积模式状态
        self._cumulative = {m: 0.0 for m in ALL_MUSCLES}
        self._rep_count = 0
        self._flash_muscles = []     # 本次动作闪亮的肌肉名
        self._flash_until = 0.0      # 闪烁截止时间戳

        logging.info(f"肌肉模型加载完成(累积模式), 支持动作: {list(self.profiles.keys())}")

    def set_exercise(self, exercise_type):
        """设置当前训练动作类型"""
        if exercise_type not in self.profiles:
            logging.warning(f"不支持的动作类型: {exercise_type}")
            return False
        self._current_exercise = exercise_type
        logging.info(f"切换动作: {self.profiles[exercise_type]['name']}")
        return True

    def set_user_params(self, height_cm=170, weight_kg=70, equipment_kg=0, gender='male'):
        """设置用户身体参数（用于力矩补偿）"""
        self._user_params = {
            'height_cm': height_cm,
            'weight_kg': weight_kg,
            'equipment_kg': equipment_kg,
            'gender': gender,
        }
        logging.info(f"用户参数: {height_cm}cm, {weight_kg}kg, 器材{equipment_kg}kg")

    def on_rep_completed(self, is_good):
        """FSM 每完成一次动作时调用 — 累积颜色深度"""
        if not self._current_exercise or self._current_exercise not in self.profiles:
            return
        self._rep_count += 1
        profile = self.profiles[self._current_exercise]
        weights = self._REP_WEIGHTS_GOOD if is_good else self._REP_WEIGHTS_BAD
        flash_list = []
        for m, info in profile['muscles'].items():
            if m not in self._cumulative:
                continue
            inc = weights.get(info['role'], 1)
            self._cumulative[m] = min(100.0, self._cumulative[m] + inc)
            if info['role'] == 'primary':
                flash_list.append(m)
        self._flash_muscles = flash_list
        self._flash_until = time.time() + 0.8  # 主动肌闪烁 0.8 秒
        tag = '✅ 标准' if is_good else '⚠️ 违规'
        logging.info(f"[累积] {tag} rep #{self._rep_count}, 主动肌累积至 {[f'{m}:{int(self._cumulative[m])}%' for m in flash_list]}")

    def reset_set(self):
        """重置一组（用户点击重置或切换动作时）"""
        self._cumulative = {m: 0.0 for m in ALL_MUSCLES}
        self._rep_count = 0
        self._flash_muscles = []
        self._flash_until = 0.0
        logging.info("[累积] 已重置")

    def compute(self, joint_data):
        """
        返回累积式肌肉激活百分比（V2.1）

        不再逐帧独立计算，而是返回 on_rep_completed() 积累的值。
        同时仍然做代偿检测（基于瞬时关节角度）。
        """
        if not self._current_exercise or self._current_exercise not in self.profiles:
            return self._zero_result()

        profile = self.profiles[self._current_exercise]
        self._compensation_warnings = []

        # 代偿检测仍基于瞬时角度
        if joint_data and joint_data.get('angles'):
            instant_act = self._compute_instant(joint_data, profile)
            self._check_compensation(instant_act, profile)

        # 返回累积值（非瞬时值）
        now = time.time()
        return {
            'activations': {m: int(self._cumulative[m]) for m in ALL_MUSCLES},
            'warnings': self._compensation_warnings,
            'exercise': self._current_exercise,
            'flash': self._flash_muscles if now < self._flash_until else [],
            'rep_count': self._rep_count,
        }

    def _compute_instant(self, joint_data, profile):
        """瞬时激活计算（仅用于代偿检测，不返回给前端）"""
        angles = joint_data['angles']
        velocities = joint_data['velocities']
        activations = {m: 0 for m in ALL_MUSCLES}

        for muscle_name, muscle_info in profile['muscles'].items():
            if muscle_name not in activations:
                continue
            role = muscle_info['role']
            related_joints = muscle_info['joints']
            action = muscle_info['action']
            base_weight = _DEFAULT_ACTIVATION_WEIGHTS.get(role, 0.2)
            joint_contributions = []
            for jname in related_joints:
                if jname not in angles:
                    continue
                a = angles[jname]
                v = velocities.get(jname, 0)
                angle_factor = max(0, (180 - a) / 180)
                speed_factor = min(1.0, abs(v) / 200)
                if action == 'extension' and v > 0:
                    direction_boost = 0.3
                elif action == 'flexion' and v < 0:
                    direction_boost = 0.3
                else:
                    direction_boost = 0
                contribution = (angle_factor * 0.6 + speed_factor * 0.3 + direction_boost * 0.1)
                joint_contributions.append(contribution)
            if joint_contributions:
                avg_contribution = np.mean(joint_contributions)
                raw_activation = avg_contribution * base_weight
                eq_kg = self._user_params.get('equipment_kg', 0)
                if eq_kg > 0:
                    raw_activation *= (1.0 + eq_kg * 0.02)
                activations[muscle_name] = int(np.clip(raw_activation * 100, 0, 100))
        return activations

    def _check_compensation(self, activations, profile):
        """检测肌肉代偿"""
        muscles = profile['muscles']
        primary_names = [m for m, info in muscles.items() if info['role'] == 'primary']
        stabilizer_names = [m for m, info in muscles.items() if info['role'] == 'stabilizer']

        primary_avg = np.mean([activations.get(m, 0) for m in primary_names]) if primary_names else 0

        for stab in stabilizer_names:
            stab_val = activations.get(stab, 0)
            # 稳定肌激活 > 主动肌 50% → 代偿警告
            if primary_avg > 10 and stab_val > primary_avg * 0.5:
                muscle_cn = _MUSCLE_CN.get(stab, stab)
                self._compensation_warnings.append(f"⚠️ {muscle_cn}过度激活！疑似代偿")

    def _zero_result(self):
        return {
            'activations': {m: 0 for m in ALL_MUSCLES},
            'warnings': [],
            'exercise': self._current_exercise,
        }


# 肌肉中文名映射（用于代偿警告显示）
_MUSCLE_CN = {
    'quadriceps': '股四头肌',
    'glutes': '臀大肌',
    'hamstrings': '腘绳肌',
    'calves': '小腿肌',
    'erector_spinae': '竖脊肌',
    'abs': '腹肌',
    'hip_adductors': '髋内收肌',
    'biceps': '肱二头肌',
    'brachialis': '肱肌',
    'brachioradialis': '肱桡肌',
    'anterior_deltoid': '三角肌前束',
    'trapezius': '斜方肌',
    'forearm_flexors': '前臂屈肌',
}


# ========== 独立测试入口 ==========
if __name__ == "__main__":
    model = MuscleModel()
    model.set_exercise('squat')
    model.set_user_params(height_cm=175, weight_kg=70, equipment_kg=0)

    # 模拟深蹲底部的关节角度
    test_joint_data = {
        'angles': {
            'l_knee': 75, 'r_knee': 78,
            'l_hip': 80, 'r_hip': 82,
            'l_elbow': 170, 'r_elbow': 165,
            'l_shoulder': 30, 'r_shoulder': 28,
        },
        'velocities': {
            'l_knee': -15, 'r_knee': -12,
            'l_hip': -10, 'r_hip': -8,
            'l_elbow': 0, 'r_elbow': 0,
            'l_shoulder': 0, 'r_shoulder': 0,
        }
    }

    result = model.compute(test_joint_data)
    print("=== 深蹲底部肌肉激活 ===")
    for m, v in sorted(result['activations'].items(), key=lambda x: -x[1]):
        if v > 0:
            cn = _MUSCLE_CN.get(m, m)
            print(f"  {cn:12s} ({m:20s}): {v:3d}%")
    if result['warnings']:
        print("\n代偿警告:")
        for w in result['warnings']:
            print(f"  {w}")
