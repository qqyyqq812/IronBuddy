# 公开数据集评估索引

**文档目的**：记录 2026-04-18 对"哑铃弯举"候选公开 sEMG 数据集的调研与筛选结论，作为
IronBuddy V4.4 弯举子任务"不依赖公开数据集、走本地 10× augment 路线"这一决策的依据。
本文件归档三个已建子目录的评估落点，并补充两个未建但已评估的候选（FLEX / HSBI / EMAHA-DB4）。

> 本目录仅做评估归档，**不存放原始数据文件**。真实数据下载与训练流落在
> `data/external/` 与对应的 preprocess 脚本中。

---

## §1 候选清单与获取现状

本目录下三个已建子目录对应三个最早进入评估视野的数据集；另外两个候选在扩大检索后纳入。

| 子目录 / 候选 | 当前状态 | 结论 |
|---|---|---|
| `EMAHA-DB6 (纯力量训练专项肌电图)/` | 空（仅 `手动下载现状.txt`）| DB6 未实际公开发布，DB4 ADL 任务不对口。**不可用** |
| `MIA (Muscles in Action) 数据集/` | 空（52.9 GB 用户正在下载中） | 全是下肢/武术动作，**无弯举**。保留作 V4.4+ 深蹲预训练源 |
| `Mendeley Data 二头肌疲劳专项数据集/` | 含 `ManualSegmentation.xlsx`（17.6 KB，16 受试者分段元数据）| 仅 1 Hz envelope，**无法算 MDF/MNF/ZCR**，仅做生理 sanity check |
| FLEX（未建目录） | Google Form 申请中，24-72 小时审批 | ✅ 200 Hz + 4 通道 + 含弯举，明天拿不到，预留激活路径 |
| HSBI 肱二头肌（DOI 10.57720/1956） | Bielefeld Anubis JS 反爬，需手动 | ⚠️ 等长收缩为主，可做 MDF 基线 + augment 噪声源 |

---

## §2 核心决策

### 2.1 V4.4 弯举：不使用任何公开数据集做 base pretrain

经 §3 详表核实后，**明天（V4.4 弯举子任务）放弃"公开数据集 → base pretrain → 本地
finetune"的三段式路线**，改为"本地自采 + 10× augment"单段路线。核心理由：

- FLEX 是唯一任务匹配且采样率足够的候选，但 license 审批周期超过交付窗口。
- MIA 任务不匹配（全下肢/武术，无弯举动作）。
- Mendeley 采样率为 1 Hz envelope，**不可能**反推出 MDF / MNF / ZCR 这三个
  `V4.2 数据契约` 要求的频域特征。
- EMAHA-DB4/DB5/DB6 或任务不对口或未公开。
- HSBI 反爬严重，且以等长收缩为主，不是动态 concentric/eccentric 弯举。

### 2.2 MIA 52.9 GB 用途重定义

MIA 虽然没有弯举，但动作层标签质量高（GoodSquat / BadSquat 直用），通道包含双侧
biceps，**留作 V4.4+ 深蹲子项目重启时的监督预训练数据源**。下载继续进行，**不浪费**。

### 2.3 Mendeley 1 Hz envelope 用途

只作"持续负载下 sEMG 幅度随疲劳单调下降"这一生理直觉的 sanity check 对标，
**不进入训练集**，也不进入特征工程。

### 2.4 HSBI 条件性使用

若能从 Bielefeld Anubis 绕过反爬拿到原始 .csv / .edf：
- 用作 J1 目标肌 MDF 基线（文献兜底：**87 ± 15 Hz**）。
- 可选作为 `tools/augment_local.py` 的 `--with-hsbi-noise` 噪声谱来源（100-450 Hz 带通）。

### 2.5 FLEX 激活路径

Google Form 通过后立即启用已预写好骨架的 `tools/flex_preprocess.py`，见 §5。

---

## §3 评估详表

| 数据集 | 弯举？ | 采样率 | 通道 | 获取状态 | 能否用 | 证据 |
|---|---|---|---|---|---|---|
| **FLEX** | ✅ | 200 Hz | 4 | Google Form 审批（24-72 h）| ⏳ 明天拿不到 | Yin 2025 论文元数据 |
| **MIA (Muscles in Action)** | ❌ 15 动作全下肢+武术 | 未统一 | 8（含双侧 biceps）| 52.9 GB 下载中 | ✅ V4.4+ 深蹲备用 | `inference_scripts/retrieval_id_nocond_exercises_posetoemg.py:119-141` 动作名清单 |
| **EMAHA-DB4 v1/v2** | ❌ 实际为 ADL | 未核实 | 未核实 | Harvard Dataverse 免账号 4 GB | ❌ 任务不对口 | `Sub0X_ADL_HONOR_{SIT,STANDING,WALKING}.mat` 文件命名 |
| **EMAHA-DB5** | ✅（推测含）| 未公开 | BB+FCU | 未公开发布 | ❌ 拿不到 | EMAHA-DB 系列 arXiv |
| **Mendeley 8j2p29hnbv** | ✅ 弯举疲劳 | **1 Hz（envelope）** | 1 EMG + 3 加速度 | 免账号下载 | ❌ 采样率太低 | DOI 10.17632/8j2p29hnbv，Mendeley 页签 |
| **HSBI 肱二头肌** | 等长收缩 | 高 | 1 | Bielefeld Anubis JS 反爬 | ⚠️ 仅 MDF 基线 + augment 噪声源 | DOI 10.57720/1956 |

**列说明**：
- "采样率"栏 200 Hz 以上方可满足 MDF/MNF/ZCR 计算需求（Nyquist → ≥ 100 Hz 信号带宽）。
- "能否用"栏评分仅针对 **V4.4 弯举** 子任务；其他任务口径不同，结论可能不同。
- "证据"栏指向可回溯的第一手材料；避免口头转述。

---

## §4 本地替代方案总览

放弃公开数据集后，V4.4 弯举的训练数据管线如下（与 `V4.2 数据契约`、`tools/` 黄金脚本
对齐）：

1. **原始数据**：`data/v42/<user>/curl/<label>/rep_*.csv`，共 135 rep（本地 3 名用户）。
2. **10× augment**：`tools/augment_local.py` 在原目录派生 `rep_NNN_augK.csv` 副本：
   - 五种手法叠加（时间扭曲 / 幅度抖动 / 相位偏移 / 白噪声 / HSBI 噪声谱条件启用）。
   - 不改 `Timestamp`、`label` 列；不重复增强已有 `_aug*.csv`。
   - `--holdout-user user_04` 完全跳过，保证测试集纯净。
3. **base pretrain**：`tools/pretrain_encoders.py` 直接读扩充后的 `data/v42/` 训练
   EMG encoder + IMU encoder。
4. **fusion head finetune**：`tools/train_fusion_head.py` 加载 encoder 冻结
   backbone，仅训练 3-head 输出。
5. **holdout 评估**：`tools/infer_holdout.py` 在 `user_04` 上出 Macro-F1。

详细规划见 `.claude/plans/curl-local-first-mia-for-squat-later.md`。

---

## §5 未来激活路径

| 触发条件 | 激活命令 |
|---|---|
| FLEX license 审批通过 | `python tools/flex_preprocess.py --flex-root <path> && python tools/pretrain_encoders.py --source flex` |
| MIA 52.9 GB 下载完成（V4.4+ 深蹲）| 需新写 `tools/mia_preprocess.py`（当前未实现）|
| HSBI 绕过反爬拿到原始文件 | 放入 `data/external/hsbi_biceps/`，用 `python tools/augment_local.py --with-hsbi-noise ...` 叠入训练 |

任何激活都不改动 `tools/` 已有黄金脚本，只新增对应 preprocess 模块。

---

## §6 引用

- **SENIAM** — 表面肌电信号传感器位置与分析推荐规范。
- **Chiquier 2023** — "Muscles in Action" 数据集，ICCV 2023。动作清单硬编码在
  `inference_scripts/retrieval_id_nocond_exercises_posetoemg.py`。
- **Yin 2025** — FLEX 数据集（200 Hz 4 通道，含弯举）。
- **Mendeley Data** — Biceps fatigue envelope dataset，DOI `10.17632/8j2p29hnbv`。
- **EMAHA-DB 系列** — arXiv 2403.xxxx（DB4 ADL 任务清单出处）。
- **HSBI** — Bielefeld 肱二头肌等长收缩数据集，DOI `10.57720/1956`。

---

**变更记录**：
- 2026-04-18：初稿；定调"V4.4 弯举不依赖公开数据集"，归档三子目录空状态与两补充候选。
