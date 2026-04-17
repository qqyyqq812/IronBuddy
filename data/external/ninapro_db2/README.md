# Ninapro DB2 (External Dataset)

**用途级别**：L3 only（plan §1 决策 #5 + §6 不做清单）

## ✅ 允许的用法
1. 抽取肱二头肌（CH2）高频噪声谱作为**本地训练数据增强**（domain randomization）
2. 计算 MDF / MNF / 归一化 RMS 分布作为 **J1/J2 族群归属判据基准**

## ❌ 禁止的用法
- **不做** Encoder next-step 预训练（静态手势 ≠ 动态弯举，任务域错位）
- **不做** 模型权重初始化
- **不做** 分类头迁移
- **绝不用于深蹲**（Ninapro 是臂部数据，与下肢无关）

## 下载

https://ninapro.hevs.ch/

需要账号（免费学术注册）。选 **DB2**（40 人 × 49 手势 × 6 次重复，2 kHz sEMG）。
感兴趣通道：
- **CH2** → 肱二头肌（Biceps Brachii）
- **CH5–CH8** → 前臂屈肌环阵（与我们 CH1 前臂电极吻合最好）

## 文件放置

下载的 `.mat` 文件直接放本目录：
```
data/external/ninapro_db2/
  S1_E1_A1.mat
  S1_E2_A1.mat
  ...
  S40_E3_A1.mat
```

## 使用

由 `tools/compute_family_baselines.py` 读取，输出 `docs/research/family_baselines.json`。
