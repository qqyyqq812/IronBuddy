import asyncio
import os
import sys
import json
import time
import math
import logging
import numpy as np
from cognitive.openclaw_bridge import OpenClawBridge
from ai_sensory.asr_worker import ASRWorker
from sensor.microphone import MicrophoneController

# V2: 生物力学引擎
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from biomechanics.lifting_3d import Lifting3D
    from biomechanics.joint_calculator import JointCalculator
    from biomechanics.muscle_model import MuscleModel
    _V2_AVAILABLE = True
except ImportError as e:
    _V2_AVAILABLE = False
    logging.warning(f"V2 生物力学引擎未加载: {e}")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [MAIN LOOP] - %(message)s')

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
    """趋势检测状态机 — 基于角度变化趋势识别深蹲

    核心思路：
        不看绝对角度阈值来判定"站直/下蹲"，而是检测角度的
        **下降趋势** 和 **上升趋势**，当从下降转为上升时
        （转折点 = 蹲到底），根据最低点角度判定合格/违规。

    状态：
        NO_PERSON       — 画面中没人
        IDLE            — 有人但角度稳定（不在做深蹲）
        DESCENDING      — 角度在快速下降（正在蹲）
        ASCENDING       — 角度在快速上升（正在起）

    判定规则：
        ✅ 标准深蹲：下降 → 最低点 < 90° → 上升
        ❌ 违规半蹲：下降 → 最低点 ≥ 90° → 上升
        🛑 静止/IDLE：角度波动 < 15° 持续一段时间

    DeepSeek 触发：仅手动按键或语音对话触发
    """
    ANGLE_STANDARD = 90    # 低于 90° = 标准深蹲，否则违规半蹲
    TREND_WINDOW = 8       # 趋势检测滑窗大小
    IDLE_RANGE = 20        # 角度波动小于此值 = 静止
    IDLE_FRAMES = 25       # 连续多少帧稳定才切入 IDLE（~3s）

    def __init__(self):
        self.state = "NO_PERSON"
        self.good_squats = 0
        self.failed_squats = 0
        self.last_active_time = time.time()
        self._last_buzzer_time = 0
        self._angle_history = []       # 原始角度历史（滑窗）
        self._min_angle_in_rep = 999   # 当前这次动作中的最低角度
        self._idle_counter = 0         # 连续稳定帧计数器
        self._last_count_time = 0      # 计数冷却期锚点
        self._last_rep_good = None     # V2.1: 最近一次动作是否合格（供累积引擎消费）

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
        if now - self._last_buzzer_time < 2.0:
            return
        self._last_buzzer_time = now
        os.system("bash /home/toybrick/hardware_engine/peripherals/buzzer_alert.sh &")
        logging.warning("🔊 音箱警报！深蹲不达标！")

    def sync_to_frontend(self, current_angle=180.0):
        try:
            state_data = {
                "state": self.state,
                "good": self.good_squats,
                "failed": self.failed_squats,
                "angle": round(current_angle, 1),
                "chat_active": os.path.exists("/dev/shm/chat_active")
            }
            with open("/dev/shm/fsm_state.json.tmp", "w", encoding="utf-8") as rf:
                json.dump(state_data, rf)
            os.rename("/dev/shm/fsm_state.json.tmp", "/dev/shm/fsm_state.json")
        except Exception:
            pass

    def _get_trend(self):
        """计算角度趋势：返回 'falling' / 'rising' / 'stable'"""
        if len(self._angle_history) < 6:
            return "stable"
        recent = self._angle_history[-6:]
        deltas = [recent[i+1] - recent[i] for i in range(len(recent)-1)]
        avg_delta = sum(deltas) / len(deltas)
        if avg_delta < -2.5:      # 降低趋势阈值，适应慢速动作
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
            if obj.get("score", 0) < 0.6:
                self.state = "NO_PERSON"
                self.sync_to_frontend()
                return None

            kpts = obj.get("kpts", [])
            if len(kpts) < 17:
                return None

            # V2: 提取全部 17 个 2D 关键点供 3D lifting
            self._last_kpts_2d = np.array([[kp[0], kp[1]] for kp in kpts[:17]], dtype=np.float32)

            hip   = [kpts[11][0], kpts[11][1]]
            knee  = [kpts[13][0], kpts[13][1]]
            ankle = [kpts[15][0], kpts[15][1]]
            raw_angle = self.calculate_angle(hip, knee, ankle)

            # 滑窗平滑（5 帧均值消抖）
            self._angle_history.append(raw_angle)
            if len(self._angle_history) > 16:
                self._angle_history.pop(0)
            smooth_n = min(5, len(self._angle_history))
            angle = sum(self._angle_history[-smooth_n:]) / smooth_n

            # 趋势检测
            trend = self._get_trend()

            # 稳定性检测：最近 N 帧的角度波动范围
            if len(self._angle_history) >= 4:
                recent_range = max(self._angle_history[-4:]) - min(self._angle_history[-4:])
                if recent_range < self.IDLE_RANGE:
                    self._idle_counter += 1
                else:
                    self._idle_counter = 0
            else:
                self._idle_counter = 0

            is_stable = self._idle_counter >= self.IDLE_FRAMES

            # ===== 状态流转 =====
            if self.state == "NO_PERSON":
                if is_stable:
                    self.state = "IDLE"
                elif trend == "falling":
                    self.state = "DESCENDING"
                    self._min_angle_in_rep = angle
                    self.last_active_time = time.time()
                else:
                    self.state = "IDLE"

            elif self.state == "IDLE":
                if trend == "falling":
                    self.state = "DESCENDING"
                    self._min_angle_in_rep = angle
                    self.last_active_time = time.time()

            elif self.state == "DESCENDING":
                self.last_active_time = time.time()
                self._min_angle_in_rep = min(self._min_angle_in_rep, angle)

                if trend == "rising" and (time.time() - self._last_count_time) > 1.5:
                    # 转折点：从下降转为上升 = 蹲到底了（1.5s 冷却防重计）
                    bottom = self._min_angle_in_rep
                    self._last_count_time = time.time()
                    if bottom < self.ANGLE_STANDARD:
                        self.good_squats += 1
                        self.state = "ASCENDING"
                        self._last_rep_good = True
                        logging.info(f"🔥 标准深蹲！（最低{bottom:.0f}°）有效计数：{self.good_squats}")
                    else:
                        self.failed_squats += 1
                        self.state = "ASCENDING"
                        self._last_rep_good = False
                        self.trigger_buzzer_alert()
                        logging.warning(f"⚠️ 半蹲违规！（最低{bottom:.0f}°≥{self.ANGLE_STANDARD}°）累计违规：{self.failed_squats}")

                elif is_stable:
                    # 下降过程中突然停住 → 不算深蹲，回到 IDLE
                    self.state = "IDLE"

            elif self.state == "ASCENDING":
                self.last_active_time = time.time()

                if trend == "falling":
                    # 又开始下一个深蹲
                    self.state = "DESCENDING"
                    self._min_angle_in_rep = angle
                elif is_stable:
                    # 起身后稳定了 → 回到 IDLE
                    self.state = "IDLE"

            self.sync_to_frontend(angle)
            return angle
        except Exception as e:
            logging.error(f"FSM 异常: {e}")
            self.state = "NO_PERSON"
            self.sync_to_frontend()
            return None


async def _deepseek_fire_and_forget(bridge, prompt, good_count, failed_count):
    """后台独立协程（不阻塞 FSM）：DeepSeek 点评 + 写 llm_reply + 飞书（带 3 次重试）"""
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
    logging.info("🚀 启动端云解耦版 IronBuddy 神经中枢 (V2)...")

    # 启动时清空旧回复
    for f in ["/dev/shm/llm_reply.txt", "/dev/shm/chat_input.txt", "/dev/shm/chat_reply.txt"]:
        try:
            os.remove(f)
        except OSError:
            pass

    gateway_url = os.environ.get("OPENCLAW_URL", "ws://127.0.0.1:18789")
    bridge = OpenClawBridge(gateway_url=gateway_url)
    connected = await bridge.connect()
    if not connected:
        logging.error("❌ 智脑通道断连。系统仅开启边缘局域计步模式！")

    fsm = SquatStateMachine()
    _last_deepseek_time = time.time()
    _ds_lock = [False]
    _last_summarized_state = (-1, -1)

    # V2: 生物力学引擎初始化
    _lifter = None
    _joint_calc = None
    _muscle_model = None
    _v2_frame_counter = 0
    _v2_update_interval = 5  # 每 5 帧跑一次 3D lifting (约 3Hz @15fps)
    if _V2_AVAILABLE:
        try:
            onnx_path = "/home/toybrick/biomechanics/checkpoints/videopose3d_243f_causal.onnx"
            data_dir = "/home/toybrick/biomechanics"
            _lifter = Lifting3D(onnx_path)
            _joint_calc = JointCalculator(fps=15.0)
            _muscle_model = MuscleModel(data_dir=data_dir)
            _muscle_model.set_exercise('squat')  # 默认深蹲
            _muscle_model.set_user_params(height_cm=175, weight_kg=70)
            logging.info("✅ V2 生物力学引擎初始化完成")
        except Exception as e:
            logging.error(f"V2 初始化失败, 降级为 V1 模式: {e}")
            _lifter = None

    async def _ds_wrapper(b, p, g, f):
        try:
            await _deepseek_fire_and_forget(b, p, g, f)
        finally:
            _ds_lock[0] = False

    # ===== 语音对话通道 =====
    _chat_lock = [False]
    _chat_mtime = [0]

    async def _chat_handler(bridge_ref, user_text):
        """语音对话协程（包含演示剧本的 Wizard of Oz 定向拦截）"""
        try:
            user_text = user_text.strip()
            if not user_text or len(user_text) < 2:
                logging.warning(f"丢弃极短或无意义的语音输入: '{user_text}'")
                return
            logging.info(f"🎤 [对话] 收到用户消息: {user_text}")
            
            # --- 剧本特调：拦截飞书发送安排指令 ---
            feishu_kw = ["飞书", "飞出", "飞叔", "非书", "废书"]
            action_kw = ["发", "推送", "计划", "安排", "生成"]
            if any(k in user_text for k in feishu_kw) and any(a in user_text for a in action_kw):
                async def delayed_feishu_push():
                    await asyncio.sleep(8) # 模拟延迟或调度的等待时间
                    plan_prompt = f"用户刚才完成了 {fsm.good_squats} 个标准深蹲和 {fsm.failed_squats} 个半蹲。请以 IronBuddy 的身份，为他生成一份明天的进阶训练安排。内容要专业、有条理，多用emoji，150字左右。"
                    plan_text = await bridge_ref.ask(plan_prompt, timeout=60, generate_new_session=True)
                    if "</think>" in plan_text: plan_text = plan_text.split("</think>")[-1].strip()
                    await bridge_ref.deliver(f"📅 【IronBuddy 定时推送】\n{plan_text}", channel="feishu")
                
                asyncio.create_task(delayed_feishu_push())
                reply = "没问题！我已经为你量身定制好了明天的进阶计划，稍后就会准时推送到你的飞书上，记得看手机注意查收哦！💪"
                logging.info(f"💬 [对话/剧本拦截] 触发飞书计划: {reply}")
                
                try:
                    with open("/dev/shm/chat_reply.txt.tmp", "w", encoding="utf-8") as rf:
                        rf.write(reply)
                    os.rename("/dev/shm/chat_reply.txt.tmp", "/dev/shm/chat_reply.txt")
                except Exception as e:
                    logging.error(f"写入回复失败: {e}")
                return
            # -----------------------------------

            prompt = (
                f"{_SOUL_TEXT[:500] + chr(10) + chr(10) if _SOUL_TEXT else ''}"
                f"当前状态：用户刚才做了 {fsm.good_squats} 个标准深蹲，{fsm.failed_squats} 个半蹲。 "
                f"请你结合该数据直接回答用户的问题。 "
                f"口语化、简短直接（50字以内）。 "
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
            logging.error(f"❌ [对话] 异常: {e}")
            with open("/dev/shm/chat_reply.txt.tmp", "w", encoding="utf-8") as rf:
                rf.write("抱歉，教练暂时无法回复，请稍后再试。")
            os.rename("/dev/shm/chat_reply.txt.tmp", "/dev/shm/chat_reply.txt")
        finally:
            _chat_lock[0] = False

    try:
        while True:
            await asyncio.sleep(0.05)

            try:
                with open("/dev/shm/pose_data.json", "r") as f:
                    pose_data = json.load(f)
                    fsm.update(pose_data)
            except (FileNotFoundError, json.JSONDecodeError):
                pass

            # V2: 生物力学管线 (每 _v2_update_interval 帧执行一次)
            if _lifter and hasattr(fsm, '_last_kpts_2d'):
                _v2_frame_counter += 1
                if _v2_frame_counter >= _v2_update_interval:
                    _v2_frame_counter = 0
                    try:
                        kpts_2d = fsm._last_kpts_2d
                        # 归一化: 像素坐标 → [-0.5, 0.5]
                        kpts_norm = kpts_2d.copy()
                        kpts_norm[:, 0] = kpts_norm[:, 0] / 640 - 0.5
                        kpts_norm[:, 1] = kpts_norm[:, 1] / 480 - 0.5
                        kpts_3d = _lifter.update(kpts_norm)
                        if kpts_3d is not None:
                            joint_data = _joint_calc.compute(kpts_3d)

                            # V2.2: 3D 角度反馈 FSM — 用视角无关的 3D 膝盖角覆盖 2D 投影角
                            angles_3d = joint_data.get('angles', {})
                            l_knee_3d = angles_3d.get('l_knee')
                            r_knee_3d = angles_3d.get('r_knee')
                            if l_knee_3d is not None and r_knee_3d is not None:
                                # 取左右膝较小值（深蹲对称，取最低角更准确）
                                knee_angle_3d = min(l_knee_3d, r_knee_3d)
                                if fsm._angle_history:
                                    fsm._angle_history[-1] = knee_angle_3d
                                fsm.sync_to_frontend(knee_angle_3d)

                            # V2.1: 检查是否有新完成的动作需要累积
                            if hasattr(fsm, '_last_rep_good') and fsm._last_rep_good is not None:
                                _muscle_model.on_rep_completed(fsm._last_rep_good)
                                fsm._last_rep_good = None  # 消费标记
                            muscle_result = _muscle_model.compute(joint_data)
                            # 写入共享内存供前端读取
                            muscle_out = {
                                'activations': muscle_result['activations'],
                                'warnings': muscle_result['warnings'],
                                'exercise': muscle_result['exercise'],
                                'flash': muscle_result.get('flash', []),
                                'rep_count': muscle_result.get('rep_count', 0),
                                'lifting_ms': round(_lifter.avg_inference_ms, 1),
                            }
                            with open('/dev/shm/muscle_activation.json.tmp', 'w') as mf:
                                json.dump(muscle_out, mf)
                            os.rename('/dev/shm/muscle_activation.json.tmp', '/dev/shm/muscle_activation.json')
                    except Exception as e:
                        logging.error(f"V2 生物力学管线异常: {e}")

            # 前端重置信号
            if os.path.exists("/dev/shm/fsm_reset_signal"):
                try:
                    os.remove("/dev/shm/fsm_reset_signal")
                except OSError:
                    pass
                fsm.good_squats = 0
                fsm.failed_squats = 0
                fsm.state = "NO_PERSON"
                fsm._angle_history.clear()
                fsm._min_angle_in_rep = 999
                fsm._idle_counter = 0
                fsm.sync_to_frontend()
                _last_deepseek_time = time.time()
                _last_summarized_state = (-1, -1)
                # V2.1: 重置肌肉累积
                if _muscle_model:
                    _muscle_model.reset_set()
                    # V2.1: 立即写入归零数据到共享内存，让前端即时刷新
                    try:
                        from biomechanics.muscle_model import ALL_MUSCLES
                        zero_out = {
                            'activations': {m: 0 for m in ALL_MUSCLES},
                            'warnings': [], 'exercise': _muscle_model._current_exercise,
                            'flash': [], 'rep_count': 0, 'lifting_ms': 0,
                        }
                        with open('/dev/shm/muscle_activation.json.tmp', 'w') as mf:
                            json.dump(zero_out, mf)
                        os.rename('/dev/shm/muscle_activation.json.tmp', '/dev/shm/muscle_activation.json')
                    except Exception:
                        pass
                try:
                    os.remove("/dev/shm/llm_reply.txt")
                except OSError:
                    pass
                logging.info("🔄 收到前端重置信号，计数+肌肉累积已清零！")
                continue

            # 手动触发 DeepSeek
            manual_trigger = os.path.exists("/dev/shm/trigger_deepseek")
            if manual_trigger:
                try:
                    os.remove("/dev/shm/trigger_deepseek")
                except OSError:
                    pass

            # DeepSeek 触发：仅手动
            has_data = fsm.good_squats > 0 or fsm.failed_squats > 0
            is_chatting = os.path.exists("/dev/shm/chat_active")

            should_trigger = manual_trigger and has_data and not _ds_lock[0] and not is_chatting

            if should_trigger:
                _last_deepseek_time = time.time()
                _last_summarized_state = (fsm.good_squats, fsm.failed_squats)
                _ds_lock[0] = True
                good_count = fsm.good_squats
                failed_count = fsm.failed_squats
                logging.info(f"⏳ 手动触发总结 (标准:{good_count} 违规:{failed_count})")

                try:
                    from datetime import datetime
                    today = datetime.now().strftime("%Y-%m-%d")
                    now_time = datetime.now().strftime("%H:%M")
                    log_path = "/home/toybrick/agent_memory/training_log.json"
                    try:
                        with open(log_path, "r") as f:
                            log_data = json.load(f)
                    except (FileNotFoundError, json.JSONDecodeError):
                        log_data = {}
                    if today not in log_data:
                        log_data[today] = {"sessions": [], "daily_summary": None}
                    log_data[today]["sessions"].append({
                        "time": now_time, "good": good_count, "failed": failed_count
                    })
                    with open(log_path + ".tmp", "w") as f:
                        json.dump(log_data, f, ensure_ascii=False, indent=2)
                    os.rename(log_path + ".tmp", log_path)
                except Exception as e:
                    logging.error(f"训练日志写入失败: {e}")

                if connected:
                    # V2.2: 富 Prompt — 注入训练数据 + 肌肉激活 + 历史
                    duration_s = int(time.time() - fsm.last_active_time) if fsm.last_active_time else 0
                    total_reps = good_count + failed_count
                    rate_pct = round(good_count / total_reps * 100) if total_reps > 0 else 0

                    # 读取肌肉激活数据
                    muscle_info = ""
                    try:
                        with open('/dev/shm/muscle_activation.json', 'r') as mf:
                            m_data = json.load(mf)
                            acts = m_data.get('activations', {})
                            top_muscles = sorted(acts.items(), key=lambda x: x[1], reverse=True)[:3]
                            if top_muscles:
                                muscle_info = "、".join(f"{n}({v}%)" for n, v in top_muscles)
                    except Exception:
                        pass

                    # 读取历史数据（最近 3 天）
                    history_lines = ""
                    try:
                        log_path_h = "/home/toybrick/agent_memory/training_log.json"
                        with open(log_path_h, "r") as lf:
                            log_h = json.load(lf)
                        recent_dates = sorted(log_h.keys())[-3:]
                        for d in recent_dates:
                            sessions = log_h[d].get("sessions", [])
                            day_good = sum(s.get("good", 0) for s in sessions)
                            day_bad = sum(s.get("failed", 0) for s in sessions)
                            history_lines += f"  {d}: 标准{day_good}次, 违规{day_bad}次\n"
                    except Exception:
                        history_lines = "  暂无历史数据\n"

                    short_prompt = (
                        f"你是 IronBuddy 健身教练。本组训练数据：\n"
                        f"- 标准深蹲: {good_count} 次，违规半蹲: {failed_count} 次（合格率 {rate_pct}%）\n"
                        f"- 总动作数: {total_reps}\n"
                        f"{f'- 肌肉激活TOP3: {muscle_info}' + chr(10) if muscle_info else ''}"
                        f"最近3天训练记录:\n{history_lines}"
                        f"要求：1.纯一段文字，禁止换行/Markdown/Emoji。"
                        f"2.先表扬进步（如有历史对比），再指出不足。"
                        f"3.给一个具体改进建议。4.控制在60字以内，口语化。"
                    )
                    asyncio.create_task(_ds_wrapper(bridge, short_prompt, good_count, failed_count))

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
                    logging.error(f"对话轮询异常: {e}")

    except KeyboardInterrupt:
        logging.info("手动中断")
    finally:
        logging.info("🧹 退出 Main Loop。")

if __name__ == "__main__":
    asyncio.run(main())
