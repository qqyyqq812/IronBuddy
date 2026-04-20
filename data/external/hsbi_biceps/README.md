# HSBI Biceps Brachii sEMG 数据集

> **用途级别：L3 only** — 仅用于 J1 频域基线 + J2 RMS 归一化基线兜底。
> **禁止**用于 Encoder 预训练、迁移学习、任何模型权重初始化。
> 参见 `.claude/plans/flex-curl-only-pivot.md` §1.6。

---

## 简介

HSBI (Hochschule Bielefeld University of Applied Sciences and Arts) 肱二头肌
表面肌电公开数据集 — 11 名健康受试者在肱二头肌等长收缩下、分别以最大自主收缩
(MVC) 的 20%、50%、75% 三个力度下做多重复试验的单通道 sEMG 原始录制。

- **采集**：Bielefeld 应用科技大学 生物医学工程实验室
- **通道**：单通道 sEMG（肱二头肌肌腹正中）
- **受试者**：11 人（S01-S11）
- **条件**：等长收缩 × 3 力度 (20/50/75 %MVC) × 多重复
- **采样率**：典型 2000 Hz（以真实压缩包为准）
- **格式**：CSV / WAV / MAT（以实际压缩包为准）

## 用途（本项目内）

| 用途 | 怎么用 | 输出到哪 |
|-----|-------|---------|
| J1 频域基线 | Welch PSD → MDF/MNF 统计分布 | `docs/research/family_baselines.json` `curl.mdf_mean_hz` |
| J2 RMS 归一化 | 取 50%MVC 作中等负载参考，`rms/rms_p95` 分布 | 同上 `curl.rms_norm_p50/p95` |

参考基线值（文献 + 本项目期望）：
- **标准弯举 MDF**：87 ± 15 Hz（Hahne 数据 + Cifrek et al. 2009 综述交叉）
- **中等负载 RMS/MVC**：0.45 ± 0.15

## 重要约束

HSBI 是**等长收缩**（肘关节保持 90°不动），与动态弯举的向心-离心循环不完全同构：

- ✅ 可做：频域带宽 / MDF / MNF 的 baseline → 弯举 J1 判据阈值
- ✅ 可做：噪声底 / SNR 估计 → 弯举 J2 判据
- ❌ 不能做：动态弯举的 pretrain（缺少运动学耦合）
- ❌ 不能做：Rep 分割模型的预训练（无周期信号）

动态弯举的真实 baseline 必须等 FLEX (MyoUP 弯举动作) 或本地采集数据到位。

## 公开下载（无账号，浏览器即可）

**官方记录页**：https://pub.uni-bielefeld.de/record/2956029
**DOI**：10.57720/1956

### 手动下载步骤

1. 浏览器打开 https://pub.uni-bielefeld.de/record/2956029
2. 页面右侧/下方点击 **"Download"** 按钮（或 "Files"→单文件下载）
3. 得到类似 `hsbi_biceps_emg.zip` 的压缩包（真实文件名以页面为准）
4. 解压到本目录：

```bash
cd data/external/hsbi_biceps/
unzip ~/Downloads/hsbi_biceps_emg.zip
```

### 推断的目录结构（真实结构以压缩包为准）

```
data/external/hsbi_biceps/
├── S01/
│   ├── 20MVC/
│   │   ├── rep01_emg_raw.csv
│   │   ├── rep02_emg_raw.csv
│   │   └── ...
│   ├── 50MVC/
│   │   └── ...
│   └── 75MVC/
│       └── ...
├── S02/ ... S11/
└── README.md  (本文件)
```

若真实解压结构与上述不同，`tools/compute_family_baselines.py::load_hsbi` 的
glob 模式需要按真实结构调整（用 `glob.glob` 扫描任意层级 .csv/.mat/.wav）。

### 自动下载尝试（可能被反爬拦截）

```bash
python tools/download_external_data.py --dataset hsbi
```

注：Bielefeld 公共知识库启用了 Anubis JS challenge 反爬，命令行 wget/curl 可能
被拦到 challenge HTML 页而非真实 zip。若自动下载失败脚本会：
1. 打印 warning
2. 写 `data/external/hsbi_biceps/_download_failed.log`
3. exit 0（不阻塞下游）

此时请按上面的"手动下载步骤"用浏览器下。

## 引用

> Hahne, J. M., et al. (2018). *HSBI Biceps Brachii sEMG Dataset*.
> Hochschule Bielefeld — University of Applied Sciences and Arts.
> DOI: 10.57720/1956. https://pub.uni-bielefeld.de/record/2956029

BibTeX：

```bibtex
@dataset{hahne2018hsbi,
  author       = {Hahne, Janne M. and others},
  title        = {HSBI Biceps Brachii sEMG Dataset},
  year         = {2018},
  publisher    = {Hochschule Bielefeld},
  doi          = {10.57720/1956},
  url          = {https://pub.uni-bielefeld.de/record/2956029}
}
```

（作者名单和年份以记录页显示为准，本项目内若有偏差请以官方为准。）
