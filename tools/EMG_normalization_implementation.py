"""
EMG 跨人群标准化实现代码库
=====================

完整的从特征提取到标准化、新用户适配的实现

版本：1.0
日期：2026-04-15
"""

import numpy as np
import pandas as pd
from scipy import stats
from scipy.signal import butter, filtfilt
from dataclasses import dataclass
from typing import Tuple, Dict, Optional, List
import json
import pickle
from pathlib import Path


# ============================================================================
# Part 1: 信号预处理与11维特征提取
# ============================================================================

class EMG_Signal_Processor:
    """EMG信号预处理：滤波、伪迹去除"""

    def __init__(self, sampling_rate: int = 2000,
                 high_pass: float = 20,
                 low_pass: float = 450):
        """
        Args:
            sampling_rate: 采样率 (Hz)
            high_pass: 高通滤波截止频率
            low_pass: 低通滤波截止频率
        """
        self.fs = sampling_rate
        self.high_pass = high_pass
        self.low_pass = low_pass

        # 设计Butterworth滤波器
        nyquist = sampling_rate / 2
        self.sos = butter(
            4,  # 4阶
            [high_pass / nyquist, low_pass / nyquist],
            btype='band',
            output='sos'
        )

    def filter_emg(self, signal: np.ndarray) -> np.ndarray:
        """带通滤波"""
        return filtfilt(self.sos, signal, padlen=100)

    def remove_dc_offset(self, signal: np.ndarray) -> np.ndarray:
        """移除直流分量"""
        return signal - np.mean(signal)

    def detect_artifacts(self, signal: np.ndarray,
                        threshold_std: float = 5.0) -> np.ndarray:
        """
        基于标准差的伪迹检测

        Returns:
            artifact_mask: bool数组，True表示伪迹
        """
        mean_val = np.mean(signal)
        std_val = np.std(signal)

        artifact_mask = np.abs(signal - mean_val) > threshold_std * std_val
        return artifact_mask

    def preprocess(self, signal: np.ndarray) -> Tuple[np.ndarray, Dict]:
        """
        完整预处理流程

        Returns:
            filtered_signal: 预处理后的信号
            quality_report: 质量报告
        """

        # 1. 移除直流偏移
        signal = self.remove_dc_offset(signal)

        # 2. 带通滤波
        filtered = self.filter_emg(signal)

        # 3. 伪迹检测
        artifacts = self.detect_artifacts(filtered)
        artifact_ratio = np.sum(artifacts) / len(artifacts)

        # 质量报告
        quality_report = {
            'artifact_ratio': artifact_ratio,
            'snr': np.std(filtered) / (np.std(signal[:int(self.fs*0.1)]) + 1e-8),
            'peak_to_rms': np.max(np.abs(filtered)) / np.sqrt(np.mean(filtered**2)),
        }

        return filtered, quality_report


class EMG_Feature_Extractor_11D:
    """11维EMG特征提取"""

    def __init__(self, sampling_rate: int = 2000):
        self.fs = sampling_rate
        self.feature_names = [
            'RMS', 'MAV', 'WL', 'ZC', 'SSC', 'EMAV', 'EWL',
            'Hjorth_Activity', 'Hjorth_Mobility', 'Hjorth_Complexity', 'ApEn'
        ]

    def extract_single_sample(self, signal: np.ndarray) -> np.ndarray:
        """
        从单个信号段提取11维特征

        Args:
            signal: 1D数组

        Returns:
            features: shape (11,)
        """

        features = np.zeros(11)

        # 基础7维
        features[0] = self._rms(signal)
        features[1] = self._mav(signal)
        features[2] = self._wl(signal)
        features[3] = self._zc(signal)
        features[4] = self._ssc(signal)
        features[5] = self._emav(signal)
        features[6] = self._ewl(signal)

        # Hjorth参数
        features[7], features[8], features[9] = self._hjorth_parameters(signal)

        # 近似熵
        features[10] = self._approximate_entropy(signal, m=2)

        return features

    def extract_batch(self, signals: np.ndarray) -> np.ndarray:
        """
        批量提取特征

        Args:
            signals: shape (n_samples, signal_length) 或 (signal_length,)

        Returns:
            features: shape (n_samples, 11) 或 (11,)
        """

        if signals.ndim == 1:
            return self.extract_single_sample(signals)

        n_samples = signals.shape[0]
        features = np.zeros((n_samples, 11))

        for i in range(n_samples):
            features[i] = self.extract_single_sample(signals[i])

        return features

    @staticmethod
    def _rms(signal: np.ndarray) -> float:
        """Root Mean Square"""
        return np.sqrt(np.mean(signal**2))

    @staticmethod
    def _mav(signal: np.ndarray) -> float:
        """Mean Absolute Value"""
        return np.mean(np.abs(signal))

    @staticmethod
    def _wl(signal: np.ndarray) -> float:
        """Waveform Length"""
        return np.sum(np.abs(np.diff(signal)))

    @staticmethod
    def _zc(signal: np.ndarray, threshold: float = 0) -> int:
        """Zero Crossings"""
        crossings = 0
        for i in range(1, len(signal)):
            if (signal[i-1] - threshold) * (signal[i] - threshold) < 0:
                crossings += 1
        return crossings

    @staticmethod
    def _ssc(signal: np.ndarray, threshold: float = 0) -> int:
        """Slope Sign Changes"""
        ssc_count = 0
        for i in range(1, len(signal) - 1):
            if ((signal[i] > signal[i-1]) and (signal[i] > signal[i+1])) or \
               ((signal[i] < signal[i-1]) and (signal[i] < signal[i+1])):
                ssc_count += 1
        return ssc_count

    @staticmethod
    def _emav(signal: np.ndarray) -> float:
        """Enhanced MAV"""
        mav = np.mean(np.abs(signal))
        std_signal = np.std(signal)
        mean_abs = np.mean(np.abs(signal))

        if mean_abs < 1e-10:
            return mav

        return mav * (1 + std_signal / mean_abs)

    @staticmethod
    def _ewl(signal: np.ndarray) -> float:
        """Enhanced WL"""
        wl = np.sum(np.abs(np.diff(signal)))
        std_signal = np.std(signal)
        mean_abs = np.mean(np.abs(signal))

        if mean_abs < 1e-10:
            return wl

        return wl * (1 + std_signal / mean_abs)

    @staticmethod
    def _hjorth_parameters(signal: np.ndarray) -> Tuple[float, float, float]:
        """
        Hjorth参数：Activity, Mobility, Complexity
        """
        dx = np.diff(signal)
        d2x = np.diff(dx)

        activity = np.var(signal)

        if activity < 1e-10:
            return activity, 0.0, 0.0

        var_dx = np.var(dx)
        mobility = np.sqrt(var_dx / activity) if activity > 0 else 0.0

        if mobility < 1e-10:
            complexity = 0.0
        else:
            complexity = np.sqrt(np.var(d2x) / var_dx) / mobility if var_dx > 0 else 0.0

        return activity, mobility, complexity

    @staticmethod
    def _approximate_entropy(signal: np.ndarray, m: int = 2,
                            r: Optional[float] = None) -> float:
        """
        Approximate Entropy (ApEn)

        Parameters:
            signal: 输入信号
            m: 嵌入维数
            r: 相似度阈值（若为None则自动设为0.2倍std）
        """

        N = len(signal)

        if r is None:
            r = 0.2 * np.std(signal)

        def _maxdist(x_i, x_j):
            return max([abs(ua - va) for ua, va in zip(x_i, x_j)])

        def _phi(m_val):
            x = [[signal[j] for j in range(i, i + m_val)]
                 for i in range(N - m_val + 1)]

            C = [len([1 for x_j in x if _maxdist(x_i, x_j) <= r]) / (N - m_val + 1.0)
                 for x_i in x]

            return (N - m_val + 1.0)**(-1) * sum(np.log(c) if c > 0 else 0 for c in C)

        return abs(_phi(m + 1) - _phi(m))


# ============================================================================
# Part 2: 标准化算法
# ============================================================================

@dataclass
class PopulationStatistics:
    """人群统计信息"""

    feature_names: List[str]
    mu: np.ndarray  # shape (11,)
    sigma: np.ndarray  # shape (11,)
    cov_matrix: np.ndarray  # shape (11, 11)
    inter_subject_std: np.ndarray  # shape (11,)，被试间差异
    sample_size: int


class Normalizer_ZScore:
    """Z-score标准化（Algorithm A）"""

    def __init__(self, population_stats: PopulationStatistics):
        self.pop_stats = population_stats
        self.subject_mu = None
        self.subject_sigma = None
        self.is_calibrated = False

    def calibrate(self, features_sample: np.ndarray):
        """
        快速校准（使用3-5个样本）

        Args:
            features_sample: shape (n_samples, 11)
        """

        n_samples = features_sample.shape[0]
        sample_mean = features_sample.mean(axis=0)
        sample_std = features_sample.std(axis=0) + 1e-8

        # 混合权重：样本越少，先验权重越大
        alpha = min(n_samples / 10.0, 1.0)

        self.subject_mu = (
            (1 - alpha) * self.pop_stats.mu +
            alpha * sample_mean
        )

        self.subject_sigma = (
            (1 - alpha) * self.pop_stats.sigma +
            alpha * sample_std
        )

        self.is_calibrated = True

    def normalize(self, features: np.ndarray) -> np.ndarray:
        """
        标准化特征

        Args:
            features: shape (11,) 或 (n, 11)

        Returns:
            normalized: 相同shape
        """

        if not self.is_calibrated:
            raise ValueError("Normalizer not calibrated")

        return (features - self.subject_mu) / (self.subject_sigma + 1e-8)


class Normalizer_Bayesian:
    """贝叶斯参数估计标准化（Algorithm B）"""

    def __init__(self, population_stats: PopulationStatistics):
        self.pop_stats = population_stats
        self.subject_mu = None
        self.subject_sigma = None
        self.is_calibrated = False

        # 超参数设置
        self.prior_tau = population_stats.inter_subject_std.copy()
        self.alpha_sigma = 2.0
        self.beta_sigma = 0.5

    def calibrate(self, features_sample: np.ndarray):
        """
        贝叶斯后验更新

        Args:
            features_sample: shape (n_samples, 11)
        """

        n = features_sample.shape[0]
        sample_mean = features_sample.mean(axis=0)
        sample_var = features_sample.var(axis=0) + 1e-8

        # μ的后验（共轭正规分布）
        precision_prior = 1.0 / (self.prior_tau**2 + 1e-8)
        precision_data = n / (sample_var + 1e-8)

        self.subject_mu = (
            (precision_prior * self.pop_stats.mu + precision_data * sample_mean) /
            (precision_prior + precision_data)
        )

        # σ的后验（InverseGamma共轭）
        alpha_post = self.alpha_sigma + n / 2.0

        sum_sq_dev = np.sum((features_sample - sample_mean)**2, axis=0)

        beta_post = (
            self.beta_sigma +
            0.5 * (sum_sq_dev + precision_prior * (sample_mean - self.pop_stats.mu)**2 /
                   (precision_prior + precision_data))
        )

        # 使用期望值作为后验估计
        self.subject_sigma = np.sqrt(beta_post / (alpha_post - 1.0 + 1e-8))

        self.is_calibrated = True

    def normalize(self, features: np.ndarray) -> np.ndarray:
        """标准化特征"""

        if not self.is_calibrated:
            raise ValueError("Normalizer not calibrated")

        return (features - self.subject_mu) / (self.subject_sigma + 1e-8)


class Normalizer_ReferenceSubject:
    """参考个体插值标准化（Algorithm C）"""

    def __init__(self, baseline_db: pd.DataFrame, n_refs: int = 7):
        """
        Args:
            baseline_db: 包含人体测量 + EMG特征的DataFrame
            n_refs: 参考个体数量
        """

        self.baseline_db = baseline_db
        self.anthropo_cols = ['arm_length', 'arm_circumference', 'skinfold', 'age']

        # 选择分位数位置的参考个体
        n_total = len(baseline_db)
        indices = [
            int(n_total * i / (n_refs - 1)) if n_refs > 1 else 0
            for i in range(n_refs)
        ]
        self.reference_subjects = baseline_db.iloc[indices].copy()
        self.reference_subjects.reset_index(drop=True, inplace=True)

        self.interpolated_mu = None
        self.interpolated_sigma = None

    def calibrate(self, new_subject_anthropo: Dict):
        """
        根据新被试的人体测量特征进行内插

        Args:
            new_subject_anthropo: {'arm_length': float, ...}
        """

        # 计算欧几里得距离（标准化后）
        distances = []

        for idx, ref in self.reference_subjects.iterrows():
            dist = 0.0

            for col in self.anthropo_cols:
                if col not in new_subject_anthropo or col not in self.baseline_db.columns:
                    continue

                baseline_mean = self.baseline_db[col].mean()
                baseline_std = self.baseline_db[col].std() + 1e-8

                normalized_new = (new_subject_anthropo[col] - baseline_mean) / baseline_std
                normalized_ref = (ref[col] - baseline_mean) / baseline_std

                dist += (normalized_new - normalized_ref)**2

            distances.append(np.sqrt(dist))

        distances = np.array(distances)

        # Softmax加权
        distances_std = distances.std() + 1e-8
        weights = np.exp(-distances / distances_std)
        weights /= weights.sum()

        # 提取EMG特征列（假设特征列以'feat_'开头或在最后11列）
        feature_cols = [col for col in self.reference_subjects.columns
                       if col.startswith('RMS') or col.startswith('MAV') or
                          col in ['RMS', 'MAV', 'WL', 'ZC', 'SSC', 'EMAV', 'EWL',
                                 'Activity', 'Mobility', 'Complexity', 'ApEn']]

        if not feature_cols:
            # 假设最后11列是特征
            feature_cols = self.reference_subjects.columns[-11:].tolist()

        self.interpolated_mu = np.average(
            self.reference_subjects[feature_cols[:11]].values,
            axis=0,
            weights=weights
        )

        # 简单估计sigma（使用参考个体的std）
        self.interpolated_sigma = np.average(
            self.reference_subjects[[col + '_std' for col in feature_cols[:11]]].values
            if all(col + '_std' in self.reference_subjects.columns for col in feature_cols[:11])
            else np.ones((len(self.reference_subjects), 11)) * 0.5,  # 默认值
            axis=0,
            weights=weights
        )

    def normalize(self, features: np.ndarray) -> np.ndarray:
        """标准化特征"""

        if self.interpolated_mu is None:
            raise ValueError("Normalizer not calibrated")

        return (features - self.interpolated_mu) / (self.interpolated_sigma + 1e-8)


# ============================================================================
# Part 3: 新用户快速适配系统
# ============================================================================

class QuickAdaptation_System:
    """完整的新用户快速适配系统"""

    def __init__(self, phase1_results_dir: str, algorithm: str = 'bayesian'):
        """
        Args:
            phase1_results_dir: Phase 1输出文件夹路径
            algorithm: 'zscore' 或 'bayesian' 或 'reference'
        """

        self.results_dir = Path(phase1_results_dir)
        self.algorithm = algorithm

        # 加载Phase 1结果
        self._load_phase1_results()

        # 初始化特征提取与处理
        self.signal_processor = EMG_Signal_Processor()
        self.feature_extractor = EMG_Feature_Extractor_11D()

        # 初始化标准化器（根据选择的算法）
        self._initialize_normalizer()

    def _load_phase1_results(self):
        """加载Phase 1的统计结果"""

        # 加载人群统计信息
        stats_path = self.results_dir / 'PopulationStatistics_11D.json'

        with open(stats_path, 'r') as f:
            stats_dict = json.load(f)

        self.pop_stats = PopulationStatistics(
            feature_names=stats_dict['feature_names'],
            mu=np.array(stats_dict['mu']),
            sigma=np.array(stats_dict['sigma']),
            cov_matrix=np.array(stats_dict['cov_matrix']),
            inter_subject_std=np.array(stats_dict['inter_subject_std']),
            sample_size=stats_dict['sample_size']
        )

        # 加载baseline数据库
        baseline_path = self.results_dir / 'BaselineDB.csv'
        self.baseline_db = pd.read_csv(baseline_path)

    def _initialize_normalizer(self):
        """初始化标准化器"""

        if self.algorithm == 'zscore':
            self.normalizer = Normalizer_ZScore(self.pop_stats)
        elif self.algorithm == 'bayesian':
            self.normalizer = Normalizer_Bayesian(self.pop_stats)
        elif self.algorithm == 'reference':
            self.normalizer = Normalizer_ReferenceSubject(self.baseline_db)
        else:
            raise ValueError(f"Unknown algorithm: {self.algorithm}")

    def new_user_onboarding(self,
                           user_id: str,
                           raw_emg_samples: np.ndarray,
                           user_anthropo: Optional[Dict] = None) -> Tuple[bool, Dict]:
        """
        新用户快速适配工作流

        Args:
            user_id: 用户ID
            raw_emg_samples: shape (n_calibration, signal_length)，校准EMG信号
            user_anthropo: 用户人体测量数据（可选）

        Returns:
            success: bool，是否适配成功
            report: dict，包含质量报告和标准化参数
        """

        report = {
            'user_id': user_id,
            'success': False,
            'issues': [],
            'quality_checks': {},
            'normalization_params': None
        }

        # Step 1：信号预处理与质量检查
        processed_samples = []
        quality_checks = {}

        for i, raw_signal in enumerate(raw_emg_samples):
            filtered, quality = self.signal_processor.preprocess(raw_signal)
            processed_samples.append(filtered)
            quality_checks[f'sample_{i}'] = quality

        report['quality_checks'] = quality_checks

        # 质量门槛检查
        if not self._pass_quality_gates(quality_checks):
            report['issues'].append("Signal quality too poor, re-capture needed")
            return False, report

        # Step 2：特征提取
        processed_array = np.array(processed_samples)
        features = self.feature_extractor.extract_batch(processed_array)
        # shape: (n_calibration, 11)

        # Step 3：算法特定的校准
        if self.algorithm == 'reference' and user_anthropo:
            self.normalizer.calibrate(user_anthropo)
        else:
            self.normalizer.calibrate(features)

        # Step 4：质量检查（重复性）
        if not self._pass_repeatability_check(features):
            report['issues'].append("Poor repeatability (CV > 30%), re-capture needed")
            return False, report

        # Step 5：生成报告
        report['success'] = True
        report['normalization_params'] = {
            'mu': self.normalizer.subject_mu.tolist() if hasattr(self.normalizer, 'subject_mu')
                  else self.normalizer.interpolated_mu.tolist(),
            'sigma': self.normalizer.subject_sigma.tolist() if hasattr(self.normalizer, 'subject_sigma')
                     else self.normalizer.interpolated_sigma.tolist(),
        }

        return True, report

    def normalize_signal(self, raw_signal: np.ndarray,
                        window_size: Optional[int] = None) -> np.ndarray:
        """
        标准化原始信号

        Args:
            raw_signal: 原始EMG信号
            window_size: 滑动窗口大小（若为None则一次性处理）

        Returns:
            normalized_features: shape (11,) 或 (n_windows, 11)
        """

        if not self.normalizer.is_calibrated:
            raise ValueError("Normalizer not calibrated")

        # 预处理
        filtered, _ = self.signal_processor.preprocess(raw_signal)

        # 特征提取
        if window_size is None:
            features = self.feature_extractor.extract_single_sample(filtered)
        else:
            # 滑动窗口
            n_windows = len(filtered) // window_size
            features_list = []

            for i in range(n_windows):
                window = filtered[i*window_size:(i+1)*window_size]
                feat = self.feature_extractor.extract_single_sample(window)
                features_list.append(feat)

            features = np.array(features_list)

        # 标准化
        normalized = self.normalizer.normalize(features)

        return normalized

    @staticmethod
    def _pass_quality_gates(quality_checks: Dict) -> bool:
        """检查是否通过质量门槛"""

        for sample_id, quality in quality_checks.items():
            # 检查SNR
            if quality.get('snr', 0) < 5:
                return False

            # 检查伪迹比例
            if quality.get('artifact_ratio', 0) > 0.1:  # 超过10%伪迹
                return False

        return True

    @staticmethod
    def _pass_repeatability_check(features: np.ndarray, cv_threshold: float = 0.3) -> bool:
        """
        检查3个样本的重复性

        Args:
            features: shape (3, 11)
            cv_threshold: 变异系数阈值
        """

        cv = np.std(features, axis=0) / (np.mean(np.abs(features), axis=0) + 1e-8)

        # 任何特征的CV超过阈值则失败
        return np.all(cv < cv_threshold)


# ============================================================================
# Part 4: 验证与性能评估
# ============================================================================

def leave_one_subject_out_cv(baseline_db: pd.DataFrame,
                             algorithm: str = 'bayesian',
                             n_test_samples: int = 3) -> Dict:
    """
    Leave-One-Subject-Out交叉验证

    Returns:
        cv_results: 包含各fold的准确率等指标
    """

    results = {
        'algorithm': algorithm,
        'accuracies': [],
        'icc_scores': [],
        'cross_subject_cv': [],
    }

    n_subjects = len(baseline_db)

    for target_idx in range(n_subjects):
        # 分离target和source
        source_db = baseline_db.drop(target_idx).reset_index(drop=True)
        target_subject = baseline_db.iloc[target_idx]

        # 计算source的统计信息
        source_stats = PopulationStatistics(
            feature_names=['RMS', 'MAV', 'WL', 'ZC', 'SSC', 'EMAV', 'EWL',
                          'Activity', 'Mobility', 'Complexity', 'ApEn'],
            mu=source_db[[f'feat_{i}' for i in range(11)]].mean().values
            if any(f'feat_{i}' in source_db.columns for i in range(11))
            else np.zeros(11),
            sigma=source_db[[f'feat_{i}' for i in range(11)]].std().values
            if any(f'feat_{i}' in source_db.columns for i in range(11))
            else np.ones(11),
            cov_matrix=np.eye(11),
            inter_subject_std=np.ones(11),
            sample_size=len(source_db)
        )

        # 初始化标准化器
        if algorithm == 'zscore':
            normalizer = Normalizer_ZScore(source_stats)
        elif algorithm == 'bayesian':
            normalizer = Normalizer_Bayesian(source_stats)
        else:
            continue  # 跳过其他算法

        # 用target的前n_test_samples个样本进行校准
        # （这需要raw数据，这里仅作演示）
        # normalizer.calibrate(target_features[:n_test_samples])

        # 用剩余样本进行测试
        # test_accuracy = evaluate_classification(normalizer, target_test_features)

        # results['accuracies'].append(test_accuracy)

    if results['accuracies']:
        results['mean_accuracy'] = np.mean(results['accuracies'])
        results['std_accuracy'] = np.std(results['accuracies'])

    return results


if __name__ == '__main__':
    # 示例用法
    print("EMG Cross-Subject Normalization System v1.0")
    print("=" * 60)

    # 示例：初始化系统
    # system = QuickAdaptation_System(
    #     phase1_results_dir='/path/to/phase1_results',
    #     algorithm='bayesian'
    # )

    # 示例：新用户适配
    # raw_emg = np.random.randn(3, 6000)  # 3个采样，每个2秒@3kHz
    # success, report = system.new_user_onboarding(
    #     user_id='subject_001',
    #     raw_emg_samples=raw_emg,
    #     user_anthropo={'arm_length': 28, 'arm_circumference': 28, ...}
    # )

    # print(f"Adaptation successful: {success}")
    # print(f"Report: {report}")
