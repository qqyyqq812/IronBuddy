# coding=utf-8
#!/usr/bin/env python3
"""
IronBuddy V4.6 · 硬件域对齐（Hardware Domain Alignment）
============================================================

目标：把 ESP32 廉价 sEMG 硬件的 RMS 分布**线性映射**到 MIA (Delsys) Delsys 域，
让已训好的 MIA 权重（models/extreme_fusion_gru_squat.pt val_acc=94.4%）
在用户硬件信号上也能正确推理，**不动模型权重**。

原理：`MIA_signal = α · User_signal + β`
    - α (gain)：双端 p95-p05 动态范围拉伸
    - β (offset)：基线对齐

输入：
    data/bicep_curl/{golden,lazy,bad}/train_*.csv   用户实测硬件基因
    data/mia_squat_raw/**/emgvalues.npy             MIA Delsys 基因

输出：
    hardware_engine/sensor/domain_calibration.json  可被 udp_emg_server 加载

依据：用户 2026-04-18 明示「硬件自身的静息低电平与肌肉力竭高电平
       的物理包络极限是恒定的」→ 跨肌群（biceps↔quad, forearm↔glutt）对齐合理。

用法：
    python tools/hardware_domain_calibrate.py
    python tools/hardware_domain_calibrate.py --method zscore
    python tools/hardware_domain_calibrate.py --mia-limit 500
"""
from __future__ import absolute_import, division, print_function

import argparse
import csv
import glob
import json
import os
import sys
import time
from collections import OrderedDict

import numpy as np


# MIA 8 通道顺序（data.py:138 muscles 列表）
CH_RIGHTQUAD = 0    # 对齐用户 Target（深蹲主发力 ↔ 弯举 biceps）
CH_RIGHTGLUTT = 4   # 对齐用户 Comp（深蹲代偿 ↔ 弯举 forearm）


def _stats(arr):
    """返回一个 dict：9 个分位数 + mean + std"""
    arr = np.asarray(arr, dtype=np.float64)
    if arr.size == 0:
        return OrderedDict([(k, 0.0) for k in
                             ['n', 'min', 'p05', 'p25', 'p50',
                              'p75', 'p95', 'max', 'mean', 'std']])
    return OrderedDict([
        ('n',    int(arr.size)),
        ('min',  float(np.min(arr))),
        ('p05',  float(np.percentile(arr, 5))),
        ('p25',  float(np.percentile(arr, 25))),
        ('p50',  float(np.percentile(arr, 50))),
        ('p75',  float(np.percentile(arr, 75))),
        ('p95',  float(np.percentile(arr, 95))),
        ('max',  float(np.max(arr))),
        ('mean', float(np.mean(arr))),
        ('std',  float(np.std(arr))),
    ])


def load_user_hardware_rms(user_root, drop_zero=True, drop_clip_100=True):
    """扫 data/bicep_curl/{golden,lazy,bad}/train_*.csv 提取 Target_RMS + Comp_RMS。

    返回 (target_arr, comp_arr, src_files)。
    """
    pattern = os.path.join(user_root, '*', 'train_*.csv')
    paths = sorted(glob.glob(pattern))
    if not paths:
        raise FileNotFoundError('未找到用户硬件 CSV: {}'.format(pattern))

    target_vals = []
    comp_vals = []
    for p in paths:
        try:
            with open(p, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    try:
                        t = float(row['Target_RMS'])
                        c = float(row['Comp_RMS'])
                    except (KeyError, ValueError):
                        continue
                    if drop_zero and t <= 0:
                        pass
                    elif drop_clip_100 and t >= 100:
                        pass
                    else:
                        target_vals.append(t)
                    if drop_zero and c <= 0:
                        pass
                    elif drop_clip_100 and c >= 100:
                        pass
                    else:
                        comp_vals.append(c)
        except Exception as exc:
            print('[WARN] 跳过不可读 CSV %s: %s' % (p, exc))
    return (np.array(target_vals, dtype=np.float64),
            np.array(comp_vals, dtype=np.float64),
            paths)


def load_mia_rms(mia_root, clip_limit=200, drop_threshold=1.0):
    """扫 MIA emgvalues.npy，取 CH0 (rightquad) + CH4 (rightglutt)，abs 后返回。

    为了加速，最多采样 clip_limit 个 clip（每个 clip 30 帧 × 8 通道）。
    """
    pattern = os.path.join(mia_root, '**', 'emgvalues.npy')
    paths = sorted(glob.glob(pattern, recursive=True))
    if not paths:
        raise FileNotFoundError('未找到 MIA emg: {}'.format(pattern))
    if clip_limit > 0 and len(paths) > clip_limit:
        # 均匀采样（而非只取前 N）防止 subject bias
        idx = np.linspace(0, len(paths) - 1, clip_limit, dtype=int)
        paths = [paths[i] for i in idx]

    ch0_vals = []
    ch4_vals = []
    for p in paths:
        try:
            emg = np.load(p)  # (T, 8)
            if emg.ndim != 2 or emg.shape[1] < 5:
                continue
            ch0 = np.abs(emg[:, CH_RIGHTQUAD].astype(np.float64))
            ch4 = np.abs(emg[:, CH_RIGHTGLUTT].astype(np.float64))
            ch0 = ch0[ch0 >= drop_threshold]
            ch4 = ch4[ch4 >= drop_threshold]
            ch0_vals.extend(ch0.tolist())
            ch4_vals.extend(ch4.tolist())
        except Exception as exc:
            print('[WARN] 跳过不可读 npy %s: %s' % (p, exc))
    return (np.array(ch0_vals, dtype=np.float64),
            np.array(ch4_vals, dtype=np.float64),
            len(paths))


def _calc_stretch(user_stats, mia_stats):
    """p05-p95 双端线性拉伸：α·x + β，把 user_p05→mia_p05, user_p95→mia_p95"""
    du = user_stats['p95'] - user_stats['p05']
    dm = mia_stats['p95'] - mia_stats['p05']
    alpha = dm / (du + 1e-6)
    beta = mia_stats['p05'] - alpha * user_stats['p05']
    return alpha, beta


def _calc_zscore(user_stats, mia_stats):
    """均值方差对齐：(x - μu)/σu · σm + μm"""
    alpha = mia_stats['std'] / (user_stats['std'] + 1e-6)
    beta = mia_stats['mean'] - alpha * user_stats['mean']
    return alpha, beta


def _apply(x, alpha, beta):
    return alpha * x + beta


def _sanity(user_arr, mia_stats, alpha, beta):
    """把用户样本套 α/β 后算新 stats，与 MIA 对比"""
    mapped = _apply(user_arr, alpha, beta)
    new_p05 = float(np.percentile(mapped, 5))
    new_p95 = float(np.percentile(mapped, 95))
    new_mean = float(np.mean(mapped))
    dev_p95 = abs(new_p95 - mia_stats['p95']) / max(mia_stats['p95'], 1e-6)
    dev_p05 = abs(new_p05 - mia_stats['p05']) / max(mia_stats['p05'], 1e-6)
    return {
        'mapped_p05':     round(new_p05, 2),
        'mapped_p50':     round(float(np.percentile(mapped, 50)), 2),
        'mapped_p95':     round(new_p95, 2),
        'mapped_mean':    round(new_mean, 2),
        'mia_p05':        round(mia_stats['p05'], 2),
        'mia_p95':        round(mia_stats['p95'], 2),
        'mia_mean':       round(mia_stats['mean'], 2),
        'deviation_p05_pct': round(dev_p05 * 100, 2),
        'deviation_p95_pct': round(dev_p95 * 100, 2),
    }


def _print_stats_table(title, stats):
    print('  [%s] n=%d  min=%.1f  p05=%.1f  p50=%.1f  p95=%.1f  max=%.1f  mean=%.2f  std=%.2f' %
          (title, stats['n'], stats['min'], stats['p05'], stats['p50'],
           stats['p95'], stats['max'], stats['mean'], stats['std']))


def main():
    ap = argparse.ArgumentParser(description='IronBuddy 硬件域对齐校准')
    ap.add_argument('--user-data', default='data/bicep_curl',
                    help='用户实测 CSV 根（含 golden/lazy/bad 子目录）')
    ap.add_argument('--mia-data', default='data/mia_squat_raw',
                    help='MIA emgvalues.npy 根目录')
    ap.add_argument('--out', default='hardware_engine/sensor/domain_calibration.json',
                    help='输出 JSON 路径')
    ap.add_argument('--method', choices=['stretch', 'zscore', 'both'], default='both',
                    help='校准方法；both 两套系数都算，primary 取 stretch')
    ap.add_argument('--mia-limit', type=int, default=200,
                    help='最多扫多少 MIA clip（加速，0=全扫）')
    args = ap.parse_args()

    print('=' * 70)
    print('IronBuddy V4.6 · 硬件域对齐校准')
    print('=' * 70)

    # --- 1) 加载用户 ---
    print('\n[1/4] 加载用户硬件基因 (%s)' % args.user_data)
    user_target, user_comp, user_files = load_user_hardware_rms(args.user_data)
    u_target_s = _stats(user_target)
    u_comp_s = _stats(user_comp)
    print('  用户 CSV: %d 个（过滤 <=0 和 >=100 clip 饱和）' % len(user_files))
    _print_stats_table('user_target (biceps)', u_target_s)
    _print_stats_table('user_comp   (forearm)', u_comp_s)

    # --- 2) 加载 MIA ---
    print('\n[2/4] 加载 MIA Delsys 基因 (%s)' % args.mia_data)
    mia_ch0, mia_ch4, mia_clip_cnt = load_mia_rms(args.mia_data, clip_limit=args.mia_limit)
    m_ch0_s = _stats(mia_ch0)
    m_ch4_s = _stats(mia_ch4)
    print('  MIA clip: %d 个（abs 后过滤 <1）' % mia_clip_cnt)
    _print_stats_table('mia_ch0 (rightquad)', m_ch0_s)
    _print_stats_table('mia_ch4 (rightglutt)', m_ch4_s)

    # --- 3) 计算两套系数 ---
    print('\n[3/4] 计算 α/β 系数')
    calibration = {}

    # stretch 主方法
    a_t_s, b_t_s = _calc_stretch(u_target_s, m_ch0_s)
    a_c_s, b_c_s = _calc_stretch(u_comp_s, m_ch4_s)
    print('  [stretch · p05-p95 线性拉伸]')
    print('    target: α=%.4f  β=%+.4f      (user_p05=%.1f→mia_p05=%.1f, user_p95=%.1f→mia_p95=%.1f)' %
          (a_t_s, b_t_s, u_target_s['p05'], m_ch0_s['p05'], u_target_s['p95'], m_ch0_s['p95']))
    print('    comp  : α=%.4f  β=%+.4f      (user_p05=%.1f→mia_p05=%.1f, user_p95=%.1f→mia_p95=%.1f)' %
          (a_c_s, b_c_s, u_comp_s['p05'], m_ch4_s['p05'], u_comp_s['p95'], m_ch4_s['p95']))
    calibration['stretch'] = {
        'target': {'alpha': round(a_t_s, 4), 'beta': round(b_t_s, 4)},
        'comp':   {'alpha': round(a_c_s, 4), 'beta': round(b_c_s, 4)},
    }

    # zscore 次方法
    a_t_z, b_t_z = _calc_zscore(u_target_s, m_ch0_s)
    a_c_z, b_c_z = _calc_zscore(u_comp_s, m_ch4_s)
    print('  [zscore · 均值方差对齐]')
    print('    target: α=%.4f  β=%+.4f      (μ: %.2f→%.2f, σ: %.2f→%.2f)' %
          (a_t_z, b_t_z, u_target_s['mean'], m_ch0_s['mean'], u_target_s['std'], m_ch0_s['std']))
    print('    comp  : α=%.4f  β=%+.4f      (μ: %.2f→%.2f, σ: %.2f→%.2f)' %
          (a_c_z, b_c_z, u_comp_s['mean'], m_ch4_s['mean'], u_comp_s['std'], m_ch4_s['std']))
    calibration['zscore'] = {
        'target': {'alpha': round(a_t_z, 4), 'beta': round(b_t_z, 4)},
        'comp':   {'alpha': round(a_c_z, 4), 'beta': round(b_c_z, 4)},
    }

    # --- 4) Sanity check ---
    print('\n[4/4] Sanity check（用 primary=stretch 系数重映射用户数据）')
    sanity_target = _sanity(user_target, m_ch0_s, a_t_s, b_t_s)
    sanity_comp = _sanity(user_comp, m_ch4_s, a_c_s, b_c_s)
    print('  [target] after stretch:')
    print('    mapped p05=%.2f (target mia_p05=%.2f, deviation %.2f%%)' %
          (sanity_target['mapped_p05'], sanity_target['mia_p05'], sanity_target['deviation_p05_pct']))
    print('    mapped p95=%.2f (target mia_p95=%.2f, deviation %.2f%%)' %
          (sanity_target['mapped_p95'], sanity_target['mia_p95'], sanity_target['deviation_p95_pct']))
    print('  [comp] after stretch:')
    print('    mapped p05=%.2f (target mia_p05=%.2f, deviation %.2f%%)' %
          (sanity_comp['mapped_p05'], sanity_comp['mia_p05'], sanity_comp['deviation_p05_pct']))
    print('    mapped p95=%.2f (target mia_p95=%.2f, deviation %.2f%%)' %
          (sanity_comp['mapped_p95'], sanity_comp['mia_p95'], sanity_comp['deviation_p95_pct']))

    # --- 写 JSON ---
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    primary = args.method if args.method != 'both' else 'stretch'
    report = OrderedDict([
        ('generated_at',  time.strftime('%Y-%m-%dT%H:%M:%S')),
        ('generator',     'tools/hardware_domain_calibrate.py'),
        ('source', OrderedDict([
            ('user_csvs',         [os.path.abspath(p) for p in user_files]),
            ('user_total_points', int(user_target.size + user_comp.size)),
            ('mia_root',          os.path.abspath(args.mia_data)),
            ('mia_clips_scanned', mia_clip_cnt),
            ('mia_total_points',  int(mia_ch0.size + mia_ch4.size)),
        ])),
        ('stats', OrderedDict([
            ('user_target', u_target_s),
            ('user_comp',   u_comp_s),
            ('mia_ch0',     m_ch0_s),
            ('mia_ch4',     m_ch4_s),
        ])),
        ('calibration', OrderedDict([
            ('method_primary', primary),
            ('apply_formula',  'MIA_signal = alpha * User_signal + beta'),
            ('target_mapping', 'user_target (biceps or quad) → mia_ch0 (rightquad)'),
            ('comp_mapping',   'user_comp (forearm or glutt) → mia_ch4 (rightglutt)'),
            ('stretch',        calibration['stretch']),
            ('zscore',         calibration['zscore']),
        ])),
        ('sanity_check', OrderedDict([
            ('method', 'stretch'),
            ('target', sanity_target),
            ('comp',   sanity_comp),
        ])),
        ('notes', (
            '跨肌群对齐（biceps↔quad, forearm↔glutt），依据用户 2026-04-18 '
            "「硬件自身的静息低电平和肌肉力竭高电平的物理包络极限是恒定的」决策。"
            ' clip 饱和 (RMS=100) 的点被过滤；低频底噪 (RMS<=0) 被过滤。'
        )),
    ])
    with open(args.out, 'w') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print('\n[OK] 产物: %s' % args.out)

    # --- 集成提示 ---
    print('\n' + '=' * 70)
    print('集成到 udp_emg_server.py（L117 附近）：')
    print('=' * 70)
    print("""
# 文件顶部（L35 附近，常量区）加载 JSON：
_DOMAIN_CALIB = {'target': (1.0, 0.0), 'comp': (1.0, 0.0)}
_CALIB_JSON = os.path.join(os.path.dirname(__file__), 'domain_calibration.json')
try:
    with open(_CALIB_JSON) as _cf:
        _cd = json.load(_cf)
        _m = _cd.get('calibration', {}).get('method_primary', 'stretch')
        _c = _cd['calibration'][_m]
        _DOMAIN_CALIB['target'] = (float(_c['target']['alpha']), float(_c['target']['beta']))
        _DOMAIN_CALIB['comp']   = (float(_c['comp']['alpha']),   float(_c['comp']['beta']))
    print('[udp_emg] 加载硬件域校准 (method=%%s): target α=%%.2f β=%%+.2f, comp α=%%.2f β=%%+.2f' %%
          (_m, _DOMAIN_CALIB['target'][0], _DOMAIN_CALIB['target'][1],
               _DOMAIN_CALIB['comp'][0],   _DOMAIN_CALIB['comp'][1]))
except Exception as _e:
    print('[udp_emg] 未加载校准 JSON，恒等变换: %%s' %% _e)

# 替换 L117（归一化后、clip 前）：
rms_raw_pct = (rms / 400.0) * 100.0  # 保留小数
ch_key = 'target' if ch == 0 else ('comp' if ch == 1 else None)
if ch_key is not None:
    a, b = _DOMAIN_CALIB[ch_key]
    rms_raw_pct = a * rms_raw_pct + b  # MIA 域映射
rms_mapped = max(0, min(100, int(round(rms_raw_pct))))
if rms_mapped < 4:
    rms_mapped = 0
CURRENT_RMS_PCT[ch] = rms_mapped
""")
    print('=' * 70)
    print('完成。下一步：按上面片段修改 udp_emg_server.py，重启 emg 服务即可生效。')
    print('=' * 70)
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
