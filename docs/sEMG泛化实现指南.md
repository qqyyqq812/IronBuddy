# sEMG 泛化实现指南

> 基于 IronBuddy 现有 DSP 管线，解决跨用户、跨负载的 EMG 幅值泛化问题。
> 目标：1-2 天内完成全部改造，无需更换硬件。

---

## 1. 当前问题分析

### 1.1 根因

当前 `udp_emg_server.py` 的归一化逻辑是绝对幅值归一化：

```python
# 现状：硬编码 400.0 作为满量程
rms_mapped = min(100, int((rms / 400.0) * 100))
```

这个 `400.0` 是针对特定人、特定哑铃重量标定的经验值。问题在于：

- **跨用户**：肌肉横截面积差异可导致 sEMG 幅值相差 3–5 倍
- **跨负载**：同一人举 5 kg 和 10 kg 时，肱二头肌 RMS 差异通常超过 2 倍
- **电极贴放位置**：偏移 1 cm 可导致幅值变化 20–40%

结果：同样的"代偿动作"在不同用户身上产生完全不同的 RMS 输出，GRU 模型无法正确分类。

### 1.2 训练数据局限

当前训练 CSV（`train_squat_golden.csv` 等）只有 1–2 人、1 种哑铃重量的数据，特征空间过窄。模型记住的是"这个人做这个重量时的绝对 RMS 值"，而非"肌肉激活模式的相对特征"。

### 1.3 解决思路

两条并行措施：

1. **运行时动态校准**：用户每次训练前做 2–3 次空手弯举，以个人峰值 RMS 为基线，后续所有值归一化到 0–1
2. **频域特征增强**：中位频率（MDF）、平均频率（MNF）、过零率（ZCR）等频域特征受幅值影响小，天然具有跨人泛化性

---

## 2. 推荐方案：频域特征 + 动态峰值归一化

### 2.1 步骤一：信号预处理改进

**现有管线**（保留不动）：

```
原始 ADC → 20 Hz 高通 → 50 Hz 陷波 → 150 Hz 低通 → 100 点滑动 RMS 包络
```

**需要并行增加**（不破坏现有输出）：

新增一路宽带滤波器用于特征提取，与现有输出解耦：

```
原始 ADC → 20–450 Hz 带通 → 全波整流 → 200 ms 窗口缓存 → 特征提取
```

理由：现有 150 Hz 低通截止过低，会丢失 150–450 Hz 的高频成分，而这个区间正是 MDF/MNF 的主要信息带宽。

安装依赖：

```bash
pip install scipy numpy
# 可选增强包（特征验证用）
pip install pyemgpipeline neurokit2
```

### 2.2 步骤二：特征提取（完整代码）

以下代码从一个 200 ms EMG 窗口（1000 Hz 采样率 = 200 个采样点）提取 6 类泛化特征：

```python
# hardware_engine/sensor/emg_feature_extractor.py

import numpy as np
from scipy.signal import welch
from typing import Optional

FS = 1000  # 采样率 Hz
WINDOW_MS = 200  # 特征提取窗口长度
WINDOW_SAMPLES = int(FS * WINDOW_MS / 1000)  # = 200


def extract_emg_features(window: np.ndarray, fs: int = FS) -> dict:
    """
    从一个 EMG 窗口提取泛化特征。

    参数:
        window: 形状 (N,) 的 numpy 数组，已经过带通滤波（20–450 Hz）
        fs: 采样率，默认 1000 Hz

    返回:
        dict，包含 mdf, mnf, zcr, wl，单位已在注释中说明
    """
    n = len(window)
    if n < 16:
        return {"mdf": 0.0, "mnf": 0.0, "zcr": 0.0, "wl": 0.0}

    # ---- 1. 中位频率 MDF（Hz）----
    # 功率谱密度，nperseg 取窗口长度或 256，取较小值
    nperseg = min(n, 256)
    freqs, psd = welch(window, fs=fs, nperseg=nperseg)

    # 只取 20–450 Hz 频段
    band_mask = (freqs >= 20) & (freqs <= 450)
    freqs_band = freqs[band_mask]
    psd_band = psd[band_mask]

    if psd_band.sum() < 1e-12:
        mdf, mnf = 0.0, 0.0
    else:
        cumsum = np.cumsum(psd_band)
        half_power = cumsum[-1] / 2.0
        # MDF：功率谱累积到一半处的频率
        mdf_idx = np.searchsorted(cumsum, half_power)
        mdf_idx = min(mdf_idx, len(freqs_band) - 1)
        mdf = float(freqs_band[mdf_idx])

        # ---- 2. 平均频率 MNF（Hz）----
        # MNF = Σ(f_i × PSD_i) / Σ(PSD_i)
        mnf = float(np.sum(freqs_band * psd_band) / psd_band.sum())

    # ---- 3. 过零率 ZCR（次/秒）----
    # 统计信号穿越零点的次数，再换算成每秒频率
    zero_crossings = np.where(np.diff(np.sign(window)))[0]
    zcr = float(len(zero_crossings) / (n / fs))

    # ---- 4. 波形长度 WL（mV·ms，相对单位）----
    # WL = Σ|x[i] - x[i-1]，表征信号复杂度，负载越重 WL 越大
    wl = float(np.sum(np.abs(np.diff(window))))

    return {
        "mdf": round(mdf, 2),    # Hz，典型范围 60–120 Hz（疲劳时下降）
        "mnf": round(mnf, 2),    # Hz，典型范围 80–150 Hz
        "zcr": round(zcr, 2),    # 次/秒，典型范围 100–400
        "wl":  round(wl, 4),     # 原始单位，需归一化后使用
    }


def compute_coactivation_ratio(
    target_rms_norm: float,
    comp_rms_norm: float,
    epsilon: float = 1e-6
) -> float:
    """
    肌肉协同比：目标肌肉 / 代偿肌肉的归一化 RMS 比值。

    值越高说明目标肌肉主导越强（动作质量好）。
    值越低说明代偿肌肉参与过多（代偿动作）。

    参数均为动态峰值归一化后的值（0–1 范围）。
    """
    return float(target_rms_norm / max(comp_rms_norm, epsilon))


def compute_peak_valley_ratio(rms_envelope: np.ndarray) -> float:
    """
    峰谷时间比：RMS 包络中，高于均值的时间占比。

    反映一次收缩-舒张周期内肌肉激活时间的分布。
    代偿动作通常表现为更短的峰值平台（快速借力）。

    参数:
        rms_envelope: 一次完整动作的 RMS 时间序列（已归一化到 0–1）
    """
    if len(rms_envelope) < 4:
        return 0.5
    threshold = float(np.mean(rms_envelope))
    above = np.sum(rms_envelope > threshold)
    return float(above / len(rms_envelope))
```

### 2.3 步骤三：动态峰值归一化（CalibrationManager 类）

这是泛化的核心机制。用户在每次训练前做 2–3 次空手弯举，系统自动记录每个通道的 RMS 峰值作为该用户在该时刻的基线。

```python
# hardware_engine/sensor/calibration_manager.py

import json
import os
import time
import logging
import numpy as np
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)

CALIB_STATE_PATH = "/dev/shm/emg_calibration.json"
CALIB_DONE_FLAG  = "/dev/shm/emg_calib_done"

# 校准参数
CALIB_WINDOW_SEC   = 5.0   # 校准采集时长（秒）
CALIB_PEAK_PERCENTILE = 95  # 用 95 百分位而非绝对峰值，抗噪声尖刺


class CalibrationManager:
    """
    动态峰值归一化管理器。

    使用方法:
        calib = CalibrationManager(num_channels=2)

        # 校准阶段（用户做 2–3 次空手弯举时调用）
        calib.start_calibration()
        while collecting:
            calib.feed(raw_rms_ch0, raw_rms_ch1)
        calib.finish_calibration()

        # 推理阶段
        norm_ch0, norm_ch1 = calib.normalize(raw_rms_ch0, raw_rms_ch1)
    """

    def __init__(self, num_channels: int = 2):
        self.num_channels = num_channels
        self._is_calibrating = False
        self._calib_buf: list[deque] = [
            deque(maxlen=int(CALIB_WINDOW_SEC * 1000))
            for _ in range(num_channels)
        ]
        # 校准基线：每通道的峰值 RMS（原始单位）
        self._peak_rms: list[float] = [400.0] * num_channels  # 默认值=旧硬编码值
        self._is_calibrated = False
        self._calib_start_time: Optional[float] = None

        # 尝试加载已有校准数据（跨进程持久化）
        self._load_from_shm()

    def start_calibration(self) -> None:
        """开始校准，清空缓冲区。"""
        self._is_calibrating = True
        self._calib_start_time = time.time()
        for buf in self._calib_buf:
            buf.clear()
        # 写入信号文件让前端显示校准提示
        try:
            with open("/dev/shm/emg_calibrating", "w") as f:
                f.write(str(time.time()))
        except OSError:
            pass
        logger.info("[Calib] 开始校准：请做 2–3 次空手弯举")

    def feed(self, *rms_values: float) -> None:
        """
        校准期间持续喂入各通道的实时 RMS 值（原始单位，未归一化）。
        在 dsp_receiver_worker 每个采样点后调用。
        """
        if not self._is_calibrating:
            return
        for ch, rms in enumerate(rms_values[:self.num_channels]):
            self._calib_buf[ch].append(float(rms))

    def finish_calibration(self) -> bool:
        """
        完成校准，计算各通道峰值基线。

        返回:
            True = 校准成功；False = 数据不足（用户没有动）
        """
        self._is_calibrating = False
        try:
            os.remove("/dev/shm/emg_calibrating")
        except OSError:
            pass

        for ch in range(self.num_channels):
            buf = list(self._calib_buf[ch])
            if len(buf) < 100:
                logger.warning(f"[Calib] 通道 {ch} 数据不足（{len(buf)} 点），保留默认基线")
                continue

            arr = np.array(buf, dtype=np.float32)
            peak = float(np.percentile(arr, CALIB_PEAK_PERCENTILE))

            if peak < 5.0:
                logger.warning(f"[Calib] 通道 {ch} 峰值过低（{peak:.1f}），可能传感器未连接")
                continue

            self._peak_rms[ch] = peak
            logger.info(f"[Calib] 通道 {ch} 基线设定为 {peak:.1f}（原始 RMS 单位）")

        self._is_calibrated = True
        self._save_to_shm()

        # 写入完成标志
        try:
            with open(CALIB_DONE_FLAG, "w") as f:
                f.write(json.dumps({"peaks": self._peak_rms, "ts": time.time()}))
        except OSError:
            pass

        return True

    def normalize(self, *rms_values: float) -> list[float]:
        """
        将原始 RMS 值归一化到 0–1 区间。

        使用各通道的校准基线作为分母。
        超过 1.0 的值被截断（允许短暂超越基线）。
        """
        result = []
        for ch, rms in enumerate(rms_values[:self.num_channels]):
            baseline = self._peak_rms[ch]
            norm = float(np.clip(rms / max(baseline, 1e-6), 0.0, 1.2))
            result.append(norm)
        return result

    def normalize_to_pct(self, *rms_values: float) -> list[int]:
        """归一化并映射到 0–100 整数（兼容现有 CURRENT_RMS_PCT 接口）。"""
        norms = self.normalize(*rms_values)
        return [min(100, int(v * 100)) for v in norms]

    @property
    def is_calibrated(self) -> bool:
        return self._is_calibrated

    @property
    def peak_rms(self) -> list[float]:
        return list(self._peak_rms)

    def _save_to_shm(self) -> None:
        try:
            data = {"peaks": self._peak_rms, "calibrated": self._is_calibrated,
                    "ts": time.time()}
            with open(CALIB_STATE_PATH + ".tmp", "w") as f:
                json.dump(data, f)
            os.rename(CALIB_STATE_PATH + ".tmp", CALIB_STATE_PATH)
        except OSError:
            pass

    def _load_from_shm(self) -> None:
        try:
            if os.path.exists(CALIB_STATE_PATH):
                with open(CALIB_STATE_PATH, "r") as f:
                    data = json.load(f)
                peaks = data.get("peaks", [])
                if len(peaks) == self.num_channels:
                    self._peak_rms = [float(p) for p in peaks]
                    self._is_calibrated = bool(data.get("calibrated", False))
                    age = time.time() - data.get("ts", 0)
                    logger.info(f"[Calib] 加载已有校准数据（{age/60:.1f} 分钟前），"
                                f"基线={self._peak_rms}")
        except Exception:
            pass
```

### 2.4 步骤四：修改 udp_emg_server.py

在现有 DSP 管线后追加特征提取，并将特征写入 `/dev/shm/emg_features.json`。

**改动点**：在 `dsp_receiver_worker` 中新增宽带缓冲区，在 `io_dumper_worker` 中新增特征计算和写盘。原有 `muscle_activation.json` 输出格式**保持不变**，仅新增一个独立的特征文件。

```python
# 在 udp_emg_server.py 顶部增加导入
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from emg_feature_extractor import extract_emg_features, compute_coactivation_ratio
from calibration_manager import CalibrationManager

# --- 新增全局状态 ---
# 宽带原始缓冲区（用于特征提取，不经 150Hz 低通）
WIDEBAND_BUF = [deque(maxlen=200), deque(maxlen=200)]  # 200 点 = 200 ms @ 1kHz

# 原始 RMS（未归一化，用于 CalibrationManager）
RAW_RMS = [0.0, 0.0]

# 校准管理器（单例）
_CALIB = CalibrationManager(num_channels=2)
```

修改 `dsp_receiver_worker` 中的 DSP 循环，在原有流程后增加宽带采样：

```python
# 在现有 DSP 循环中，处理完 y3（低通输出）之后，紧接着追加：

# ---- 新增：宽带缓冲（用 y2 = 陷波后输出，跳过 150Hz 低通）----
# y2 已经过 20Hz 高通 + 50Hz 陷波，保留了 20–~500Hz 宽带信号
WIDEBAND_BUF[ch].append(y2)

# 存储原始 RMS（原始单位，未归一化）
raw_rms_val = math.sqrt(max(0, f['sum_sq']) / max(1, len(f['ring'])))
RAW_RMS[ch] = raw_rms_val

# 动态归一化（替代旧的硬编码 400.0）
rms_norm = _CALIB.normalize(raw_rms_val)[0]
rms_mapped = min(100, int(rms_norm * 100))
if rms_mapped < 4:
    rms_mapped = 0
CURRENT_RMS_PCT[ch] = rms_mapped

# 如果正在校准，喂入原始 RMS
if _CALIB._is_calibrating:
    _CALIB.feed(RAW_RMS[0], RAW_RMS[1])
```

在 `io_dumper_worker` 中追加特征计算：

```python
def io_dumper_worker():
    """低频稳固生产者线程（约 33Hz）"""
    while True:
        now = time.time()

        if not IS_CONNECTED:
            try:
                os.remove('/dev/shm/emg_heartbeat')
            except OSError:
                pass
            time.sleep(0.5)
            continue

        target_pct = CURRENT_RMS_PCT[0]
        comp_pct   = CURRENT_RMS_PCT[1]

        # ---- 新增：特征提取（每 33Hz 计算一次）----
        emg_features = {}
        try:
            if len(WIDEBAND_BUF[0]) >= 64:  # 至少 64 点才有意义
                buf0 = np.array(list(WIDEBAND_BUF[0]), dtype=np.float32)
                buf1 = np.array(list(WIDEBAND_BUF[1]), dtype=np.float32)

                feats0 = extract_emg_features(buf0)
                feats1 = extract_emg_features(buf1)

                # 归一化 WL（使用各自峰值 WL 作为参考，这里用简单的滑动最大值）
                # 实际部署时可以在校准阶段同步收集 WL 峰值
                norm_rms0 = target_pct / 100.0
                norm_rms1 = comp_pct / 100.0

                emg_features = {
                    "mdf_target":  feats0["mdf"],
                    "mdf_comp":    feats1["mdf"],
                    "mnf_target":  feats0["mnf"],
                    "mnf_comp":    feats1["mnf"],
                    "zcr_target":  feats0["zcr"],
                    "zcr_comp":    feats1["zcr"],
                    "wl_target":   feats0["wl"],
                    "wl_comp":     feats1["wl"],
                    "coactivation_ratio": compute_coactivation_ratio(norm_rms0, norm_rms1),
                    "calibrated":  _CALIB.is_calibrated,
                    "peak_baseline": _CALIB.peak_rms,
                }
        except Exception as feat_err:
            logging.debug(f"特征提取失败: {feat_err}")

        # 写入特征文件（GRU 主循环读取）
        if emg_features:
            try:
                with open('/dev/shm/emg_features.json.tmp', 'w') as f:
                    json.dump(emg_features, f)
                os.rename('/dev/shm/emg_features.json.tmp', '/dev/shm/emg_features.json')
            except Exception:
                pass

        # ---- 以下为原有逻辑，保持不变 ----
        warnings = []
        exercise = "squat"
        try:
            if os.path.exists('/dev/shm/user_profile.json'):
                with open('/dev/shm/user_profile.json', 'r') as f:
                    exercise = json.load(f).get('exercise', 'squat')
        except Exception:
            pass

        if exercise == "bicep_curl":
            acts = {"quadriceps": comp_pct, "glutes": comp_pct,
                    "calves": 0, "biceps": target_pct}
        else:
            acts = {"quadriceps": target_pct, "glutes": target_pct,
                    "calves": 0, "biceps": comp_pct}

        out = {"activations": acts, "warnings": warnings, "exercise": exercise}
        try:
            with open('/dev/shm/muscle_activation.json.tmp', 'w') as f:
                json.dump(out, f)
            os.rename('/dev/shm/muscle_activation.json.tmp', '/dev/shm/muscle_activation.json')
            with open('/dev/shm/emg_heartbeat', 'w') as f:
                f.write(str(now))
        except Exception:
            pass

        time.sleep(0.03)
```

### 2.5 步骤五：修改 fusion_model.py 的输入维度（7D → 11D）

**当前 7D 特征**：`Ang_Vel, Angle, Ang_Accel, Target_RMS, Comp_RMS, Symmetry_Score, Phase_Progress`

**新增 4D 频域特征**：`MDF_target, MDF_comp, ZCR_target, CoactivationRatio`

在 `fusion_model.py` 的常量区域追加：

```python
# fusion_model.py 常量区，追加以下内容

FEATURES_11D = [
    'Ang_Vel', 'Angle', 'Ang_Accel', 'Target_RMS', 'Comp_RMS',
    'Symmetry_Score', 'Phase_Progress',
    # 新增频域泛化特征
    'MDF_target', 'MDF_comp', 'ZCR_target', 'CoactivationRatio',
]

# 各特征的归一化参数（用于推理时的实时归一化）
FEATURE_11D_NORM = {
    'Ang_Vel':           {'scale': 20.0,   'clip': (-1.0, 1.0)},
    'Angle':             {'scale': 180.0,  'clip': (0.0, 1.0)},
    'Ang_Accel':         {'scale': 10.0,   'clip': (-1.0, 1.0)},
    'Target_RMS':        {'scale': 100.0,  'clip': (0.0, 1.0)},
    'Comp_RMS':          {'scale': 100.0,  'clip': (0.0, 1.0)},
    'Symmetry_Score':    {'scale': 1.0,    'clip': (0.0, 1.0)},
    'Phase_Progress':    {'scale': 1.0,    'clip': (0.0, 1.0)},
    'MDF_target':        {'scale': 200.0,  'clip': (0.0, 1.0)},  # 典型最大 200 Hz
    'MDF_comp':          {'scale': 200.0,  'clip': (0.0, 1.0)},
    'ZCR_target':        {'scale': 500.0,  'clip': (0.0, 1.0)},  # 典型最大 500 次/秒
    'CoactivationRatio': {'scale': 5.0,    'clip': (0.0, 1.0)},  # 超过 5 倍视为满值
}
```

修改 `CompensationGRU.__init__` 使其支持 11D（向后兼容 7D 和 4D）：

```python
class CompensationGRU(nn.Module):
    def __init__(self, input_size: int = 11, hidden_size: int = 16, num_layers: int = 1):
        super().__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size

        # 兼容旧模型：4D 或 7D 输入通过线性投影升维到 11D
        self._needs_proj = (input_size != 11)
        if self._needs_proj:
            self.input_proj = nn.Linear(input_size, 11)

        # GRU 内部维度从 7 改为 11
        self.gru = nn.GRU(11, hidden_size, num_layers, batch_first=True)

        # 以下各 head 保持不变
        self.golden_embed = nn.Parameter(torch.randn(hidden_size))
        self.sim_head = nn.Sequential(
            nn.Linear(hidden_size + 1, 8),
            nn.ReLU(),
            nn.Linear(8, 1),
            nn.Sigmoid(),
        )
        self.cls_head   = nn.Linear(hidden_size, 3)
        self.phase_head = nn.Linear(hidden_size, 4)
```

在 `main_claw_loop.py` 中修改特征组装（在 `_gru_feature_buf.append(...)` 处）：

```python
# main_claw_loop.py 中，GRU 特征组装部分

# 读取频域特征
mdf_target, mdf_comp, zcr_target, coact_ratio = 0.0, 0.0, 0.0, 1.0
try:
    if os.path.exists("/dev/shm/emg_features.json"):
        with open("/dev/shm/emg_features.json", "r") as ef:
            efd = json.load(ef)
            # 归一化到 0–1（与 FEATURE_11D_NORM 对应）
            mdf_target  = float(efd.get("mdf_target", 0.0)) / 200.0
            mdf_comp    = float(efd.get("mdf_comp", 0.0)) / 200.0
            zcr_target  = float(efd.get("zcr_target", 0.0)) / 500.0
            coact_ratio = min(1.0, float(efd.get("coactivation_ratio", 1.0)) / 5.0)
except Exception:
    pass

_gru_feature_buf.append([
    ang_vel, angle, ang_accel,
    target_emg, comp_emg,
    1.0,         # Symmetry_Score placeholder
    phase_prog,
    # 新增 4 个频域特征
    mdf_target,
    mdf_comp,
    zcr_target,
    coact_ratio,
])
```

同时修改模型加载时的 `input_size`：

```python
# main_claw_loop.py 中 _load_gru_model()
model = load_model(path, input_size=11)  # 从 7 改为 11
```

---

## 3. 推荐工具安装

```bash
# 核心依赖（已在项目中使用）
pip install numpy scipy

# 可选：EMG 特征验证和对比实验
pip install pyemgpipeline neurokit2

# pyemgpipeline 提供标准化的 EMG 预处理管线，可用于验证自实现特征的正确性
# neurokit2 提供 nk.emg_process() 快速原型验证
```

验证安装：

```python
import scipy.signal
import numpy as np
# 生成测试信号
t = np.linspace(0, 0.2, 200)
test_sig = np.sin(2 * np.pi * 80 * t) + 0.1 * np.random.randn(200)
from emg_feature_extractor import extract_emg_features
feats = extract_emg_features(test_sig)
print(feats)  # 期望 mdf ≈ 80 Hz
```

---

## 4. 数据采集建议

### 4.1 采集方案

每次数据采集需同步记录**用户体重**和**哑铃重量**，以支持后续跨负载分析。

```bash
# 在 main_claw_loop.py 的 CSV 写入处增加 metadata 列
# CSV 格式从现有的：
# Timestamp,Ang_Vel,Angle,Target_RMS,Comp_RMS
# 扩展为：
# Timestamp,Ang_Vel,Angle,Ang_Accel,Target_RMS,Comp_RMS,Symmetry_Score,Phase_Progress,MDF_target,MDF_comp,ZCR_target,CoactRatio,UserWeight,DumbbellWeight

# 在 CSV 写入代码中追加：
user_weight    = 70.0  # 从 user_profile.json 读取
dumbbell_weight = 0.0  # 从 user_profile.json 读取

with open(csv_file, "a") as csvf:
    if not exists:
        csvf.write("Timestamp,Ang_Vel,Angle,Ang_Accel,Target_RMS,Comp_RMS,"
                   "Symmetry_Score,Phase_Progress,MDF_target,MDF_comp,"
                   "ZCR_target,CoactRatio,UserWeight,DumbbellWeight\n")
    csvf.write(
        f"{time.time():.3f},{ang_vel:.2f},{angle:.2f},{ang_accel:.2f},"
        f"{target_emg:.2f},{comp_emg:.2f},1.0,{phase_prog:.3f},"
        f"{mdf_target:.4f},{mdf_comp:.4f},{zcr_target:.4f},{coact_ratio:.4f},"
        f"{user_weight:.1f},{dumbbell_weight:.1f}\n"
    )
```

### 4.2 采集协议（1–2 人即可验证泛化效果）

| 轮次 | 哑铃重量 | 动作质量 | 次数 | record_mode |
|------|----------|----------|------|-------------|
| 1    | 空手     | 标准     | 10   | `golden`    |
| 2    | 空手     | 代偿     | 5    | `lazy`      |
| 3    | 轻（5 kg）| 标准    | 10   | `golden`    |
| 4    | 轻（5 kg）| 代偿    | 5    | `lazy`      |
| 5    | 重（10 kg）| 标准   | 10   | `golden`    |
| 6    | 重（10 kg）| 代偿   | 5    | `lazy`      |

每轮采集前执行一次校准（2–3 次空手弯举）。

**触发采集**（与现有机制兼容）：

```bash
# 开始录制标准动作
echo "golden" > /dev/shm/record_mode

# 开始录制代偿动作
echo "lazy" > /dev/shm/record_mode

# 停止录制
rm /dev/shm/record_mode
```

---

## 5. 验证步骤

### 5.1 第一阶段：特征有效性验证

用 RandomForest 快速验证频域特征是否确实携带跨负载信息：

```python
# validate_generalization.py
# 放在项目根目录执行：python validate_generalization.py

import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import LeaveOneGroupOut
from sklearn.metrics import accuracy_score
import glob
import os

# 加载所有 CSV
dfs = []
for pattern in ["train_squat_golden.csv", "train_squat_lazy.csv"]:
    for fpath in glob.glob(f"hardware_engine/{pattern}"):
        df = pd.read_csv(fpath)
        label = 0 if "golden" in fpath else 1
        df["label"] = label
        dfs.append(df)

if not dfs:
    print("未找到训练 CSV，请先采集数据。")
    exit(1)

data = pd.concat(dfs, ignore_index=True)
print(f"总样本数: {len(data)}")

# ---- 实验 A：只用幅值特征（旧方案）----
amp_features = ["Target_RMS", "Comp_RMS"]
X_amp = data[amp_features].fillna(0).values
y = data["label"].values
groups = data.get("DumbbellWeight", pd.Series(np.zeros(len(data)))).values

logo = LeaveOneGroupOut()
scores_amp = []
for train_idx, test_idx in logo.split(X_amp, y, groups):
    clf = RandomForestClassifier(n_estimators=50, random_state=42)
    clf.fit(X_amp[train_idx], y[train_idx])
    pred = clf.predict(X_amp[test_idx])
    scores_amp.append(accuracy_score(y[test_idx], pred))

# ---- 实验 B：加入频域特征（新方案）----
freq_features = ["Target_RMS", "Comp_RMS", "MDF_target", "MDF_comp",
                 "ZCR_target", "CoactRatio"]
available = [c for c in freq_features if c in data.columns]
X_freq = data[available].fillna(0).values

scores_freq = []
for train_idx, test_idx in logo.split(X_freq, y, groups):
    clf = RandomForestClassifier(n_estimators=50, random_state=42)
    clf.fit(X_freq[train_idx], y[train_idx])
    pred = clf.predict(X_freq[test_idx])
    scores_freq.append(accuracy_score(y[test_idx], pred))

print(f"\n留一法跨负载准确率（Leave-One-Weight-Out）:")
print(f"  仅幅值特征:         {np.mean(scores_amp)*100:.1f}% ± {np.std(scores_amp)*100:.1f}%")
print(f"  幅值 + 频域特征:    {np.mean(scores_freq)*100:.1f}% ± {np.std(scores_freq)*100:.1f}%")

if np.mean(scores_freq) >= 0.80:
    print("\n✓ 验证通过：跨负载准确率 >= 80%，可以用新特征重新训练 GRU")
else:
    print("\n✗ 准确率不足 80%，建议增加采集量或检查校准步骤")
```

### 5.2 第二阶段：重新训练 GRU

验证通过后，用 11D 特征重新训练：

```bash
cd hardware_engine
python cognitive/fusion_model.py .
# 输出 extreme_fusion_gru.pt（约 50–100 KB）
```

### 5.3 第三阶段：在线推理验证

重启主循环，观察 GRU 日志：

```bash
cd hardware_engine
python main_claw_loop.py

# 期望日志：
# [GRU] 第1个动作判定: 相似度=0.823 分类=标准 置信度=0.912
# [GRU] 第2个动作判定: 相似度=0.341 分类=代偿 置信度=0.876
```

如果推理结果混乱（相似度全在 0.5 附近），检查：
1. 校准是否完成（`/dev/shm/emg_calib_done` 是否存在）
2. `emg_features.json` 中的 MDF 值是否合理（期望 60–150 Hz）
3. GRU 模型文件是否是用 11D 特征重新训练的

---

## 6. 改动文件清单

| 操作 | 文件 | 改动摘要 |
|------|------|----------|
| 新建 | `hardware_engine/sensor/emg_feature_extractor.py` | MDF/MNF/ZCR/WL 特征提取 |
| 新建 | `hardware_engine/sensor/calibration_manager.py` | 动态峰值归一化管理器 |
| 修改 | `hardware_engine/sensor/udp_emg_server.py` | 引入校准管理器，追加特征写盘 |
| 修改 | `hardware_engine/cognitive/fusion_model.py` | 输入维度 7→11，追加 FEATURES_11D |
| 修改 | `hardware_engine/main_claw_loop.py` | 读取频域特征，组装 11D 特征向量 |
| 新建 | `validate_generalization.py`（项目根目录）| 跨负载验证脚本 |

---

## 7. 常见问题

**Q: 校准完成后 RMS 值全是 0？**
A: 检查 `_CALIB.peak_rms`，如果峰值 < 5.0，说明校准期间用户没有产生足够大的肌电信号。让用户用力做弯举，而不是轻微动作。

**Q: MDF 值全是 0？**
A: `WIDEBAND_BUF` 积累的数据不足 64 点。确认 `WIDEBAND_BUF[ch].append(y2)` 在 DSP 循环中正确执行，`y2` 是陷波后、低通前的输出。

**Q: 新训练的 GRU 模型加载失败？**
A: 旧模型权重形状是 `gru.weight_ih_l0: [48, 7]`，新模型是 `[48, 11]`。两者不兼容，必须用新特征从头训练，不能加载旧权重。用 `input_size=11` 调用 `load_model`。

**Q: 跨用户但同一负载，准确率已 > 80%，还需要频域特征吗？**
A: 如果只需要跨用户泛化，动态峰值归一化（2.3 节）已经足够。频域特征（2.2 节）主要解决跨负载问题。可以只部署动态校准，暂不增加特征维度。
