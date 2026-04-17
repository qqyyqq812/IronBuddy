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

def _load_gru_model():
    """尝试加载 extreme_fusion_gru.pt，支持 7D 和旧 4D 模型。"""
    _dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(_dir, "extreme_fusion_gru.pt"),
        os.path.join(_dir, "cognitive", "extreme_fusion_gru.pt"),
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                model = load_model(path, input_size=7)
                size_kb = os.path.getsize(path) / 1024
                logging.info(f"[GRU] Loaded {path} ({size_kb:.1f} KB)")
                return model
            except Exception as e:
                logging.warning(f"[GRU] load_model failed for {path}: {e}")
                # Fallback: try 4D model
                try:
                    model = load_model(path, input_size=4)
                    logging.info(f"[GRU] Loaded 4D-compat model from {path}")
                    return model
                except Exception as e2:
                    logging.warning(f"[GRU] 4D fallback also failed: {e2}")
    logging.warning("[GRU] No model file found — inference disabled.")
    return None

_GRU_MODEL = _load_gru_model()

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
    ANGLE_STANDARD = 110    # 调整：放宽深蹲判定阀值，低于 110° 即判定为合格
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

    def trigger_buzzer_alert(self):
        now = time.time()
        if now - self._last_buzzer_time < 3.0:
            return
        self._last_buzzer_time = now
        # 通过信号文件通知 voice_daemon 播报违规
        try:
            tmp = "/dev/shm/violation_alert.txt.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("不标准")
            os.rename(tmp, "/dev/shm/violation_alert.txt")
            logging.warning("🔊 违规警报已发送到 voice_daemon")
        except Exception as e:
            logging.error("违规警报写入失败: %s", e)

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

            # ===== 状态流转 (绝对阈值迟滞法) =====
            if self.state in ["NO_PERSON", "IDLE", "ASCENDING", "STAND"]:
                # 只有大角度稳定跌破 140°，才被认为是真正地开始了下蹲流程
                if angle < 140:
                    self.state = "DESCENDING"
                    self._min_angle_in_rep = angle
                    self.last_active_time = time.time()
                else:
                    self.state = "STAND"
                    
            elif self.state == "DESCENDING":
                self.last_active_time = time.time()
                self._min_angle_in_rep = min(self._min_angle_in_rep, angle)
                
                # 只要起身到 145，就立刻结账，不必等完全 150，这极大防止了计数丢失
                if angle > 145:
                    bottom = self._min_angle_in_rep
                    
                    if bottom < self.ANGLE_STANDARD:
                        # 完美深蹲
                        self.good_squats += 1
                        volume = 1500.0 / 7.0  # 调整：7个动作即刻满级！
                        self.total_fatigue_volume += volume
                        logging.info(f"🟢 好球！（角度{bottom:.0f}°）当前总疲劳值: {self.total_fatigue_volume:.1f}/1500")
                    else:
                        # 行程不足
                        self.failed_squats += 1
                        self.trigger_buzzer_alert()
                        logging.warning(f"🟡 违规：下蹲幅度不足！（当前最低{bottom:.0f}°）累计违规：{self.failed_squats}")

                    # 结算完毕，复位回归直立监控区
                    self.state = "STAND"
                    self._min_angle_in_rep = 999
                    self._last_count_time = time.time()

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

    def trigger_buzzer_alert(self):
        now = time.time()
        if now - self._last_buzzer_time < 3.0:
            return
        self._last_buzzer_time = now
        try:
            tmp = "/dev/shm/violation_alert.txt.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("不标准")
            os.rename(tmp, "/dev/shm/violation_alert.txt")
            logging.warning("🔊 弯举违规警报已发送到 voice_daemon")
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

            if self.state in ["NO_PERSON", "IDLE", "STAND", "EXTENDING"]:
                if angle < 140:
                    self.state = "CURLING"
                    self._min_angle_in_rep = angle
                    self.last_active_time = time.time()
                else:
                    self.state = "STAND"
                    
            elif self.state == "CURLING":
                self.last_active_time = time.time()
                self._min_angle_in_rep = min(self._min_angle_in_rep, angle)
                
                if angle > 145:
                    bottom = self._min_angle_in_rep
                    if bottom < self.ANGLE_STANDARD:
                        self._good_reps += 1
                        volume = 1500.0 / 7.0 
                        self.total_fatigue_volume += volume
                        logging.info(f"🟢 弯举达标！（顶峰角度{bottom:.0f}°）总疲劳值: {self.total_fatigue_volume:.1f}/1500")
                    else:
                        self._failed_reps += 1
                        self.trigger_buzzer_alert()
                        logging.warning(f"🟡 弯举违规：收缩幅度不足！（收缩极限仅有{bottom:.0f}°）累计违规：{self._failed_reps}")

                    self.state = "STAND"
                    self._min_angle_in_rep = 999
                    self._last_count_time = time.time()

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
            reply = await bridge.ask(prompt, timeout=60)
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
            except Exception as e:
                logging.error(f"下发回复至内存盘失败: {e}")

            # 飞书推送已改为手动/语音触发，不再自动推送每次训练点评
            return
        except Exception as e:
            logging.error(f"❌ [后台] 尝试 {attempt+1} 异常: {e}")
            if attempt < 2:
                await asyncio.sleep(3)
    logging.error("❌ [后台] DeepSeek 3 次重试全部失败")


async def main():
    logging.info("🚀 启动 IronBuddy V3 双轨融合状态机中枢...")

    for f in ["/dev/shm/llm_reply.txt", "/dev/shm/chat_input.txt", "/dev/shm/chat_reply.txt"]:
        try:
            os.remove(f)
        except OSError:
            pass

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
    fsm = SquatStateMachine()
    # Sprint5: 开启首个训练 session
    try:
        _d = _db()
        if _d is not None: _DB_SESSION[0] = _d.start_session(current_exercise)
    except Exception as _e: logging.warning("[DB] session init skipped: %s", _e)
    _last_deepseek_time = time.time()
    _ds_lock = [False]
    _fatigue_limit = [1500]  # 可通过语音调整

    async def _ds_wrapper(b, p, g, f):
        try:
            await _deepseek_fire_and_forget(b, p, g, f)
        finally:
            _ds_lock[0] = False

    _chat_lock = [False]
    _chat_mtime = [0]

    async def _chat_handler(bridge_ref, user_text):
        try:
            user_text = user_text.strip()
            if not user_text or len(user_text) < 2: return
            logging.info(f"🎤 [对话] 收到用户消息: {user_text}")
            
            prompt = (
                f"{_SOUL_TEXT[:500] + chr(10) + chr(10) if _SOUL_TEXT else ''}"
                f"当前状态：用户刚才做了 {fsm.good_squats} 个标准深蹲，{fsm.failed_squats} 个半蹲（违规）。 "
                f"当前积累的疲劳值为 {fsm.total_fatigue_volume:.1f} / 1500。"
                f"请你结合该数据直接回答用户的问题。 "
                f"口语化、简短直接（50字以内）。绝对不要包含任何你的内心思考过程（如<think>标签），直接给我最终的话语。"
                f"用户说：{user_text}"
            )
            reply = await bridge_ref.ask(prompt, timeout=60)
            if "</think>" in reply:
                reply = reply.split("</think>")[-1].strip()
            logging.info(f"💬 [对话] DeepSeek 回复: {reply}")
            with open("/dev/shm/chat_reply.txt.tmp", "w", encoding="utf-8") as rf:
                rf.write(reply)
            os.rename("/dev/shm/chat_reply.txt.tmp", "/dev/shm/chat_reply.txt")
        except Exception as e:
            pass
        finally:
            _chat_lock[0] = False

    try:
        while True:
            await asyncio.sleep(0.05)

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
                            target_emg = m_data.get("activations", {}).get("glutes", 0.0)
                            comp_emg = m_data.get("activations", {}).get("biceps", 0.0)
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
                    # 检测 rep 计数是否变化（good 或 failed 增加了）
                    _cur_reps = getattr(fsm, 'good_squats', 0) + getattr(fsm, 'failed_squats', 0) + \
                                getattr(fsm, '_good_reps', 0) + getattr(fsm, '_failed_reps', 0)
                    if not hasattr(fsm, '_prev_total_reps'):
                        fsm._prev_total_reps = _cur_reps

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
                        fsm._prev_total_reps = _cur_reps  # 保证无 GRU 时也能推进

                    nn_result = None
                    if _cur_reps > fsm._prev_total_reps and _GRU_MODEL is not None and _inference_mode == "vision_sensor":
                        # 一个动作刚完成！用积累的数据推理
                        fsm._prev_total_reps = _cur_reps
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
                                logging.info(f"🧠 [GRU] 第{_cur_reps}个动作判定: "
                                             f"相似度={nn_result['similarity']:.3f} "
                                             f"分类={cls_cn.get(nn_result['classification'], nn_result['classification'])} "
                                             f"置信度={nn_result['confidence']:.3f}")
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
            fatigue_trigger = fsm.total_fatigue_volume >= _fatigue_limit[0] and not _ds_lock[0]

            # Cooldown: prevent rapid-fire auto-triggers after reset
            if fatigue_trigger and (time.time() - _last_deepseek_time) < 30:
                fatigue_trigger = False

            if manual_trigger:
                try: os.remove("/dev/shm/trigger_deepseek")
                except OSError: pass

            has_data = fsm.good_squats > 0 or fsm.failed_squats > 0
            is_chatting = os.path.exists("/dev/shm/chat_active")

            should_trigger = (manual_trigger or fatigue_trigger) and has_data and not _ds_lock[0] and not is_chatting

            if should_trigger:
                _ds_lock[0] = True
                _last_deepseek_time = time.time()
                good_count = fsm.good_squats
                failed_count = fsm.failed_squats
                reason = "疲劳满值自动" if fatigue_trigger else "手动按键"
                logging.info(f"⏳ 触发大模型结组 ({reason}) - (标准:{good_count} 违规:{failed_count})")

                if connected:
                    rate_pct = round(good_count / (good_count + failed_count) * 100) if (good_count+failed_count) > 0 else 0
                    current_fatigue = fsm.total_fatigue_volume
                    fl = _fatigue_limit[0]
                    fatigue_str = f"{fl}满分！" if current_fatigue >= fl - 10 else f"{current_fatigue:.1f}/{fl}"
                    
                    short_prompt = (
                        f"你是 IronBuddy 健身教练。\n"
                        f"本组运动真实数据：\n"
                        f"- 当前肌肉疲劳累计达到：{fatigue_str}\n"
                        f"- 标准深蹲: {good_count} 次，违规半蹲: {failed_count} 次（合格率 {rate_pct}%）\n"
                        f"要求：1.纯一段文字，禁止换行/Markdown/Emoji。\n"
                        f"2.若疲劳值满了就疯狂赞美；若疲劳值只有几百（比如做了几下就放弃），要无情嘲讽他半途而废。\n"
                        f"3.毒舌口语化，不要像机器人。控制在50字左右。\n"
                        f"4.绝对不要产生任何思维链推导（不要用<think>），第一句话直接给我最终的点评输出！"
                    )
                    if bridge is not None:
                        asyncio.create_task(_ds_wrapper(bridge, short_prompt, good_count, failed_count))
                        try:
                            _d = _db()
                            if _d is not None: _d.log_llm(reason, short_prompt, "", 0, 0)
                        except Exception as _e: logging.warning("[DB] log_llm: %s", _e)

                    # Auto-reset FSM after fatigue trigger
                    if fatigue_trigger:
                        logging.info("🔄 疲劳满值自动重置！")
                        if current_exercise == "bicep_curl":
                            fsm = DumbbellCurlFSM()
                        else:
                            fsm = SquatStateMachine()
                        fsm.sync_to_frontend()

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
        logging.info("🧹 退出 Main Loop。")

if __name__ == "__main__":
    asyncio.run(main())
