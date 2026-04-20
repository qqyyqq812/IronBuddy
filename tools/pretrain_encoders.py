# coding=utf-8
"""
V4.2 Encoder 本地自监督预训练（Masked AutoEncoder）
===================================================

plan §4.5 Stage 1 + §5.3(c) + .claude/plans/distributed-puzzling-wilkinson.md Agent-C

- 数据源：data/v42/user_*/{curl,squat}/{standard,compensation,bad_form}/rep_*.csv
- 增强  ：时间扭曲 ±10% + 幅度抖动 ±5% +（可选）Ninapro 高频噪声叠加
- 任务  ：随机 mask 30% 时间步，MSE 只在 mask 位置计算
- 产出  ：hardware_engine/cognitive/weights/vision_encoder_local.pt + emg_encoder_local.pt
- 板端  ：CPU 优先；有 CUDA 自动启用。Python 3.7 兼容。

绝对不做（plan §1 决策 #4/#5 红线）：
- Ninapro / Camargo 用作 Encoder 权重初始化或 next-step 预训练
- Ninapro 用于深蹲通道

用法：
    python tools/pretrain_encoders.py --exercise both --epochs 30
    python tools/pretrain_encoders.py --exercise curl --epochs 10          # smoke
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
import time
import warnings

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset

from hardware_engine.cognitive.dual_branch_fusion import (
    VisionEncoder, EMGEncoder,
    VISION_INPUT_DIM, EMG_INPUT_DIM,
    VISION_SEQ_LEN, EMG_SEQ_LEN,
    EMB_DIM,
)

try:
    import scipy.signal  # type: ignore
    import scipy.io  # type: ignore
    _HAS_SCIPY = True
except ImportError:
    _HAS_SCIPY = False

# CSV 列索引（与 sandbox_data_mock.py 的 CSV_HEADER 对齐）
CSV_COLS = {
    'Timestamp': 0, 'Ang_Vel': 1, 'Angle': 2, 'Ang_Accel': 3,
    'Target_RMS_Norm': 4, 'Comp_RMS_Norm': 5, 'Symmetry_Score': 6, 'Phase_Progress': 7,
    'Target_MDF': 8, 'Target_MNF': 9, 'Target_ZCR': 10, 'Target_Raw_Unfilt': 11,
    'label': 12,
}

# 视觉 4 列（按 VisionEncoder 输入顺序）
VISION_COLS = ['Angle', 'Ang_Vel', 'Ang_Accel', 'Phase_Progress']
# EMG 6 列原始 + 第 7 列为 target/comp 派生比
EMG_BASE_COLS = ['Target_RMS_Norm', 'Comp_RMS_Norm', 'Target_MDF', 'Target_MNF', 'Target_ZCR', 'Target_Raw_Unfilt']


# ------------------------------------------------------------------ utilities

def _resample_1d(arr: np.ndarray, new_len: int) -> np.ndarray:
    """线性插值到新长度。"""
    if len(arr) == new_len:
        return arr.astype(np.float32)
    x_old = np.linspace(0.0, 1.0, len(arr))
    x_new = np.linspace(0.0, 1.0, new_len)
    return np.interp(x_new, x_old, arr).astype(np.float32)


def _read_csv_float(path: str):
    """读 CSV（不依赖 pandas），返回 {col_name: np.ndarray}。"""
    with open(path, 'r') as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = [row for row in reader if row]
    if not rows:
        return None
    data = np.array(rows, dtype=float)
    return {name: data[:, i] for i, name in enumerate(header)}


def load_all_reps(data_root: str, exercise: str, source: str = 'local'):
    """扫 rep CSV 构造:
      vision_arr: (N, VISION_SEQ_LEN=30, VISION_INPUT_DIM=4)
      emg_arr   : (N, EMG_SEQ_LEN=200,   EMG_INPUT_DIM=7)

    source='local'（默认，V4.2 兼容）:
      扫 data_root/user_*/<exercise>/*/rep_*.csv，exercise ∈ {'curl','squat','both'}

    source='flex'（V4.3 FLEX 预处理产物）:
      扫 data_root/curl/*/rep_*.csv（data_root 默认 data/flex）。exercise 参数被忽略。
    """
    if source == 'flex':
        # FLEX 预处理产物（tools/flex_preprocess.py 输出）
        patterns = [os.path.join(data_root, 'curl', '*', 'rep_*.csv')]
    elif exercise == 'both':
        patterns = [
            os.path.join(data_root, 'user_*', 'curl', '*', 'rep_*.csv'),
            os.path.join(data_root, 'user_*', 'squat', '*', 'rep_*.csv'),
        ]
    else:
        patterns = [os.path.join(data_root, 'user_*', exercise, '*', 'rep_*.csv')]

    files = []
    for p in patterns:
        files.extend(sorted(glob.glob(p)))

    if not files:
        print('[pretrain_encoders] ❌ 未找到任何 rep CSV，路径: %s' % patterns)
        print('[pretrain_encoders]    请先跑 tools/sandbox_data_mock.py 或完成真数据采集。')
        sys.exit(1)

    vision_list = []
    emg_list = []
    skipped = 0
    for path in files:
        d = _read_csv_float(path)
        if d is None:
            skipped += 1
            continue
        # 视觉 4 列 → 30 长度。Angle 归一化到 [0,1]（0~180°），
        # Ang_Vel/Ang_Accel 按经验尺度除以 500/5000 压到 ~[-1,1]，Phase_Progress 已是 [0,1]。
        try:
            angle = _resample_1d(d['Angle'], VISION_SEQ_LEN) / 180.0
            ang_vel = _resample_1d(d['Ang_Vel'], VISION_SEQ_LEN) / 500.0
            ang_accel = _resample_1d(d['Ang_Accel'], VISION_SEQ_LEN) / 5000.0
            phase = _resample_1d(d['Phase_Progress'], VISION_SEQ_LEN)
        except KeyError as e:
            warnings.warn('CSV %s 缺列 %s，跳过' % (path, e))
            skipped += 1
            continue
        vision_list.append(np.stack([angle, ang_vel, ang_accel, phase], axis=-1))  # (30, 4)

        # EMG 6 原始列 → 200 长度
        try:
            e_base = [_resample_1d(d[name], EMG_SEQ_LEN) for name in EMG_BASE_COLS]
        except KeyError as e:
            warnings.warn('CSV %s 缺 EMG 列 %s，跳过' % (path, e))
            skipped += 1
            continue
        # 第 7 列：派生 target/comp ratio（clip 到 [0, 5]）
        target = e_base[0]
        comp = e_base[1]
        ratio = target / (comp + 1e-6)
        ratio = np.clip(ratio, 0.0, 5.0) / 5.0  # 归一化到 [0, 1]
        e_cols = e_base + [ratio.astype(np.float32)]
        emg_list.append(np.stack(e_cols, axis=-1))  # (200, 7)

    if not vision_list:
        print('[pretrain_encoders] ❌ 所有 CSV 都 skip 了，无法训练。')
        sys.exit(1)

    vision_arr = np.stack(vision_list, axis=0).astype(np.float32)  # (N, 30, 4)
    emg_arr = np.stack(emg_list, axis=0).astype(np.float32)        # (N, 200, 7)
    print('[pretrain_encoders] 载入 rep 数: %d (skip %d)' % (vision_arr.shape[0], skipped))
    print('[pretrain_encoders]   vision shape %s | emg shape %s' % (vision_arr.shape, emg_arr.shape))
    return vision_arr, emg_arr


# ------------------------------------------------------------------ augmentation

def _time_warp(x: np.ndarray, factor: float) -> np.ndarray:
    """对时间轴先重采样到 factor×T，再采回 T（制造轻微时间扭曲）。x: (T, D)。"""
    T, D = x.shape
    new_T = max(4, int(round(T * factor)))
    if _HAS_SCIPY:
        warped = scipy.signal.resample(x, new_T, axis=0)
        back = scipy.signal.resample(warped, T, axis=0)
    else:
        idx = np.linspace(0, T - 1, new_T)
        warped = np.stack([np.interp(idx, np.arange(T), x[:, d]) for d in range(D)], axis=-1)
        back_idx = np.linspace(0, new_T - 1, T)
        back = np.stack([np.interp(back_idx, np.arange(new_T), warped[:, d]) for d in range(D)], axis=-1)
    return back.astype(np.float32)


def _amp_jitter(x: np.ndarray, rng: np.random.RandomState) -> np.ndarray:
    scale = rng.uniform(0.95, 1.05, size=x.shape[-1]).astype(np.float32)
    return (x * scale[None, :]).astype(np.float32)


def _load_ninapro_highfreq_template(external_root: str) -> "np.ndarray | None":
    """尝试读 Ninapro DB2 的 CH2/CH5 通道，抽 >100Hz 高频谱。
    失败则返回 None（上游会 fallback）。
    """
    if not _HAS_SCIPY:
        return None
    mat_files = sorted(glob.glob(os.path.join(external_root, 'ninapro_db2', '*.mat')))
    if not mat_files:
        return None
    try:
        data = scipy.io.loadmat(mat_files[0])
        emg = data.get('emg')
        if emg is None or emg.ndim != 2 or emg.shape[1] < 6:
            return None
        # CH2 + CH5 (index 1 + 4)
        chans = emg[:, [1, 4]].astype(np.float32)
        # 取前 4000 点、FFT 保留 > 100Hz 部分
        sig = chans[:4000]
        # Ninapro DB2 Fs = 2000Hz, 归一化谱幅度作模板
        fs = 2000.0
        freqs = np.fft.rfftfreq(sig.shape[0], d=1.0 / fs)
        fft = np.fft.rfft(sig, axis=0)
        mask = freqs > 100.0
        # 返回幅度谱 (freq_bins_hi, 2)
        return np.abs(fft[mask]).astype(np.float32)
    except Exception as e:
        warnings.warn('Ninapro 载入失败: %s' % e)
        return None


def augment_batch(arr: np.ndarray, multiplier: int, rng: np.random.RandomState,
                  ninapro_template: "np.ndarray | None" = None,
                  is_emg: bool = False) -> np.ndarray:
    """对一个 (N, T, D) 张量做 multiplier 倍扩增。"""
    assert multiplier >= 1
    out = [arr.copy()]
    for _ in range(multiplier - 1):
        copies = []
        for i in range(arr.shape[0]):
            x = arr[i]
            # 时间扭曲
            factor = rng.uniform(0.9, 1.1)
            y = _time_warp(x, factor)
            # 幅度抖动
            y = _amp_jitter(y, rng)
            # EMG + Ninapro 高频噪声叠加（可选）
            if is_emg and ninapro_template is not None:
                # 合成同频谱的高斯噪声：简化方案——抽模板幅度 × 随机相位 → ifft → 实部
                noise_len = y.shape[0]
                freqs = np.fft.rfftfreq(noise_len)
                mag = np.zeros((freqs.shape[0], y.shape[1]), dtype=np.float32)
                # 把 ninapro 模板映射到当前长度
                for d in range(y.shape[1]):
                    # 简化：取模板第 0/1 通道重复覆盖 EMG 7 列
                    src = ninapro_template[:, d % ninapro_template.shape[1]]
                    src_interp = np.interp(
                        np.linspace(0, 1, len(freqs)),
                        np.linspace(0, 1, len(src)),
                        src,
                    )
                    mag[:, d] = src_interp.astype(np.float32)
                phase = rng.uniform(-np.pi, np.pi, size=mag.shape).astype(np.float32)
                spectrum = mag * (np.cos(phase) + 1j * np.sin(phase))
                noise = np.fft.irfft(spectrum, n=noise_len, axis=0).astype(np.float32)
                # 归一到很小的幅度（保持主信号占主导）
                if noise.std() > 1e-6:
                    noise = noise / (noise.std() + 1e-6) * 0.01
                y = y + noise
            copies.append(y)
        out.append(np.stack(copies, axis=0))
    return np.concatenate(out, axis=0).astype(np.float32)


# ------------------------------------------------------------------ Masked AE

class MaskedAE(nn.Module):
    """通用包装：Encoder + 线性解码重建全序列。"""
    def __init__(self, encoder: nn.Module, seq_len: int, input_dim: int):
        super().__init__()
        self.encoder = encoder
        self.seq_len = seq_len
        self.input_dim = input_dim
        self.decoder = nn.Linear(EMB_DIM, seq_len * input_dim)

    def forward(self, x):
        emb = self.encoder(x)  # (B, EMB_DIM)
        rec = self.decoder(emb).view(-1, self.seq_len, self.input_dim)
        return rec, emb


def _mask_input(x: torch.Tensor, ratio: float, rng: np.random.RandomState) -> (torch.Tensor, torch.Tensor):
    """随机把 ratio 的时间步整个置零；返回 masked_input + mask 矩阵 (B, T, 1)。"""
    B, T, D = x.shape
    n_mask = max(1, int(T * ratio))
    mask = torch.zeros(B, T, 1, device=x.device)
    for b in range(B):
        idx = rng.choice(T, n_mask, replace=False)
        mask[b, idx, 0] = 1.0
    masked = x * (1.0 - mask)  # mask==1 处置零
    return masked, mask


def train_masked_ae(name: str, encoder: nn.Module, arr: np.ndarray,
                    seq_len: int, input_dim: int,
                    epochs: int, batch_size: int, lr: float,
                    mask_ratio: float, device: torch.device,
                    rng: np.random.RandomState) -> nn.Module:
    print('[pretrain_encoders] === 训练 %s MaskedAE ===' % name)
    print('  样本: %d | seq_len=%d | input_dim=%d | epochs=%d | batch=%d | lr=%g | mask=%.2f'
          % (arr.shape[0], seq_len, input_dim, epochs, batch_size, lr, mask_ratio))
    ae = MaskedAE(encoder, seq_len=seq_len, input_dim=input_dim).to(device)
    opt = torch.optim.Adam(ae.parameters(), lr=lr)

    tensor = torch.from_numpy(arr).float()
    ds = TensorDataset(tensor)
    loader = DataLoader(ds, batch_size=batch_size, shuffle=True, drop_last=False)

    for ep in range(1, epochs + 1):
        ae.train()
        ep_loss = 0.0
        n_batches = 0
        for (batch,) in loader:
            batch = batch.to(device)
            masked, mask = _mask_input(batch, mask_ratio, rng)
            rec, _emb = ae(masked)
            mask_exp = mask.expand_as(batch)
            diff = (rec - batch) ** 2 * mask_exp
            denom = mask_exp.sum().clamp_min(1.0)
            loss = diff.sum() / denom
            opt.zero_grad()
            loss.backward()
            opt.step()
            ep_loss += float(loss.item())
            n_batches += 1
        avg = ep_loss / max(1, n_batches)
        print('  epoch %02d/%02d  loss=%.5f' % (ep, epochs, avg))

    # Final embedding cosine-similarity distribution（调试）
    ae.eval()
    with torch.no_grad():
        emb = ae.encoder(tensor.to(device))
        # 两两 cos 分布（N 大时只抽 256 个样本）
        n = min(emb.shape[0], 256)
        sub = F.normalize(emb[:n], dim=-1)
        sim = sub @ sub.t()
        iu = torch.triu_indices(n, n, offset=1)
        vals = sim[iu[0], iu[1]].cpu().numpy()
        print('  embedding cos_sim: mean=%.3f std=%.3f min=%.3f max=%.3f' %
              (vals.mean(), vals.std(), vals.min(), vals.max()))

    return ae.encoder


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', choices=['local', 'flex'], default='local',
                    help="数据源：'local' = data/v42 本地采集；'flex' = data/flex FLEX 预处理产物")
    ap.add_argument('--exercise', choices=['curl', 'squat', 'both'], default='both',
                    help="仅 source=local 时有效；source=flex 固定扫 curl")
    ap.add_argument('--data-root', default=None,
                    help="默认 local → data/v42，flex → data/flex")
    ap.add_argument('--external-root', default=os.path.join(ROOT, 'data/external'))
    ap.add_argument('--output-dir', default=os.path.join(ROOT, 'hardware_engine/cognitive/weights'))
    ap.add_argument('--epochs', type=int, default=30)
    ap.add_argument('--batch-size', type=int, default=32)
    ap.add_argument('--lr', type=float, default=1e-3)
    ap.add_argument('--augment-multiplier', type=int, default=3)
    ap.add_argument('--mask-ratio', type=float, default=0.3)
    ap.add_argument('--use-ninapro-noise', action='store_true')
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()

    # 根据 source 默认 data_root
    if args.data_root is None:
        args.data_root = os.path.join(ROOT, 'data', 'flex') if args.source == 'flex' \
            else os.path.join(ROOT, 'data', 'v42')

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    print('[pretrain_encoders] device=%s | torch %s' % (device, torch.__version__))
    print('[pretrain_encoders] source=%s data_root=%s exercise=%s'
          % (args.source, args.data_root, args.exercise))

    rng = np.random.RandomState(args.seed)
    torch.manual_seed(args.seed)

    # 1) 载数据
    t0 = time.time()
    vision_arr, emg_arr = load_all_reps(args.data_root, args.exercise, source=args.source)

    # 2) Ninapro 噪声模板（可选）
    ninapro_template = None
    if args.use_ninapro_noise:
        ninapro_template = _load_ninapro_highfreq_template(args.external_root)
        if ninapro_template is None:
            print('[pretrain_encoders] ⚠ Ninapro 噪声模板加载失败或未启用，跳过该增强。')
        else:
            print('[pretrain_encoders] Ninapro 模板形状 %s' % (ninapro_template.shape,))

    # 3) 增强
    vision_aug = augment_batch(vision_arr, args.augment_multiplier, rng,
                                ninapro_template=None, is_emg=False)
    emg_aug = augment_batch(emg_arr, args.augment_multiplier, rng,
                             ninapro_template=ninapro_template, is_emg=True)
    print('[pretrain_encoders] 增强后 vision=%s emg=%s' % (vision_aug.shape, emg_aug.shape))

    # 4) 训练两 Encoder
    os.makedirs(args.output_dir, exist_ok=True)

    vision_enc = VisionEncoder()
    vision_enc = train_masked_ae(
        name='Vision', encoder=vision_enc, arr=vision_aug,
        seq_len=VISION_SEQ_LEN, input_dim=VISION_INPUT_DIM,
        epochs=args.epochs, batch_size=args.batch_size, lr=args.lr,
        mask_ratio=args.mask_ratio, device=device, rng=rng,
    )
    v_suffix = 'flex_pretrained' if args.source == 'flex' else 'local'
    v_path = os.path.join(args.output_dir, 'vision_encoder_%s.pt' % v_suffix)
    torch.save(vision_enc.state_dict(), v_path)
    print('[pretrain_encoders] 已保存 %s' % v_path)

    emg_enc = EMGEncoder()
    emg_enc = train_masked_ae(
        name='EMG', encoder=emg_enc, arr=emg_aug,
        seq_len=EMG_SEQ_LEN, input_dim=EMG_INPUT_DIM,
        epochs=args.epochs, batch_size=args.batch_size, lr=args.lr,
        mask_ratio=args.mask_ratio, device=device, rng=rng,
    )
    e_path = os.path.join(args.output_dir, 'emg_encoder_%s.pt' % v_suffix)
    torch.save(emg_enc.state_dict(), e_path)
    print('[pretrain_encoders] 已保存 %s' % e_path)

    print('[pretrain_encoders] 完成。总耗时 %.1fs' % (time.time() - t0))


if __name__ == '__main__':
    main()
