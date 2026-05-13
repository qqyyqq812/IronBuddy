"""V7.30 M1 修补测试：Ang_Vel 列推理时归一化对齐训练分布。

训练侧 (tools/train_gru_three_class.py:138) 用 clip(/30, [-3,3]) 归一化 Ang_Vel；
推理侧 (main_claw_loop.py ~1062) 之前漏掉这一列，导致原始 [-15,15] 直接喂模型，
分布偏移 5 倍。本文件测试归一化逻辑本身的不变量。
"""
import numpy as np


def normalize_window(window):
    window = window.copy()
    window[:, 0] = np.clip(window[:, 0] / 30.0, -3.0, 3.0)
    window[:, 1] /= 180.0
    window[:, 2] = np.clip(window[:, 2] / 10.0, -1.0, 1.0)
    window[:, 3] /= 100.0
    window[:, 4] /= 100.0
    return window


def test_ang_vel_in_training_range_after_normalize():
    rng = np.random.default_rng(42)
    window = rng.uniform(-15, 15, (30, 7)).astype(np.float32)
    out = normalize_window(window)
    assert out[:, 0].min() >= -3.0
    assert out[:, 0].max() <= 3.0


def test_extreme_ang_vel_clipped():
    window = np.zeros((30, 7), dtype=np.float32)
    window[:, 0] = 200.0  # 200/30 ≈ 6.67 → clip to 3
    out = normalize_window(window)
    assert (out[:, 0] == 3.0).all()


def test_extreme_negative_ang_vel_clipped():
    window = np.zeros((30, 7), dtype=np.float32)
    window[:, 0] = -200.0
    out = normalize_window(window)
    assert (out[:, 0] == -3.0).all()


def test_other_columns_untouched_in_first_pass():
    window = np.zeros((30, 7), dtype=np.float32)
    window[:, 5] = 1.0
    window[:, 6] = 0.5
    out = normalize_window(window)
    assert (out[:, 5] == 1.0).all()
    assert (out[:, 6] == 0.5).all()
