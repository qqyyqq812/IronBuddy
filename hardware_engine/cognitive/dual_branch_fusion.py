# coding=utf-8
"""
IronBuddy V4.2 · Dual-Branch Mid-Fusion Model (Skeleton)
========================================================

本文件为 plan §5.2 锁定架构的**骨架实现**，仅定义类签名 + 前向契约 + 参数预算红线。
权重加载 / 完整前向逻辑 / KL 异常检测等具体实现推迟到板子连接后。

架构契约（来自 plan §1 锁定决策 #2 + §5.2）：
- rep-level 触发（非 frame-level）
- embedding-level 中融合（非 logit-level 晚融合）
- Encoder 输出 **8d**（不是 32d）
- 融合头输入 = 8 (vision) + 8 (emg) + **5 个手工标量** = **21 维**
- 分类器 `Linear(21, 3) = 66 参数`（欠拟合才解锁到 `Linear(21, 8) → Linear(8, 3) = 203 参数`）

参数预算红线（LOSO 90 样本/折）：
- 融合头 ≤ 200 参数
- 总可训参数 ≤ 800

5 个手工标量（显式编码 biomechanics prior）：
1. emg_target_comp_ratio - 代偿强度
2. cos_sim(vision_emb, emg_emb) - 模态一致性
3. phase_progress_max - 幅度到位度
4. angular_acc_peak_abs - 弯举甩臂线索
5. torso_forward_tilt_peak - 深蹲弯腰线索

三类标签（plan §2.2）：
0: standard, 1: compensation (跨模态核心靶点), 2: bad_form (幅度不到位)
"""
from __future__ import annotations

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False


# 常量契约（修改需同步 plan §2.3 + main_claw_loop.py）
VISION_INPUT_DIM = 4      # [Angle, Ang_Vel, Ang_Accel, Phase_Progress]
EMG_INPUT_DIM = 7         # [Target_RMS_Norm, Comp_RMS_Norm, Target/Comp_Ratio, MDF, MNF, ZCR, Raw_Unfilt]
EMB_DIM = 8               # Encoder 输出 embedding 维度（不是 32d）
HAND_CRAFTED_DIM = 5      # 手工标量特征数
FUSION_INPUT_DIM = 2 * EMB_DIM + HAND_CRAFTED_DIM  # 21
NUM_CLASSES = 3           # standard / compensation / bad_form

VISION_SEQ_LEN = 30       # 视觉时序帧数（~1 秒 @ 30Hz）
EMG_SEQ_LEN = 200         # EMG 特征点数（1 秒 @ 200ms 窗口累计）

LABEL_NAMES = ['standard', 'compensation', 'bad_form']


# Encoder GRU hidden size tuned to honor plan §5.3 param budget (~600 total).
# hidden=16 (plan's initial spec) gave 2500+ total due to GRU's 3-gate 参数增长. hidden=6 compromise fits 600.
ENCODER_HIDDEN = 6


class VisionEncoder(nn.Module if _HAS_TORCH else object):
    """视觉分支 Encoder（~270 参数 with hidden=6）
    输入: (batch, VISION_SEQ_LEN, VISION_INPUT_DIM)
    输出: vision_emb (batch, EMB_DIM)

    TODO(板连后): 完整前向实现 + 本地 masked AE 预训练加载
    """
    def __init__(self):
        if not _HAS_TORCH:
            raise RuntimeError("PyTorch not installed")
        super().__init__()
        self.gru = nn.GRU(VISION_INPUT_DIM, ENCODER_HIDDEN, batch_first=True)
        self.head = nn.Linear(ENCODER_HIDDEN, EMB_DIM)

    def forward(self, x):
        _, h_n = self.gru(x)
        return self.head(h_n[-1])


class EMGEncoder(nn.Module if _HAS_TORCH else object):
    """EMG 分支 Encoder（~330 参数 with hidden=6）
    输入: (batch, EMG_SEQ_LEN, EMG_INPUT_DIM)
    输出: emg_emb (batch, EMB_DIM)

    TODO(板连后): 完整前向 + 本地 270 rep masked AE 预训练加载
    注意 plan §1 决策 #5: 拒绝 Ninapro/Camargo next-step 预训练（伪迁移）
    """
    def __init__(self):
        if not _HAS_TORCH:
            raise RuntimeError("PyTorch not installed")
        super().__init__()
        self.gru = nn.GRU(EMG_INPUT_DIM, ENCODER_HIDDEN, batch_first=True)
        self.head = nn.Linear(ENCODER_HIDDEN, EMB_DIM)

    def forward(self, x):
        _, h_n = self.gru(x)
        return self.head(h_n[-1])


class HandCraftedFeatureExtractor:
    """手工标量特征提取（0 参数）
    从 rep 原始时序数据计算 5 个显式 biomechanics 线索。
    参见 plan §2.3:
        1. emg_target_comp_ratio
        2. cos_sim(vision_emb, emg_emb)  ← 需两 Encoder 完成后计算
        3. phase_progress_max
        4. angular_acc_peak_abs
        5. torso_forward_tilt_peak

    TODO(板连后): 从 main_claw_loop 缓冲区抽取实际数据
    """
    @staticmethod
    def extract(rep_data, vision_emb=None, emg_emb=None):
        """
        rep_data: dict 含 emg_series / angle_series / phase_progress / torso_angle_series
        vision_emb: tensor (EMB_DIM,) 可选
        emg_emb: tensor (EMB_DIM,) 可选
        返回: tensor (HAND_CRAFTED_DIM,)
        """
        raise NotImplementedError("T4 骨架：板连后实现")


class FusionHead(nn.Module if _HAS_TORCH else object):
    """融合头（66 参数 default，203 参数 deep 模式）
    输入: 21 维拼接 (vision_emb ⊕ emg_emb ⊕ 5 handcrafted)
    输出: 3 类 logits

    参数预算（plan §1 决策 #3）:
      - shallow (default): Linear(21, 3) = 66 参数
      - deep (欠拟合救援): Linear(21, 8) → ReLU → Linear(8, 3) = 203 参数
    """
    def __init__(self, deep: bool = False, dropout: float = 0.3):
        if not _HAS_TORCH:
            raise RuntimeError("PyTorch not installed")
        super().__init__()
        self.deep = deep
        if deep:
            # 203 参数
            self.net = nn.Sequential(
                nn.Dropout(dropout),
                nn.Linear(FUSION_INPUT_DIM, 8),
                nn.ReLU(),
                nn.Dropout(dropout),
                nn.Linear(8, NUM_CLASSES),
            )
        else:
            # 66 参数
            self.net = nn.Sequential(
                nn.Dropout(dropout),
                nn.Linear(FUSION_INPUT_DIM, NUM_CLASSES),
            )

    def forward(self, z):
        # z: (B, 21) → (B, 3) logits
        return self.net(z)


class DualBranchFusionModel(nn.Module if _HAS_TORCH else object):
    """V4.2 完整模型
    架构 plan §5.2:
      Vision Encoder → vision_emb (8d) ─┐
      EMG Encoder    → emg_emb (8d)   ─┤ concat + HandCrafted(5d) = 21d
                                       └─ FusionHead → 3 类 logits

    推理流程 (rep-level)：
      1. FSM 发 rep_complete 信号
      2. 取视觉 30 帧 × 4d + EMG 200 点 × 7d
      3. [并行] 两 Encoder 各出 8d emb
      4. HandCraftedFeatureExtractor 算 5d
      5. concat → FusionHead → logits → softmax → label
      6. 并行: embedding 距离异常检测（cos_sim vs 训练分布 95 百分位）
    """
    def __init__(self, deep_fusion: bool = False):
        if not _HAS_TORCH:
            raise RuntimeError("PyTorch not installed")
        super().__init__()
        self.vision_encoder = VisionEncoder()
        self.emg_encoder = EMGEncoder()
        self.fusion_head = FusionHead(deep=deep_fusion)

    def forward(self, vision_seq, emg_seq, handcrafted):
        # vision_seq: (B, 30, 4); emg_seq: (B, 200, 7); handcrafted: (B, 5)
        v_emb = self.vision_encoder(vision_seq)  # (B, 8)
        e_emb = self.emg_encoder(emg_seq)        # (B, 8)
        z = torch.cat([v_emb, e_emb, handcrafted], dim=-1)  # (B, 21)
        return {
            'logits': self.fusion_head(z),       # (B, 3)
            'vision_emb': v_emb,
            'emg_emb': e_emb,
        }

    def param_count(self):
        """参数预算自检（plan §1 决策 #3 红线: ≤ 800）"""
        return sum(p.numel() for p in self.parameters())


if __name__ == '__main__':
    # Smoke test: model builds + param count under budget
    if not _HAS_TORCH:
        print("PyTorch unavailable - skeleton only")
    else:
        m = DualBranchFusionModel(deep_fusion=False)
        n = m.param_count()
        print(f"V4.2 shallow fusion: {n} params (budget ≤ 800)")
        assert n <= 800, "Param budget exceeded"
        md = DualBranchFusionModel(deep_fusion=True)
        nd = md.param_count()
        print(f"V4.2 deep fusion: {nd} params (rescue mode)")
        print("OK")
