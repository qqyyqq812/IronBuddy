# Camargo 2021 Georgia Tech Open-Source Biomechanics (External Dataset)

**用途级别**：L3 only（plan §1 决策 #5 + §6 不做清单）

## ✅ 允许的用法
1. 抽取股直肌（Rectus Femoris）高频噪声谱作为**本地深蹲训练数据增强**
2. 计算股直肌 MDF / MNF / 归一化 RMS 分布作为 **J1/J2 族群归属判据基准**

## ❌ 禁止的用法
- **不做** Encoder next-step 预训练（sit-to-stand 不等于深蹲，生物力学只是近邻）
- **不做** 模型权重初始化
- **不做** 分类头迁移
- 不用它训任何弯举相关组件

## 引用

> Camargo, J., Ramanathan, A., Flanagan, W., Young, A. (2021).
> "A comprehensive, open-source dataset of lower limb biomechanics in multiple conditions
>  of stairs, ramps, and level-ground ambulation and transitions."
> Journal of Biomechanics, 119, 110320.

## 下载

https://www.epic.gatech.edu/opensource-biomechanics-camargo-et-al/

22 人下肢任务（步态 + 楼梯 + 坡道 + sit-to-stand 转换），**含股直肌 EMG + IK 角度 + GRF**。

感兴趣通道：
- **股直肌 (Rectus Femoris, RF)** → 深蹲发力点 CH0 完美匹配
- 臀中肌 / 腓肠肌 / 胫骨前肌等：作为辅助统计参考
- **不含竖脊肌** → 本项目代偿通道 CH1 无对应，纯本地从头训

关注时段：**sit-to-stand transitions**（最贴近深蹲起身生物力学）

## 文件放置

按其官网 README 解压：
```
data/external/camargo_2021/
  AB01/
    emg/...
    ik/...
  AB02/
  ...
```

## 使用

由 `tools/compute_family_baselines.py` 读取，输出 `docs/research/family_baselines.json`。
