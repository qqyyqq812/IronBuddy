#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MIA-driven EMG UDP simulator for squat GRU testing.

读取 MIA Squat CSV 波形池, 基于用户实时膝角 (pose_data.json) 计算 phase,
按 --label 合成 target/comp 双通道 EMG 原始采样, 以 UDP ASCII 协议发往
127.0.0.1:8080 (或 --host 指定). udp_emg_server 的 DSP 流水线自动做
HP/Notch/LP + 100ms RMS + MVC 归一化.

三类波形对应:
  standard     — 直接用 golden 池插值
  non_standard — golden × 0.3~0.5 (偷懒半蹲, 两路都弱)
  compensating — target × 0.5, comp × 2.0 + 起身相位 comp 尖峰到 80%

MVC 自动配合:
  默认 --mvc-assist on. 脚本以 20Hz 监听 /dev/shm/mvc_calibrate.request,
  检测到即进入 3.5s MVC 模式, 发送 target_pct=95 / comp_pct=90 最大发力,
  让 udp_emg_server 的 3s 峰值采集窗口抓到正确的个体化 MVC 基线.

使用:
  python3 tools/simulate_emg_from_mia.py --label standard
  python3 tools/simulate_emg_from_mia.py --label compensating --host 10.18.76.224
  python3 tools/simulate_emg_from_mia.py --label non_standard --mia-dir data/mia/squat

Python 3.7 兼容 (板端直接跑).
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import logging
import math
import os
import random
import socket
import sys
import time
from collections import defaultdict

# V7.16: 共享 capture 模块 (FSM-对齐 rep + 信号文件触发落盘)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _emg_capture_common as _cap  # noqa: E402

# ---------------------------------------------------------------------- 常量
DEFAULT_MIA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "mia", "squat",
)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_RATE_HZ = 500         # V7.14: 1000Hz 会压垮板端 CPU, 500Hz 足够 100-sample ring = 200ms 窗口
DEFAULT_NOISE = 0.15
DEFAULT_POSE_SHM = "/dev/shm/pose_data.json"
DEFAULT_STATE_SHM = "/dev/shm/fsm_state.json"
MVC_REQUEST_SHM = "/dev/shm/mvc_calibrate.request"
MVC_WINDOW_SEC = 3.5
MVC_BASE = 400.0              # udp_emg_server 默认 MVC 分母
INFERENCE_MODE_SHM = "/dev/shm/inference_mode.json"
MODE_CACHE_TTL = 0.1          # 100ms 缓存, 不要每 1ms 读盘

# 膝角生理范围 (深蹲): 顶部 ~175°, 底部 ~60°
SQUAT_ANGLE_TOP = 175.0
SQUAT_ANGLE_BOTTOM = 60.0

PHASE_BUCKETS = 20            # 将 phase [0,1] 分 20 桶存储波形池
LABEL_CHOICES = ("standard", "non_standard", "compensating")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [EMG_SIM] - %(message)s",
)


# ---------------------------------------------------------------------- 波形池
def _load_mia_csvs(mia_dir, label):
    # type: (str, str) -> list
    """按 label 加载所有 CSV 行, 返回 (phase, target_rms, comp_rms) 元组列表."""
    # MIA 里只有 golden / bad 两类. standard → golden, 其余 → bad (+ 变换)
    src_sub = "golden" if label == "standard" else "bad"
    pattern = os.path.join(mia_dir, src_sub, "*.csv")
    files = sorted(glob.glob(pattern))
    if not files:
        # Fallback: 如果 bad/ 为空 (可能预处理只产出了 golden), 也从 golden 借波形
        files = sorted(glob.glob(os.path.join(mia_dir, "golden", "*.csv")))
    if not files:
        logging.error("MIA 目录无 CSV: %s", mia_dir)
        sys.exit(2)

    rows = []
    max_files = 60  # 60 个 CSV 大约 1800 行, 足够覆盖 phase 空间
    for fp in files[:max_files]:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    try:
                        p = float(r["Phase_Progress"])
                        t = float(r["Target_RMS"])
                        c = float(r["Comp_RMS"])
                        rows.append((p, t, c))
                    except (KeyError, ValueError):
                        continue
        except IOError:
            continue

    logging.info("MIA 波形池加载: %s 个 CSV → %d 行 (源=%s)", len(files[:max_files]), len(rows), src_sub)
    return rows


def _build_phase_pool(rows):
    # type: (list) -> dict
    """把 (phase, target, comp) 按桶分组, 每桶存多条可抽样的 (target, comp)."""
    pool = defaultdict(list)
    for p, t, c in rows:
        idx = min(PHASE_BUCKETS - 1, max(0, int(p * PHASE_BUCKETS)))
        pool[idx].append((t, c))
    # 空桶用邻桶填充
    for i in range(PHASE_BUCKETS):
        if not pool[i]:
            # 左右搜最近非空桶
            for offset in range(1, PHASE_BUCKETS):
                if i - offset >= 0 and pool[i - offset]:
                    pool[i] = list(pool[i - offset])
                    break
                if i + offset < PHASE_BUCKETS and pool[i + offset]:
                    pool[i] = list(pool[i + offset])
                    break
    return dict(pool)


def _transform_for_label(target_pct, comp_pct, phase, label):
    # type: (float, float, float, str) -> tuple
    """根据 label 对 (target, comp) 做类别变换. target_pct/comp_pct 已是百分比域."""
    if label == "standard":
        return target_pct, comp_pct
    if label == "non_standard":
        # 偷懒半蹲: 主肌+辅肌都降到 30-50%
        factor = 0.3 + 0.2 * random.random()
        return target_pct * factor, comp_pct * factor
    if label == "compensating":
        # 代偿: 主肌减半, 辅肌翻倍. 起身相位 (phase 回降期, 即 phase<0.5 但正在减小)
        # 额外 comp 尖峰. 这里无法判断相位方向, 以"底部附近(phase>0.7)且 comp>50" 时打尖峰
        t_new = target_pct * 0.5
        c_new = comp_pct * 2.0
        if phase > 0.55:
            c_new = max(c_new, 65.0 + random.random() * 20.0)  # 65-85% 尖峰
        return t_new, c_new
    return target_pct, comp_pct


# ---------------------------------------------------------------------- 角度 → phase
def _read_angle():
    # type: () -> float
    """从 /dev/shm 读取最新膝角. 优先 fsm_state.json (平滑过), fallback pose_data.json."""
    # fsm_state.json 有 FSM 平滑过的 angle
    try:
        with open(DEFAULT_STATE_SHM, "r") as f:
            d = json.load(f)
            ang = d.get("angle")
            if isinstance(ang, (int, float)) and 20.0 < ang < 200.0:
                return float(ang)
    except (IOError, ValueError):
        pass
    # 回退: 直接从 pose_data.json 计算
    try:
        with open(DEFAULT_POSE_SHM, "r") as f:
            pd = json.load(f)
        objs = pd.get("objects", [])
        if not objs:
            return SQUAT_ANGLE_TOP
        kpts = objs[0].get("kpts", [])
        if len(kpts) < 17:
            return SQUAT_ANGLE_TOP
        # 取右侧髋(12)/膝(14)/踝(16)
        hip = kpts[12]; knee = kpts[14]; ankle = kpts[16]
        v1 = (hip[0] - knee[0], hip[1] - knee[1])
        v2 = (ankle[0] - knee[0], ankle[1] - knee[1])
        dot = v1[0] * v2[0] + v1[1] * v2[1]
        m1 = math.hypot(v1[0], v1[1]) + 1e-6
        m2 = math.hypot(v2[0], v2[1]) + 1e-6
        cos_a = max(-1.0, min(1.0, dot / (m1 * m2)))
        return math.degrees(math.acos(cos_a))
    except (IOError, ValueError, IndexError):
        return SQUAT_ANGLE_TOP


def _angle_to_phase(angle):
    # type: (float) -> float
    """膝角 → phase ∈ [0,1]. 0=站立, 1=底部."""
    span = SQUAT_ANGLE_TOP - SQUAT_ANGLE_BOTTOM
    p = (SQUAT_ANGLE_TOP - angle) / span
    return max(0.0, min(1.0, p))


def _sample_pool(pool, phase):
    # type: (dict, float) -> tuple
    """在 phase 桶里随机抽一条 (target, comp)."""
    idx = min(PHASE_BUCKETS - 1, max(0, int(phase * PHASE_BUCKETS)))
    candidates = pool.get(idx, [(20.0, 15.0)])
    return random.choice(candidates)


# ---------------------------------------------------------------------- 原始采样合成
# 设计思路: 产生一个 80Hz 载波 + 宽带噪声, 使其 RMS (经 HP20+Notch50+LP150+100ms
# rolling RMS 后) 约等于 (pct/100) * MVC_BASE. 经实验校准系数 K 约为 2.0.

_K_AMP = 2.0   # 幅度缩放系数 (经滤波后 RMS 衰减补偿)

# ---------- V7.14 domain_calibration 反变换 ----------
# udp_emg_server 加载 domain_calibration.json 后对 UDP 包做 MIA_signal = α*User_signal + β 正变换.
# 由于 simulator 合成的数据本身就是 MIA 域 (直接从 MIA CSV 抽样), 必须做反变换
# 才能让 udp_emg_server 再次正变换后回到 MIA 域.
# 否则 α≈2.12 β≈-21 会把所有值饱和到 100%.
_DOMAIN_INV = {"target": (1.0, 0.0), "comp": (1.0, 0.0)}  # (alpha, beta) for inverse
_CAL_PATH_CANDIDATES = [
    # 板端
    "/home/toybrick/streamer_v3/hardware_engine/sensor/domain_calibration.json",
    # WSL dev
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "hardware_engine", "sensor", "domain_calibration.json"),
]
for _p in _CAL_PATH_CANDIDATES:
    if os.path.exists(_p):
        try:
            with open(_p, "r") as _cf:
                _cd = json.load(_cf)
            _m = _cd.get("calibration", {}).get("method_primary", "stretch")
            _c = _cd["calibration"][_m]
            _DOMAIN_INV["target"] = (float(_c["target"]["alpha"]), float(_c["target"]["beta"]))
            _DOMAIN_INV["comp"]   = (float(_c["comp"]["alpha"]),   float(_c["comp"]["beta"]))
            logging.info("[DOMAIN_INV] 已加载 %s: target α=%.3f β=%+.3f, comp α=%.3f β=%+.3f",
                         _p, _DOMAIN_INV["target"][0], _DOMAIN_INV["target"][1],
                         _DOMAIN_INV["comp"][0], _DOMAIN_INV["comp"][1])
            break
        except (IOError, ValueError, KeyError) as _e:
            logging.warning("[DOMAIN_INV] 加载失败 %s: %s", _p, _e)


def _inverse_domain(pct_mia, channel):
    # type: (float, str) -> float
    """MIA 域 pct → ESP32 域 pct (反变换). udp_emg_server 正变换后回到 MIA 域."""
    alpha, beta = _DOMAIN_INV.get(channel, (1.0, 0.0))
    if abs(alpha) < 1e-6:
        return pct_mia
    return (pct_mia - beta) / alpha


def _synth_raw_sample(pct, t_sec):
    # type: (float, float) -> float
    """给定目标百分比 pct (0-100), 合成单个原始 EMG ADC 值."""
    target_rms = (max(0.0, pct) / 100.0) * MVC_BASE
    amp = target_rms * _K_AMP * 1.414  # sine 峰值 ≈ RMS * sqrt(2)
    carrier = amp * math.sin(2.0 * math.pi * 80.0 * t_sec)
    noise = amp * 0.3 * random.gauss(0.0, 1.0)
    return carrier + noise


# ---------------------------------------------------------------------- 主循环
def _read_inference_mode(state):
    # type: (dict) -> str
    """读 /dev/shm/inference_mode.json, 缓存 100ms 避免频繁 I/O.

    返回 'pure_vision' 或 'vision_sensor'. 文件不存在时默认 pure_vision.
    """
    now = time.time()
    if now - state.get("mode_last_check", 0.0) < MODE_CACHE_TTL:
        return state.get("mode_cached", "pure_vision")
    state["mode_last_check"] = now
    mode = "pure_vision"
    try:
        if os.path.exists(INFERENCE_MODE_SHM):
            with open(INFERENCE_MODE_SHM, "r") as f:
                d = json.load(f)
                m = d.get("mode")
                if m in ("pure_vision", "vision_sensor"):
                    mode = m
    except (IOError, ValueError):
        pass
    state["mode_cached"] = mode
    return mode


def _trigger_mvc_if_requested(state):
    # type: (dict) -> None
    """20Hz 检测 MVC 请求. 检测到即写入 mvc_end_ts 让主循环进入最大发力模式."""
    now = time.time()
    # 100Hz 轮询确保比 udp_emg_server 30Hz 删除文件更快地抓到请求
    if now - state["last_mvc_check"] < 0.01:
        return
    state["last_mvc_check"] = now
    if state["mvc_end_ts"] > now:
        return  # 已在 MVC 窗口内
    if os.path.exists(MVC_REQUEST_SHM):
        state["mvc_end_ts"] = now + MVC_WINDOW_SEC
        logging.info("🔴 检测到 MVC 校准请求 → 进入 %.1fs 最大发力模式", MVC_WINDOW_SEC)


def main():
    ap = argparse.ArgumentParser(description="MIA-driven EMG UDP simulator for squat")
    ap.add_argument("--label", required=True, choices=LABEL_CHOICES,
                    help="动作类别标签")
    ap.add_argument("--mia-dir", default=DEFAULT_MIA_DIR, help="MIA 预处理 CSV 根目录")
    ap.add_argument("--host", default=DEFAULT_HOST, help="udp_emg_server 的 IP (板端用 127.0.0.1)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--rate-hz", type=int, default=DEFAULT_RATE_HZ,
                    help="UDP 发送速率, 默认 1000Hz 与 DSP Fs 对齐")
    ap.add_argument("--noise", type=float, default=DEFAULT_NOISE,
                    help="target/comp 百分比的乘性噪声 (±比例)")
    ap.add_argument("--mvc-assist", choices=("on", "off"), default="off",
                    help="是否自动响应 MVC 校准请求 (默认 off, 彩排流程走独立 simulate_mvc_burst.py)")
    args = ap.parse_args()

    # V7.16: 预加载三类波形池, 允许运行时跟随 UI label 切换
    pools_by_label = {}
    for lb in LABEL_CHOICES:
        _rows = _load_mia_csvs(args.mia_dir, lb)
        pools_by_label[lb] = _build_phase_pool(_rows)
    pool = pools_by_label[args.label]  # 默认
    logging.info("三类波形池均就绪, 默认 label=%s", args.label)

    # 通知 udp_emg_server 这是 squat (避免残留 bicep_curl 设置把 EMG 映射错)
    try:
        profile_path = "/dev/shm/user_profile.json"
        prof = {}
        if os.path.exists(profile_path):
            try:
                with open(profile_path, "r") as f:
                    prof = json.load(f)
            except (IOError, ValueError):
                prof = {}
        prof["exercise"] = "squat"
        tmp = profile_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(prof, f)
        os.rename(tmp, profile_path)
    except (IOError, OSError) as e:
        logging.warning("写 user_profile.json 失败 (非致命): %s", e)

    # UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 15)
    dest = (args.host, args.port)
    logging.info("🚀 开始推送 label=%s → %s:%d @ %dHz", args.label, args.host, args.port, args.rate_hz)
    logging.info("   MVC 自动配合: %s", args.mvc_assist)

    interval = 1.0 / float(args.rate_hz)
    state = {
        "mvc_end_ts": 0.0,
        "last_mvc_check": 0.0,
        "last_angle_log": 0.0,
        "mode_cached": "pure_vision",
        "mode_last_check": 0.0,
        "last_mode_log": 0.0,
        "cap": _cap.make_state(),   # V7.16 capture 子状态
    }

    # 预热: 先读一次角度
    last_phase = 0.0
    pkt_count = 0
    t0 = time.time()

    try:
        while True:
            now = time.time()

            # 20Hz 监听 MVC 请求
            if args.mvc_assist == "on":
                _trigger_mvc_if_requested(state)

            # V7.16: 100Hz 监听 test_capture 启停信号 (内部自节流)
            _cap.capture_poll(state["cap"])

            in_mvc = state["mvc_end_ts"] > now
            # V7.13 模式闸门: 纯视觉模式下 UI 不应显示任何 EMG 数据
            # → 发零 (udp_emg_server DSP 后 RMS=0, muscle_activation.json 两路都是 0)
            # MVC 窗口例外 (若 --mvc-assist on): 总是发最大值让 UI 视觉反馈
            current_mode = _read_inference_mode(state)
            gate_silent = (current_mode == "pure_vision") and not in_mvc

            if in_mvc:
                target_pct = 95.0 + random.uniform(-3.0, 3.0)
                comp_pct = 90.0 + random.uniform(-3.0, 3.0)
                angle = SQUAT_ANGLE_TOP  # MVC 模式假定站立
            else:
                # V7.16: 采集中以 UI 选的 label 为准, 否则用启动参数
                active_label = _cap.get_active_label(state["cap"]) or args.label
                # 100Hz 刷新 phase (比 UDP 发包慢, 降低 IO 开销)
                angle = _read_angle()
                phase = _angle_to_phase(angle)
                # EMA 平滑, 避免相邻帧跳跃
                last_phase = 0.7 * last_phase + 0.3 * phase
                t_pct, c_pct = _sample_pool(pools_by_label[active_label], last_phase)
                t_pct, c_pct = _transform_for_label(t_pct, c_pct, last_phase, active_label)
                # 乘性噪声
                t_pct *= (1.0 + args.noise * random.uniform(-1.0, 1.0))
                c_pct *= (1.0 + args.noise * random.uniform(-1.0, 1.0))
                target_pct = max(0.0, min(100.0, t_pct))
                comp_pct = max(0.0, min(100.0, c_pct))

            # 合成原始 EMG 采样 (双通道). 模式闸门关闭时直接发零
            if gate_silent:
                t_raw, c_raw = 0.0, 0.0
            else:
                # V7.14 反变换: MIA 域 pct → ESP32 域, 让 udp_emg_server 正变换后回到 MIA 域
                t_pct_esp = _inverse_domain(target_pct, "target")
                c_pct_esp = _inverse_domain(comp_pct, "comp")
                # 钳位防止反变换后负值
                t_pct_esp = max(0.0, t_pct_esp)
                c_pct_esp = max(0.0, c_pct_esp)
                t_raw = _synth_raw_sample(t_pct_esp, now)
                c_raw = _synth_raw_sample(c_pct_esp, now)

            msg = "{:.1f} {:.1f}".format(t_raw, c_raw).encode("ascii")
            try:
                sock.sendto(msg, dest)
            except (OSError, socket.error) as e:
                logging.warning("UDP 发送失败: %s", e)

            pkt_count += 1

            # V7.16: 记录采集帧 (capture 关闭时 near-zero cost)
            _cap.capture_record_frame(
                state["cap"],
                ts=now,
                sim_phase=last_phase,
                sim_angle=angle,
                sim_target_pct=target_pct,
                sim_comp_pct=comp_pct,
                sim_udp_target=t_raw,
                sim_udp_comp=c_raw,
                simulator_src="tools/simulate_emg_from_mia.py",
            )

            # 每 2 秒打印一次状态
            if now - state["last_angle_log"] > 2.0:
                elapsed = now - t0
                hz_eff = pkt_count / max(elapsed, 1e-3)
                if in_mvc:
                    tag = "MVC-PEAK"
                elif gate_silent:
                    tag = "SILENT(pure_vision)"
                else:
                    tag = "label={}".format(args.label)
                logging.info("📡 [%s] angle=%.1f° phase=%.2f target=%.1f%% comp=%.1f%% @ %.0fHz",
                             tag, _read_angle(), last_phase, target_pct, comp_pct, hz_eff)
                state["last_angle_log"] = now

            # 速率控制
            elapsed_tick = time.time() - now
            if elapsed_tick < interval:
                time.sleep(interval - elapsed_tick)

    except KeyboardInterrupt:
        logging.info("\n[EXIT] 用户中断, 共发送 %d 包.", pkt_count)
        # V7.16: 若采集仍在进行, 自动 flush (不丢数据)
        if state["cap"].get("enabled"):
            logging.info("[CAP] Ctrl+C 检测到 capture 仍启用, 自动 flush ...")
            _cap._finalize_and_flush(state["cap"], discard=False)
        sock.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
