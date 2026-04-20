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
- 分类器 `Linear(21, 3) = 66 参数`（欠拟合才解锁到 `Linear(21, 8) -> Linear(8, 3) = 203 参数`）

参数预算红线（LOSO 90 样本/折）：
- 融合头 <= 200 参数
- 总可训参数 <= 800

5 个手工标量（显式编码 biomechanics prior）：
1. emg_target_comp_ratio - 代偿强度
2. cos_sim(vision_emb, emg_emb) - 模态一致性
3. phase_progress_max - 幅度到位度
4. angular_acc_peak_abs_early - 弯举甩臂线索（前 30% 窗口）
5. torso_forward_tilt_peak - 深蹲弯腰线索

三类标签（plan §2.2）：
0: standard, 1: compensation (跨模态核心靶点), 2: bad_form (幅度不到位)

板端约束：Python 3.7，禁 `X | None` / `match/case` / `:=` / `pandas`。
"""
# 注意：保留 from __future__ 以便板端 3.7 正常 import
from __future__ import absolute_import, division, print_function

import os

try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

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
# hidden=16 gave 2500+ total due to GRU's 3-gate param growth. hidden=6 compromise fits 600.
ENCODER_HIDDEN = 6


# ---------------------------------------------------------------------------
# 手工标量归一化辅助
# ---------------------------------------------------------------------------
def _clip(x, lo, hi):
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def _to_np_array(series):
    """容错：支持 np.ndarray / list / tuple / None；返回 1D np.ndarray (float32)。"""
    if series is None:
        if _HAS_NUMPY:
            return np.zeros((0,), dtype=np.float32)
        return []
    if _HAS_NUMPY and isinstance(series, np.ndarray):
        return series.astype(np.float32, copy=False).ravel()
    # list / tuple / 其他 iterable
    if _HAS_NUMPY:
        return np.asarray(list(series), dtype=np.float32).ravel()
    return list(series)


def _emb_to_tensor(emb):
    """将 (EMB_DIM,) 或 (1, EMB_DIM) 的 Tensor/ndarray/list 规整为 1D Tensor (EMB_DIM,)。"""
    if not _HAS_TORCH:
        raise RuntimeError("PyTorch not installed")
    if isinstance(emb, torch.Tensor):
        t = emb.detach().float()
    elif _HAS_NUMPY and isinstance(emb, np.ndarray):
        t = torch.as_tensor(emb, dtype=torch.float32)
    else:
        t = torch.as_tensor(list(emb), dtype=torch.float32)
    if t.dim() == 2 and t.shape[0] == 1:
        t = t.squeeze(0)
    return t


# ---------------------------------------------------------------------------
# Encoder / FusionHead
# ---------------------------------------------------------------------------
class VisionEncoder(nn.Module if _HAS_TORCH else object):
    """视觉分支 Encoder（~270 参数 with hidden=6）
    输入: (batch, VISION_SEQ_LEN, VISION_INPUT_DIM)
    输出: vision_emb (batch, EMB_DIM)
    """
    def __init__(self):
        if not _HAS_TORCH:
            raise RuntimeError("PyTorch not installed")
        super(VisionEncoder, self).__init__()
        self.gru = nn.GRU(VISION_INPUT_DIM, ENCODER_HIDDEN, batch_first=True)
        self.head = nn.Linear(ENCODER_HIDDEN, EMB_DIM)

    def forward(self, x):
        _, h_n = self.gru(x)
        return self.head(h_n[-1])


class EMGEncoder(nn.Module if _HAS_TORCH else object):
    """EMG 分支 Encoder（~330 参数 with hidden=6）
    输入: (batch, EMG_SEQ_LEN, EMG_INPUT_DIM)
    输出: emg_emb (batch, EMB_DIM)

    注意 plan §1 决策 #5: 拒绝 Ninapro/Camargo next-step 预训练（伪迁移）
    """
    def __init__(self):
        if not _HAS_TORCH:
            raise RuntimeError("PyTorch not installed")
        super(EMGEncoder, self).__init__()
        self.gru = nn.GRU(EMG_INPUT_DIM, ENCODER_HIDDEN, batch_first=True)
        self.head = nn.Linear(ENCODER_HIDDEN, EMB_DIM)

    def forward(self, x):
        _, h_n = self.gru(x)
        return self.head(h_n[-1])


class HandCraftedFeatureExtractor(object):
    """手工标量特征提取（0 参数）
    从 rep 原始时序数据计算 5 个显式 biomechanics 线索。

    rep_data 期望 keys（全部允许缺失 / 长度 0，自动降级为 0）：
      - emg_target_series (np.ndarray (T,))        Target_RMS_Norm
      - emg_comp_series   (np.ndarray (T,))        Comp_RMS_Norm
      - angle_series      (np.ndarray (Tv,))       关节角度
      - phase_progress    (np.ndarray (Tv,))       0-1
      - ang_accel_series  (np.ndarray (Tv,))       角加速度
      - torso_tilt_series (np.ndarray (Tv,))       躯干前倾（弯举填零数组）

    vision_emb / emg_emb: (EMB_DIM,) 或 (1, EMB_DIM) Tensor/ndarray/list
    返回：torch.Tensor shape (HAND_CRAFTED_DIM=5,) float32
    """
    @staticmethod
    def extract(rep_data, vision_emb, emg_emb):
        if not _HAS_TORCH:
            raise RuntimeError("PyTorch not installed")
        if not _HAS_NUMPY:
            raise RuntimeError("NumPy not installed")

        # --- 1. emg_target_comp_ratio ---------------------------------------
        target = _to_np_array(rep_data.get('emg_target_series'))
        comp = _to_np_array(rep_data.get('emg_comp_series'))
        if target.size == 0 or comp.size == 0:
            ratio_raw = 0.0
        else:
            t_max = float(target.max())
            c_max = float(comp.max())
            ratio_raw = t_max / (c_max + 1e-6)
        # clip 到 [0, 5]，归一化到 [0, 1]
        ratio_norm = _clip(ratio_raw, 0.0, 5.0) / 5.0

        # --- 2. cos_sim(vision_emb, emg_emb) --------------------------------
        v_t = _emb_to_tensor(vision_emb)
        e_t = _emb_to_tensor(emg_emb)
        cos_sim = F.cosine_similarity(v_t.unsqueeze(0), e_t.unsqueeze(0), dim=1).item()
        cos_sim = _clip(cos_sim, -1.0, 1.0)

        # --- 3. phase_progress_max ------------------------------------------
        phase = _to_np_array(rep_data.get('phase_progress'))
        if phase.size == 0:
            phase_max = 0.0
        else:
            phase_max = float(phase.max())
        phase_max = _clip(phase_max, 0.0, 1.0)

        # --- 4. angular_acc_peak_abs_early（前 30% 窗口）---------------------
        ang_accel = _to_np_array(rep_data.get('ang_accel_series'))
        if ang_accel.size == 0:
            ang_peak_raw = 0.0
        else:
            cutoff = max(1, int(ang_accel.size * 0.3))
            ang_peak_raw = float(np.abs(ang_accel[:cutoff]).max())
        # 单位 deg/s^2，归一化 / 10 再 clip [0, 1]
        ang_peak_norm = _clip(ang_peak_raw / 10.0, 0.0, 1.0)

        # --- 5. torso_forward_tilt_peak（弯举 → 全零数组 → 0）----------------
        torso = _to_np_array(rep_data.get('torso_tilt_series'))
        if torso.size == 0:
            torso_peak_raw = 0.0
        else:
            torso_peak_raw = float(torso.max())
        # 归一化 / 90 clip [0, 1]
        torso_peak_norm = _clip(torso_peak_raw / 90.0, 0.0, 1.0)

        vec = torch.tensor([
            ratio_norm,
            cos_sim,
            phase_max,
            ang_peak_norm,
            torso_peak_norm,
        ], dtype=torch.float32)
        return vec


class FusionHead(nn.Module if _HAS_TORCH else object):
    """融合头（66 参数 default，203 参数 deep 模式）
    输入: 21 维拼接 (vision_emb ⊕ emg_emb ⊕ 5 handcrafted)
    输出: 3 类 logits

    参数预算（plan §1 决策 #3）:
      - shallow (default): Linear(21, 3) = 66 参数
      - deep (欠拟合救援): Linear(21, 8) -> ReLU -> Linear(8, 3) = 203 参数
    """
    def __init__(self, deep=False, dropout=0.3):
        if not _HAS_TORCH:
            raise RuntimeError("PyTorch not installed")
        super(FusionHead, self).__init__()
        self.deep = deep
        if deep:
            self.net = nn.Sequential(
                nn.Dropout(dropout),
                nn.Linear(FUSION_INPUT_DIM, 8),
                nn.ReLU(),
                nn.Dropout(dropout),
                nn.Linear(8, NUM_CLASSES),
            )
        else:
            self.net = nn.Sequential(
                nn.Dropout(dropout),
                nn.Linear(FUSION_INPUT_DIM, NUM_CLASSES),
            )

    def forward(self, z):
        return self.net(z)


class DualBranchFusionModel(nn.Module if _HAS_TORCH else object):
    """V4.2 完整模型
    架构 plan §5.2:
      Vision Encoder -> vision_emb (8d) -\\
      EMG Encoder    -> emg_emb (8d)   -+ concat + HandCrafted(5d) = 21d
                                       -/
      FusionHead -> 3 类 logits
    """
    def __init__(self, deep_fusion=False):
        if not _HAS_TORCH:
            raise RuntimeError("PyTorch not installed")
        super(DualBranchFusionModel, self).__init__()
        self.vision_encoder = VisionEncoder()
        self.emg_encoder = EMGEncoder()
        self.fusion_head = FusionHead(deep=deep_fusion)

    def forward(self, vision_seq, emg_seq, handcrafted):
        v_emb = self.vision_encoder(vision_seq)
        e_emb = self.emg_encoder(emg_seq)
        z = torch.cat([v_emb, e_emb, handcrafted], dim=-1)
        return {
            'logits': self.fusion_head(z),
            'vision_emb': v_emb,
            'emg_emb': e_emb,
        }

    def param_count(self):
        """参数预算自检（plan §1 决策 #3 红线: <= 800）"""
        return sum(p.numel() for p in self.parameters())

    def load_pretrained_encoders(self, weights_dir):
        """尝试加载本地 masked-AE 预训练 Encoder 权重。

        weights_dir 下期望两个文件:
          - vision_encoder_local.pt
          - emg_encoder_local.pt

        返回 True 表示全部加载成功；False 表示任一缺失（此时 Linear/GRU 用 xavier 初始化）。
        """
        if not _HAS_TORCH:
            raise RuntimeError("PyTorch not installed")

        vision_path = os.path.join(weights_dir, 'vision_encoder_local.pt')
        emg_path = os.path.join(weights_dir, 'emg_encoder_local.pt')

        if os.path.isfile(vision_path) and os.path.isfile(emg_path):
            try:
                v_state = torch.load(vision_path, map_location='cpu')
                e_state = torch.load(emg_path, map_location='cpu')
                self.vision_encoder.load_state_dict(v_state)
                self.emg_encoder.load_state_dict(e_state)
                print("[DualBranchFusionModel] loaded pretrained encoders from %s" % weights_dir)
                return True
            except Exception as ex:
                print("[DualBranchFusionModel] WARN: load failed (%s), falling back to xavier init" % ex)

        # 任一缺失 or 加载失败 → xavier 初始化
        print("[DualBranchFusionModel] WARN: pretrained encoders not found in %s, "
              "using xavier_uniform_ init" % weights_dir)
        for m in list(self.vision_encoder.modules()) + list(self.emg_encoder.modules()):
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight)
                if m.bias is not None:
                    nn.init.zeros_(m.bias)
            elif isinstance(m, nn.GRU):
                for name, param in m.named_parameters():
                    if 'weight' in name:
                        nn.init.xavier_uniform_(param)
                    elif 'bias' in name:
                        nn.init.zeros_(param)
        return False


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    if not _HAS_TORCH:
        print("PyTorch unavailable - skeleton only")
    else:
        m = DualBranchFusionModel(deep_fusion=False)
        n = m.param_count()
        print("V4.2 shallow fusion: %d params (budget <= 800)" % n)
        assert n <= 800, "Param budget exceeded"

        md = DualBranchFusionModel(deep_fusion=True)
        nd = md.param_count()
        print("V4.2 deep fusion: %d params (rescue mode)" % nd)

        # --- forward smoke test ---
        B = 2
        vision_seq = torch.randn(B, VISION_SEQ_LEN, VISION_INPUT_DIM)
        emg_seq = torch.randn(B, EMG_SEQ_LEN, EMG_INPUT_DIM)
        handcrafted = torch.randn(B, HAND_CRAFTED_DIM)
        out = m(vision_seq, emg_seq, handcrafted)
        assert out['logits'].shape == (B, NUM_CLASSES), \
            "logits shape mismatch: %s" % (out['logits'].shape,)
        assert out['vision_emb'].shape == (B, EMB_DIM), \
            "vision_emb shape mismatch: %s" % (out['vision_emb'].shape,)
        assert out['emg_emb'].shape == (B, EMB_DIM), \
            "emg_emb shape mismatch: %s" % (out['emg_emb'].shape,)
        print("forward OK: logits=%s vision_emb=%s emg_emb=%s" % (
            tuple(out['logits'].shape),
            tuple(out['vision_emb'].shape),
            tuple(out['emg_emb'].shape),
        ))

        # --- HandCraftedFeatureExtractor smoke test ---
        if _HAS_NUMPY:
            rep_data = {
                'emg_target_series': np.random.rand(200).astype(np.float32),
                'emg_comp_series': np.random.rand(200).astype(np.float32) * 0.5,
                'angle_series': np.linspace(0, 120, 30, dtype=np.float32),
                'phase_progress': np.linspace(0, 1, 30, dtype=np.float32),
                'ang_accel_series': np.random.randn(30).astype(np.float32) * 5,
                'torso_tilt_series': np.random.rand(30).astype(np.float32) * 15,
            }
            v_emb_sample = torch.randn(EMB_DIM)
            e_emb_sample = torch.randn(1, EMB_DIM)  # 测试 (1, D) 容错
            hc = HandCraftedFeatureExtractor.extract(rep_data, v_emb_sample, e_emb_sample)
            assert hc.shape == (HAND_CRAFTED_DIM,), \
                "handcrafted shape mismatch: %s" % (hc.shape,)
            assert hc.dtype == torch.float32, "handcrafted dtype should be float32"
            print("handcrafted OK: %s values=%s" % (tuple(hc.shape), hc.tolist()))

            # 空输入容错
            empty_rep = {
                'emg_target_series': np.zeros((0,), dtype=np.float32),
                'emg_comp_series': [],
                'phase_progress': None,
                'ang_accel_series': np.zeros((0,), dtype=np.float32),
                'torso_tilt_series': np.zeros(10, dtype=np.float32),
            }
            hc2 = HandCraftedFeatureExtractor.extract(empty_rep, v_emb_sample, e_emb_sample)
            assert hc2.shape == (HAND_CRAFTED_DIM,)
            print("handcrafted empty-input OK: %s" % hc2.tolist())

        # --- load_pretrained_encoders(nonexistent) smoke test ---
        ok = m.load_pretrained_encoders("/nonexistent/path_for_test_ironbuddy_v42")
        assert ok is False, "nonexistent path must return False"
        print("load_pretrained_encoders(bad path) -> False, no crash. OK")

        print("ALL SMOKE TESTS PASSED")
