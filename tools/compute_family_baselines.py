# coding=utf-8
"""
HSBI Biceps sEMG 家族统计基线计算（弯举 Only）
==============================================

flex-curl-only-pivot plan §1.6 + §4 J1/J2 判据

**用途级别**：L3 only（红线）
- [OK] 合法：计算 MDF/MNF/RMS 分布作为弯举 J1/J2 判据基线
- [OK] 合法：抽取高频噪声谱（由其他脚本读本输出 + 原始 CSV）
- [NO] 禁止：用于 Encoder next-step 预训练（伪迁移）
- [NO] 禁止：用于任何模型权重初始化

流程：
1. 扫描 data/external/hsbi_biceps/S*/{20,50,75}MVC/*.csv（推断结构；
   若压缩包解压结构不同，函数内有降级 glob 兜底）
2. 单通道 EMG → 去直流 → 20-450 Hz 带通 → Welch PSD → MDF/MNF/RMS
3. 取 50%MVC（中等负载）作为弯举 baseline 主分布
4. 产出 docs/research/family_baselines.json：
   {"curl": {...}, "generated_at": "...", "protocol_note": "..."}

历史备注：2026-04-18 弯举 Only 转向后，Camargo 深蹲 + Ninapro 手势均淘汰。

使用：
    python tools/compute_family_baselines.py
    python tools/compute_family_baselines.py --force-rewrite
    python tools/compute_family_baselines.py --hsbi-dir path/to/data
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np

# 尝试导入 scipy，若失败也能跑（只会给出占位 JSON）
try:
    from scipy.signal import butter, sosfiltfilt, decimate, welch
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_HSBI_DIR = os.path.join(ROOT, 'data/external/hsbi_biceps')
DEFAULT_OUTPUT = os.path.join(ROOT, 'docs/research/family_baselines.json')

LOG_PREFIX = "[compute_baselines]"

# 信号处理常量
EMG_FS_DEFAULT = 2000  # Hz（HSBI 典型采样率）
EMG_FS_TARGET = 1000   # 降采样后
BANDPASS_LOW = 20      # Hz
BANDPASS_HIGH = 450    # Hz
WINDOW_SEC = 1.0       # 分段窗口（秒）
MIN_SEG_SAMPLES = 512  # PSD 最少样本数


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"{LOG_PREFIX} [WARN] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 信号处理
# ---------------------------------------------------------------------------

def butter_bandpass_sos(low: float, high: float, fs: float, order: int = 4):
    """Butterworth 带通 SOS 滤波器。"""
    nyq = 0.5 * fs
    return butter(order, [low / nyq, high / nyq], btype='band', output='sos')


def preprocess_signal(raw: np.ndarray, fs_in: int = EMG_FS_DEFAULT,
                      fs_out: int = EMG_FS_TARGET) -> Tuple[np.ndarray, int]:
    """去直流 + 可选降采样 + 20-450 Hz 带通。返回 (signal, 实际 fs)。"""
    if not SCIPY_AVAILABLE:
        return raw, fs_in
    x = raw - np.mean(raw)
    fs = fs_in
    # 只在 fs_in > fs_out 时降采样（整数倍）
    if fs_in > fs_out and fs_in % fs_out == 0:
        q = fs_in // fs_out
        x = decimate(x, q=q, ftype='iir', zero_phase=True)
        fs = fs_out
    # Band-pass
    hi = min(BANDPASS_HIGH, fs / 2 - 1)
    if hi <= BANDPASS_LOW:
        return x, fs
    sos = butter_bandpass_sos(BANDPASS_LOW, hi, fs, order=4)
    x = sosfiltfilt(sos, x)
    return x, fs


def compute_freq_features(signal: np.ndarray, fs: float) -> Optional[Dict[str, float]]:
    """
    计算单段信号的 MDF / MNF / RMS。
    MDF: 累计功率到 50% 时的频率
    MNF: ∑(f*P(f)) / ∑P(f)
    RMS: √(mean(x²))
    """
    if len(signal) < MIN_SEG_SAMPLES:
        return None
    if not SCIPY_AVAILABLE:
        return None
    try:
        freqs, psd = welch(signal, fs=fs, nperseg=min(512, len(signal)))
        mask = (freqs >= BANDPASS_LOW) & (freqs <= BANDPASS_HIGH)
        f = freqs[mask]
        p = psd[mask]
        total = np.sum(p)
        if total <= 0 or len(f) == 0:
            return None
        mnf = float(np.sum(f * p) / total)
        cum = np.cumsum(p)
        idx = int(np.searchsorted(cum, total / 2.0))
        idx = min(idx, len(f) - 1)
        mdf = float(f[idx])
        rms = float(np.sqrt(np.mean(signal ** 2)))
        return {"mdf": mdf, "mnf": mnf, "rms": rms}
    except Exception:
        return None


def segment_and_extract(signal: np.ndarray, fs: float,
                        window_sec: float = WINDOW_SEC) -> List[Dict[str, float]]:
    """按 window_sec 切段提取特征。"""
    win = int(window_sec * fs)
    if len(signal) < win:
        return []
    segs = []
    for start in range(0, len(signal) - win + 1, win):
        seg = signal[start:start + win]
        feat = compute_freq_features(seg, fs)
        if feat is not None:
            segs.append(feat)
    return segs


def aggregate_stats(features: List[Dict[str, float]]) -> Dict[str, float]:
    """聚合 features 列表的 mean/std/p50/p95。"""
    if not features:
        return {}
    mdf = np.array([f["mdf"] for f in features])
    mnf = np.array([f["mnf"] for f in features])
    rms = np.array([f["rms"] for f in features])
    # RMS 归一化：除以 p95 作粗糙 MVC 代理
    rms_p95 = float(np.percentile(rms, 95)) if len(rms) > 0 else 1.0
    rms_norm = rms / (rms_p95 + 1e-9)
    # SNR 近似：p95 / p5
    rms_p5 = float(np.percentile(rms, 5)) if len(rms) > 0 else 0.0
    snr_p95 = rms_p95 / (rms_p5 + 1e-6) if rms_p5 > 0 else float('inf')
    if not np.isfinite(snr_p95):
        snr_p95 = 100.0

    return {
        "mdf_mean_hz": float(np.mean(mdf)),
        "mdf_std_hz": float(np.std(mdf)),
        "mnf_mean_hz": float(np.mean(mnf)),
        "mnf_std_hz": float(np.std(mnf)),
        "rms_norm_p50": float(np.percentile(rms_norm, 50)),
        "rms_norm_p95": float(np.percentile(rms_norm, 95)),
        "snr_p95": float(snr_p95),
        "n_samples": int(len(features)),
    }


# ---------------------------------------------------------------------------
# HSBI 加载
# ---------------------------------------------------------------------------

def _hsbi_read_csv(csv_path: str) -> Optional[np.ndarray]:
    """
    读 HSBI 单通道 EMG CSV。真实格式可能是：
      - 单列（纯 EMG 值，每行一个样本）
      - 双列（time, emg）
      - 多列带 header（emg_ch1, ...）
    这里尝试自适应。
    """
    try:
        # 先探 header
        with open(csv_path, 'r', encoding='utf-8', errors='ignore') as f:
            first = f.readline().strip()
        # 判断是否为纯数字行（没 header）
        skip = 0
        has_header = False
        try:
            [float(x) for x in first.split(',')]
        except ValueError:
            has_header = True
            skip = 1

        data = np.loadtxt(csv_path, delimiter=',', skiprows=skip)
        if data.ndim == 1:
            return data.astype(np.float64)
        # 多列：若有 header 且名字里有 'emg'，取该列；否则取最后一列
        if has_header:
            cols = [c.strip() for c in first.split(',')]
            emg_idx = None
            for i, c in enumerate(cols):
                if 'emg' in c.lower() or 'biceps' in c.lower():
                    emg_idx = i
                    break
            if emg_idx is not None:
                return data[:, emg_idx].astype(np.float64)
        # 降级：2 列猜 (time, emg) → 取第 2 列；>=3 列取最后一列
        if data.shape[1] == 2:
            return data[:, 1].astype(np.float64)
        return data[:, -1].astype(np.float64)
    except Exception:
        return None


def load_hsbi(hsbi_dir: str,
              preferred_level: str = '50MVC',
              fs: int = EMG_FS_DEFAULT) -> Optional[Dict[str, float]]:
    """
    加载 HSBI biceps EMG，只用 preferred_level (默认 50%MVC) 的重复做 baseline。
    降级：若 preferred_level 目录里没文件，就接受所有等级的 CSV。

    若目录不存在/空/scipy 不可用，返回 None。
    """
    if not os.path.isdir(hsbi_dir):
        warn(f"HSBI 目录不存在：{hsbi_dir}，跳过弯举基线")
        return None

    # 推断结构 S*/20MVC/*.csv 等
    preferred_glob = os.path.join(hsbi_dir, 'S*', preferred_level, '*.csv')
    csvs = sorted(glob.glob(preferred_glob))
    scope = preferred_level

    if not csvs:
        # 降级 1：任意等级任意 CSV（S*/*/*.csv）
        csvs = sorted(glob.glob(os.path.join(hsbi_dir, 'S*', '*', '*.csv')))
        scope = "any-level"
    if not csvs:
        # 降级 2：递归任意深度
        csvs = sorted(glob.glob(os.path.join(hsbi_dir, '**', '*.csv'),
                                recursive=True))
        # 排除 README 之类的非数据 csv
        csvs = [p for p in csvs if os.path.getsize(p) > 1024]
        scope = "recursive"

    if not csvs:
        warn(f"HSBI 未下载或无 CSV 文件（{hsbi_dir}），跳过弯举基线")
        return None

    if not SCIPY_AVAILABLE:
        warn("scipy 不可用，HSBI 处理跳过")
        return None

    log(f"HSBI 找到 {len(csvs)} 个 CSV（scope={scope}）")

    all_features: List[Dict[str, float]] = []
    files_processed = 0
    # 限制数量防 OOM，真实 11 受试者 * 多重复典型 < 100 个文件够
    for i, path in enumerate(csvs[:100]):
        try:
            raw = _hsbi_read_csv(path)
            if raw is None or len(raw) < MIN_SEG_SAMPLES:
                continue
            # 截取最多 60s（按默认 fs 估）防内存
            max_samples = fs * 60
            if len(raw) > max_samples:
                raw = raw[:max_samples]
            processed, fs_used = preprocess_signal(raw, fs_in=fs,
                                                   fs_out=EMG_FS_TARGET)
            feats = segment_and_extract(processed, fs=fs_used)
            all_features.extend(feats)
            files_processed += 1
            if (i + 1) % 20 == 0:
                log(f"  HSBI 进度：{i + 1}/{min(100, len(csvs))} 文件")
        except Exception as e:
            warn(f"  无法加载 {os.path.basename(path)}："
                 f"{type(e).__name__}: {str(e)[:60]}")

    log(f"HSBI 完成：处理 {files_processed} 文件，提取 {len(all_features)} 段")

    if not all_features:
        warn("HSBI 未提取到任何特征段（格式可能非预期）")
        return None

    stats = aggregate_stats(all_features)
    stats["source"] = "hsbi_biceps"
    stats["scope"] = scope
    stats["channels"] = "biceps_brachii_single_ch"
    return stats


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def _empty_curl_block(note: str) -> Dict:
    return {
        "mdf_mean_hz": None, "mdf_std_hz": None,
        "mnf_mean_hz": None, "mnf_std_hz": None,
        "rms_norm_p50": None, "rms_norm_p95": None,
        "snr_p95": None, "n_samples": 0,
        "source": note,
    }


def build_payload(curl_stats: Optional[Dict]) -> Dict:
    protocol_note = (
        "HSBI is isometric-only; for dynamic curl baseline use FLEX once "
        "available. L3 use only (flex-curl-only-pivot §1.6): do NOT use for "
        "encoder pretraining or transfer learning. Baselines feed J1/J2 "
        "judgment thresholds and noise augmentation only."
    )
    if curl_stats:
        return {
            "curl": curl_stats,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "protocol_note": protocol_note,
            "status": "ok",
        }
    return {
        "curl": _empty_curl_block("hsbi_not_downloaded"),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "protocol_note": protocol_note,
        "status": "empty",
    }


def write_output(payload: Dict, output_path: str, force: bool) -> None:
    """写 JSON 到 output_path。"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if os.path.exists(output_path) and not force:
        log(f"输出文件已存在：{output_path}（用 --force-rewrite 覆盖）")
        return
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    log(f"已写入：{output_path}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute family baselines from HSBI biceps sEMG"
    )
    parser.add_argument('--hsbi-dir', default=DEFAULT_HSBI_DIR)
    parser.add_argument('--output', default=DEFAULT_OUTPUT)
    parser.add_argument('--force-rewrite', action='store_true',
                        help='覆盖已有的 family_baselines.json')
    parser.add_argument('--level', default='50MVC',
                        choices=['20MVC', '50MVC', '75MVC'],
                        help='首选的 %%MVC 等级（默认 50MVC 中等负载）')
    parser.add_argument('--fs', type=int, default=EMG_FS_DEFAULT,
                        help='HSBI 原始采样率（默认 2000 Hz）')
    args = parser.parse_args()

    log("V4.3 family baselines 计算启动（HSBI biceps only / 弯举 Only）")
    log(f"  HSBI dir     : {args.hsbi_dir}")
    log(f"  Preferred lvl: {args.level}")
    log(f"  FS           : {args.fs} Hz")
    log(f"  Output       : {args.output}")
    log("  Usage: L3 only; 拒绝伪迁移 (flex-curl-only-pivot §1.6)")

    if not SCIPY_AVAILABLE:
        warn("scipy 未安装！无法做信号处理，将只输出占位 JSON")
        warn("安装：pip install scipy numpy")
        payload = build_payload(None)
        write_output(payload, args.output, args.force_rewrite)
        return 0

    log("---- Loading HSBI (curl baseline, isometric 50%MVC) ----")
    curl_stats = load_hsbi(args.hsbi_dir, preferred_level=args.level,
                           fs=args.fs)

    # 汇报
    if curl_stats:
        log(f"[curl] MDF {curl_stats['mdf_mean_hz']:.1f} "
            f"± {curl_stats['mdf_std_hz']:.1f} Hz, "
            f"MNF {curl_stats['mnf_mean_hz']:.1f} Hz, "
            f"n={curl_stats['n_samples']}")
    else:
        warn("[curl] 弯举基线缺失（HSBI 未下载）")
        warn("fallback 文献缺省值：MDF 87 ± 15 Hz (Cifrek et al. 2009)")

    payload = build_payload(curl_stats)
    write_output(payload, args.output, args.force_rewrite)

    log("完成。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
