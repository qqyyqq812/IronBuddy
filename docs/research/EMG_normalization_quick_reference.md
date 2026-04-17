# EMG 跨人群标准化快速参考指南

## 执行概要

**问题**：同一人群中不同个体的EMG信号差异大（RMS幅值可能相差3-5倍），导致无法直接对比或构建通用模型

**解决方案**：三层解耦框架 → 特征标准化 → 快速个体化

**预期成果**：
- 跨人群准确率从 75% → 91%（+16%）
- 新用户3样本快速校准，90%精度恢复
- 系统延迟 < 300ms

---

## 一、核心概念

### 三层标准化框架

```
原始EMG信号 = 个体生理特征 × 动作意图 × 环境因素
                ↓
                目标：消除"个体生理特征"，保留"动作意图"
```

**三个关键步骤**：

1. **基线建立** (Week 1-2)
   - 招募35人，覆盖男女、体型、年龄多样性
   - 采集标准化动作 (30%, 50%, 70% MVC肱二头肌屈曲)
   - 建立人群EMG统计模型

2. **参数标准化** (Week 2-3)
   - 三种算法：Z-score / 贝叶斯 / 参考个体
   - 通过LOSO交叉验证选择最优算法

3. **新用户快速适配** (Week 3-4)
   - 新用户仅需3次标准收缩 (~2分钟)
   - 系统自动推断其标准化参数
   - 即时反馈质量和可用性

---

## 二、7维 → 11维特征扩展

### 为什么扩展？

7维特征缺乏频率和规律性信息，跨人群泛化能力有限。

| 特征维度 | 维数 | 新增原因 | 预期改进 |
|---------|------|--------|--------|
| RMS | 1 | 基础能量 | - |
| MAV | 1 | 平均绝对值 | - |
| WL | 1 | 波形复杂度 | - |
| ZC, SSC | 2 | 零交叉/坡度 | - |
| EMAV, EWL | 2 | 增强特征 | - |
| **Hjorth** | **3** | **频率特征** | **+3-4% 准确率** |
| **ApEn** | **1** | **信号规律性** | **+2-3% 准确率** |
| **小计** | **11** | | **+5-7% 总体** |

### 新增4维的含义

```
特征              | 定义                    | 对什么敏感
Hjorth_Activity   | var(信号)              | 肌肉激活强度
Hjorth_Mobility   | sqrt(var(dX)/var(X))   | 频率特性
Hjorth_Complexity | Mobility(dX)/Mobility(X) | 频谱复杂度
ApEn              | 近似熵                  | 肌肉协同模式、疲劳
```

**关键优势**：
- Hjorth参数：无需FFT，O(n)计算，适合实时处理
- ApEn：对个体间差异不敏感（跨人群鲁棒性好）

---

## 三、三种标准化算法对比

### A. Z-score 标准化（推荐初期）

```
EMG_norm = (EMG - μ_个体) / σ_个体

μ_个体 = (1-α)×μ_人群 + α×μ_测量
σ_个体 = (1-α)×σ_人群 + α×σ_测量

α = min(样本数/10, 1)  // 样本越多，个体化程度越高
```

**优点**：
- 计算简单，延迟<50ms
- 需要样本少（3-5个）
- 易于解释

**缺点**：
- 不考虑特征间相关性
- 当样本非常少时，个体化程度有限

**适用场景**：快速部署、实时应用、延迟敏感

### B. 贝叶斯参数估计（推荐高精度）

```
观测：EMG ~ N(μ_个体, σ_个体²)

先验：μ ~ N(μ_人群, τ²), σ ~ InverseGamma(α, β)

后验：使用共轭先验性质快速更新
```

**优点**：
- 最优个体化参数估计
- 自然地结合先验和观测
- 考虑参数不确定性

**缺点**：
- 计算复杂度高（O(n²)）
- 需要样本略多（5-10个）
- 延迟可能~200ms

**适用场景**：高精度需求、可以容忍延迟

### C. 参考个体法（零样本适配）

```
1. 从baseline选择7-10个代表性个体（体型跨度分位数：10%, 25%, ..., 90%）
2. 计算新被试与各参考个体的"相似度"（基于人体测量）
3. 加权内插参考个体的标准化参数
```

**优点**：
- 无需新被试样本（零样本）
- 延迟极低（<10ms）
- 即插即用

**缺点**：
- 精度相对低（75-80% vs 90%+）
- 需要精确的人体测量

**适用场景**：初期快速部署、无法采集校准样本的情况

### 推荐策略（分阶段）

```
Month 1（MVP）：算法C + A的混合
  └─ 零样本快速识别 + 可选的3样本精细化
  └─ 精度预期：82-85%

Month 2-3（增强版）：升级到算法A + B选择
  └─ 自动选择：如果用户提供3+样本 → 用A/B；否则用C
  └─ 精度预期：88-92%

Month 4+（生产版）：完全贝叶斯 + 元学习
  └─ 元学习学习"如何快速适应"，1-2梯度更新
  └─ 精度预期：94-96%
```

---

## 四、新用户快速适配工作流

### 用户视角（总耗时：5分钟）

```
1. 穿戴电极        (1 min)
   └─ 清洁肱二头肌长头上1/3处
   └─ 粘贴预制电极
   └─ 连接采集设备

2. 人体测量        (2 min)
   └─ 上臂周长 (单圈皮尺)
   └─ 上臂长度 (肩→肘)
   └─ 年龄、性别（预填）

3. 标准收缩校准    (2 min)
   └─ 做3次肱二头肌屈曲
   └─ 每次3秒，间隔30秒
   └─ 系统自动采集

4. 质量检查 (自动)
   └─ ✓ 适配成功
   └─ ✗ 需要重新采集（不符合质量）
```

### 系统内部流程

```
PreProcess → FeatureExtract → Calibrate → QualityCheck → Normalize
  (50ms)      (10ms)         (100ms)    (50ms)        (ready!)
                                │
                    ┌───────────┴────────────┐
                    ↓                        ↓
                PASS                       FAIL
              返回参数                    提示重采
```

### 质量检查准则

```
通过标准：
□ SNR > 5 (信号幅值/噪声幅值 > 5)
□ 伪迹率 < 10% (运动伪迹占比 < 10%)
□ 重复性 CV < 30% (3次收缩的变异系数 < 30%)
□ 幅值范围合理 (0.3-3倍人群均值)

未通过 → 用户重新采集（通常1-2次即可）
```

---

## 五、部署检查清单

### Phase 1 验收标准

```
□ 被试招募：35人
□ 采集完成率：100%
□ 数据质量通过率：>95%
  └─ 异常值（3σ外）<5%
  └─ 缺失值：0%
  └─ SNR检验：所有样本 SNR > 5

□ 特征提取：100% 成功
  └─ 无NaN/Inf值
  └─ 11维特征都在合理范围

□ 模型验证：LOSO CV
  └─ Z-score 准确率 > 86%
  └─ 贝叶斯准确率 > 92%
  └─ 参考个体准确率 > 79%
```

### Phase 2 验收标准

```
□ 新被试验证：10+ 人
□ 适配成功率：>90% (3样本)
□ 标准化后一致性：ICC > 0.75
□ 校准时间：<200ms
□ 准确率恢复：>88% (相对人群基准)
```

### 部署前清单

```
□ 代码审查完成
  └─ 特征提取：通过单元测试
  └─ 标准化算法：数值验证 (已知输入)
  └─ 质量检查：边界情况测试

□ 文档完整
  └─ API 文档
  └─ SOP 用户手册
  └─ 故障排除指南

□ 数据安全
  └─ 个体参数加密存储
  └─ 审计日志开启
  └─ 隐私合规检查

□ 性能优化
  └─ 特征提取 < 20ms
  └─ 标准化 < 50ms
  └─ 总端到端 < 300ms
```

---

## 六、常见问题与故障排除

### Q1：新用户标准化后准确率只有70%，低于预期
**可能原因**：
- 采集时运动伪迹过多 → 重新采集，保持手臂稳定
- 电极接触不良 → 清洁皮肤、重新贴电极
- 用户肌肉特征异常（如发达运动员或肌少症） → 考虑分层模型

**解决方案**：
```
if accuracy < 80%:
    if quality_check_failed:
        提示用户重新采集
    else if outlier_detected:
        考虑加入"异常个体"分类模型
        或增加该用户类型的training数据
```

### Q2：为什么有的用户3样本精度恢复好，有的不好？
**可能原因**：
- 个体间差异本身就大（生理特征多样性）
- 先验（人群模型）与该用户偏离 → Bayesian更新缓慢
- 样本不独立（3个样本来自同一个动作重复）

**解决方案**：
```
for each new_user:
    if accuracy_recovery < 85% and n_samples == 3:
        建议用户提供5-10样本
        或使用"元学习"方案（学习更好的初始化）
```

### Q3：跨人群一致性ICC=0.6，未达到0.75的目标
**可能原因**：
- 选择的标准化算法不匹配该任务
- 7维→11维的改进不足（需要更多频域特征）
- 被试间的生理差异超出预期（需要分层/分段建模）

**解决方案**：
```
逐个尝试：
1. 从Z-score升级到Bayesian → +3-5% ICC
2. 11维 → 15维 (加入Wavelet/频谱特征) → +2-3% ICC
3. 分层模型 (男性/女性/年龄段) → +5-8% ICC
```

### Q4：采集时间太长（>3分钟），用户体验差
**解决方案**：
```
方案A（快速路线）：
  - 仅需1个标准收缩样本 + 参考个体法
  - 耗时 < 1 分钟
  - 精度: 78-82%（可接受for certain apps）

方案B（平衡路由）：
  - 2个样本 + 元学习
  - 耗时: 1.5 分钟
  - 精度: 85-88%

方案C（精确路由）：
  - 5个样本 + 贝叶斯
  - 耗时: 3 分钟
  - 精度: 92-94%

根据应用场景自适应选择
```

---

## 七、代码使用示例

### 初始化系统

```python
from EMG_normalization_implementation import QuickAdaptation_System

# 初始化系统（假设Phase 1已完成）
system = QuickAdaptation_System(
    phase1_results_dir='/path/to/phase1_output/',
    algorithm='bayesian'  # 或 'zscore', 'reference'
)
```

### 新用户适配

```python
# 原始EMG采集 (3个重复，每个3秒@2000Hz)
raw_emg_samples = np.array([
    raw_signal_1,  # shape (6000,)
    raw_signal_2,  # shape (6000,)
    raw_signal_3   # shape (6000,)
])

# 人体测量数据（可选，reference法需要）
user_anthropo = {
    'arm_length': 28.5,        # cm
    'arm_circumference': 29.0, # cm
    'skinfold': 12.0,          # mm
    'age': 35                  # years
}

# 执行适配
success, report = system.new_user_onboarding(
    user_id='subject_001',
    raw_emg_samples=raw_emg_samples,
    user_anthropo=user_anthropo
)

if success:
    print("✓ Adaptation successful")
    print(f"Normalization params: {report['normalization_params']}")
else:
    print("✗ Adaptation failed:")
    for issue in report['issues']:
        print(f"  - {issue}")
```

### 实时标准化

```python
# 使用已适配的系统对新信号进行标准化

raw_signal = np.array([...])  # 新采集的EMG信号

# 一次性提取11维特征
normalized_features = system.normalize_signal(raw_signal)
# shape: (11,)

# 或滑动窗口处理（用于连续监测）
normalized_trajectory = system.normalize_signal(
    raw_signal,
    window_size=512  # 256ms窗口 @ 2000Hz
)
# shape: (n_windows, 11)

# 后续可用于分类、手势识别等下游任务
gesture_class = classifier.predict(normalized_features)
```

---

## 八、参考文献与资源

### 核心论文（2024-2025）

1. **标准化综述**
   - Title: "Standardizing EMG Pipelines for Muscle Synergy Analysis"
   - Venue: MDPI 2024
   - URL: https://www.mdpi.com/2624-6120/6/4/68

2. **基准对比**
   - Title: "Ref-EMGBench: Benchmarking Reference Normalization"
   - Venue: OpenReview 2024
   - URL: https://openreview.net/forum?id=ju4EwaLeoI

3. **快速校准**
   - Title: "From zero- to few-shot: deep temporal learning of wrist EMG"
   - Venue: PubMed 2025
   - URL: https://pubmed.ncbi.nlm.nih.gov/40967242/

4. **域适应**
   - Title: "Cross-subject EMG hand gesture via dynamic domain generalization"
   - Venue: IEEE 2024
   - URL: https://ieeexplore.ieee.org/document/10340691/

### 开源工具

- **libEMG**：特征提取库 (https://libemg.github.io/)
- **BioSPPy**：生物信号处理 (pip install biosppy)
- **scikit-learn**：分类验证工具

---

## 九、时间表与资源计划

```
Week 1-2：Phase 1 基线建立
  资源：1名采集员 + 2名数据分析员
  输出：35人baseline数据库

Week 2.5-3：Phase 2 模型开发
  资源：1名算法工程师 + 1名数据科学家
  输出：3种标准化算法 + LOSO验证

Week 3.5-4：Phase 3 新用户验证
  资源：1名系统测试员 + 1名工程师
  输出：部署就绪的系统 + 完整文档

Total：4 周，3-4人月
```

---

**文档版本**：1.0  
**最后更新**：2026-04-15  
**维护者**：EMG标准化工作组

Questions? 参考主文档 `EMG_cross_subject_normalization_plan.md`
