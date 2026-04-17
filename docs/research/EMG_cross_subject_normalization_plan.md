# EMG 跨人群标准化实施方案

**制定日期**：2026年4月15日  
**目标**：建立人群无关的EMG信号标准化框架，使不同体型、性别的被试在相似环境下获得一致的结果

---

## 一、论文搜索方向与关键词

基于最新研究进展（2024-2025），建议的论文搜索方向：

### 1. 核心搜索词（按优先级）
- **第1优先级**：
  - "EMG cross-subject normalization standardization"
  - "Surface EMG normalization techniques comparison"
  - "inter-subject EMG variability reduction"

- **第2优先级**：
  - "domain adaptation EMG gesture recognition"
  - "transfer learning EMG subject-independent"
  - "EMG feature standardization bioelectric biomechanical"

- **第3优先级**：
  - "few-shot learning EMG calibration"
  - "fast adaptation EMG new subject"
  - "reference normalization electromyography"

### 2. 关键研究团队与机构
- MDPI/Sensors 期刊（EMG标准化权威发布渠道）
- IEEE Xplore（跨人群手势识别）
- Nature Scientific Reports（算法创新）
- arXiv（最新前沿工作）

### 3. 核心参考文献方向
- **标准化方法论**：Standardizing EMG Pipelines for Muscle Synergy Analysis（MDPI 2024）
- **基准对比**：Ref-EMGBench: Benchmarking Reference Normalization（OpenReview 2024）
- **年龄性别因素**：A Comparison of Normalization Techniques in Older and Young Adults（MDPI 2024）
- **快速校准**：From zero- to few-shot deep temporal learning（PubMed 2025）
- **域适应**：Cross-subject EMG hand gesture via dynamic domain generalization（IEEE 2024）

---

## 二、跨人群标准化的理论框架

### 理论基础：三层解耦模型

EMG信号的跨人群标准化问题本质上是**解耦生理差异与动作意图**的问题。

```
原始信号 = f(个体生理特征) × g(动作意图) × h(环境因素)

目标：提取 g(动作意图) 与 h(环境因素)，消除 f(个体生理特征)
```

### 三个关键步骤

#### **第一步：基线建立 (Baseline Establishment)**

**目标**：量化个体差异的范围与结构

**核心逻辑**：
- 同一个体在"重复相同动作"时的信号变化最小
- 不同个体做"相同标准化动作"时的信号最具可比性
- 存在可度量的**生理特征指标** (anthropometric features)，与EMG幅值呈线性或非线性关系

**生理特征维度**（需要测量）：
1. **肢体尺寸**：上臂长度、上臂周长、前臂周长
2. **体成分**：皮脂厚度（biceps处）、肌肉横截面积(CSA)估算
3. **性别与年龄**：作为分层变量
4. **肌肉特性**：肌肉纤维类型比例(可从等长收缩时间推断)、神经传导速度

**基线数据特性**：
- 采集20-30个被试（覆盖男女、体型差异大）
- 每人采集3-5次重复的标准动作（肱二头肌最大随意收缩 MVC，肱二头肌屈曲)
- 单位：RMS、MAV等标准特征
- **预期结果**：建立"生理特征 → EMG幅值" 的**回归模型**

#### **第二步：参数标准化 (Parametric Normalization)**

**目标**：将新被试的EMG映射到"标准人群空间"

**关键假设**（基于方差转移理论）：
> 虽然EMG特征的**绝对值**因个体而异，但其**方差结构与相对比例**在人群间保持一致

**具体实施**：

##### 方法1：生物电标准化（推荐）
```
标准化后的EMG = (原始EMG - μ_个体) / σ_个体

其中：
- μ_个体 = 从个体静息状态测得的基础噪声均值
- σ_个体 = 从个体MVC标准化参考测得的幅值标准差
```

这种方法利用了**MVC参考标准化**和**个体Z-score标准化**的组合。

##### 方法2：生物力学标准化
```
标准化后的EMG = 原始EMG / (肌肉CSA × 神经传导速度)

其中：
- 肌肉CSA可从上臂周长与皮脂厚度推算
- 神经传导速度与年龄和肌肉类型相关
```

##### 方法3：参考个体法（实用折中）
```
选择5-10个"基准个体"（覆盖体型range的10%, 25%, 50%, 75%, 90%分位数）
对新被试，找最接近的基准个体，应用线性内插标准化参数
```

**推荐采用：方法1 + 方法3的混合**
- 先用方法1做粗标准化
- 再用方法3进行个体微调

#### **第三步：新用户快速适配 (Rapid User Adaptation)**

**目标**：用最少样本（5-10次重复）估计新被试的标准化参数

**算法方案**：

##### A. 贝叶斯更新框架
```
设先验 Prior = 人群平均的(μ, σ)
设观测 Observation = 新被试的少量标准动作数据
后验 Posterior = Bayesian update(Prior, Observation)

使用更新后的 μ_new 和 σ_new 进行标准化
```

**数学实现**：
```
若假设EMG服从高斯分布 N(μ, σ²)：

先验：μ ~ N(μ_群体, τ²), σ ~ InverseGamma(α, β)
观测：n个样本点 x₁, ..., xₙ
后验：使用共轭先验性质快速更新

需要的新被试数据：
- 数据点数：5-10个重复
- 计算时间：< 100ms
- 收敛性：KL散度降低至5%以内
```

**实施步骤**：
1. 新用户做3次标准肌肉收缩（肱二头肌屈曲，30%, 50%, 70% MVC）
2. 提取每次收缩的RMS/MAV特征
3. 与人群先验的(μ, σ)进行贝叶斯更新
4. 得到个体的(μ_个体, σ_个体)用于后续标准化

##### B. 元学习框架（可选的高阶方案）
```
使用MAML (Model-Agnostic Meta-Learning) 或 Prototypical Networks

预训练阶段：
- 从20个基线被试学习"如何快速适应新人"
- 每个被试虚拟划分为source和target，进行meta-learning

适配阶段：
- 新被试提供5个样本
- 使用学到的meta-learner，1-2步梯度更新
- 获得该被试的标准化参数
```

**预期性能**：
- 3样本适配：75-80% 精度恢复
- 5样本适配：85-90% 精度恢复
- 10样本适配：92-96% 精度恢复

---

## 三、详细的实施工作流

### Phase 1：基线建立（2-3周）

#### 阶段目标
建立覆盖人群多样性的基线数据库，量化生理特征与EMG幅值的关系

#### 被试选择标准

| 维度 | 要求 | 理由 |
|------|------|------|
| **样本量** | 25-35人 | 足以覆盖分布，支持多元线性回归（P > 10） |
| **性别** | 男女各50% | 性别是主要分层因素 |
| **年龄** | 20-60岁均匀分布 | 年龄影响神经传导速度和肌肉质量 |
| **BMI** | 18.5-35覆盖 | BMI<18.5和>35各1人（极端） |
| **运动背景** | 久坐+运动员各20% | 肌肉类型比例不同 |
| **上臂周长** | 24-35cm覆盖所有分位数 | 直接影响EMG幅值 |

#### 采集流程

**第1天：被试信息采集**
```python
采集项目清单：
1. 人口统计：年龄、性别、利手性
2. 人体测量：
   - 上臂长度（肩锋到肘尖）：cm
   - 上臂周长（肱二头肌处）：cm
   - 前臂周长（腕部上5cm）：cm
   - 肩宽：cm
3. 体成分：
   - 体重、身高 → BMI
   - 皮脂厚度（三头肌+二头肌处）：使用专用卡尺
4. 肌肉特性问卷：
   - 运动频率（周次数）
   - 主要训练方式（力量/耐力/混合）
5. 医学排除标准：
   - 神经肌肉疾病史
   - 肩臂损伤史
   - 正在服用神经影响药物
```

**第2天：EMG基线采集（上午）**
```
环境标准化：
- 温度：22-24°C
- 湿度：40-60%
- 采样率：2000Hz
- 带通滤波：20-450Hz Butterworth 4阶

电极放置标准：
- 肱二头肌长头（biceps长头）上1/3处
- 参考电极：肘尖上方5cm（非肌肉处）
- 皮肤准备：酒精擦洗+轻微打磨（≤10秒）
- 电极固定：医用胶布，确保全程接触

测试流程：
1. 热身（3分钟轻度屈伸）
2. 静息基线（60秒）→ 后续作为 μ_static
3. MVC测试：
   - 方法A：最大随意收缩肱二头肌屈曲（3秒）× 3次，间隔60秒
   - 方法B：等长收缩：90°肘角，抵抗固定力量计，增压到最大（3秒）× 3次
   - 取3次中的最大RMS值作为 MVC_ref
4. 标准收缩序列（每人需要采集）：
   - 30% MVC 肱二头肌屈曲：10次，每次3秒，间隔3秒
   - 50% MVC 肱二头肌屈曲：10次，每次3秒，间隔3秒
   - 70% MVC 肱二头肌屈曲：10次，每次3秒，间隔3秒
   - 总耗时：约30分钟

数据存储格式：
被试ID_日期.csv 包含列：
timestamp(ms), EMG_raw(mV), force_reference(N), 
task_label(30%/50%/70%MVC), anthropometric_ID
```

**第2天：生理特征与信号对应分析（下午）**
```
关键计算：

对每个被试的每个收缩强度（30%, 50%, 70%），计算：
1. RMS = sqrt(mean(signal²))
2. MAV = mean(|signal|)
3. WL = sum(|signal[i] - signal[i-1]|)
4. ZC = 零交叉次数
5. SSC = 坡度符号变化次数

建立线性关系：
RMS_被试 = a₀ + a₁·(上臂周长) + a₂·(皮脂厚度) + a₃·(年龄) + ...

目标：R² > 0.75（单个特征可达0.6-0.7，组合可达0.8+）
```

#### 质量控制

```python
质量控制检查清单：
□ 每个被试3次MVC的变异系数 < 10%
□ 30/50/70% MVC的RMS值单调递增（p<0.01）
□ 静息基线噪声(RMS) < 50μV
□ 没有可见的运动伪迹（baseline漂移>500μV）
□ EMG信号与力传感器参考信号的相关系数 > 0.85

若不符合：
- 变异系数>10%：重新采集该次MVC
- 噪声>50μV：重新清洁皮肤+电极，重采
- 伪迹明显：标记该段并排除
```

#### Phase 1输出物

```
输出物清单：
1. BaselineDB.csv
   - 35行（被试）× 30列（生理特征 + 11维EMG特征均值）
   
2. AnthropometricModel.pkl
   - 线性/非线性回归模型
   - 输入：生理特征 → 输出：EMG幅值预测值 + 置信区间
   
3. PopulationStatistics.json
   - μ_RMS, σ_RMS, μ_MAV, σ_MAV, ...（按性别分层）
   - 相关系数矩阵（RMS, MAV, WL等之间）
   
4. CalibrationProtocol.md
   - 详细的采集SOP文档
   
5. 数据质量报告
   - 异常检测结果
   - 各被试重复性指标
```

---

### Phase 2：参数标准化与算法选择（2-3周）

#### 2.1 标准化算法设计

##### 算法A：自适应Z-score标准化（推荐初期方案）

**基本形式**：
```
EMG_normalized[t] = (EMG_raw[t] - μ_个体[t]) / σ_个体[t]

其中：
μ_个体 = (1-α) × μ_人群 + α × μ_个体_测
σ_个体 = (1-α) × σ_人群 + α × σ_个体_测

α ∈ [0,1]：混合权重
- α=0：完全使用人群先验（适合样本很少时）
- α=1：完全使用个体测量（适合样本充足时）
```

**实施方法**：
```python
# 伪代码
class EMG_Normalizer:
    def __init__(self, population_stats):
        """
        population_stats: {
            'RMS': {'mean': μ, 'std': σ},
            'MAV': {'mean': μ, 'std': σ},
            ...
        }
        """
        self.pop_stats = population_stats
        self.subject_stats = None
        self.calibration_samples = []
    
    def calibrate(self, raw_emg_samples, n_samples_needed=5):
        """
        快速校准：用n_samples_needed个样本更新个体参数
        raw_emg_samples: shape (n_samples, signal_length)
        """
        features = extract_features(raw_emg_samples)
        # features: shape (n_samples, n_features)
        
        # 特征均值和方差
        self.subject_stats = {
            'mean': features.mean(axis=0),
            'std': features.std(axis=0)
        }
        # 平滑处理：混合人群先验
        alpha = min(len(features) / 20, 1.0)  # 样本越多α越接近1
        for key in self.subject_stats:
            self.subject_stats[key] = (
                (1 - alpha) * self.pop_stats[key] +
                alpha * self.subject_stats[key]
            )
    
    def normalize(self, raw_emg):
        """
        对新信号进行标准化
        raw_emg: shape (signal_length,)
        """
        features = extract_features(raw_emg)
        
        normalized = (features - self.subject_stats['mean']) / self.subject_stats['std']
        return normalized
```

##### 算法B：贝叶斯参数估计（推荐高级方案）

**概率模型**：
```
观测模型：EMG_features ~ N(μ_个体, σ_个体²)

先验：
  μ_个体 ~ N(μ_人群, τ_μ²)
  σ_个体 ~ InverseGamma(α_σ, β_σ)

贝叶斯更新后验：
  p(μ, σ | data) ∝ p(data | μ, σ) × p(μ, σ)
```

**实施代码**：
```python
import numpy as np
from scipy import stats

class Bayesian_EMG_Normalizer:
    def __init__(self, population_stats_dict):
        # 从Phase 1的BaselineDB计算先验
        self.prior_mu = population_stats_dict['mean']
        self.prior_sigma = population_stats_dict['std']
        self.prior_tau = np.std(population_stats_dict['all_means'])  # 均值间的方差
        
        # InverseGamma参数（可从baseline数据估计）
        self.alpha_sigma = 2.0
        self.beta_sigma = 0.5
    
    def calibrate(self, features_observed):
        """
        features_observed: shape (n_samples, n_features)
        返回后验参数
        """
        n = features_observed.shape[0]
        sample_mean = features_observed.mean(axis=0)
        sample_var = features_observed.var(axis=0)
        
        # 共轭先验更新（正规分布+逆伽马）
        # μ的后验
        precision_prior = 1.0 / self.prior_tau**2
        precision_data = n / sample_var
        
        posterior_mu = (
            (precision_prior * self.prior_mu + precision_data * sample_mean) /
            (precision_prior + precision_data)
        )
        posterior_tau_sq = 1.0 / (precision_prior + precision_data)
        
        # σ的后验
        posterior_alpha = self.alpha_sigma + n / 2.0
        sum_sq_dev = np.sum((features_observed - sample_mean)**2)
        posterior_beta = (
            self.beta_sigma +
            0.5 * (sum_sq_dev + precision_prior * (sample_mean - self.prior_mu)**2 /
                   (precision_prior + precision_data))
        )
        
        self.subject_mu = posterior_mu
        self.subject_sigma = np.sqrt(posterior_beta / (posterior_alpha - 1))
        
        return self.subject_mu, self.subject_sigma
    
    def normalize(self, features_new):
        """标准化新数据"""
        return (features_new - self.subject_mu) / self.subject_sigma
```

##### 算法C：参考个体线性内插（实用折中方案）

**原理**：
```
1. 从baseline中选择5-10个"标志性个体"（体型代表性强）
2. 对新被试，计算与各标志个体的"相似度"（基于人体测量数据）
3. 进行加权线性内插
```

**代码实现**：
```python
class ReferenceSubject_Normalizer:
    def __init__(self, baseline_db, n_refs=7):
        """
        baseline_db: DataFrame with anthropometric + EMG features
        n_refs: 选择的参考个体数
        """
        self.baseline_db = baseline_db
        # 选择参考个体：按上臂周长分位数选择
        anthropo_cols = ['arm_length', 'arm_circumference', 'skinfold', 'age']
        
        # K-means聚类或分位数直接选择
        n_total = len(baseline_db)
        indices = [
            int(n_total * p / (n_refs - 1)) for p in range(n_refs)
        ]
        self.reference_subjects = baseline_db.iloc[indices].copy()
    
    def calibrate(self, new_subject_anthropo):
        """
        new_subject_anthropo: dict or Series
            {'arm_length': cm, 'arm_circumference': cm, ...}
        """
        anthropo_cols = ['arm_length', 'arm_circumference', 'skinfold', 'age']
        
        # 计算欧几里得距离（标准化后）
        distances = []
        for idx, ref in self.reference_subjects.iterrows():
            dist = 0
            for col in anthropo_cols:
                baseline_mean = self.baseline_db[col].mean()
                baseline_std = self.baseline_db[col].std()
                
                normalized_new = (
                    (new_subject_anthropo[col] - baseline_mean) / baseline_std
                )
                normalized_ref = (
                    (ref[col] - baseline_mean) / baseline_std
                )
                dist += (normalized_new - normalized_ref)**2
            
            distances.append(np.sqrt(dist))
        
        distances = np.array(distances)
        
        # softmax加权（距离越近，权重越高）
        weights = np.exp(-distances / distances.std())
        weights /= weights.sum()
        
        # 加权平均参考个体的标准化参数
        self.interpolated_mu = np.average(
            self.reference_subjects[['RMS', 'MAV', 'WL']].values,
            axis=0,
            weights=weights
        )
        self.interpolated_sigma = np.average(
            self.reference_subjects[['RMS_std', 'MAV_std', 'WL_std']].values,
            axis=0,
            weights=weights
        )
    
    def normalize(self, features_new):
        return (features_new - self.interpolated_mu) / self.interpolated_sigma
```

#### 2.2 算法对比与选择

| 算法 | 样本需求 | 计算复杂度 | 个体化程度 | 适用场景 |
|------|---------|----------|----------|--------|
| **A. Z-score** | 3-5 | O(n) | 中 | 快速部署，实时应用 |
| **B. 贝叶斯** | 5-10 | O(n²) | 高 | 高精度需求，可承受延迟 |
| **C. 参考个体** | 0-3 | O(m) | 中 | 初期无样本，零样本适配 |

**推荐策略**（分阶段）：
```
初期（Month 1）：使用算法C（零样本）+ A（快速参考）
  - 新用户无需特殊校准
  - 精度预期：75-80%

中期（Month 2-3）：升级到算法A（Z-score）
  - 新用户提供3个标准收缩样本
  - 精度预期：85-90%

长期（Month 4+）：部署算法B（贝叶斯）
  - 精度预期：92-96%
  - 个体化参数估计最优
```

#### 2.3 验证与评估

**交叉验证设计**：
```python
# Leave-One-Subject-Out (LOSO) CV
对baseline中的35个被试：
    for target_subject in all_subjects:
        source_subjects = all_subjects - {target_subject}
        
        # 用source构建标准化模型
        model = fit_normalizer(source_subjects)
        
        # 预测target在标准化后与source的一致性
        # 使用3个target样本进行快速校准
        calibration_samples = target_subject.samples[:3]
        model.calibrate(calibration_samples)
        
        test_samples = target_subject.samples[3:]
        predictions = model.normalize(test_samples)
        
        # 评估指标：
        # 1. 标准化后跨人群的RMS一致性（目标：CV < 15%）
        # 2. 分类准确率（如后续用于手势识别，目标：>90%）
        # 3. 校准效率（单个样本适配时间，目标：<100ms）
```

**预期结果**：
```
LOSO CV结果：
- Z-score法：86% ± 4% 准确率
- 贝叶斯法：92% ± 3% 准确率
- 参考个体：79% ± 6% 准确率（无校准情况下）
```

---

### Phase 3：新用户快速适配（1-2周）

#### 3.1 新用户工作流

**Total Flow（用户时间成本：~5分钟）**：
```
1. 穿戴EMG电极（1分钟）
   - 清洁肱二头肌长头上1/3处
   - 粘贴预制电极
   - 连接到采集设备

2. 人体测量（2分钟）
   - 上臂周长（环绕肱二头肌处）
   - 上臂长度
   - 年龄、性别（已有可跳过）

3. 标准收缩校准（2分钟）
   - 做3次标准肱二头肌屈曲
   - 每次3秒，间隔30秒
   - 系统自动采集与计算

4. 即时反馈（自动）
   - 适配成功 ✓ 可以使用
   - 或 ✗ 需要重新采集（不符合质量标准）

系统后端耗时：< 200ms（全部在采集过程中完成）
```

#### 3.2 技术实现

**假设场景：已部署算法A + 快速贝叶斯更新**

```python
class QuickAdaptation_EMG_System:
    def __init__(self, phase1_results_path):
        # 加载Phase 1结果
        self.population_stats = load_pickle(phase1_results_path + '/PopulationStatistics.json')
        self.anthropo_model = load_pickle(phase1_results_path + '/AnthropometricModel.pkl')
        self.baseline_db = pd.read_csv(phase1_results_path + '/BaselineDB.csv')
    
    def new_user_onboarding(self, user_anthropo_data, calibration_emg_samples):
        """
        Args:
            user_anthropo_data: dict
                {'arm_circumference': float, 'arm_length': float, 'age': int, 'gender': str}
            calibration_emg_samples: np.ndarray, shape (3, sample_length)
                3次标准收缩的原始EMG信号
        
        Returns:
            normalizer: 该用户专属的标准化器对象
            quality_report: 采集质量评估
        """
        
        # Step 1：特征提取
        calibration_features = self.extract_features_batch(calibration_emg_samples)
        # shape: (3, 7)，7维特征为[RMS, MAV, WL, ZC, SSC, EMAV, EWL]
        
        # Step 2：贝叶斯快速更新（基于先验）
        posterior_mu, posterior_sigma = self.bayesian_quick_update(
            calibration_features,
            self.population_stats
        )
        
        # Step 3：质量检查
        quality_report = self.quality_check(
            calibration_emg_samples,
            calibration_features
        )
        
        if not quality_report['pass']:
            return None, quality_report  # 提示重新采集
        
        # Step 4：生成用户标准化器
        normalizer = EMG_Normalizer_Individual(
            mu=posterior_mu,
            sigma=posterior_sigma,
            user_id=user_id,
            anthropo=user_anthropo_data
        )
        
        return normalizer, quality_report
    
    def extract_features_batch(self, emg_signals):
        """
        Args:
            emg_signals: np.ndarray, shape (n_samples, signal_length)
        
        Returns:
            features: np.ndarray, shape (n_samples, 7)
        """
        features_list = []
        for signal in emg_signals:
            # 每个特征计算
            rms = np.sqrt(np.mean(signal**2))
            mav = np.mean(np.abs(signal))
            wl = np.sum(np.abs(np.diff(signal)))
            zc = np.sum(np.abs(np.diff(np.sign(signal))) > 0)
            ssc = np.sum(np.abs(np.diff(np.sign(np.diff(signal)))) > 0)
            
            # Enhanced特征（需要更多计算但更鲁棒）
            emav = np.mean(np.abs(signal)) * (1 + np.std(signal) / np.mean(np.abs(signal)))
            ewl = np.sum(np.abs(np.diff(signal))) * (1 + np.std(signal) / np.mean(np.abs(signal)))
            
            features_list.append([rms, mav, wl, zc, ssc, emav, ewl])
        
        return np.array(features_list)
    
    def bayesian_quick_update(self, observed_features, prior_stats):
        """
        快速贝叶斯后验更新（基于共轭先验）
        
        Args:
            observed_features: shape (3, 7)
            prior_stats: {'mu': array, 'sigma': array, ...}
        
        Returns:
            posterior_mu, posterior_sigma: 各shape (7,)
        """
        n = observed_features.shape[0]  # 3
        sample_mean = observed_features.mean(axis=0)
        sample_var = observed_features.var(axis=0) + 1e-8  # 避免div by zero
        
        # 超参数（从baseline估计）
        prior_mu = prior_stats['mu']  # shape (7,)
        prior_sigma = prior_stats['sigma']
        prior_tau = prior_stats['inter_subject_std']  # 被试间的std
        
        # 正规分布共轭先验更新
        precision_prior = 1.0 / (prior_tau**2 + 1e-8)
        precision_data = n / (sample_var + 1e-8)
        
        posterior_mu = (
            (precision_prior * prior_mu + precision_data * sample_mean) /
            (precision_prior + precision_data)
        )
        
        # InverseGamma更新（简化：保持方差，混合prior和data）
        alpha = 0.4  # 混合权重（3个样本时给prior更多权重）
        posterior_sigma = (
            (1 - alpha) * prior_sigma +
            alpha * np.sqrt(sample_var)
        )
        
        return posterior_mu, posterior_sigma
    
    def quality_check(self, raw_emg, features):
        """
        质量控制检查
        
        Returns:
            quality_report: dict
        """
        report = {'pass': True, 'issues': []}
        
        # Check 1：信噪比
        noise_floor = np.std(raw_emg[:, :100])  # 前100个点作为噪声
        signal_level = np.std(raw_emg)
        snr = signal_level / (noise_floor + 1e-8)
        
        if snr < 5:
            report['issues'].append("SNR too low, re-capture needed")
            report['pass'] = False
        
        # Check 2：重复性（3次收缩的特征应接近）
        cv = np.std(features, axis=0) / (np.mean(features, axis=0) + 1e-8)
        if np.any(cv > 0.3):  # CoV > 30%
            report['issues'].append("Repeatability poor (CV > 30%), re-capture needed")
            report['pass'] = False
        
        # Check 3：幅值范围（应该在合理范围内）
        expected_rms = prior_stats['mu'][0]  # RMS的人群均值
        actual_rms = features[:, 0].mean()
        
        if actual_rms < expected_rms * 0.3 or actual_rms > expected_rms * 3:
            report['issues'].append(f"Signal amplitude abnormal: {actual_rms:.2f}mV")
            report['pass'] = False
        
        # Check 4：运动伪迹检测（baseline漂移）
        baseline_drift = np.max(np.convolve(raw_emg, np.ones(100)/100, mode='valid')) - \
                        np.min(np.convolve(raw_emg, np.ones(100)/100, mode='valid'))
        
        if baseline_drift > 1000:  # μV
            report['issues'].append("Motion artifacts detected, re-capture needed")
            report['pass'] = False
        
        return report
```

#### 3.3 新用户适配性能指标

**预期精度恢复曲线**：

```
样本数 | 预期准确率 | 典型用途
-------|----------|--------
0      | 75%      | 零样本（纯参考个体法）
1      | 80%      | 应急一次校准
3      | 88%      | 标准快速校准（推荐）
5      | 92%      | 深度校准
10     | 95%      | 完全个体化

说明：
- 假设后续任务为"在给定标准化信号上进行手势分类"
- 准确率 = 该用户在标准化后的分类精度
- baseline为"无标准化直接分类"的75%准确率
```

---

## 四、7维→11维的具体实施方案

### 4.1 从7维到11维的特征扩展

**当前7维特征**（基础，适用于快速处理）：
```
1. RMS - Root Mean Square（有效值，反映整体能量）
2. MAV - Mean Absolute Value（平均绝对值，时间域能量）
3. WL - Waveform Length（波形长度，信号复杂度）
4. ZC - Zero Crossings（零交叉数，频率信息）
5. SSC - Slope Sign Changes（坡度符号变化，频率信息）
6. EMAV - Enhanced MAV（加权MAV，对幅值变化敏感）
7. EWL - Enhanced WL（加权WL，对复杂度变化敏感）

计算耗时：~5-10ms（单个样本）
```

**新增的4维特征**（频域+时频特性）：

```
8. Hjorth Activity - 频谱中心频率的代理
   定义：activity = var(dx/dt)
   说明：信号一阶导数的方差，反映频率分布
   优势：无需FFT，计算快，对肌肉疲劳敏感

9. Hjorth Mobility - 频率特性强度
   定义：mobility = sqrt(var(d²x/dt²) / var(dx/dt))
   说明：信号变化的"速度"
   优势：低频干扰鲁棒性好

10. Hjorth Complexity - 频率成分多样性
    定义：complexity = mobility(dx/dt) / mobility(x)
    说明：信号的频谱复杂度
    优势：区分肌肉激活模式

11. Approximate Entropy（ApEn）- 信号规律性
    定义：ApEn(m, r, N) = φ(m) - φ(m+1)
    说明：量化信号的自相似性与随机性
    优势：对疲劳、肌肉同步性敏感，跨人群变异小
```

### 4.2 11维特征的计算与实现

```python
import numpy as np
from scipy import signal as scipy_signal

class EMG_Feature_Extractor_11D:
    """11维EMG特征提取器"""
    
    def __init__(self, sampling_rate=2000):
        self.fs = sampling_rate
        self.feature_names = [
            'RMS', 'MAV', 'WL', 'ZC', 'SSC', 'EMAV', 'EWL',
            'Hjorth_Activity', 'Hjorth_Mobility', 'Hjorth_Complexity', 'ApEn'
        ]
    
    def extract_11d_features(self, signal, window_size=None):
        """
        Args:
            signal: np.ndarray, shape (signal_length,)
            window_size: int，滑动窗口大小（如果None则全信号）
        
        Returns:
            features: np.ndarray, shape (11,) 或 (n_windows, 11)
        """
        
        if window_size is None:
            # 一次性提取整个信号的特征
            return self._compute_all_features(signal)
        else:
            # 滑动窗口提取多个特征向量
            n_windows = len(signal) // window_size
            features_windowed = []
            
            for i in range(n_windows):
                window = signal[i*window_size:(i+1)*window_size]
                feat = self._compute_all_features(window)
                features_windowed.append(feat)
            
            return np.array(features_windowed)
    
    def _compute_all_features(self, signal):
        """计算11维特征，返回向量"""
        
        f = np.zeros(11)
        
        # 基础7维
        f[0] = self._rms(signal)
        f[1] = self._mav(signal)
        f[2] = self._wl(signal)
        f[3] = self._zc(signal)
        f[4] = self._ssc(signal)
        f[5] = self._emav(signal)
        f[6] = self._ewl(signal)
        
        # 新增4维（Hjorth + ApEn）
        f[7], f[8], f[9] = self._hjorth_parameters(signal)
        f[10] = self._approximate_entropy(signal)
        
        return f
    
    # 基础7维特征计算
    @staticmethod
    def _rms(signal):
        return np.sqrt(np.mean(signal**2))
    
    @staticmethod
    def _mav(signal):
        return np.mean(np.abs(signal))
    
    @staticmethod
    def _wl(signal):
        return np.sum(np.abs(np.diff(signal)))
    
    @staticmethod
    def _zc(signal, threshold=0):
        """零交叉计数"""
        crossings = 0
        for i in range(1, len(signal)):
            if (signal[i-1] - threshold) * (signal[i] - threshold) < 0:
                crossings += 1
        return crossings
    
    @staticmethod
    def _ssc(signal, threshold=0):
        """坡度符号变化"""
        ssc_count = 0
        for i in range(1, len(signal) - 1):
            if ((signal[i] > signal[i-1]) and (signal[i] > signal[i+1])) or \
               ((signal[i] < signal[i-1]) and (signal[i] < signal[i+1])):
                ssc_count += 1
        return ssc_count
    
    @staticmethod
    def _emav(signal):
        """Enhanced MAV"""
        mav = np.mean(np.abs(signal))
        std_signal = np.std(signal)
        mean_abs = np.mean(np.abs(signal))
        if mean_abs < 1e-10:
            return 0
        return mav * (1 + std_signal / mean_abs)
    
    @staticmethod
    def _ewl(signal):
        """Enhanced WL"""
        wl = np.sum(np.abs(np.diff(signal)))
        std_signal = np.std(signal)
        mean_abs = np.mean(np.abs(signal))
        if mean_abs < 1e-10:
            return 0
        return wl * (1 + std_signal / mean_abs)
    
    @staticmethod
    def _hjorth_parameters(signal):
        """
        Hjorth特征：Activity, Mobility, Complexity
        
        Returns:
            activity, mobility, complexity: floats
        """
        # 一阶导数
        dx = np.diff(signal)
        # 二阶导数
        d2x = np.diff(dx)
        
        # Activity = Var(X)
        activity = np.var(signal)
        
        # Mobility = sqrt(Var(dX) / Var(X))
        if activity < 1e-10:
            mobility = 0
            complexity = 0
        else:
            mobility = np.sqrt(np.var(dx) / activity)
            
            # Complexity = Mobility(dX) / Mobility(X)
            if mobility < 1e-10:
                complexity = 0
            else:
                complexity = np.sqrt(np.var(d2x) / np.var(dx)) / mobility
        
        return activity, mobility, complexity
    
    @staticmethod
    def _approximate_entropy(signal, m=2, r=None):
        """
        Approximate Entropy (ApEn)
        
        Parameters:
            signal: 输入信号
            m: 嵌入维数（通常2-3）
            r: 相似度阈值（通常为std的0.2-0.25倍）
        
        Returns:
            apen: float，近似熵值
        """
        
        N = len(signal)
        
        # 自动设置r为信号std的0.2倍
        if r is None:
            r = 0.2 * np.std(signal)
        
        def _maxdist(x_i, x_j):
            """计算两个向量的最大欧几里得距离"""
            return max([abs(ua - va) for ua, va in zip(x_i, x_j)])
        
        def _phi(m):
            """计算相似度"""
            x = [[signal[j] for j in range(i, i + m - 1 + 1)] for i in range(N - m + 1)]
            C = [len([1 for x_j in x if _maxdist(x_i, x_j) <= r]) / (N - m + 1.0) 
                 for x_i in x]
            return (N - m + 1.0)**(-1) * np.sum(np.log(C))
        
        return abs(_phi(m+1) - _phi(m))
```

### 4.3 11维特征在标准化中的应用

**修改后的标准化器**：

```python
class EMG_Normalizer_11D:
    """使用11维特征的标准化器"""
    
    def __init__(self, phase1_baseline_stats_11d):
        """
        Args:
            phase1_baseline_stats_11d: dict
            {
                'feature_names': ['RMS', 'MAV', ..., 'ApEn'],
                'mu': array(11,),         # 人群均值
                'sigma': array(11,),      # 人群标准差
                'corr_matrix': array(11, 11),  # 特征间相关性
                'feature_importance': array(11,)  # 各特征对分类的重要性
            }
        """
        self.baseline_stats = phase1_baseline_stats_11d
        self.feature_extractor = EMG_Feature_Extractor_11D()
        self.subject_mu = None
        self.subject_sigma = None
        self.is_calibrated = False
    
    def calibrate_with_11d(self, raw_emg_samples):
        """
        快速校准（3个样本）
        
        Args:
            raw_emg_samples: np.ndarray, shape (3, signal_length)
        """
        # 提取11维特征
        features_11d = self.feature_extractor.extract_11d_features(raw_emg_samples)
        # shape: (3, 11)
        
        # 贝叶斯更新
        n_samples = features_11d.shape[0]
        sample_mean = features_11d.mean(axis=0)  # (11,)
        sample_cov = np.cov(features_11d.T)  # (11, 11)
        
        # 混合权重（基于样本数）
        alpha = min(n_samples / 10.0, 1.0)
        
        # 特征级别的更新（考虑特征间的相关性）
        self.subject_mu = (
            (1 - alpha) * self.baseline_stats['mu'] +
            alpha * sample_mean
        )
        
        # 使用Frobenius范数加权协方差
        self.subject_sigma = np.sqrt(np.diag(
            (1 - alpha) * np.diag(np.diag(self.baseline_stats['cov'])) +
            alpha * np.diag(sample_cov)
        ))
        
        self.is_calibrated = True
    
    def normalize_11d(self, raw_emg_signal):
        """
        标准化新信号（11维特征空间）
        
        Args:
            raw_emg_signal: np.ndarray, shape (signal_length,)
        
        Returns:
            normalized_features_11d: np.ndarray, shape (11,)
        """
        if not self.is_calibrated:
            raise ValueError("Normalizer not calibrated yet")
        
        # 提取11维特征
        features = self.feature_extractor.extract_11d_features(raw_emg_signal)
        # shape: (11,)
        
        # Z-score标准化
        normalized = (features - self.subject_mu) / (self.subject_sigma + 1e-8)
        
        return normalized
    
    def normalize_11d_windowed(self, raw_emg_signal, window_size=256):
        """
        滑动窗口标准化（适用于实时处理）
        
        Args:
            raw_emg_signal: shape (signal_length,)
            window_size: 窗口大小（样本数）
        
        Returns:
            normalized_trajectory: shape (n_windows, 11)
        """
        features_windowed = self.feature_extractor.extract_11d_features(
            raw_emg_signal,
            window_size=window_size
        )
        # shape: (n_windows, 11)
        
        # 对每个窗口的每个特征进行标准化
        normalized_windowed = (
            (features_windowed - self.subject_mu) /
            (self.subject_sigma + 1e-8)
        )
        
        return normalized_windowed
```

### 4.4 7维→11维的增益分析

**预期改进**（基于相关文献）：

```
评估指标        | 7维特征 | 11维特征 | 改进幅度
            |        |         |
跨人群准确率    | 86%    | 91%     | +5.8%
新用户快速适配  | 85%    | 90%     | +5.9%
（3样本）      |        |         |
计算耗时增加    | 基准   | 1.8×    | +80%（可接受）
特征稳定性      | 0.76   | 0.84    | +10.5%
（Intra-class  |        |         | 相关系数）
```

**为什么新增的4维有效**：

1. **Hjorth参数**：捕捉频率域信息，无需FFT
   - Activity：肌肉激活强度（补充MAV的频率信息）
   - Mobility/Complexity：区分肌肉疲劳状态

2. **ApEn**：规律性度量，跨人群强鲁棒性
   - 对肌肉协同模式敏感
   - 年龄、性别影响小（相比于RMS影响大得多）
   - 个体间差异相对均匀

---

## 五、数据收集与验证计划

### 5.1 全周期数据收集计划

**Timeline：4周，分三个阶段**

#### Phase 1A：基线数据收集（Week 1-2）

**被试招募**：
```
目标样本量：30-35人

招募条件：
□ 年龄：20-60岁
□ 健康状态：无神经肌肉疾病、肩臂损伤史
□ 排除条件：
  - 怀孕
  - 正在服用肌肉松弛剂
  - 肱二头肌功能受损
  - 3个月内做过上肢手术

人口统计目标：
性别：    男50% (15-18人), 女50% (15-18人)
年龄：    20-30 (8人), 31-40 (8人), 41-50 (8人), 51-60 (6人)
运动水平：久坐 (12人), 轻度运动 (12人), 运动员 (8人)
BMI：     18-25 (15人), 25-30 (12人), >30 (3-5人)
```

**每个被试采集时间表**：

```
Day 1：上午（1小时）
├─ 知情同意、问卷（15min）
├─ 人体测量（20min）
│  ├─ 体重、身高
│  ├─ 上臂长度 (肩锋→肘尖)
│  ├─ 上臂周长（肱二头肌处）
│  ├─ 前臂周长（腕上5cm）
│  ├─ 肩宽
│  └─ 皮脂厚度 (二头肌+三头肌, Skinfold calipers)
└─ EMG设备适配（15min）
   ├─ 皮肤清洁与准备
   ├─ 电极固定
   └─ 信号质量检验 (SNR > 5)

Day 2：上午（45min）
├─ 热身（5min）
└─ EMG采集（40min）
   ├─ 静息基线（2min）
   ├─ MVC标定（3×3s = 9min）
   ├─ 30% MVC标准动作（10重复 × 3s = 30s + 间隔 = 5min）
   ├─ 50% MVC标准动作（10重复 × 3s = 5min）
   └─ 70% MVC标准动作（10重复 × 3s = 5min）

总耗时：1.75小时/人
总数据量：35人 × 1.75h = 61.25人·小时
按日程：每天6人 → 6天完成（Week 1-2）
```

#### Phase 1B：标准化模型构建与验证（Week 2.5-3）

**所需工作**：

```
1. 特征提取（1人-day）
   - 从35×3段采集的原始EMG提取11维特征
   - 35人 × 3个强度 × 10个重复 = 1050个特征向量

2. 数据分析（0.5人-day）
   ├─ 描述性统计（按性别分层）
   ├─ 异常值检测与处理
   └─ 相关性分析

3. 模型拟合（0.5人-day）
   ├─ 生理特征 → EMG幅值回归模型
   ├─ 人群EMG统计模型拟合
   └─ 参考个体选择

4. LOSO交叉验证（1.5人-day）
   - 35个fold，每个fold耗时~10min
   - 最终生成性能报告

输出物：
□ BaselineDB.csv (35 × 30列)
□ NormalizationModels.pkl (3个算法)
□ PopulationStatistics_11D.json
□ LOSO_CV_Results.pdf (性能报告)
```

#### Phase 2：新用户适配验证（Week 3.5-4）

**验证目标**：验证快速校准的有效性

**新的验证被试**：
```
招募10-15名新被试（不在baseline中）
每人采集与Phase 1相同的EMG数据

验证流程：
对每个新被试
  使用仅前3个样本 → 快速校准
  用剩余样本进行"盲测"（是否能正确标准化）

评估指标：
□ 准确率（标准化后能否正确分类动作）
□ 跨人群一致性（标准化后与baseline的相似度）
□ 校准时间（<200ms）
```

### 5.2 质量控制与验证指标

#### 5.2.1 数据收集质量

**实时检查清单**：

```python
class DataQualityControl:
    def check_signal_quality(self, raw_emg, expected_fs=2000):
        """采集时的实时质量检查"""
        
        checks = {}
        
        # 1. 采样率
        checks['sampling_rate'] = len(raw_emg) / (3.0 * expected_fs) > 0.95
        
        # 2. 信噪比（SNR）
        noise = np.std(raw_emg[:100])  # 前100ms假设为噪声
        signal = np.std(raw_emg)
        checks['SNR'] = (signal / noise) > 5
        
        # 3. 基线漂移（运动伪迹）
        baseline_drift = (np.max(raw_emg) - np.min(raw_emg)) / np.abs(np.mean(raw_emg))
        checks['baseline_stability'] = baseline_drift < 5  # 不超过5倍均值
        
        # 4. 峰值范围（不饱和）
        peak_ratio = np.max(np.abs(raw_emg)) / (np.quantile(np.abs(raw_emg), 0.99))
        checks['no_saturation'] = peak_ratio < 1.1
        
        # 5. 周期性（应有明确的肌肉激活周期）
        fft = np.fft.fft(raw_emg)
        power_spectrum = np.abs(fft)**2
        checks['has_activation_pattern'] = (
            np.max(power_spectrum[int(len(power_spectrum)*0.02):int(len(power_spectrum)*0.2)]) >
            np.mean(power_spectrum) * 2
        )
        
        return checks
    
    def check_anthropometric_data(self, anthropo_dict):
        """人体测量数据合理性检查"""
        
        checks = {}
        
        # 合理范围检查
        arm_circ = anthropo_dict['arm_circumference']
        checks['arm_circumference_reasonable'] = 20 < arm_circ < 40  # cm
        
        arm_len = anthropo_dict['arm_length']
        checks['arm_length_reasonable'] = 20 < arm_len < 35  # cm
        
        bmi = anthropo_dict['weight'] / (anthropo_dict['height']/100)**2
        checks['BMI_reasonable'] = 15 < bmi < 40
        
        # 内部一致性（上臂周长与体重的关系）
        expected_arm_circ = 20 + anthropo_dict['weight'] * 0.05  # 粗略估计
        checks['consistency_arm_weight'] = (
            abs(arm_circ - expected_arm_circ) / expected_arm_circ < 0.3
        )
        
        return checks
```

#### 5.2.2 模型验证指标

```python
class ModelValidation:
    """标准化模型的性能验证"""
    
    def compute_validation_metrics(self, predictions, ground_truth):
        """
        predictions: 标准化后的特征值
        ground_truth: 真实标签（e.g., 动作类别）
        """
        
        metrics = {}
        
        # 1. 分类准确率（如后续用于手势识别）
        metrics['classification_accuracy'] = accuracy_score(ground_truth, predictions)
        
        # 2. 跨人群一致性（Intraclass Correlation Coefficient）
        metrics['ICC_3_1'] = calculate_icc(
            predictions, model='ICC3k'
        )  # ICC(3,k) for absolute agreement
        
        # 3. 标准化后的变异系数
        metrics['cross_subject_cv'] = np.std(predictions) / np.mean(np.abs(predictions))
        # 目标：CV < 0.15（15%）
        
        # 4. 个体内重复性（Intra-subject repeatability）
        metrics['intra_subject_repeatability'] = np.mean([
            np.std(subj_predictions) for subj_predictions in group_by_subject(predictions)
        ])
        # 目标：越小越好，< 0.1
        
        # 5. Robustness to noise（加高斯噪声后的性能保留）
        noisy_predictions = predictions + np.random.normal(0, 0.1, predictions.shape)
        metrics['noise_robustness'] = (
            accuracy_score(ground_truth, noisy_predictions) /
            accuracy_score(ground_truth, predictions)
        )
        # 目标：> 0.95（噪声不影响分类）
        
        return metrics
```

#### 5.2.3 最终验收标准

```
Phase 1验收：
✓ BaselineDB完成度：100%（35人）
✓ 数据质量通过率：>95%（异常值<5%）
✓ 特征计算成功率：100%（无NaN/Inf）
✓ LOSO CV准确率：>86%（Z-score法）或>92%（贝叶斯法）

Phase 2验收：
✓ 新用户快速适配成功率：>90%（3样本）
✓ 标准化后跨人群一致性 ICC：>0.75
✓ 校准时间：<200ms
✓ 在unseen被试上的准确率：>88%

整体验收：
✓ 系统部署就绪（可移交给应用团队）
✓ 完整文档（SOP + API + 用户手册）
```

---

## 六、实施风险与应对方案

### 6.1 主要风险

| 风险 | 影响 | 发生概率 | 应对方案 |
|------|------|---------|--------|
| **基线被试招募困难** | 延迟1-2周 | 中 | 预留备选被试名单；提前发布招募公告 |
| **EMG采集质量不稳定** | 数据可用率<80% | 低-中 | 采集前充分培训操作员；每日质控检查 |
| **电极相容性问题** | 需更换电极品牌 | 低 | 前期小样本测试；备选电极 |
| **跨人群差异超预期大** | 标准化效果不理想 | 低-中 | 增加非线性模型（SVM/Neural Net）；分类别建模 |
| **新用户适配收敛慢** | 需要>10样本 | 低 | 启用元学习方案；增加先验信息 |
| **计算资源不足** | 实时处理延迟 | 极低 | 优化特征提取代码；使用GPU |

### 6.2 备选方案

**如果7维→11维的改进<3%**：
```
保留7维方案，投入更多到"参考个体法"和"元学习"
成本低、易维护
```

**如果跨人群差异仍>15%**：
```
分类建模：分别为男性、女性、年龄段建立独立模型
准确率+3-5%，计算复杂度+50%
```

**如果新用户需要>5样本**：
```
采用辅助设备（传感器融合）：
- 加入力传感器（获得MVC参考）
- 加入IMU（运动补偿）
准确率+5-8%，硬件成本+
```

---

## 七、交付物清单与时间表

### 交付物

```
Week 2：
□ BaselineDB_v1.0.csv (35人 × 30特征)
□ DataQualityReport.pdf

Week 3：
□ NormalizationModels_11D.pkl (3个算法 + 权重)
□ LOSO_CrossValidation_Results.xlsx (含性能指标)
□ PopulationStatistics.json

Week 4：
□ QuickAdaptation_System.py (部署代码 + 文档)
□ SOP_NewUserOnboarding.pdf (用户操作手册)
□ FinalValidationReport.pdf (新用户验证结果)
□ Implementation_Guide.md (技术文档)
```

### 预期成果

```
定量指标：
- 跨人群准确率：从75% → 91%（+16%）
- 新用户快速适配（3样本）：>90% 精度恢复
- 系统延迟：<300ms（采集+处理+输出）

定性成果：
- 建立业界标准的EMG标准化框架
- 可复用的Python工具库
- 详细的理论与实现文档
```

---

## 附录：关键参考

### 论文与资源

1. **标准化综述**：
   - Standardizing EMG Pipelines for Muscle Synergy Analysis (MDPI 2024)
     https://www.mdpi.com/2624-6120/6/4/68

2. **基准对比**：
   - Ref-EMGBench: Benchmarking Reference Normalization (OpenReview 2024)
     https://openreview.net/forum?id=ju4EwaLeoI

3. **年龄性别影响**：
   - A Comparison of EMG Normalization Techniques in Older and Young Adults (MDPI 2024)
     https://www.mdpi.com/2411-5142/9/2/90

4. **快速校准**：
   - From zero- to few-shot: Deep temporal learning of wrist EMG (2025)
     https://pubmed.ncbi.nlm.nih.gov/40967242/

5. **域适应**：
   - Cross-subject EMG hand gesture via dynamic domain generalization (IEEE 2024)
     https://ieeexplore.ieee.org/document/10340691/

6. **信号处理**：
   - Surface EMG Signal Processing and Classification Techniques (PMC)
     https://pmc.ncbi.nlm.nih.gov/articles/PMC3821366/

### 工具与库

- **libEMG**：开源EMG特征提取库
  https://libemg.github.io/

- **BioSPPy**：生物信号处理Python包
  pip install biosppy

- **scikit-learn**：机器学习标准库
  用于分类验证

---

**文档版本**：1.0  
**最后更新**：2026-04-15  
**维护者**：EMG标准化工作组
