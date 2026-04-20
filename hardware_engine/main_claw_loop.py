import asyncio
import os
import sys
import json
import time
import math
import logging
import numpy as np
import torch
from cognitive.openclaw_bridge import OpenClawBridge
from cognitive.deepseek_direct import DeepSeekDirect
from ai_sensory.asr_worker import ASRWorker
from sensor.microphone import MicrophoneController
from cognitive.fusion_model import CompensationGRU, load_model, _compute_derived_features

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [MAIN LOOP] - %(message)s')

# ===== Sprint5: SQLite 持久化懒加载 =====
_DB = [None]
_DB_SESSION = [None]
def _db():
    if _DB[0] is not None: return _DB[0]
    try:
        from persistence.db import FitnessDB
        d = FitnessDB(); d.connect(); _DB[0] = d; return d
    except Exception as e:
        logging.warning("[DB] init failed: %s", e); return None

# ===== Agent 3 GRU 推理引擎 =====
_GRU_MODEL = None  # type: CompensationGRU or None
_GRU_WINDOW_SIZE = 30
# 滚动特征缓冲区: 每行是 7D 特征向量 (归一化前)
_gru_feature_buf = []  # list of 7D feature vectors
# 推理跳帧计数器 (每 N 帧推理一次，节省 CPU)
_GRU_INFER_EVERY = 3
_gru_frame_ctr   = 0
# 上一帧 ang_vel (用于在主循环里计算 ang_accel)
_gru_prev_ang_vel: float = 0.0

# 按 exercise 选择权重文件名 (弯举使用独立权重, 避免覆盖深蹲)
_GRU_WEIGHT_BY_EXERCISE = {
    "squat":      "extreme_fusion_gru.pt",
    "bicep_curl": "extreme_fusion_gru_bicep.pt",
}

def _load_gru_model(exercise="squat"):
    """尝试加载对应 exercise 的 GRU 权重 (7D), 失败回退 4D.

    优先首选 hardware_engine/<name>.pt, 其次 cognitive/<name>.pt, 最后通用 extreme_fusion_gru.pt.
    """
    _dir = os.path.dirname(os.path.abspath(__file__))
    model_name = _GRU_WEIGHT_BY_EXERCISE.get(exercise, "extreme_fusion_gru.pt")
    candidates = [
        os.path.join(_dir, model_name),
        os.path.join(_dir, "cognitive", model_name),
        # 通用兜底 (旧共用权重)
        os.path.join(_dir, "extreme_fusion_gru.pt"),
        os.path.join(_dir, "cognitive", "extreme_fusion_gru.pt"),
    ]
    tried = set()
    for path in candidates:
        if path in tried or not os.path.exists(path):
            continue
        tried.add(path)
        try:
            model = load_model(path, input_size=7)
            size_kb = os.path.getsize(path) / 1024
            logging.info(f"[GRU] Loaded {path} for exercise={exercise} ({size_kb:.1f} KB)")
            return model
        except Exception as e:
            logging.warning(f"[GRU] load_model failed for {path}: {e}")
            try:
                model = load_model(path, input_size=4)
                logging.info(f"[GRU] Loaded 4D-compat model from {path}")
                return model
            except Exception as e2:
                logging.warning(f"[GRU] 4D fallback also failed: {e2}")
    logging.warning(f"[GRU] No model file found for exercise={exercise} — inference disabled.")
    return None

_GRU_MODEL = _load_gru_model("squat")

# V2.5: 加载教练人格
_SOUL_TEXT = ""
try:
    _soul_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cognitive', 'SOUL.md')
    if os.path.exists(_soul_path):
        with open(_soul_path, 'r', encoding='utf-8') as sf:
            _SOUL_TEXT = sf.read().strip()
        logging.info(f"✅ 教练人格 SOUL.md 已加载 ({len(_SOUL_TEXT)} chars)")
except Exception as e:
    logging.warning(f"SOUL.md 加载失败: {e}")


class SquatStateMachine:
    ANGLE_STANDARD = 100    # 必须下蹲到 100° 以内才算标准（用户要求 v5.3）
    TREND_WINDOW = 8       # 趋势检测滑窗大小
    IDLE_RANGE = 20        # 角度波动小于此值 = 静止
    IDLE_FRAMES = 25       # 连续多少帧稳定才切入 IDLE（~3s）

    def __init__(self):
        self.state = "NO_PERSON"
        self.good_squats = 0
        self.failed_squats = 0
        self.last_active_time = time.time()
        self._last_buzzer_time = 0
        self._angle_history = []
        self._min_angle_in_rep = 999
        self._idle_counter = 0
        self._last_count_time = 0
        self.total_fatigue_volume = 0  # <--- V3新增：双轨疲劳积分池
        # V7.13 底部外插补偿: 即使帧率波动也不漏捕底部角度
        self._last_valid_ts = 0.0
        self._last_valid_angle_sq = None
        self._last_ang_vel_sq = 0.0  # deg/s, 负值=下落
        # M8 (V7.14, 2026-04-20): 代偿计数器 + 防重复
        # GRU 分类 "compensating" 时递增; 同一 rep(_cur_reps) 只计一次
        self._compensation_count = 0
        self._compensation_last_rep = -1
        # V7.15: FSM 独立 rep 边界计数 (无关模式). vision_sensor 模式下 good/failed 由 GRU 分类决定
        self._total_reps_count = 0
        # V7.15: inference_mode 缓存 (避免每帧读盘)
        self._mode_cache = "pure_vision"
        self._mode_last_ts = 0.0
        # V7.16: rep-level debounce 三件套 (复用 self._last_count_time 作为冷却闸门)
        self._descending_start_ts = 0.0   # 进入 DESCENDING 的时戳
        self._falling_frames = 0          # 连续 falling 趋势计数 (入场门控)
        self._rising_frames = 0           # 连续 rising 趋势计数 (离场门控)
        # V7.17 (2026-04-20): BOTTOM/ASCENDING 可见化 —— 用户验收拍片需要完整"蹲到底→上升"动作反馈
        self._bottom_frames = 0           # 连续处于底部稳定带的帧数
        self._BOTTOM_WINDOW = 4           # 连续 4 帧稳定 = 蹲到底 (~0.3s @ 13fps)
        self._BOTTOM_EPS = 5.0            # 度 — 底部稳定带宽度

    def calculate_angle(self, a, b, c):
        try:
            ba = [a[0] - b[0], a[1] - b[1]]
            bc = [c[0] - b[0], c[1] - b[1]]
            dot_prod = ba[0]*bc[0] + ba[1]*bc[1]
            mag_ba = math.sqrt(ba[0]**2 + ba[1]**2)
            mag_bc = math.sqrt(bc[0]**2 + bc[1]**2)
            if mag_ba * mag_bc == 0:
                return 180.0
            cos_angle = dot_prod / (mag_ba * mag_bc)
            cos_angle = max(min(cos_angle, 1.0), -1.0)
            return math.degrees(math.acos(cos_angle))
        except Exception:
            return 180.0

    def trigger_buzzer_alert(self, kind="不标准"):
        """V7.6: 支持两种警报 —— "不标准" (幅度不够) / "代偿" (GRU 检测到代偿)"""
        now = time.time()
        if now - self._last_buzzer_time < 3.0:
            return
        self._last_buzzer_time = now
        try:
            tmp = "/dev/shm/violation_alert.txt.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(kind)
            os.rename(tmp, "/dev/shm/violation_alert.txt")
            logging.warning("🔊 警报已发送: %s", kind)
        except Exception as e:
            logging.error("警报写入失败: %s", e)

    def _read_emg(self):
        try:
            with open("/dev/shm/muscle_activation.json", "r") as f:
                d = json.load(f)
                return d.get("activations", {})
        except Exception:
            return {}

    _last_nn_result = None  # class-level cache for latest GRU result

    def sync_to_frontend(self, current_angle=180.0, nn_result=None):
        if nn_result is not None:
            SquatStateMachine._last_nn_result = nn_result
        try:
            emg_feats = self._read_emg()
            state_data = {
                "state": self.state,
                "good": self.good_squats,
                "failed": self.failed_squats,
                "comp": getattr(self, "_compensation_count", 0),   # V7.15: 暴露代偿计数
                "angle": round(current_angle, 1),
                "fatigue": round(self.total_fatigue_volume, 1),
                "chat_active": os.path.exists("/dev/shm/chat_active"),
                "exercise": "squat",
                "emg_activations": [
                    emg_feats.get("quadriceps", 0),
                    emg_feats.get("glutes", 0),
                    emg_feats.get("calves", 0),
                    emg_feats.get("biceps", 0)
                ]
            }
            # NN 推理结果 — 使用缓存保证每帧都有
            cached = SquatStateMachine._last_nn_result
            if cached and self.state != "NO_PERSON":
                state_data["similarity"]     = cached.get("similarity", 0.0)
                state_data["classification"] = cached.get("classification", "unknown")
                state_data["nn_confidence"]  = cached.get("confidence", 0.0)
                state_data["nn_phase"]       = cached.get("phase", "unknown")

            with open("/dev/shm/fsm_state.json.tmp", "w", encoding="utf-8") as rf:
                json.dump(state_data, rf)
            os.rename("/dev/shm/fsm_state.json.tmp", "/dev/shm/fsm_state.json")
        except Exception:
            pass

    def _get_trend(self):
        if len(self._angle_history) < 6:
            return "stable"
        recent = self._angle_history[-6:]
        deltas = [recent[i+1] - recent[i] for i in range(len(recent)-1)]
        avg_delta = sum(deltas) / len(deltas)
        if avg_delta < -2.5: 
            return "falling"
        elif avg_delta > 2.5:
            return "rising"
        return "stable"

    def update(self, pose_data):
        try:
            objects = pose_data.get("objects", [])
            if not objects:
                self.state = "NO_PERSON"
                self.sync_to_frontend()
                return None

            obj = objects[0]
            if obj.get("score", 0) < 0.05:
                self.state = "NO_PERSON"
                self.sync_to_frontend()
                return None

            kpts = obj.get("kpts", [])
            if len(kpts) < 17:
                return None

            # 关键点置信度过滤: 低于阈值的坐标不可信, 跳过此帧
            MIN_KPT_CONF = 0.05
            l_score = kpts[11][2] + kpts[13][2] + kpts[15][2]
            r_score = kpts[12][2] + kpts[14][2] + kpts[16][2]
            best_score = max(l_score, r_score)

            # 三个关键点(髋/膝/踝)的平均置信度 < 阈值 → 骨架不可信
            if best_score / 3.0 < MIN_KPT_CONF:
                # 置信度太低，不更新状态
                return None

            if l_score > r_score:
                hip   = [kpts[11][0], kpts[11][1]]
                knee  = [kpts[13][0], kpts[13][1]]
                ankle = [kpts[15][0], kpts[15][1]]
            else:
                hip   = [kpts[12][0], kpts[12][1]]
                knee  = [kpts[14][0], kpts[14][1]]
                ankle = [kpts[16][0], kpts[16][1]]
            raw_angle = self.calculate_angle(hip, knee, ankle)

            # 角度合理性过滤 (Task 4): 量化模型噪声产生不可能的角度
            if raw_angle < 20 or raw_angle > 175:
                logging.debug("角度异常丢弃: %.1f° (合理范围 20-175)", raw_angle)
                return None

            # 关键点间距检查: 髋-踝太近 = 关键点重叠不可信
            dist_ha = math.hypot(hip[0] - ankle[0], hip[1] - ankle[1])
            if dist_ha < 30:
                logging.debug("关键点间距过小: %.1f px, 丢弃此帧", dist_ha)
                return None

            self._angle_history.append(raw_angle)
            if len(self._angle_history) > 16:
                self._angle_history.pop(0)
            smooth_n = min(5, len(self._angle_history))
            angle = sum(self._angle_history[-smooth_n:]) / smooth_n

            trend = self._get_trend()

            # ===== 状态流转 (V7.17: 五级可见 —— NO_PERSON/STAND/DESCENDING/BOTTOM/ASCENDING) =====
            if self.state in ["NO_PERSON", "IDLE", "STAND"]:
                # V7.16: 维护连续 falling 帧计数 (其他趋势重置)
                if trend == "falling":
                    self._falling_frames += 1
                elif trend == "rising":
                    self._falling_frames = 0

                # V7.16: 入场必须满足 ALL 三项 —— 角度阈值 + 2 连续 falling + 0.8s 冷却
                _cooldown_ok = (time.time() - self._last_count_time) >= 0.8
                if angle < 140 and self._falling_frames >= 2 and _cooldown_ok:
                    self.state = "DESCENDING"
                    self._min_angle_in_rep = angle
                    self._descending_start_ts = time.time()
                    self._rising_frames = 0
                    self._bottom_frames = 0
                    self.last_active_time = time.time()
                else:
                    self.state = "STAND"

            elif self.state == "DESCENDING":
                self.last_active_time = time.time()
                # V7.16: 维护连续 rising 帧计数 (在 DESCENDING 中统计起身信号)
                if trend == "rising":
                    self._rising_frames += 1
                elif trend == "falling":
                    self._rising_frames = 0
                # V7.13 底部外插: 若两帧间隙 > 80ms 且之前在下落, 认为错过了真实底部
                # 用 angle_prev + ang_vel_prev * dt/2 估算中间点的最深角度
                now_ts = time.time()
                virtual_bottom = None
                if self._last_valid_angle_sq is not None:
                    dt = now_ts - self._last_valid_ts
                    if 0.08 < dt < 0.25 and self._last_ang_vel_sq < -8.0:
                        predicted = self._last_valid_angle_sq + self._last_ang_vel_sq * (dt * 0.5)
                        # 物理下限钳位: 不可能低于 40°(髋过膝风险位置)
                        virtual_bottom = max(40.0, predicted)
                if virtual_bottom is not None:
                    self._min_angle_in_rep = min(self._min_angle_in_rep, virtual_bottom, angle)
                else:
                    self._min_angle_in_rep = min(self._min_angle_in_rep, angle)

                # V7.17: 底部稳定带 —— 角度在 min + 5° 内连续 N 帧 ⇒ BOTTOM（蹲到底）
                if angle <= self._min_angle_in_rep + self._BOTTOM_EPS:
                    self._bottom_frames += 1
                else:
                    self._bottom_frames = 0

                # V7.17: 快速 rep（无明显 hold）—— 已离底 > 10° 且连续 rising ⇒ 直入 ASCENDING
                if angle > self._min_angle_in_rep + 10.0 and self._rising_frames >= 2:
                    self.state = "ASCENDING"
                # V7.17: 标准 rep —— 底部稳定带达标 ⇒ BOTTOM
                elif self._bottom_frames >= self._BOTTOM_WINDOW:
                    self.state = "BOTTOM"
                    self._rising_frames = 0

            elif self.state == "BOTTOM":
                # V7.17: 蹲到底稳定态 —— 继续下落则回 DESCENDING 更新 min；连续 rising ⇒ ASCENDING
                self.last_active_time = time.time()
                self._min_angle_in_rep = min(self._min_angle_in_rep, angle)
                if trend == "rising":
                    self._rising_frames += 1
                elif trend == "falling":
                    self._rising_frames = 0
                    # 还在继续探底 — 回 DESCENDING 刷 min
                    if angle < self._min_angle_in_rep - 1.5:
                        self.state = "DESCENDING"
                        self._bottom_frames = 0
                if self._rising_frames >= 2 and angle > self._min_angle_in_rep + 5.0:
                    self.state = "ASCENDING"

            elif self.state == "ASCENDING":
                # V7.17: 上升段 —— 原 DESCENDING 内的结账逻辑搬到此处
                self.last_active_time = time.time()
                if trend == "rising":
                    self._rising_frames += 1
                elif trend == "falling":
                    self._rising_frames = 0

                # V7.16: 结账必须满足 ALL 三项 —— angle>150 (安全边距) + 2 连续 rising + 最小 rep 时长 0.5s
                _dur_ok = (time.time() - self._descending_start_ts) >= 0.5
                if angle > 150 and self._rising_frames >= 2 and _dur_ok:
                    bottom = self._min_angle_in_rep
                    # V7.15: 无论模式都推进 rep 边界计数 + 疲劳 (外层 GRU 推理靠此触发)
                    self._total_reps_count += 1
                    volume = 1500.0 / 7.0
                    self.total_fatigue_volume += volume

                    # V7.15: 读 inference_mode (100ms 缓存), 决定是否走角度硬判定
                    now_for_mode = time.time()
                    if now_for_mode - self._mode_last_ts > 0.1:
                        try:
                            if os.path.exists("/dev/shm/inference_mode.json"):
                                with open("/dev/shm/inference_mode.json", "r") as _mf:
                                    m = json.load(_mf).get("mode", "pure_vision")
                                    if m in ("pure_vision", "vision_sensor"):
                                        self._mode_cache = m
                        except Exception:
                            pass
                        self._mode_last_ts = now_for_mode

                    if self._mode_cache == "vision_sensor":
                        # V7.15: vision_sensor 模式 — good/failed/comp 由外层 GRU 分类决定
                        # 不在此处累加, 不触发"不标准"警报
                        logging.info(f"⏳ [vision_sensor] rep #{self._total_reps_count} 角度{bottom:.0f}° 等待 GRU 分类")
                    else:
                        # pure_vision 模式 — 保留角度硬判定
                        if bottom < self.ANGLE_STANDARD:
                            self.good_squats += 1
                            logging.info(f"🟢 好球！（角度{bottom:.0f}°）当前总疲劳值: {self.total_fatigue_volume:.1f}")
                        else:
                            self.failed_squats += 1
                            self.trigger_buzzer_alert()
                            logging.warning(f"🟡 违规：下蹲幅度不足！（当前最低{bottom:.0f}°）累计违规：{self.failed_squats}")

                    # 结算完毕，复位回归直立监控区
                    self.state = "STAND"
                    self._min_angle_in_rep = 999
                    self._last_count_time = time.time()   # V7.16: 激活 rep 冷却锁
                    # V7.16: 清零 debounce 计数，避免串到下一 rep
                    self._falling_frames = 0
                    self._rising_frames = 0
                    self._descending_start_ts = 0.0
                    self._bottom_frames = 0               # V7.17: 底部帧计数复位
                    # V7.13: rep 结算后清空外插追踪, 避免串到下一 rep
                    self._last_valid_angle_sq = None
                    self._last_ang_vel_sq = 0.0

            # V7.13: 每帧末尾刷新角速度追踪 (用于下一帧的外插基准)
            _now_update = time.time()
            if self._last_valid_angle_sq is not None:
                _dt_upd = _now_update - self._last_valid_ts
                if _dt_upd > 1e-3:
                    self._last_ang_vel_sq = (angle - self._last_valid_angle_sq) / _dt_upd
            self._last_valid_angle_sq = angle
            self._last_valid_ts = _now_update

            self.sync_to_frontend(angle)
            return angle
        except Exception as e:
            logging.error(f"FSM 异常: {e}")
            self.state = "NO_PERSON"
            self.sync_to_frontend()
            return None


class DumbbellCurlFSM:
    ANGLE_STANDARD = 50    # 大臂小臂夹角小于50度视为合格弯举收缩
    
    def __init__(self):
        self.state = "NO_PERSON"
        self._good_reps = 0
        self._failed_reps = 0
        self.last_active_time = time.time()
        self._last_buzzer_time = 0
        self._angle_history = []
        self._min_angle_in_rep = 999
        self._last_count_time = 0
        self.total_fatigue_volume = 0
        # V7.13 顶峰外插补偿: 即使帧率波动也不漏捕最小角度 (手臂收紧峰值)
        self._last_valid_ts = 0.0
        self._last_valid_angle_cu = None
        self._last_ang_vel_cu = 0.0  # deg/s, 负值=肘关节正在闭合
        # M8 (V7.14): 弯举动作也要有代偿计数, prompt 统一
        self._compensation_count = 0
        self._compensation_last_rep = -1
        # V7.15: FSM 独立 rep 边界计数 (无关模式)
        self._total_reps_count = 0
        self._mode_cache = "pure_vision"
        self._mode_last_ts = 0.0
        # V7.16: rep-level debounce 三件套 (与 SquatStateMachine 对称)
        self._curling_start_ts = 0.0      # 进入 CURLING 的时戳
        self._closing_frames = 0          # 连续肘关节闭合 (falling) 帧 — 入场门控
        self._opening_frames = 0          # 连续肘关节张开 (rising) 帧 — 离场门控

    # V7.16: 与 SquatStateMachine._get_trend 同逻辑，curl 的 falling=收紧、rising=伸展
    def _get_trend(self):
        if len(self._angle_history) < 6:
            return "stable"
        recent = self._angle_history[-6:]
        deltas = [recent[i+1] - recent[i] for i in range(len(recent)-1)]
        avg_delta = sum(deltas) / len(deltas)
        if avg_delta < -2.5:
            return "falling"
        elif avg_delta > 2.5:
            return "rising"
        return "stable"

    @property
    def good_squats(self): return self._good_reps
    @good_squats.setter
    def good_squats(self, val): self._good_reps = val
    
    @property
    def failed_squats(self): return self._failed_reps
    @failed_squats.setter
    def failed_squats(self, val): self._failed_reps = val

    def calculate_angle(self, a, b, c):
        try:
            ba = [a[0] - b[0], a[1] - b[1]]
            bc = [c[0] - b[0], c[1] - b[1]]
            dot_prod = ba[0]*bc[0] + ba[1]*bc[1]
            mag_ba = math.sqrt(ba[0]**2 + ba[1]**2)
            mag_bc = math.sqrt(bc[0]**2 + bc[1]**2)
            if mag_ba * mag_bc == 0:
                return 180.0
            cos_angle = dot_prod / (mag_ba * mag_bc)
            cos_angle = max(min(cos_angle, 1.0), -1.0)
            return math.degrees(math.acos(cos_angle))
        except Exception:
            return 180.0

    def trigger_buzzer_alert(self, kind="不标准"):
        now = time.time()
        if now - self._last_buzzer_time < 3.0:
            return
        self._last_buzzer_time = now
        try:
            tmp = "/dev/shm/violation_alert.txt.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(kind)
            os.rename(tmp, "/dev/shm/violation_alert.txt")
            logging.warning("🔊 弯举警报已发送: %s", kind)
        except Exception as e:
            logging.error("违规警报写入失败: %s", e)

    def _read_emg(self):
        try:
            with open("/dev/shm/muscle_activation.json", "r") as f:
                d = json.load(f)
                return d.get("activations", {})
        except Exception:
            return {}

    _last_nn_result = None

    def sync_to_frontend(self, current_angle=180.0, nn_result=None):
        if nn_result is not None:
            DumbbellCurlFSM._last_nn_result = nn_result
        try:
            emg_feats = self._read_emg()
            state_data = {
                "state": self.state,
                "good": self._good_reps,
                "failed": self._failed_reps,
                "comp": getattr(self, "_compensation_count", 0),   # V7.15
                "angle": round(current_angle, 1),
                "fatigue": round(self.total_fatigue_volume, 1),
                "chat_active": os.path.exists("/dev/shm/chat_active"),
                "exercise": "bicep_curl",
                "emg_activations": [
                    emg_feats.get("biceps", 0),
                    emg_feats.get("forearm", 0),
                    emg_feats.get("shoulder", emg_feats.get("deltoid", 0)),
                    emg_feats.get("triceps", 0)
                ]
            }
            cached = DumbbellCurlFSM._last_nn_result
            if cached and self.state != "NO_PERSON":
                state_data["similarity"]     = cached.get("similarity", 0.0)
                state_data["classification"] = cached.get("classification", "unknown")
                state_data["nn_confidence"]  = nn_result.get("confidence", 0.0)
                state_data["nn_phase"]       = nn_result.get("phase", "unknown")

            with open("/dev/shm/fsm_state.json.tmp", "w", encoding="utf-8") as rf:
                json.dump(state_data, rf)
            os.rename("/dev/shm/fsm_state.json.tmp", "/dev/shm/fsm_state.json")
        except Exception:
            pass

    def update(self, pose_data):
        try:
            objects = pose_data.get("objects", [])
            if not objects:
                self.state = "NO_PERSON"
                self.sync_to_frontend()
                return None

            obj = objects[0]
            if obj.get("score", 0) < 0.05:
                self.state = "NO_PERSON"
                self.sync_to_frontend()
                return None

            kpts = obj.get("kpts", [])
            if len(kpts) < 17:
                return None

            MIN_KPT_CONF = 0.05
            l_score = kpts[5][2] + kpts[7][2] + kpts[9][2]
            r_score = kpts[6][2] + kpts[8][2] + kpts[10][2]
            if max(l_score, r_score) / 3.0 < MIN_KPT_CONF:
                return None

            if l_score > r_score:
                shoulder = [kpts[5][0], kpts[5][1]]
                elbow    = [kpts[7][0], kpts[7][1]]
                wrist    = [kpts[9][0], kpts[9][1]]
            else:
                shoulder = [kpts[6][0], kpts[6][1]]
                elbow    = [kpts[8][0], kpts[8][1]]
                wrist    = [kpts[10][0], kpts[10][1]]
                
            raw_angle = self.calculate_angle(shoulder, elbow, wrist)

            # 角度合理性过滤 (Task 4)
            if raw_angle < 10 or raw_angle > 175:
                logging.debug("弯举角度异常丢弃: %.1f°", raw_angle)
                return None

            # 关键点间距检查
            dist_sw = math.hypot(shoulder[0] - wrist[0], shoulder[1] - wrist[1])
            if dist_sw < 20:
                logging.debug("弯举关键点间距过小: %.1f px", dist_sw)
                return None

            self._angle_history.append(raw_angle)
            if len(self._angle_history) > 16:
                self._angle_history.pop(0)
            smooth_n = min(5, len(self._angle_history))
            angle = sum(self._angle_history[-smooth_n:]) / smooth_n

            # V7.16: 四级防抖状态流转（与 squat 对称）
            trend = self._get_trend()
            if self.state in ["NO_PERSON", "IDLE", "STAND", "EXTENDING"]:
                if trend == "falling":
                    self._closing_frames += 1
                elif trend == "rising":
                    self._closing_frames = 0
                _cooldown_ok = (time.time() - self._last_count_time) >= 0.8
                if angle < 140 and self._closing_frames >= 2 and _cooldown_ok:
                    self.state = "CURLING"
                    self._min_angle_in_rep = angle
                    self._curling_start_ts = time.time()
                    self._opening_frames = 0
                    self.last_active_time = time.time()
                else:
                    self.state = "STAND"

            elif self.state == "CURLING":
                self.last_active_time = time.time()
                if trend == "rising":
                    self._opening_frames += 1
                elif trend == "falling":
                    self._opening_frames = 0
                # V7.13 顶峰外插: 若两帧间隙 > 80ms 且之前在收紧, 认为错过了真实顶峰
                now_ts = time.time()
                virtual_peak = None
                if self._last_valid_angle_cu is not None:
                    dt = now_ts - self._last_valid_ts
                    if 0.08 < dt < 0.25 and self._last_ang_vel_cu < -8.0:
                        predicted = self._last_valid_angle_cu + self._last_ang_vel_cu * (dt * 0.5)
                        # 物理下限钳位: 肘关节最小可闭合角 ~25°
                        virtual_peak = max(25.0, predicted)
                if virtual_peak is not None:
                    self._min_angle_in_rep = min(self._min_angle_in_rep, virtual_peak, angle)
                else:
                    self._min_angle_in_rep = min(self._min_angle_in_rep, angle)

                # V7.16: 结账门 —— angle>150 + 2 连续 rising + 最小 rep 时长 0.5s
                _dur_ok = (time.time() - self._curling_start_ts) >= 0.5
                if angle > 150 and self._opening_frames >= 2 and _dur_ok:
                    bottom = self._min_angle_in_rep
                    # V7.15: 无论模式都推进 rep 边界计数 + 疲劳
                    self._total_reps_count += 1
                    volume = 1500.0 / 7.0
                    self.total_fatigue_volume += volume

                    # V7.15: 读 inference_mode (100ms 缓存)
                    now_for_mode = time.time()
                    if now_for_mode - self._mode_last_ts > 0.1:
                        try:
                            if os.path.exists("/dev/shm/inference_mode.json"):
                                with open("/dev/shm/inference_mode.json", "r") as _mf:
                                    m = json.load(_mf).get("mode", "pure_vision")
                                    if m in ("pure_vision", "vision_sensor"):
                                        self._mode_cache = m
                        except Exception:
                            pass
                        self._mode_last_ts = now_for_mode

                    if self._mode_cache == "vision_sensor":
                        logging.info(f"⏳ [vision_sensor] 弯举 rep #{self._total_reps_count} 顶峰{bottom:.0f}° 等待 GRU 分类")
                    else:
                        if bottom < self.ANGLE_STANDARD:
                            self._good_reps += 1
                            logging.info(f"🟢 弯举达标！（顶峰角度{bottom:.0f}°）总疲劳值: {self.total_fatigue_volume:.1f}")
                        else:
                            self._failed_reps += 1
                            self.trigger_buzzer_alert()
                            logging.warning(f"🟡 弯举违规：收缩幅度不足！（收缩极限仅有{bottom:.0f}°）累计违规：{self._failed_reps}")

                    self.state = "STAND"
                    self._min_angle_in_rep = 999
                    self._last_count_time = time.time()   # V7.16: rep 冷却锁
                    # V7.16: 清零 debounce 计数
                    self._closing_frames = 0
                    self._opening_frames = 0
                    self._curling_start_ts = 0.0
                    # V7.13: rep 结算后清空外插追踪, 避免串到下一 rep
                    self._last_valid_angle_cu = None
                    self._last_ang_vel_cu = 0.0

            # V7.13: 每帧末尾刷新角速度追踪 (用于下一帧的外插基准)
            _now_update = time.time()
            if self._last_valid_angle_cu is not None:
                _dt_upd = _now_update - self._last_valid_ts
                if _dt_upd > 1e-3:
                    self._last_ang_vel_cu = (angle - self._last_valid_angle_cu) / _dt_upd
            self._last_valid_angle_cu = angle
            self._last_valid_ts = _now_update

            self.sync_to_frontend(angle)
            return angle
        except Exception as e:
            logging.error(f"FSM 异常: {e}")
            self.state = "NO_PERSON"
            self.sync_to_frontend()
            return None


async def _deepseek_fire_and_forget(bridge, prompt, good_count, failed_count):
    for attempt in range(3):
        try:
            logging.info(f"📤 [后台] 发送战报给 DeepSeek (尝试 {attempt+1}/3)...")
            start_time = time.time()
            reply = await bridge.ask(prompt, timeout=15)  # V7.10 60s->15s
            elapsed = time.time() - start_time

            if "Timeout" in reply or "Gateway" in reply or "rejected" in reply:
                logging.warning(f"⚠️ [后台] 尝试 {attempt+1} 返回错误: {reply}")
                if attempt < 2:
                    await asyncio.sleep(3)
                    continue

            # Strip <think>...</think> reasoning block (same as chat path)
            if "</think>" in reply:
                reply = reply.split("</think>")[-1].strip()
            logging.info(f"💡 [后台] DeepSeek 响应 ({elapsed:.2f}s): {reply}")

            try:
                with open("/dev/shm/llm_reply.txt.tmp", "w", encoding="utf-8") as rf:
                    rf.write(reply)
                os.rename("/dev/shm/llm_reply.txt.tmp", "/dev/shm/llm_reply.txt")
                # V5.0: 写 seq 递增,voice_daemon 的 _llm_reply_watcher 靠 seq 捕获同秒多写
                try:
                    _seq_path = "/dev/shm/llm_reply.txt.seq"
                    _prev = 0
                    if os.path.exists(_seq_path):
                        with open(_seq_path, "r") as _sf:
                            _prev = int((_sf.read() or "0").strip() or "0")
                    with open(_seq_path + ".tmp", "w") as _sf:
                        _sf.write(str(_prev + 1))
                    os.rename(_seq_path + ".tmp", _seq_path)
                except Exception:
                    pass
            except Exception as e:
                logging.error(f"下发回复至内存盘失败: {e}")

            # 飞书推送已改为手动/语音触发，不再自动推送每次训练点评
            return reply
        except Exception as e:
            logging.error(f"❌ [后台] 尝试 {attempt+1} 异常: {e}")
            if attempt < 2:
                await asyncio.sleep(3)
    logging.error("❌ [后台] DeepSeek 3 次重试全部失败")
    return ""


async def main():
    # 必须在函数顶部声明 global, 因为下方 909/920 行会读 _GRU_MODEL,
    # 982 行才赋值; Python 3 要求 global 声明先于任何读写
    global _GRU_MODEL
    logging.info("🚀 启动 IronBuddy V3 双轨融合状态机中枢...")

    for f in ["/dev/shm/llm_reply.txt", "/dev/shm/chat_input.txt", "/dev/shm/chat_reply.txt"]:
        try:
            os.remove(f)
        except OSError:
            pass

    # ===== M10 (V7.16, 2026-04-20): 启动初始化 - 清理所有残留信号文件 =====
    # 背景: 残留 /dev/shm/user_profile.json 导致 "语音切 curl 后 50ms 被拉回 squat" bug.
    # 该文件每帧被 main_claw 读取, fallback=squat, 无 mtime 去重 -> 循环覆盖.
    # 配合清理 inference_mode.json 让重启默认走纯视觉 (与用户验收要求一致).
    _M10_CLEANUP = [
        "/dev/shm/user_profile.json",       # UI exercise 选择 (主要污染源)
        "/dev/shm/exercise_mode.json",      # 语音 exercise 指令
        "/dev/shm/inference_mode.json",     # 视觉模式 (清后 -> 默认 pure_vision)
        "/dev/shm/fatigue_limit.json",      # 疲劳上限
        "/dev/shm/ui_fatigue_limit.json",   # UI 疲劳上限镜像
        "/dev/shm/next_set.request",        # 下一组请求
        "/dev/shm/fatigue_reset.request",   # 清零请求
        "/dev/shm/mvc_calibrate.request",   # MVC 请求
        "/dev/shm/mvc_calibrate.result",    # MVC 结果
        "/dev/shm/trigger_deepseek",        # 手动 DeepSeek 触发
        "/dev/shm/fsm_reset_signal",        # FSM 重置信号
        "/dev/shm/voice_interrupt",         # 语音打断
        "/dev/shm/chat_active",             # 对话激活标志
        "/dev/shm/violation_alert.txt",     # 残留违规警报
    ]
    _cleaned = 0
    for f in _M10_CLEANUP:
        try:
            if os.path.exists(f):
                os.remove(f)
                _cleaned += 1
        except OSError:
            pass
    logging.info(f"🧹 [M10] 启动清理完成: 移除 {_cleaned} 个残留 shm 信号 (默认 pure_vision + squat)")

    # LLM 后端切换: LLM_BACKEND=direct 使用 DeepSeek 直连, 否则走 OpenClaw Gateway
    llm_backend = os.environ.get("LLM_BACKEND", "direct").lower()
    bridge = None
    connected = False
    if llm_backend == "direct":
        logging.info("LLM 后端: DeepSeek Direct (绕过 Gateway)")
        try:
            bridge = DeepSeekDirect(soul_text=_SOUL_TEXT[:500] if _SOUL_TEXT else "")
            connected = await bridge.connect()
        except Exception as _e:
            logging.warning("DeepSeek Direct 初始化失败: %s", _e)
            connected = False
        if not connected:
            # 直连失败，尝试回退到 OpenClaw
            logging.warning("DeepSeek Direct 不可用，尝试 OpenClaw Gateway")
            try:
                gateway_url = os.environ.get("OPENCLAW_URL", "ws://127.0.0.1:18789")
                bridge = OpenClawBridge(gateway_url=gateway_url)
                connected = await bridge.connect()
            except Exception as _e:
                logging.warning("OpenClaw Gateway 也不可用: %s", _e)
                connected = False
    else:
        logging.info("LLM 后端: OpenClaw Gateway")
        try:
            gateway_url = os.environ.get("OPENCLAW_URL", "ws://127.0.0.1:18789")
            bridge = OpenClawBridge(gateway_url=gateway_url)
            connected = await bridge.connect()
        except Exception as _e:
            logging.warning("OpenClaw Gateway 连接失败: %s", _e)
            connected = False

    if not connected:
        logging.warning("⚠️ 所有 LLM 后端均不可用，FSM 将以纯视觉模式运行（无 AI 对话）")
        bridge = None
    
    current_exercise = "squat"
    _last_applied_modes = {"inference": "pure_vision", "exercise": "squat"}  # V6.1
    # V7.11 \u8de8\u7ec4\u603b\u8ba1: \u4e0b\u4e00\u7ec4\u91cd\u7f6e fsm \u65f6 \u4f1a\u5148\u628a\u672c\u7ec4\u6570\u636e merge \u5230\u8fd9\u91cc
    _session_totals = {"good": 0, "failed": 0, "comp": 0}
    fsm = SquatStateMachine()
    # Sprint5: 开启首个训练 session
    try:
        _d = _db()
        if _d is not None: _DB_SESSION[0] = _d.start_session(current_exercise)
    except Exception as _e: logging.warning("[DB] session init skipped: %s", _e)
    _last_deepseek_time = time.time()
    _ds_lock = [False]
    _this_set_triggered = [False]  # V7.11: \u6bcf\u7ec4\u53ea\u89e6\u53d1\u4e00\u6b21 API \u603b\u7ed3, "\u4e0b\u4e00\u7ec4" \u624d\u91cd\u7f6e
    _fatigue_limit = [1500]  # 可通过语音调整

    async def _ds_wrapper(b, p, g, f, trigger_reason="fatigue"):
        reply_text = ""
        try:
            reply_text = await _deepseek_fire_and_forget(b, p, g, f) or ""
        except Exception as exc:
            logging.error("[_ds_wrapper] 失败: %s", exc)
        finally:
            # Sprint5: LLM 调用完成后落库（带真实回复）
            try:
                _d = _db()
                if _d is not None:
                    _d.log_llm(trigger_reason, p, reply_text or "(empty)", 0, 0)
            except Exception as _e:
                logging.warning("[DB] log_llm 失败: %s", _e)
            _ds_lock[0] = False
            logging.info("[_ds_wrapper] 已释放 _ds_lock")

    _chat_lock = [False]
    _chat_mtime = [0]

    async def _chat_handler(bridge_ref, user_text):
        try:
            user_text = user_text.strip()
            if not user_text or len(user_text) < 2: return
            # V7.5: voice_daemon 的 B 路闲聊已经自己调 DeepSeek, 尾部标 [voice-handled], FSM 跳过防双调
            if "[voice-handled]" in user_text:
                logging.info(f"[voice-handled] 跳过 (voice_daemon 已处理)")
                return
            # V7.2: 静音态下不响应 chat_input
            try:
                if os.path.exists("/dev/shm/mute_signal.json"):
                    with open("/dev/shm/mute_signal.json", "r") as _mf:
                        if bool(json.load(_mf).get("muted", False)):
                            logging.info(f"[静音] 忽略 chat_input: {user_text[:30]}")
                            return
            except Exception:
                pass
            logging.info(f"🎤 [对话] 收到用户消息: {user_text}")
            
            # V4.5: 疲劳上限实时读取 _fatigue_limit（用户可通过语音/UI 改为 1900 等任意值）
            _fl_now = _fatigue_limit[0]
            _fl_pct = round(fsm.total_fatigue_volume / _fl_now * 100) if _fl_now > 0 else 0
            # V7.8: 板端时区 UTC, 手动 +8h 转北京时间
            _cn_ts = time.time() + 8 * 3600
            _now_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime(_cn_ts))
            _weekday_cn = ["一", "二", "三", "四", "五", "六", "日"][time.gmtime(_cn_ts).tm_wday]
            prompt = (
                f"现在是 {_now_str} 星期{_weekday_cn}。"
                f"{_SOUL_TEXT[:500] + chr(10) + chr(10) if _SOUL_TEXT else ''}"
                f"当前数据: 标准深蹲 {fsm.good_squats} 次, 违规 {fsm.failed_squats} 次, "
                f"疲劳 {fsm.total_fatigue_volume:.0f}/{_fl_now}（{_fl_pct}%）。"
                f"汇报疲劳时**必须使用当前真实上限 {_fl_now}**，不要说 1500 等旧数字。"
                f"结合数据客观回答, 用正式专业教练语气, 40字内。不说'你小子'、'老铁'、'行啊'等口语俚语。"
                f"不要 <think> 标签。"
                f"用户说: {user_text}"
            )
            reply = await bridge_ref.ask(prompt, timeout=60)
            if "</think>" in reply:
                reply = reply.split("</think>")[-1].strip()
            logging.info(f"💬 [对话] DeepSeek 回复: {reply}")
            with open("/dev/shm/chat_reply.txt.tmp", "w", encoding="utf-8") as rf:
                rf.write(reply)
            os.rename("/dev/shm/chat_reply.txt.tmp", "/dev/shm/chat_reply.txt")
            # V5.0: 写 seq 递增,voice_daemon 的 _chat_reply_watcher 靠 seq 捕获同秒多写
            try:
                _seq_path = "/dev/shm/chat_reply.txt.seq"
                _prev = 0
                if os.path.exists(_seq_path):
                    with open(_seq_path, "r") as _sf:
                        _prev = int((_sf.read() or "0").strip() or "0")
                with open(_seq_path + ".tmp", "w") as _sf:
                    _sf.write(str(_prev + 1))
                os.rename(_seq_path + ".tmp", _seq_path)
            except Exception:
                pass
            # V4.7：语音对话也要落库（供后端 OpenClaw 偏好学习使用）
            try:
                _d = _db()
                if _d is not None:
                    _d.log_llm("voice_chat", prompt, reply or "(empty)", 0, 0)
            except Exception as _e:
                logging.warning("[DB] log_llm voice_chat 失败: %s", _e)
        except Exception as e:
            pass
        finally:
            _chat_lock[0] = False

    # V7.13: 轮询 pose_data.json 的间隔由环境变量 FSM_POLL_INTERVAL 控制
    # 默认 0.03s (33Hz) 匹配上游 25fps 视觉帧率, 比旧 0.05s 快 40%
    _fsm_poll_interval = float(os.environ.get("FSM_POLL_INTERVAL", "0.03"))
    try:
        while True:
            await asyncio.sleep(_fsm_poll_interval)

            try:
                with open("/dev/shm/pose_data.json", "r") as f:
                    pose_data = json.load(f)
                    
                angle = fsm.update(pose_data)
                
                # ===== Agent 3 探针：7D Late-Fusion 特征拦截 + GRU 推理 =====
                if angle is not None:
                    target_emg, comp_emg = 0.0, 0.0
                    try:
                        with open("/dev/shm/muscle_activation.json", "r") as mf:
                            m_data = json.load(mf)
                            acts = m_data.get("activations", {})
                            # exercise 感知 key 路由 (对齐 udp_emg_server.py:313-326)
                            # 弯举: udp_emg 把 target_pct 写到 biceps, comp_pct 写到 glutes
                            # 深蹲: udp_emg 把 target_pct 写到 glutes,  comp_pct 写到 biceps
                            if current_exercise == "bicep_curl":
                                target_emg = acts.get("biceps", 0.0)
                                comp_emg   = acts.get("glutes", 0.0)
                            else:
                                target_emg = acts.get("glutes", 0.0)
                                comp_emg   = acts.get("biceps", 0.0)
                    except:
                        pass

                    ang_vel = 0.0
                    if len(fsm._angle_history) >= 2:
                        ang_vel = fsm._angle_history[-1] - fsm._angle_history[-2]

                    # --- CSV 录制 (兼容旧格式 4D + 新格式 7D) ---
                    if os.path.exists("/dev/shm/record_mode"):
                        try:
                            with open("/dev/shm/record_mode", "r") as rf:
                                mode = rf.read().strip()
                            if mode in ["golden", "lazy", "bad"]:
                                csv_file = f"train_squat_{mode}.csv"
                                exists = os.path.exists(csv_file)
                                with open(csv_file, "a") as csvf:
                                    if not exists:
                                        csvf.write("Timestamp,Ang_Vel,Angle,Target_RMS,Comp_RMS\n")
                                    csvf.write(f"{time.time():.3f},{ang_vel:.2f},{angle:.2f},{target_emg:.2f},{comp_emg:.2f}\n")
                        except:
                            pass

                    # --- GRU 推理 ---
                    global _gru_feature_buf, _gru_frame_ctr, _gru_prev_ang_vel
                    ang_accel = ang_vel - _gru_prev_ang_vel
                    _gru_prev_ang_vel = ang_vel

                    # Phase progress: rough estimate from angle history
                    _ah = fsm._angle_history
                    if len(_ah) >= 2:
                        a_min = min(_ah)
                        a_max = max(_ah)
                        phase_prog = float(np.clip(
                            1.0 - (angle - a_min) / max(a_max - a_min, 1.0),
                            0.0, 1.0
                        ))
                    else:
                        phase_prog = 0.0

                    _gru_feature_buf.append([
                        ang_vel, angle, ang_accel,
                        target_emg, comp_emg,
                        1.0,         # Symmetry_Score placeholder
                        phase_prog,
                    ])
                    if len(_gru_feature_buf) > 200:  # keep ~10s buffer
                        _gru_feature_buf.pop(0)

                    # GRU 推理：仅在一个完整动作结束时触发
                    # V7.15: 用 FSM 独立的 _total_reps_count (vision_sensor 模式下 good/failed 不自增)
                    _cur_reps = getattr(fsm, '_total_reps_count', 0)
                    if _cur_reps == 0:
                        # 回退: 旧逻辑兼容 (pure_vision 下 good+failed)
                        _cur_reps = getattr(fsm, 'good_squats', 0) + getattr(fsm, 'failed_squats', 0) + \
                                    getattr(fsm, '_good_reps', 0) + getattr(fsm, '_failed_reps', 0)
                    if not hasattr(fsm, '_prev_total_reps'):
                        fsm._prev_total_reps = _cur_reps
                    # V7.14 FIX: 拆分 DB 路径与 GRU 路径的 rep 推进计数器, 否则 DB 路径先推
                    # _prev_total_reps, GRU 触发条件永远为 False → classification 永远缺失 → UI "无模型"
                    if not hasattr(fsm, '_prev_total_reps_gru'):
                        fsm._prev_total_reps_gru = _cur_reps

                    # Read inference mode (pure_vision = skip GRU, vision_sensor = run GRU)
                    _inference_mode = "pure_vision"
                    try:
                        _im_path = "/dev/shm/inference_mode.json"
                        if os.path.exists(_im_path):
                            with open(_im_path, "r") as _imf:
                                _inference_mode = json.load(_imf).get("mode", "pure_vision")
                    except Exception:
                        pass

                    # Sprint5: 检测到 rep 变化 → 写入 DB（与 GRU 推理独立，不依赖模型）
                    if _cur_reps > fsm._prev_total_reps:
                        try:
                            _d = _db()
                            if _d is not None:
                                _cur_good = getattr(fsm,'good_squats',0)
                                _is_good = _cur_good > getattr(fsm,'_db_prev_good',0)
                                fsm._db_prev_good = _cur_good
                                _d.log_rep(_DB_SESSION[0], _is_good, getattr(fsm,'_min_angle_in_rep',0.0), target_emg, comp_emg)
                        except Exception as _e: logging.warning("[DB] log_rep: %s", _e)
                        fsm._prev_total_reps = _cur_reps  # DB 路径独立计数器

                    nn_result = None
                    # V7.14 FIX: 用独立的 _prev_total_reps_gru 避免与 DB 路径冲突
                    if _cur_reps > fsm._prev_total_reps_gru and _GRU_MODEL is not None and _inference_mode == "vision_sensor":
                        # 一个动作刚完成！用积累的数据推理
                        fsm._prev_total_reps_gru = _cur_reps
                        if len(_gru_feature_buf) >= _GRU_WINDOW_SIZE:
                            try:
                                window = np.array(_gru_feature_buf[-_GRU_WINDOW_SIZE:],
                                                  dtype=np.float32)
                                window[:, 1] /= 180.0
                                window[:, 3] /= 100.0
                                window[:, 4] /= 100.0
                                window[:, 2]  = np.clip(window[:, 2] / 10.0, -1.0, 1.0)
                                nn_result = _GRU_MODEL.infer(window)
                                cls_cn = {"standard":"标准","compensating":"代偿","non_standard":"错误"}
                                _cls = nn_result.get("classification", "unknown")
                                logging.info(f"🧠 [GRU] 第{_cur_reps}个动作判定: "
                                             f"相似度={nn_result['similarity']:.3f} "
                                             f"分类={cls_cn.get(_cls, _cls)} "
                                             f"置信度={nn_result['confidence']:.3f}")
                                # V7.15: vision_sensor 模式 — 按 GRU 分类累加 good/failed/comp
                                # (FSM DESCENDING/CURLING 分支在 vision_sensor 下已跳过这些累加)
                                if _cls == "standard":
                                    fsm.good_squats = getattr(fsm, 'good_squats', 0) + 1
                                elif _cls == "non_standard":
                                    fsm.failed_squats = getattr(fsm, 'failed_squats', 0) + 1
                                    try:
                                        fsm.trigger_buzzer_alert(kind="不标准")
                                    except Exception:
                                        pass
                                elif _cls == "compensating":
                                    try:
                                        fsm.trigger_buzzer_alert(kind="代偿")
                                    except Exception:
                                        pass
                                    # 防重复累加
                                    if _cur_reps != fsm._compensation_last_rep:
                                        fsm._compensation_count += 1
                                        fsm._compensation_last_rep = _cur_reps
                                        logging.info(f"📊 [M8] 代偿计数 -> {fsm._compensation_count} (rep={_cur_reps})")
                            except Exception as _e:
                                logging.debug(f"[GRU] infer error: {_e}")

                    fsm.sync_to_frontend(angle, nn_result=nn_result)
                # =================================================

            except (FileNotFoundError, json.JSONDecodeError):
                pass
                
            # 动作类型热切换 (前端 user_profile + 语音 exercise_mode)
            try:
                target_exercise = None
                # 语音指令信号文件 (优先)
                if os.path.exists("/dev/shm/exercise_mode.json"):
                    with open("/dev/shm/exercise_mode.json", "r", encoding="utf-8") as ef:
                        em_data = json.load(ef)
                        mode = em_data.get("mode", "")
                        if mode == "squat":
                            target_exercise = "squat"
                        elif mode == "curl":
                            target_exercise = "bicep_curl"
                    os.remove("/dev/shm/exercise_mode.json")
                # 前端 user_profile
                if target_exercise is None and os.path.exists("/dev/shm/user_profile.json"):
                    with open("/dev/shm/user_profile.json", "r", encoding="utf-8") as uf:
                        p_data = json.load(uf)
                        target_exercise = p_data.get("exercise", "squat")
                if target_exercise and target_exercise != current_exercise:
                    logging.info(f"🔄 动作模式切换: {current_exercise} -> {target_exercise}")
                    current_exercise = target_exercise
                    if current_exercise == "bicep_curl":
                        fsm = DumbbellCurlFSM()
                    else:
                        fsm = SquatStateMachine()
                    # 重载对应 exercise 的 GRU 权重 + 清滑窗, 防首个 rep 串入上一 exercise 的特征
                    # (global 声明已移至 main() 顶部)
                    _GRU_MODEL = _load_gru_model(current_exercise)
                    _gru_feature_buf.clear()
                    fsm.sync_to_frontend()
            except Exception:
                pass

            # 语音疲劳上限调整
            try:
                if os.path.exists("/dev/shm/fatigue_limit.json"):
                    with open("/dev/shm/fatigue_limit.json", "r", encoding="utf-8") as fl:
                        fl_data = json.load(fl)
                        new_limit = fl_data.get("limit", 1500)
                        logging.info(f"🎯 收到语音指令：疲劳上限改为 {new_limit}")
                        # 更新全局疲劳阈值 (在 DeepSeek trigger 判定中使用)
                        _fatigue_limit[0] = new_limit
                    os.remove("/dev/shm/fatigue_limit.json")
            except Exception:
                pass

            # V6.1: \u628a\u6a21\u5f0f+\u9608\u503c\u5199\u5165 fsm_state.json \u8865\u5145\u5b57\u6bb5 (\u4f9b voice_daemon/UI \u786e\u8ba4)
            try:
                # V7.11 \u8de8\u7ec4\u603b\u8ba1 (\u5f53\u524d\u603b\u8ba1 + \u672c\u7ec4\u5b9e\u65f6): UI \u5e95\u90e8\u72b6\u6001\u680f\u5c55\u793a
                _cur_comp = getattr(fsm, "_compensation_count", 0)
                _ext = {
                    "fatigue_limit": int(_fatigue_limit[0]),
                    "inference_mode": (os.path.exists("/dev/shm/inference_mode.json")
                                       and json.load(open("/dev/shm/inference_mode.json")).get("mode")
                                       or _last_applied_modes.get("inference", "pure_vision")),
                    "exercise": current_exercise,
                    "total_good": _session_totals["good"] + fsm.good_squats,
                    "total_failed": _session_totals["failed"] + fsm.failed_squats,
                    "total_comp": _session_totals["comp"] + _cur_comp,
                }
                _last_applied_modes["inference"] = _ext["inference_mode"]
                if os.path.exists("/dev/shm/fsm_state.json"):
                    try:
                        with open("/dev/shm/fsm_state.json", "r", encoding="utf-8") as _fs:
                            _cur = json.load(_fs)
                        _cur.update(_ext)
                        with open("/dev/shm/fsm_state.json.tmp", "w", encoding="utf-8") as _rf:
                            json.dump(_cur, _rf)
                        os.rename("/dev/shm/fsm_state.json.tmp", "/dev/shm/fsm_state.json")
                    except Exception:
                        pass
            except Exception:
                pass

            # V4.8: 疲劳满自动清零 (UI 或语音触发)
            try:
                if os.path.exists("/dev/shm/fatigue_reset.request"):
                    os.remove("/dev/shm/fatigue_reset.request")
                    fsm.total_fatigue_volume = 0.0
                    logging.info("疲劳积分已清零 (触发源: UI/语音 fatigue_reset.request)")
            except Exception:
                pass

            # V7.10 \u201c\u4e0b\u4e00\u7ec4\u201d: \u603b\u7ed3\u540e\u4fdd\u7559\u6570\u636e, \u8bed\u97f3\u8bf4\u201c\u4e0b\u4e00\u7ec4\u201d\u624d\u91cd\u7f6e
            try:
                if os.path.exists("/dev/shm/next_set.request"):
                    os.remove("/dev/shm/next_set.request")
                    # V7.11: \u672c\u7ec4\u6570\u636e merge \u5230\u8de8\u7ec4\u603b\u8ba1 (\u4f9b\u5e95\u90e8\u72b6\u6001\u680f + OpenClaw \u62c9\u53d6)
                    _session_totals["good"] += fsm.good_squats
                    _session_totals["failed"] += fsm.failed_squats
                    _session_totals["comp"] += getattr(fsm, "_compensation_count", 0)
                    logging.info(f"\u2728 \u4e0b\u4e00\u7ec4 \u2014 \u672c\u7ec4: \u6807\u51c6{fsm.good_squats} \u8fdd\u89c4{fsm.failed_squats} | \u603b\u8ba1 {_session_totals}")
                    if current_exercise == "bicep_curl":
                        fsm = DumbbellCurlFSM()
                    else:
                        fsm = SquatStateMachine()
                    fsm.sync_to_frontend()
                    _ds_lock[0] = False
                    _this_set_triggered[0] = False
            except Exception as _e:
                logging.warning(f"[next_set] \u91cd\u7f6e\u5931\u8d25: {_e}")

            # 前端重置信号
            if os.path.exists("/dev/shm/fsm_reset_signal"):
                try: os.remove("/dev/shm/fsm_reset_signal")
                except OSError: pass
                # Sprint5: 结算上个 session + 开启新 session
                try:
                    _d = _db()
                    if _d is not None:
                        _d.end_session(_DB_SESSION[0], fsm.good_squats, fsm.failed_squats, fsm.total_fatigue_volume)
                        _DB_SESSION[0] = _d.start_session(current_exercise)
                except Exception as _e: logging.warning("[DB] reset cycle: %s", _e)
                # 最残暴的重置：直接将整个 FSM 脑叶切除再造，一劳永逸
                if current_exercise == "bicep_curl":
                    fsm = DumbbellCurlFSM()
                else:
                    fsm = SquatStateMachine()
                fsm.sync_to_frontend()
                
                try: os.remove("/dev/shm/llm_reply.txt")
                except OSError: pass
                logging.info("🔄 收到前端重置信号，全轨数据已强制初始化零！")
                continue

            # DeepSeek trigger: manual OR fatigue auto-trigger at limit
            manual_trigger = os.path.exists("/dev/shm/trigger_deepseek")
            # V7.11: \u6bcf\u7ec4\u53ea\u89e6\u53d1\u4e00\u6b21 (_this_set_triggered \u7531"\u4e0b\u4e00\u7ec4"\u91cd\u7f6e)
            fatigue_trigger = (fsm.total_fatigue_volume >= _fatigue_limit[0]
                               and not _ds_lock[0]
                               and not _this_set_triggered[0])

            # Cooldown: prevent rapid-fire auto-triggers after reset
            if fatigue_trigger and (time.time() - _last_deepseek_time) < 30:
                fatigue_trigger = False

            if manual_trigger:
                try: os.remove("/dev/shm/trigger_deepseek")
                except OSError: pass

            has_data = fsm.good_squats > 0 or fsm.failed_squats > 0
            is_chatting = os.path.exists("/dev/shm/chat_active")
            # V7.2: 静音态下不触发 FSM 自动 DeepSeek (防止 UI 出现意外推送)
            is_muted = False
            try:
                if os.path.exists("/dev/shm/mute_signal.json"):
                    with open("/dev/shm/mute_signal.json", "r") as _mf:
                        is_muted = bool(json.load(_mf).get("muted", False))
            except Exception:
                pass

            should_trigger = (manual_trigger or fatigue_trigger) and has_data and not _ds_lock[0] and not is_chatting and not is_muted

            if should_trigger:
                # V7.11: \u4e0d\u518d\u6e05\u96f6, \u8ba9\u75b2\u52b3\u503c\u6ea2\u51fa\u7ee7\u7eed\u7d2f\u52a0 (\u7528\u6237\u53ef\u89c1 UI \u4e00\u76f4\u589e)
                # _this_set_triggered \u91cd\u590d\u89e6\u53d1\u9632\u62a4, "\u4e0b\u4e00\u7ec4" \u624d\u91cd\u7f6e
                _ds_lock[0] = True
                if fatigue_trigger:
                    _this_set_triggered[0] = True  # V7.11: \u6bcf\u7ec4\u89e6\u53d1\u4e00\u6b21\u540e\u952e\u4e0a\u9501, "\u4e0b\u4e00\u7ec4"\u89e3\u9501
                _last_deepseek_time = time.time()
                good_count = fsm.good_squats
                failed_count = fsm.failed_squats
                reason = "疲劳满值自动" if fatigue_trigger else "手动按键"
                logging.info(f"⏳ 触发大模型结组 ({reason}) - (标准:{good_count} 违规:{failed_count})")

                if connected:
                    # V7.10 + M8 (V7.14): prompt 必含代偿维度, 并区分三类反馈
                    #  (a) 全标准 -> 鼓励
                    #  (b) 有违规半蹲 -> 指出幅度问题
                    #  (c) 有代偿 -> 指出代偿问题 (腰背/膝内扣等)
                    # 三类同时出现: 综合点评
                    comp_count = getattr(fsm, '_compensation_count', 0)
                    is_perfect = failed_count == 0 and comp_count == 0
                    # 统一 data_line 格式, 代偿始终写入 (即使为 0, 也让教练能表扬"无代偿")
                    data_line = f"标准{good_count}次，不标准{failed_count}次，代偿{comp_count}次"
                    if is_perfect:
                        feedback_line = "给出正向鼓励继续保持"
                    elif comp_count > 0 and failed_count == 0:
                        feedback_line = "重点提醒代偿问题，注意腰背和膝盖姿态"
                    elif comp_count == 0 and failed_count > 0:
                        feedback_line = "提醒下蹲幅度不够，下次蹲到位"
                    else:
                        feedback_line = "同时指出幅度不够和代偿问题，下次更认真"
                    short_prompt = (
                        f"你是健身教练。本组{data_line}。"
                        f"{feedback_line}。"
                        f"要求: 25字内, 一段话, 无标点花哨, 不提疲劳分数。"
                    )
                    if bridge is not None:
                        asyncio.create_task(_ds_wrapper(bridge, short_prompt, good_count, failed_count, reason))
                    # V7.10: 组间休息机制 — 总结后保留数据, 等用户说"下一组"才重置
                    # 通过 /dev/shm/next_set.request 信号触发 (语音"下一组"硬编码写入)

            # ===== 语音对话轮询 =====
            if connected and bridge is not None and not _chat_lock[0] and os.path.exists("/dev/shm/chat_input.txt"):
                try:
                    mtime = os.path.getmtime("/dev/shm/chat_input.txt")
                    if mtime != _chat_mtime[0]:
                        _chat_mtime[0] = mtime
                        with open("/dev/shm/chat_input.txt", "r", encoding="utf-8") as cf:
                            user_text = cf.read().strip()
                        if user_text:
                            _chat_lock[0] = True
                            asyncio.create_task(_chat_handler(bridge, user_text))
                except Exception as e:
                    pass

    except KeyboardInterrupt:
        logging.info("手动中断")
    finally:
        # Sprint5: 主循环退出时结算当前 session
        try:
            _d = _db()
            if _d is not None and _DB_SESSION[0] is not None:
                _d.end_session(
                    _DB_SESSION[0],
                    getattr(fsm, 'good_squats', 0),
                    getattr(fsm, 'failed_squats', 0),
                    getattr(fsm, 'total_fatigue_volume', 0.0),
                )
                logging.info("[DB] 主循环退出，session=%s 已结算", _DB_SESSION[0])
        except Exception as _e:
            logging.warning("[DB] 退出 end_session 失败: %s", _e)
        logging.info("🧹 退出 Main Loop。")

if __name__ == "__main__":
    asyncio.run(main())
