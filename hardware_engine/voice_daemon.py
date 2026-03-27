#!/usr/bin/env python3
"""
IronBuddy V2.2 唤醒式语音对话守护进程 — Vosk 离线 ASR 版
- 完全离线，不依赖外网
- 流式识别：每个 chunk 实时喂给 Vosk，延迟 <1s
- 检测唤醒词"教练" → TTS "我在" → 录对话 → DeepSeek
"""
import os
import sys
import time
import wave
import struct
import subprocess
import logging
import json

# 确保不走任何代理（彻底离线）
for key in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
    os.environ.pop(key, None)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [VOICE] - %(message)s',
    handlers=[logging.StreamHandler()]
)

# ===== 配置 =====
MIC_CANDIDATES = ["plughw:0,0", "plughw:2,0", "plughw:3,0"]
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SECONDS = 3
SILENCE_THRESHOLD = 150         # V2.1 调优值
WAKE_WORDS = ["教练", "教", "叫练", "交练", "焦练", "iron", "buddy",
              "爱人", "巴蒂", "铁哥", "铁哥们", "铁头", "coach"]
TTS_REPLY = "我在，请说"
DEVICE_SPK = "plughw:0,0"
EDGE_TTS = "/home/toybrick/.local/bin/edge-tts"
TTS_VOICE = "zh-CN-YunxiNeural"
CHAT_INPUT_FILE = "/dev/shm/chat_input.txt"
STARTUP_DELAY = 15  # 秒

# Vosk 模型路径（板端需要提前下载 vosk-model-small-cn-0.22）
VOSK_MODEL_PATH = "/home/toybrick/vosk-model-small-cn-0.22"

# ===== ASR 引擎初始化 =====
ASR_ENGINE = None  # "vosk" | "google" | None

try:
    from vosk import Model, KaldiRecognizer
    if os.path.exists(VOSK_MODEL_PATH):
        _vosk_model = Model(VOSK_MODEL_PATH)
        ASR_ENGINE = "vosk"
        logging.info(f"✅ Vosk 离线 ASR 就绪 (模型: {VOSK_MODEL_PATH})")
    else:
        logging.warning(f"Vosk 模型目录不存在: {VOSK_MODEL_PATH}")
except ImportError:
    logging.warning("Vosk 未安装 (pip install vosk)")

# Fallback: Google ASR（需要外网）
if not ASR_ENGINE:
    try:
        import speech_recognition as sr
        recognizer = sr.Recognizer()
        ASR_ENGINE = "google"
        logging.info("⚠️ 降级为 Google ASR（需要外网）")
    except ImportError:
        logging.error("❌ 无任何 ASR 引擎可用")

MIC_DEVICE = None


def find_working_mic():
    """尝试每个候选麦克风，返回第一个能录到声音的"""
    subprocess.run(["amixer", "-c", "0", "sset", "Capture MIC Path", "Main Mic"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for dev in MIC_CANDIDATES:
        test_path = "/tmp/mic_probe.wav"
        try:
            result = subprocess.run(
                ["arecord", "-D", dev, "-f", "S16_LE", "-r", str(SAMPLE_RATE),
                 "-c", str(CHANNELS), "-d", "1", test_path],
                timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            if result.returncode == 0 and os.path.exists(test_path):
                os.remove(test_path)
                return dev
        except Exception:
            pass
        try:
            os.remove(test_path)
        except OSError:
            pass
    return None


def record_audio(duration=3, output_path="/tmp/voice_chunk.wav"):
    global MIC_DEVICE
    if not MIC_DEVICE:
        return None
    cmd = [
        "arecord", "-D", MIC_DEVICE,
        "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", str(CHANNELS),
        "-d", str(duration), output_path
    ]
    try:
        result = subprocess.run(cmd, timeout=duration + 5,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if result.returncode != 0:
            err = result.stderr.decode('utf-8', errors='ignore').strip()
            if 'busy' in err:
                logging.warning(f"设备被占用，5s 后重试...")
                time.sleep(5)
                MIC_DEVICE = find_working_mic()
                if MIC_DEVICE:
                    logging.info(f"切换到麦克风: {MIC_DEVICE}")
            else:
                logging.error(f"录音失败: {err}")
            return None
        if os.path.exists(output_path) and os.path.getsize(output_path) > 100:
            return output_path
        return None
    except Exception as e:
        logging.error(f"录音异常: {e}")
        return None


def get_audio_energy(wav_path):
    try:
        with wave.open(wav_path, 'rb') as wf:
            frames = wf.readframes(wf.getnframes())
            samples = struct.unpack(f"<{len(frames)//2}h", frames)
            if not samples:
                return 0
            return sum(abs(s) for s in samples) / len(samples)
    except Exception:
        return 0


def speak_tts(text):
    tmp_mp3 = "/tmp/voice_tts.mp3"
    try:
        subprocess.run(
            [EDGE_TTS, "--text", text, "--voice", TTS_VOICE, "--write-media", tmp_mp3],
            timeout=10, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        subprocess.run(["amixer", "-c", "0", "sset", "Playback Path", "SPK"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            ["mpg123", "-a", DEVICE_SPK, "-r", "16000", "-f", "8000", "-q", tmp_mp3],
            timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception as e:
        logging.error(f"TTS 失败: {e}")
    finally:
        try:
            os.remove(tmp_mp3)
        except OSError:
            pass


def transcribe(wav_path):
    """V2.2: Vosk 离线优先，Google 在线 fallback"""
    if ASR_ENGINE == "vosk":
        return _transcribe_vosk(wav_path)
    elif ASR_ENGINE == "google":
        return _transcribe_google(wav_path)
    return ""


def _transcribe_vosk(wav_path):
    """Vosk 离线识别 — 快速、无网络依赖"""
    try:
        rec = KaldiRecognizer(_vosk_model, SAMPLE_RATE)
        with wave.open(wav_path, 'rb') as wf:
            while True:
                data = wf.readframes(4000)
                if len(data) == 0:
                    break
                rec.AcceptWaveform(data)
        result = json.loads(rec.FinalResult())
        return result.get("text", "")
    except Exception as e:
        logging.error(f"Vosk 识别异常: {e}")
        return ""


def _transcribe_google(wav_path):
    """Google 在线 ASR fallback（带 5s 超时）"""
    try:
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)
        import socket
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(5)
        try:
            result = recognizer.recognize_google(audio, language="zh-CN")
        finally:
            socket.setdefaulttimeout(old_timeout)
        return result
    except Exception as e:
        logging.error(f"Google ASR 异常: {e}")
        return ""


def write_chat_input(text):
    try:
        with open(CHAT_INPUT_FILE + ".tmp", "w", encoding="utf-8") as f:
            f.write(text)
        os.rename(CHAT_INPUT_FILE + ".tmp", CHAT_INPUT_FILE)
        logging.info(f"对话输入: {text}")
    except Exception as e:
        logging.error(f"写入失败: {e}")


def main():
    global MIC_DEVICE

    logging.info(f"等待 {STARTUP_DELAY}s 让其他进程初始化...")
    time.sleep(STARTUP_DELAY)

    # 自动检测可用麦克风
    for attempt in range(6):
        MIC_DEVICE = find_working_mic()
        if MIC_DEVICE:
            logging.info(f"麦克风就绪: {MIC_DEVICE}")
            break
        logging.warning(f"第 {attempt+1} 次检测失败，10s 后重试...")
        time.sleep(10)

    if not MIC_DEVICE:
        logging.error("所有麦克风不可用，进入待机模式（每 30s 重试）")
        while True:
            time.sleep(30)
            MIC_DEVICE = find_working_mic()
            if MIC_DEVICE:
                logging.info(f"麦克风恢复: {MIC_DEVICE}")
                break

    if not ASR_ENGINE:
        logging.error("无 ASR 引擎，退出")
        while True:
            time.sleep(60)

    logging.info(f"唤醒式语音守护就绪 | 引擎={ASR_ENGINE} | 麦克风={MIC_DEVICE}")

    conversation_mode = False
    silence_count = 0
    chunk_count = 0
    accumulated_text = ""

    while True:
        try:
            wav_path = record_audio(duration=CHUNK_SECONDS)
            if not wav_path:
                time.sleep(1)
                continue

            energy = get_audio_energy(wav_path)
            chunk_count += 1
            logging.info(f"[chunk#{chunk_count}] 能量={energy:.0f} (阈值={SILENCE_THRESHOLD}){'  ★ 有声' if energy >= SILENCE_THRESHOLD else ''}")

            if energy < SILENCE_THRESHOLD:
                text = ""
            else:
                logging.info(f"语音检测 (能量={energy:.0f})")
                text = transcribe(wav_path)

            if not text:
                if conversation_mode:
                    silence_count += 1
                    max_silence = 2 if not accumulated_text.strip() else 1
                    if silence_count >= max_silence:
                        logging.info("对话结束（ASR返回空，判定为结束）")
                        if accumulated_text.strip():
                            write_chat_input(accumulated_text.strip())
                            accumulated_text = ""
                        else:
                            logging.warning("录音超时且无任何文字，判定为没听清")
                            speak_tts("抱歉，我没听清，请再说一次。")

                        conversation_mode = False
                        silence_count = 0
                        try:
                            os.remove("/dev/shm/chat_active")
                            os.remove("/dev/shm/chat_draft.txt")
                        except OSError:
                            pass
                continue

            silence_count = 0
            logging.info(f"识别: {text}")

            if not conversation_mode:
                is_wake = any(w in text.lower() for w in WAKE_WORDS)

                if is_wake:
                    logging.info("唤醒词触发! 执行语音最高级打断...")
                    os.system("killall aplay edge-tts mpg123 espeak 2>/dev/null")

                    conversation_mode = True
                    try:
                        open("/dev/shm/chat_active", "w").close()
                    except Exception:
                        pass
                    speak_tts(TTS_REPLY)

                    remaining = ""
                    for w in WAKE_WORDS:
                        if w in text:
                            remaining = text.split(w, 1)[-1].strip()
                            if remaining:
                                break
                    if remaining:
                        accumulated_text = remaining
                        try:
                            with open("/dev/shm/chat_draft.txt.tmp", "w") as f:
                                f.write(accumulated_text)
                            os.rename("/dev/shm/chat_draft.txt.tmp", "/dev/shm/chat_draft.txt")
                        except Exception:
                            pass
            else:
                logging.info(f"对话积攒: {text}")
                accumulated_text += (" " if accumulated_text else "") + text
                try:
                    with open("/dev/shm/chat_draft.txt.tmp", "w") as f:
                        f.write(accumulated_text)
                    os.rename("/dev/shm/chat_draft.txt.tmp", "/dev/shm/chat_draft.txt")
                except Exception:
                    pass

        except KeyboardInterrupt:
            break
        except Exception as e:
            logging.error(f"异常: {e}")
            time.sleep(1)


if __name__ == "__main__":
    main()
