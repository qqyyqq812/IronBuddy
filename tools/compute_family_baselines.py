# coding=utf-8
"""
Ninapro DB2 + Camargo 2021 家族统计基准计算（Skeleton）
========================================================

plan §4.1 B1 + §3.7.3

**用途级别**：L3 only（plan §1 决策 #5）
- ✅ 合法：计算 MDF/MNF/RMS 分布作为 J1/J2 判据基准
- ✅ 合法：抽取高频噪声谱用于本地数据增强
- ❌ 禁止：用于 Encoder next-step 预训练（伪迁移）
- ❌ 禁止：用于任何模型权重初始化

流程：
1. Ninapro DB2 (弯举)：
   - 下载 40 人 CH2 肱二头肌 + CH5-8 前臂通道（SENIAM 位置近似）
   - 2kHz → 1kHz 重采样 + 20–450 Hz 带通
   - 算每通道 MDF / MNF / 归一化 RMS 的全体分布
2. Camargo 2021 (深蹲)：
   - 下载 22 人 sit-to-stand 段股直肌通道（非深蹲但生物力学近邻）
   - 同流程
3. 输出到 docs/research/family_baselines.json:
   {
     "curl":  {"mdf_mean": ..., "mdf_std": ..., "rms_norm_p50": ..., ...},
     "squat": {"mdf_mean": ..., "mdf_std": ..., "rms_norm_p50": ..., ...}
   }

这个 JSON 作为 plan §4.6 J1/J2 判据的阈值来源。

TODO（板连后，实际下载数据）:
1. Ninapro DB2 下载：http://ninapro.hevs.ch/
2. Camargo 2021 下载：https://www.epic.gatech.edu/opensource-biomechanics-camargo-et-al/
3. 重采样 + 带通 + 频域分析
4. 写 docs/research/family_baselines.json

使用：
    python tools/compute_family_baselines.py
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ninapro-dir', default=os.path.join(ROOT, 'data/external/ninapro_db2'))
    parser.add_argument('--camargo-dir', default=os.path.join(ROOT, 'data/external/camargo_2021'))
    parser.add_argument('--output', default=os.path.join(ROOT, 'docs/research/family_baselines.json'))
    args = parser.parse_args()

    print("[compute_family_baselines] V4.2 skeleton")
    print(f"  Ninapro  →  {args.ninapro_dir}")
    print(f"  Camargo  →  {args.camargo_dir}")
    print(f"  Output   →  {args.output}")
    print("  Usage: L3 only (噪声谱增强 + J1/J2 基准). 拒绝伪迁移 (plan §1 决策 #5)")
    raise NotImplementedError("T4 skeleton: implementation pending board deploy (plan B1)")


if __name__ == '__main__':
    main()
