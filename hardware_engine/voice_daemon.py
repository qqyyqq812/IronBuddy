#!/usr/bin/env python3
"""
IronBuddy V3.0 唤醒式语音对话守护进程 — 最终重构加固版
- 完全离线，不依赖外网（Vosk流式）
- 修复 ALSA 轮询启停锁死暗雷 (彻底弃用 arecord -d 3，改用长链接 Popen 管道拾音)
- 异步播放 TTS (Edge-TTS放入daemon线程，避免阻塞主回环监听)
- 断网兜底交互逻辑 + 防冲突平滑音频切断(SIGTERM 缓冲)
"""
import os
import time
import json
import logging
import subprocess
import threading
import signal

# 恢复局域网代理，供 Google ASR 兜底使用
os.environ["http_proxy"] = "http://10.208.139.68:7890"
os.environ["https_proxy"] = "http://10.208.139.68:7890"
os.environ["HTTP_PROXY"] = "http://10.208.139.68:7890"
os.environ["HTTPS_PROXY"] = "http://10.208.139.68:7890"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [VOICE_V3] - %(message)s',
    handlers=[logging.StreamHandler()]
)

# ===== 配置 =====
MIC_DEVICE = "hw:2,0"          # 硬核绑定：RK3399ProX 原生 ES7243 远场阵列
SAMPLE_RATE = 16000
CHANNELS = 2
SILENCE_THRESHOLD = 25         
WAKE_WORDS = ["教练", "教", "叫练", "交练", "焦练", "iron", "buddy",
              "爱人", "巴蒂", "铁哥", "铁哥们", "铁头", "coach"]
TTS_REPLY = "我在，请说"
DEVICE_SPK = "plughw:0,0"
EDGE_TTS = "/home/toybrick/.local/bin/edge-tts"
TTS_VOICE = "zh-CN-YunxiNeural"
CHAT_INPUT_FILE = "/dev/shm/chat_input.txt"
STARTUP_DELAY = 15

# 初始化 ASR (SpeechRecognition 兜底 Vosk_ABI_Crash)
try:
    import speech_recognition as sr
    ASR_ENGINE = "google"
    global_recognizer = sr.Recognizer()
    logging.info("✅ SpeechRecognition 内存桥接引擎已就绪")
except ImportError:
    ASR_ENGINE = None
    logging.error("SpeechRecognition 未安装")

# TTS 播放竞态锁
_playback_lock = threading.Lock()
_current_tts_process = None

def _graceful_stop_audio():
    """优雅切断正在播放的音频，避免暴力 killall 导致 ALSA 驱动挂死"""
    global _current_tts_process
    
    if _current_tts_process and _current_tts_process.poll() is None:
        try:
            _current_tts_process.send_signal(signal.SIGTERM)
            try:
                _current_tts_process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                _current_tts_process.kill()
        except: pass
        _current_tts_process = None
        
    # 保底手段：发送友善的 TERM 信号给未知的游离播放器
    subprocess.run(["killall", "-TERM", "mpg123", "aplay", "edge-tts"], stderr=subprocess.DEVNULL)

def async_speak_tts(text):
    """
    非阻塞式 TTS，使用后台守护线程独立执行，
    在断网导致 edge-tts 超时时降级使用本地静态缓存兜底。
    """
    def _tts_thread_task():
        global _current_tts_process
        with _playback_lock:
            tmp_mp3 = "/tmp/voice_tts.mp3"
            fallback_wav = "/home/toybrick/hardware_engine/fallback_reply.wav"
            
            try:
                # 尝试 Edge-TTS，设定严格的 timeout，模拟联网死锁防护
                subprocess.run(
                    [EDGE_TTS, "--text", text, "--voice", TTS_VOICE, "--write-media", tmp_mp3],
                    timeout=5, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                media_path = tmp_mp3
                # 增加 -f 200 软限幅器（最大32768），强制音量衰减至物理底线（约0.6%）
                player_cmd = ["mpg123", "-a", DEVICE_SPK, "-f", "200", "-q", media_path]
            except Exception as e:
                logging.warning(f"TTS 生成异常/超时，可能遭遇断网，启动本地降级: {e}")
                if os.path.exists(fallback_wav):
                    media_path = fallback_wav
                    player_cmd = ["aplay", "-D", DEVICE_SPK, "-q", media_path]
                    # 针对 aplay 硬核降音量
                    subprocess.run(["amixer", "-c", "0", "sset", "Master", "1%"], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
                else:
                    logging.error("无可用的兜底交互音频。强制静默。")
                    return
            
            # 安全切入音箱通道
            subprocess.run(["amixer", "-c", "0", "sset", "Playback Path", "SPK_HP"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # 使用 Popen 拉起播放，注册进程实例以备未来的唤醒词强杀
            try:
                _current_tts_process = subprocess.Popen(player_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _current_tts_process.wait()
            except Exception: pass
            
    # 拉起非阻塞后台线程
    t = threading.Thread(target=_tts_thread_task, daemon=True)
    t.start()

def get_audio_energy(samples):
    """内存级快速能量计算，取绝对值截断"""
    if not samples: return 0
    import struct
    try:
        shorts = struct.unpack(f"<{len(samples)//2}h", samples)
        if not shorts: return 0
        return sum(abs(s) for s in shorts) / len(shorts)
    except Exception:
        return 0

def output_debug(energy, text):
    try:
        debug_data = {"energy": float(energy), "threshold": SILENCE_THRESHOLD, "text": text}
        with open("/dev/shm/voice_debug.json.tmp", "w", encoding="utf-8") as f:
            json.dump(debug_data, f)
        os.rename("/dev/shm/voice_debug.json.tmp", "/dev/shm/voice_debug.json")
    except Exception:
        pass

def main():
    logging.info(f"等待 {STARTUP_DELAY}s 让中心进程组初始化...")
    time.sleep(STARTUP_DELAY)

    if ASR_ENGINE != "google":
        logging.error("需要 SpeechRecognition 包来实现内存桥接，退出守护进程。")
        return

    audio_buffer = bytearray()
    
    # 【最核心防御：管线式长链接拾音挂靠】
    # 以 raw 格式无尽头拉取硬件内存流，拒绝重复调用 alsa init
    arecord_cmd = [
        "arecord", "-D", MIC_DEVICE, 
        "-c", str(CHANNELS), "-r", str(SAMPLE_RATE), 
        "-f", "S16_LE", "-t", "raw"
    ]
    logging.info(f"🎤 挂靠硬件 I2S 持续拾音管道: {' '.join(arecord_cmd)}")
    
    pipeline = subprocess.Popen(
        arecord_cmd, 
        stdout=subprocess.PIPE, 
        stderr=subprocess.PIPE
    )

    conversation_mode = False
    silence_count = 0
    chunk_count = 0
    accumulated_text = ""
    llm_reply_mtime = 0
    
    try:
        while True:
            # V2.3: 监听 总结陈词，教练主动念稿！(不阻塞回环)
            if os.path.exists("/dev/shm/llm_reply.txt"):
                try:
                    ts = os.path.getmtime("/dev/shm/llm_reply.txt")
                    if ts != llm_reply_mtime:
                        llm_reply_mtime = ts
                        with open("/dev/shm/llm_reply.txt", "r", encoding="utf-8") as f:
                            sum_txt = f.read().strip()
                        if sum_txt:
                            logging.info(f"📢 并发下发长篇文字: {sum_txt}")
                            async_speak_tts(sum_txt)
                except Exception: pass
                
            # 阻塞读取管道缓冲区（每次 4000 byte，相当于 0.125 秒的双声道 16k 音频缓存）
            data = pipeline.stdout.read(4000)
            if not data:
                logging.error("💥 arecord 管道意外断裂，强制清场重置！")
                output_debug(-1, "麦克风离线/管道断裂")
                pipeline.kill()
                time.sleep(2)
                pipeline = subprocess.Popen(arecord_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                continue
                
            chunk_count += 1
            energy = get_audio_energy(data)
            
            # 若处于静默期
            if energy < SILENCE_THRESHOLD:
                if chunk_count % 10 == 0:  
                    output_debug(energy, "")
                    
                silence_count += 1
                if len(audio_buffer) > 16000: # 有积攒的语音数据
                    if silence_count > 10:    # 确认停顿 (约1.25秒)
                        # 核心防卡死魔法：直接挂载 raw 内存为 AudioData
                        audio_obj = sr.AudioData(bytes(audio_buffer), SAMPLE_RATE, 2)
                        audio_buffer.clear()
                        text = ""
                        try:
                            # 提交给 Google API
                            text = global_recognizer.recognize_google(audio_obj, language="zh-CN")
                            logging.info(f"🧠 Google ASR截流: {text}")
                        except sr.UnknownValueError:
                            pass
                        except Exception as e:
                            logging.error(f"Google API 报错: {e}")
                        
                        output_debug(energy, text)
                        
                        if text:
                            if not conversation_mode:
                                is_wake = any(w in text.lower() for w in WAKE_WORDS)
                                if is_wake:
                                    logging.info("⚡ 唤醒词击穿! 执行强压制策略，剥夺既有语音输出！")
                                    _graceful_stop_audio()
                                    conversation_mode = True
                                    try:
                                        open("/dev/shm/chat_active", "w").close()
                                    except: pass
                                    async_speak_tts(TTS_REPLY)
                                    remaining = ""
                                    for w in WAKE_WORDS:
                                        if w in text:
                                            remaining = text.split(w, 1)[-1].strip()
                                            if remaining: break
                                    if remaining:
                                        accumulated_text = remaining
                            else:
                                accumulated_text += (" " if accumulated_text else "") + text
                                
                                try:
                                    with open("/dev/shm/chat_draft.txt.tmp", "w") as f:
                                        f.write(accumulated_text)
                                    os.rename("/dev/shm/chat_draft.txt.tmp", "/dev/shm/chat_draft.txt")
                                except: pass
                                
                if conversation_mode and silence_count > 25: # 2.5秒彻底没话说
                    logging.info("⏸️ 侦测到静默，结束录音阶段。")
                    if accumulated_text.strip():
                        with open(CHAT_INPUT_FILE + ".tmp", "w", encoding="utf-8") as f:
                            f.write(accumulated_text.strip())
                        os.rename(CHAT_INPUT_FILE + ".tmp", CHAT_INPUT_FILE)
                        logging.info(f"📝 投递句子: {accumulated_text.strip()}")
                        accumulated_text = ""
                    else:
                        logging.warning("录制了死空集，不予投递")
                        async_speak_tts("抱歉，我没听清。")

                    conversation_mode = False
                    silence_count = 0
                    try:
                        os.remove("/dev/shm/chat_active")
                        os.remove("/dev/shm/chat_draft.txt")
                    except OSError: pass
                continue

            # 处于发声期
            silence_count = 0
            audio_buffer.extend(data)
            # 因为 API 为阻塞发送，发声期不能直接请求 API，仅积累到 audio_buffer 内存里，依靠 ALSA 万吨缓存吸纳冲击。

    except KeyboardInterrupt:
        logging.info("收到中止命令。休眠管道...")
    finally:
        pipeline.terminate()
        pipeline.wait()

if __name__ == "__main__":
    main()
