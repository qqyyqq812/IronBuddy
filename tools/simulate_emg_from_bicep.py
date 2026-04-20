#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
弯举 EMG UDP simulator (哑铃弯举专用).

实现思路与 simulate_emg_from_mia.py 一致, 但数据源不是 MIA (MIA 不含弯举).
采用**解析式波形表**编码三类动作的肌电时序:

  biceps = 目标肌 (target ch0)
  deltoid/trap/swinging = 代偿肌 (comp ch1)

三类标签:
  standard     — 肱二头肌发力曲线, 代偿肌安静 (10-15%)
  non_standard — 发力不足, 角度没到顶峰, 双通道都弱 (偷懒)
  compensating — 主肌发力不足, 代偿肌在**起始抬起**时突刺 (身体摆动借力)

弯举角度 (肩-肘-腕): 170° 手臂自然下垂 → 40° 肱二头紧缩顶峰. phase = (170-angle)/(170-40).

MVC 自动配合: 与 squat 版本一致, 见 --mvc-assist.

使用:
  python3 tools/simulate_emg_from_bicep.py --label standard
  python3 tools/simulate_emg_from_bicep.py --label compensating --host 10.18.76.224

Python 3.7 兼容.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import socket
import sys
import time

# V7.16: 共享 capture 模块 (FSM-对齐 rep + 信号文件触发落盘)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _emg_capture_common as _cap  # noqa: E402

# ---------------------------------------------------------------------- 常量
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_RATE_HZ = 500   # V7.14: 与 mia simulator 对齐, 降 CPU 占用
DEFAULT_NOISE = 0.15
DEFAULT_POSE_SHM = "/dev/shm/pose_data.json"
DEFAULT_STATE_SHM = "/dev/shm/fsm_state.json"
MVC_REQUEST_SHM = "/dev/shm/mvc_calibrate.request"
MVC_WINDOW_SEC = 3.5
MVC_BASE = 400.0
INFERENCE_MODE_SHM = "/dev/shm/inference_mode.json"
MODE_CACHE_TTL = 0.1

CURL_ANGLE_EXTENDED = 170.0  # 手臂自然伸直
CURL_ANGLE_PEAK = 40.0       # 肱二头最紧缩

LABEL_CHOICES = ("standard", "non_standard", "compensating")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [EMG_SIM_BICEP] - %(message)s",
)


# ---------------------------------------------------------------------- 波形表
# 每个 label 给出 (phase, target_pct, comp_pct) 锚点, 线性插值得到连续曲线.
# phase 0 = 手臂伸直, phase 1 = 完全紧缩.
_WAVEFORM = {
    "standard": [
        # 标准弯举: 平滑上升, 顶峰 75%, 代偿肌持续低水平
        (0.00,  8.0,  10.0),
        (0.20, 25.0,  12.0),
        (0.50, 55.0,  15.0),
        (0.75, 70.0,  14.0),
        (1.00, 78.0,  13.0),
    ],
    "non_standard": [
        # 偷懒半举: 目标肌不过 30%, 代偿肌也安静
        (0.00,  6.0,   6.0),
        (0.30, 18.0,   8.0),
        (0.50, 25.0,  10.0),
        (0.75, 28.0,  10.0),
        (1.00, 30.0,  10.0),
    ],
    "compensating": [
        # 代偿: 起始摆动 → 代偿肌尖峰 (phase 0.1-0.4 肩背发力借力)
        # 目标肌被动跟随, 水平低
        (0.00, 10.0,  12.0),
        (0.15, 20.0,  65.0),  # 开始摆动发力 → 代偿尖峰
        (0.35, 30.0,  78.0),  # 代偿峰值
        (0.55, 38.0,  55.0),  # 动作上到顶部, 代偿松
        (0.80, 42.0,  35.0),
        (1.00, 45.0,  28.0),
    ],
}


def _interp_waveform(label, phase):
    # type: (str, float) -> tuple
    """按 phase 在波形表中做线性插值, 返回 (target_pct, comp_pct)."""
    table = _WAVEFORM[label]
    if phase <= table[0][0]:
        return table[0][1], table[0][2]
    if phase >= table[-1][0]:
        return table[-1][1], table[-1][2]
    for i in range(len(table) - 1):
        p0, t0, c0 = table[i]
        p1, t1, c1 = table[i + 1]
        if p0 <= phase <= p1:
            w = (phase - p0) / max(p1 - p0, 1e-6)
            return t0 + w * (t1 - t0), c0 + w * (c1 - c0)
    return table[-1][1], table[-1][2]


# ---------------------------------------------------------------------- 角度 → phase
def _read_angle():
    # type: () -> float
    """读取最新肘角. 优先 fsm_state.json, 回退从 pose_data.json 计算."""
    try:
        with open(DEFAULT_STATE_SHM, "r") as f:
            d = json.load(f)
            ang = d.get("angle")
            if isinstance(ang, (int, float)) and 10.0 < ang < 200.0:
                return float(ang)
    except (IOError, ValueError):
        pass
    try:
        with open(DEFAULT_POSE_SHM, "r") as f:
            pd = json.load(f)
        objs = pd.get("objects", [])
        if not objs:
            return CURL_ANGLE_EXTENDED
        kpts = objs[0].get("kpts", [])
        if len(kpts) < 11:
            return CURL_ANGLE_EXTENDED
        # 优先右侧: 肩(6) / 肘(8) / 腕(10); 低置信度切左侧 5/7/9
        r_score = kpts[6][2] + kpts[8][2] + kpts[10][2]
        l_score = kpts[5][2] + kpts[7][2] + kpts[9][2]
        if r_score >= l_score:
            sh, el, wr = kpts[6], kpts[8], kpts[10]
        else:
            sh, el, wr = kpts[5], kpts[7], kpts[9]
        v1 = (sh[0] - el[0], sh[1] - el[1])
        v2 = (wr[0] - el[0], wr[1] - el[1])
        dot = v1[0] * v2[0] + v1[1] * v2[1]
        m1 = math.hypot(v1[0], v1[1]) + 1e-6
        m2 = math.hypot(v2[0], v2[1]) + 1e-6
        cos_a = max(-1.0, min(1.0, dot / (m1 * m2)))
        return math.degrees(math.acos(cos_a))
    except (IOError, ValueError, IndexError):
        return CURL_ANGLE_EXTENDED


def _angle_to_phase(angle):
    # type: (float) -> float
    """肘角 → phase ∈ [0,1]. 0=手臂伸直, 1=完全紧缩."""
    span = CURL_ANGLE_EXTENDED - CURL_ANGLE_PEAK
    p = (CURL_ANGLE_EXTENDED - angle) / span
    return max(0.0, min(1.0, p))


# ---------------------------------------------------------------------- 原始采样合成
_K_AMP = 2.0

# V7.14 domain_calibration 反变换 (与 simulate_emg_from_mia.py 一致)
_DOMAIN_INV = {"target": (1.0, 0.0), "comp": (1.0, 0.0)}
_CAL_PATH_CANDIDATES = [
    "/home/toybrick/streamer_v3/hardware_engine/sensor/domain_calibration.json",
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
    alpha, beta = _DOMAIN_INV.get(channel, (1.0, 0.0))
    if abs(alpha) < 1e-6:
        return pct_mia
    return (pct_mia - beta) / alpha


def _synth_raw_sample(pct, t_sec):
    target_rms = (max(0.0, pct) / 100.0) * MVC_BASE
    amp = target_rms * _K_AMP * 1.414
    carrier = amp * math.sin(2.0 * math.pi * 80.0 * t_sec)
    noise = amp * 0.3 * random.gauss(0.0, 1.0)
    return carrier + noise


def _read_inference_mode(state):
    # type: (dict) -> str
    """读 /dev/shm/inference_mode.json, 缓存 100ms. 默认 pure_vision."""
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
    now = time.time()
    # 100Hz 轮询确保比 udp_emg_server 30Hz 删除文件更快地抓到请求
    if now - state["last_mvc_check"] < 0.01:
        return
    state["last_mvc_check"] = now
    if state["mvc_end_ts"] > now:
        return
    if os.path.exists(MVC_REQUEST_SHM):
        state["mvc_end_ts"] = now + MVC_WINDOW_SEC
        logging.info("🔴 检测到 MVC 校准请求 → 进入 %.1fs 最大发力模式", MVC_WINDOW_SEC)


def main():
    ap = argparse.ArgumentParser(description="EMG UDP simulator for bicep curl")
    ap.add_argument("--label", required=True, choices=LABEL_CHOICES)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--rate-hz", type=int, default=DEFAULT_RATE_HZ)
    ap.add_argument("--noise", type=float, default=DEFAULT_NOISE)
    ap.add_argument("--mvc-assist", choices=("on", "off"), default="off",
                    help="默认 off, 彩排流程走独立 simulate_mvc_burst.py")
    args = ap.parse_args()

    # 预先通知 udp_emg_server 这是 bicep_curl (让其正确 routing EMG 到 biceps key)
    # 通过写 /dev/shm/user_profile.json 实现
    try:
        profile_path = "/dev/shm/user_profile.json"
        prof = {}
        if os.path.exists(profile_path):
            try:
                with open(profile_path, "r") as f:
                    prof = json.load(f)
            except (IOError, ValueError):
                prof = {}
        prof["exercise"] = "bicep_curl"
        tmp = profile_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(prof, f)
        os.rename(tmp, profile_path)
        logging.info("已写 user_profile.exercise=bicep_curl → udp_emg_server 会把 EMG 映射到 biceps 主肌")
    except (IOError, OSError) as e:
        logging.warning("写 user_profile.json 失败 (非致命): %s", e)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 15)
    dest = (args.host, args.port)
    logging.info("🚀 弯举推送 label=%s → %s:%d @ %dHz", args.label, args.host, args.port, args.rate_hz)
    logging.info("   MVC 自动配合: %s", args.mvc_assist)

    interval = 1.0 / float(args.rate_hz)
    state = {
        "mvc_end_ts": 0.0,
        "last_mvc_check": 0.0,
        "last_angle_log": 0.0,
        "mode_cached": "pure_vision",
        "mode_last_check": 0.0,
        "cap": _cap.make_state(),   # V7.16 capture 子状态
    }
    last_phase = 0.0
    pkt_count = 0
    t0 = time.time()

    try:
        while True:
            now = time.time()

            if args.mvc_assist == "on":
                _trigger_mvc_if_requested(state)

            # V7.16: 100Hz 监听 test_capture 启停信号 (内部自节流)
            _cap.capture_poll(state["cap"])

            in_mvc = state["mvc_end_ts"] > now
            # V7.13 模式闸门: pure_vision 下发零, UI 肌电条静默
            current_mode = _read_inference_mode(state)
            gate_silent = (current_mode == "pure_vision") and not in_mvc

            if in_mvc:
                target_pct = 92.0 + random.uniform(-3.0, 3.0)
                comp_pct = 88.0 + random.uniform(-3.0, 3.0)
                angle = CURL_ANGLE_EXTENDED
            else:
                # V7.16: 采集中以 UI 选的 label 为准
                active_label = _cap.get_active_label(state["cap"]) or args.label
                angle = _read_angle()
                phase = _angle_to_phase(angle)
                last_phase = 0.7 * last_phase + 0.3 * phase
                t_pct, c_pct = _interp_waveform(active_label, last_phase)
                t_pct *= (1.0 + args.noise * random.uniform(-1.0, 1.0))
                c_pct *= (1.0 + args.noise * random.uniform(-1.0, 1.0))
                target_pct = max(0.0, min(100.0, t_pct))
                comp_pct = max(0.0, min(100.0, c_pct))

            if gate_silent:
                t_raw, c_raw = 0.0, 0.0
            else:
                # V7.14 反变换: 训练集域 → ESP32 域 (见 simulate_emg_from_mia.py 注释)
                t_pct_esp = max(0.0, _inverse_domain(target_pct, "target"))
                c_pct_esp = max(0.0, _inverse_domain(comp_pct, "comp"))
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
                simulator_src="tools/simulate_emg_from_bicep.py",
            )

            if now - state["last_angle_log"] > 2.0:
                elapsed = now - t0
                hz_eff = pkt_count / max(elapsed, 1e-3)
                if in_mvc:
                    tag = "MVC-PEAK"
                elif gate_silent:
                    tag = "SILENT(pure_vision)"
                else:
                    tag = "label={}".format(args.label)
                logging.info("📡 [%s] elbow=%.1f° phase=%.2f target=%.1f%% comp=%.1f%% @ %.0fHz",
                             tag, _read_angle(), last_phase, target_pct, comp_pct, hz_eff)
                state["last_angle_log"] = now

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
