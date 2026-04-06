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
        if now - self._last_buzzer_time < 2.0:
            return
        self._last_buzzer_time = now
        os.system("bash /home/toybrick/hardware_engine/peripherals/buzzer_alert.sh &")
        logging.warning("🔊 音箱警报！动作判定违规！")

    def _read_emg(self):
        try:
            with open("/dev/shm/muscle_activation.json", "r") as f:
                d = json.load(f)
                return d.get("activations", {})
        except Exception:
            return {}

    def sync_to_frontend(self, current_angle=180.0):
        try:
            emg_feats = self._read_emg()
            state_data = {
                "state": self.state,
                "good": self.good_squats,
                "failed": self.failed_squats,
                "angle": round(current_angle, 1),
                "fatigue": round(self.total_fatigue_volume, 1),
                "chat_active": os.path.exists("/dev/shm/chat_active"),
                # 暴露给前端做渲染
                "emg_activations": [
                    emg_feats.get("quadriceps", 0), 
                    emg_feats.get("glutes", 0),
                    emg_feats.get("calves", 0),
                    emg_feats.get("biceps", 0)
                ]
            }
                
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
            if obj.get("score", 0) < 0.35:
                self.state = "NO_PERSON"
                self.sync_to_frontend()
                return None

            kpts = obj.get("kpts", [])
            if len(kpts) < 17:
                return None

            hip   = [kpts[11][0], kpts[11][1]]
            knee  = [kpts[13][0], kpts[13][1]]
            ankle = [kpts[15][0], kpts[15][1]]
            raw_angle = self.calculate_angle(hip, knee, ankle)

            self._angle_history.append(raw_angle)
            if len(self._angle_history) > 16:
                self._angle_history.pop(0)
            smooth_n = min(5, len(self._angle_history))
            angle = sum(self._angle_history[-smooth_n:]) / smooth_n

            trend = self._get_trend()

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

                if trend == "rising" and (time.time() - self._last_count_time) > 1.0:
                    bottom = self._min_angle_in_rep
                    self._last_count_time = time.time()
                    # 视觉纯净版：不再参考任何 EMG 发力情况
                    
                    if bottom < self.ANGLE_STANDARD:
                        # 完美深蹲，累计固定的疲劳量 (完全摘除对quad/glute的依赖)
                        self.good_squats += 1
                        self.state = "ASCENDING"
                        volume = 50.0 # 模拟每次+50点疲劳，后续会与时序对齐后恢复真值
                        self.total_fatigue_volume += volume
                        logging.info(f"🟢 好球！（角度{bottom:.0f}°）当前总疲劳值: {self.total_fatigue_volume:.1f}/1500")
                    else:
                        # 行程不足，判定犯规半蹲
                        self.failed_squats += 1
                        self.state = "ASCENDING"
                        self.trigger_buzzer_alert()
                        logging.warning(f"🟡 违规：下蹲幅度不足！（当前最低{bottom:.0f}°）累计违规：{self.failed_squats}")

                elif is_stable:
                    self.state = "IDLE"

            elif self.state == "ASCENDING":
                self.last_active_time = time.time()
                if trend == "falling":
                    self.state = "DESCENDING"
                    self._min_angle_in_rep = angle
                elif is_stable:
                    self.state = "IDLE"

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
            pass
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

            # 前端重置信号
            if os.path.exists("/dev/shm/fsm_reset_signal"):
                try: os.remove("/dev/shm/fsm_reset_signal")
                except OSError: pass
                fsm.good_squats = 0
                fsm.failed_squats = 0
                fsm.state = "NO_PERSON"
                fsm._angle_history.clear()
                fsm._min_angle_in_rep = 999
                fsm._idle_counter = 0
                fsm.total_fatigue_volume = 0
                fsm.sync_to_frontend()
                try: os.remove("/dev/shm/llm_reply.txt")
                except OSError: pass
                logging.info("🔄 收到前端重置信号，全轨数据已清零！")
                continue

            # DeepSeek 结组触发触发：手动
            manual_trigger = os.path.exists("/dev/shm/trigger_deepseek")
            
            if manual_trigger:
                try: os.remove("/dev/shm/trigger_deepseek")
                except OSError: pass

            has_data = fsm.good_squats > 0 or fsm.failed_squats > 0
            is_chatting = os.path.exists("/dev/shm/chat_active")

            should_trigger = manual_trigger and has_data and not _ds_lock[0] and not is_chatting

            if should_trigger:
                _ds_lock[0] = True
                good_count = fsm.good_squats
                failed_count = fsm.failed_squats
                reason = "手动按键"
                logging.info(f"⏳ 触发大模型结组 ({reason}) - (标准:{good_count} 违规:{failed_count})")

                if connected:
                    rate_pct = round(good_count / (good_count + failed_count) * 100) if (good_count+failed_count) > 0 else 0
                    short_prompt = (
                        f"你是 IronBuddy 健身教练。现在用户已经拉崩了他们的肌肉，完美完成了本组训练！\n"
                        f"这组真实硬核的运动数据：\n"
                        f"- 达成总运动量/疲劳池：1500点满分！\n"
                        f"- 标准深蹲: {good_count} 次，触发犯规代偿的废动作: {failed_count} 次（合格率 {rate_pct}%）\n"
                        f"要求：1.纯一段文字，禁止换行/Markdown/Emoji。"
                        f"2.疯狂赞美他们这组达成了 1500 肌肉疲劳值，但也要毒舌地点出他们做废的动作次数。"
                        f"3.控制在50字以内，口语化。"
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
                    pass

    except KeyboardInterrupt:
        logging.info("手动中断")
    finally:
        logging.info("🧹 退出 Main Loop。")

if __name__ == "__main__":
    asyncio.run(main())
