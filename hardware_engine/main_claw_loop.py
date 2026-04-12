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
from ai_sensory.asr_worker import ASRWorker
from sensor.microphone import MicrophoneController
from cognitive.fusion_model import CompensationGRU, load_model, _compute_derived_features

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [MAIN LOOP] - %(message)s')

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
        if now - self._last_buzzer_time < 0.5:
            return
        self._last_buzzer_time = now
        # [紧急禁音] 屏蔽物理音箱调用，防止自习室爆鸣
        # os.system("bash /home/toybrick/hardware_engine/peripherals/buzzer_alert.sh &")
        logging.warning("🔊 音箱警报！动作判定违规！(已触发静音保护不发声)")

    def _read_emg(self):
        try:
            with open("/dev/shm/muscle_activation.json", "r") as f:
                d = json.load(f)
                return d.get("activations", {})
        except Exception:
            return {}

    def sync_to_frontend(self, current_angle=180.0, nn_result=None):
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
                # 暴露给前端做渲染
                "emg_activations": [
                    emg_feats.get("quadriceps", 0),
                    emg_feats.get("glutes", 0),
                    emg_feats.get("calves", 0),
                    emg_feats.get("biceps", 0)
                ]
            }
            # NN 推理结果 (由 Agent 3 注入)
            if nn_result:
                state_data["similarity"]     = nn_result.get("similarity", 0.0)
                state_data["classification"] = nn_result.get("classification", "unknown")
                state_data["nn_confidence"]  = nn_result.get("confidence", 0.0)
                state_data["nn_phase"]       = nn_result.get("phase", "unknown")

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
            if obj.get("score", 0) < 0.15:
                self.state = "NO_PERSON"
                self.sync_to_frontend()
                return None

            kpts = obj.get("kpts", [])
            if len(kpts) < 17:
                return None

            # 智能择优捕获：根据左右腿三关节点的综合置信度，选出朝向摄像头无遮挡的一侧
            l_score = kpts[11][2] + kpts[13][2] + kpts[15][2]
            r_score = kpts[12][2] + kpts[14][2] + kpts[16][2]
            
            if l_score > r_score:
                hip   = [kpts[11][0], kpts[11][1]]
                knee  = [kpts[13][0], kpts[13][1]]
                ankle = [kpts[15][0], kpts[15][1]]
            else:
                hip   = [kpts[12][0], kpts[12][1]]
                knee  = [kpts[14][0], kpts[14][1]]
                ankle = [kpts[16][0], kpts[16][1]]
            raw_angle = self.calculate_angle(hip, knee, ankle)

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
        if now - self._last_buzzer_time < 0.5:
            return
        self._last_buzzer_time = now
        logging.warning("🔊 音箱警报！动作判定违规！(已触发静音保护)")

    def _read_emg(self):
        try:
            with open("/dev/shm/muscle_activation.json", "r") as f:
                d = json.load(f)
                return d.get("activations", {})
        except Exception:
            return {}

    def sync_to_frontend(self, current_angle=180.0, nn_result=None):
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
            if nn_result:
                state_data["similarity"]     = nn_result.get("similarity", 0.0)
                state_data["classification"] = nn_result.get("classification", "unknown")
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
            if obj.get("score", 0) < 0.15:
                self.state = "NO_PERSON"
                self.sync_to_frontend()
                return None

            kpts = obj.get("kpts", [])
            if len(kpts) < 17:
                return None

            l_score = kpts[5][2] + kpts[7][2] + kpts[9][2]
            r_score = kpts[6][2] + kpts[8][2] + kpts[10][2]
            
            if l_score > r_score:
                shoulder = [kpts[5][0], kpts[5][1]]
                elbow    = [kpts[7][0], kpts[7][1]]
                wrist    = [kpts[9][0], kpts[9][1]]
            else:
                shoulder = [kpts[6][0], kpts[6][1]]
                elbow    = [kpts[8][0], kpts[8][1]]
                wrist    = [kpts[10][0], kpts[10][1]]
                
            raw_angle = self.calculate_angle(shoulder, elbow, wrist)

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

            try:
                feishu_msg = f"🏋️ IronBuddy 训练速报\n✅ 标准: {good_count}  ⚠️ 违规: {failed_count}\n💬 {reply}"
                await bridge.deliver(feishu_msg, channel="feishu")
            except Exception as e:
                logging.error(f"飞书推送失败: {e}")
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

    gateway_url = os.environ.get("OPENCLAW_URL", "ws://127.0.0.1:18789")
    bridge = OpenClawBridge(gateway_url=gateway_url)
    connected = await bridge.connect()
    
    current_exercise = "squat"
    fsm = SquatStateMachine()
    _last_deepseek_time = time.time()
    _ds_lock = [False]

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
                    if len(_gru_feature_buf) > _GRU_WINDOW_SIZE:
                        _gru_feature_buf.pop(0)

                    _gru_frame_ctr += 1
                    nn_result = None
                    if (
                        _GRU_MODEL is not None
                        and len(_gru_feature_buf) >= _GRU_WINDOW_SIZE
                        and _gru_frame_ctr % _GRU_INFER_EVERY == 0
                    ):
                        try:
                            window = np.array(_gru_feature_buf[-_GRU_WINDOW_SIZE:],
                                              dtype=np.float32)
                            # normalise in-place (same as SquatDataset)
                            window[:, 1] /= 180.0   # Angle
                            window[:, 3] /= 100.0   # Target_RMS
                            window[:, 4] /= 100.0   # Comp_RMS
                            window[:, 2]  = np.clip(window[:, 2] / 10.0, -1.0, 1.0)
                            nn_result = _GRU_MODEL.infer(window)
                        except Exception as _e:
                            logging.debug(f"[GRU] infer error: {_e}")

                    # 将 NN 结果附加到 FSM 状态写入前端
                    fsm.sync_to_frontend(angle, nn_result=nn_result)
                # =================================================

            except (FileNotFoundError, json.JSONDecodeError):
                pass
                
            # 动作类型热切换
            try:
                if os.path.exists("/dev/shm/user_profile.json"):
                    with open("/dev/shm/user_profile.json", "r", encoding="utf-8") as uf:
                        p_data = json.load(uf)
                        exercise = p_data.get("exercise", "squat")
                        if exercise != current_exercise:
                            logging.info(f"🔄 动作模式切换: {current_exercise} -> {exercise}")
                            current_exercise = exercise
                            if exercise == "bicep_curl":
                                fsm = DumbbellCurlFSM()
                            else:
                                fsm = SquatStateMachine()
                            fsm.sync_to_frontend()
            except Exception:
                pass

            # 前端重置信号
            if os.path.exists("/dev/shm/fsm_reset_signal"):
                try: os.remove("/dev/shm/fsm_reset_signal")
                except OSError: pass
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

            # DeepSeek trigger: manual OR fatigue auto-trigger at 1500
            manual_trigger = os.path.exists("/dev/shm/trigger_deepseek")
            fatigue_trigger = fsm.total_fatigue_volume >= 1500 and not _ds_lock[0]

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
                    fatigue_str = f"1500满分！" if current_fatigue >= 1490 else f"{current_fatigue:.1f}/1500"
                    
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
                    asyncio.create_task(_ds_wrapper(bridge, short_prompt, good_count, failed_count))

                    # Auto-reset FSM after fatigue trigger
                    if fatigue_trigger:
                        logging.info("🔄 疲劳满值自动重置！")
                        if current_exercise == "bicep_curl":
                            fsm = DumbbellCurlFSM()
                        else:
                            fsm = SquatStateMachine()
                        fsm.sync_to_frontend()

            # ===== 语音对话轮询 =====
            if connected and not _chat_lock[0] and os.path.exists("/dev/shm/chat_input.txt"):
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
