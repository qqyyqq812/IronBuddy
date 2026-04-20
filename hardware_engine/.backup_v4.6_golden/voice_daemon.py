#!/usr/bin/env python3
"""
IronBuddy V4 语音守护进程 — 百度 AipSpeech 版
- TTS: 百度在线语音合成 → WAV → aplay 播放
- STT: arecord 录音 + 自适应VAD → 百度在线短语音识别
- 唤醒: 录音→STT→关键词匹配 ("教练"等)
- 参考: docs/hardware_ref/main2.py (已验证方案)
- 抛弃: Vosk (ABI不兼容), edge-tts (依赖微软，不稳定), Google ASR
- V4.5 (2026-04-18): 单轮问答 / ASR 幻觉过滤 / 硬警报尊重静音 / TTS 串音修复 / preflush arecord
"""
import os
import sys
import time
import json
import wave
import logging
import subprocess
import threading
import signal
import collections
import struct
import ctypes

# Proxy disabled
for k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
    os.environ.pop(k, None)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [VOICE_V4] - %(message)s',
    handlers=[logging.StreamHandler()]
)

# ===== ALSA 错误静音 (参考 main2.py) =====
try:
    ERROR_HANDLER_FUNC = ctypes.CFUNCTYPE(
        None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
    def _py_error_handler(filename, line, function, err, fmt):
        pass
    _c_error_handler = ERROR_HANDLER_FUNC(_py_error_handler)
    _asound = ctypes.cdll.LoadLibrary('libasound.so.2')
    _asound.snd_lib_error_set_handler(_c_error_handler)
except Exception:
    pass

# ===== 配置 =====
BAIDU_APP_ID = os.environ.get("BAIDU_APP_ID", "")
BAIDU_API_KEY = os.environ.get("BAIDU_API_KEY", "")
BAIDU_SECRET_KEY = os.environ.get("BAIDU_SECRET_KEY", "")

DEVICE_REC = os.environ.get("VOICE_FORCE_MIC", "hw:0,0")  # 默认板载 RK809 MIC (card 0)；USB Webcam 可通过 VOICE_FORCE_MIC=hw:2,0 降级
DEVICE_SPK = os.environ.get("VOICE_SPK", "plughw:0,0")
REC_RATE = 44100       # 录音采样率 (硬件原始)
ASR_RATE = 16000       # 百度ASR要求16kHz
SILENCE_LIMIT = 1.2    # 停顿多久算说完 (秒)
VAD_TIMEOUT = 12       # 最长录音 (秒)
WAKE_TIMEOUT = 6       # 唤醒监听超时 (秒)

WAKE_WORDS = ["教练", "教", "叫练", "交练", "焦练", "铁哥", "coach"]
CHAT_INPUT_FILE = "/dev/shm/chat_input.txt"
STARTUP_DELAY = 5

# 静音状态 (V4.5: 改为 list[bool] 容器，支持跨线程 / 嵌套函数可变访问，无需 global)
_is_muted = [False]
_speech_lock = threading.Lock()
_play_proc = None  # current aplay process

# 违规警报监听
VIOLATION_ALERT_FILE = "/dev/shm/violation_alert.txt"
_violation_mtime = 0

# T5 (参考 main2.py hard_alarm_worker): L0 级硬警报
# 独立线程 + Event，违规警报立即 SIGKILL 正在播放的 aplay 并强制播放 (无视静音)
_violation_event = threading.Event()
_violation_text_latest = [""]  # mutable holder for thread communication


# ===== ALSA mixer 路径激活 (参考 main2.py + toybrick_board_rules §2) =====
# 板载 RK809 codec 掉电后 Playback Path 会归零，Capture MIC Path 默认 OFF。
# 启动时幂等激活一次；板上 sudoers.d/ironbuddy 已配置 NOPASSWD，无须交互。
_mixer_ready = False

def ensure_mixer_paths():
    global _mixer_ready
    if _mixer_ready:
        return
    try:
        # Capture MIC Path=1 (Main Mic) —— 激活板载模拟麦克风通路
        subprocess.run(
            ["sudo", "-n", "amixer", "-c", "0", "cset",
             "numid=2,iface=MIXER,name=Capture MIC Path", "1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3, check=False)
        # Playback Path=2 (SPK PH2.0 板载扬声器) —— 对齐老师标准命令 + main2.py；2026-04-18 decisions.md 坑 10 平反
        subprocess.run(
            ["sudo", "-n", "amixer", "-c", "0", "cset",
             "numid=1,iface=MIXER,name=Playback Path", "2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3, check=False)
        logging.info("ALSA mixer 路径已激活: Capture MIC Path=1, Playback Path=2")
        _mixer_ready = True
    except Exception as e:
        logging.warning("ensure_mixer_paths 执行失败: %s (继续运行，依赖开机预设)", e)


# ===== 百度 AipSpeech 初始化 =====
def _init_baidu():
    try:
        from aip import AipSpeech
        if not BAIDU_APP_ID or not BAIDU_API_KEY or not BAIDU_SECRET_KEY:
            logging.error("百度语音 API 凭证未配置 (BAIDU_APP_ID/API_KEY/SECRET_KEY)")
            return None
        client = AipSpeech(BAIDU_APP_ID, BAIDU_API_KEY, BAIDU_SECRET_KEY)
        logging.info("百度 AipSpeech 已就绪 (APP_ID: %s)", BAIDU_APP_ID)
        return client
    except ImportError:
        logging.error("baidu-aip 未安装，请执行: pip3 install --user baidu-aip")
        return None


# ===== TTS: 百度合成 + aplay 播放 =====
def text2sound(client, text, file_path="/tmp/voice_tts.wav"):
    # type: (object, str, str) -> bool
    with _speech_lock:
        try:
            result = client.synthesis(text, 'zh', 1, {'vol': 7, 'per': 4, 'aue': 6})
            if not isinstance(result, dict):
                with open(file_path, 'wb') as f:
                    f.write(result)
                return True
            logging.error("TTS 合成失败: %s", result)
            return False
        except Exception as e:
            logging.error("TTS 异常: %s", e)
            return False


def play_audio(file_path="/tmp/voice_tts.wav", allow_interrupt=True):
    # type: (str, bool) -> None
    global _play_proc
    if not os.path.exists(file_path):
        return

    # V4.5: 先等前一个 aplay 自然结束 (最多 2s)，而不是 killall -9
    # killall -9 aplay 会在 ALSA 未释放设备时被新进程抢占 PCM，出现尖锐倍速串音
    if _play_proc is not None and _play_proc.poll() is None:
        try:
            _play_proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                _play_proc.terminate()
            except Exception:
                pass
            time.sleep(0.1)

    # 设置音箱通道 (板端掉电会归零，V4.4 已改 Playback Path=2 对齐 main2.py)
    subprocess.run(
        ["sudo", "amixer", "-c", "0", "cset", "numid=1,iface=MIXER,name=Playback Path", "2"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cmd = ["sudo", "aplay", "-D" + DEVICE_SPK, "-q", file_path]
    _play_proc = subprocess.Popen(cmd)

    while _play_proc.poll() is None:
        if allow_interrupt and os.path.exists("/dev/shm/voice_interrupt"):
            logging.info("语音播放被打断")
            try:
                _play_proc.terminate()
                subprocess.run(["killall", "-9", "aplay"], stderr=subprocess.DEVNULL)
            except Exception:
                pass
            try:
                os.remove("/dev/shm/voice_interrupt")
            except OSError:
                pass
            break
        time.sleep(0.05)


def speak(client, text, allow_interrupt=True):
    # type: (object, str, bool) -> None
    """TTS合成 + 播放 (一步到位)"""
    if text2sound(client, text):
        play_audio("/tmp/voice_tts.wav", allow_interrupt=allow_interrupt)


# ===== STT: arecord + VAD + 百度识别 =====
def record_with_vad(timeout=VAD_TIMEOUT):
    # type: (int) -> str
    """
    录音，自适应VAD检测说话结束。
    返回: "SUCCESS" (录到了), "SILENCE" (没人说话), "INTERRUPTED"
    """
    import audioop
    import numpy as np

    # V4.5 preflush: 清理僵尸 arecord (decisions.md 坑 10 教训)
    # 长时间运行后板端可能留下僵尸 arecord 占用 PCM, 造成隐形锁死 (录音但无数据)
    subprocess.run(["killall", "-9", "arecord"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.2)

    cmd = ["sudo", "arecord", "-D" + DEVICE_REC, "-r%d" % REC_RATE,
           "-f", "S16_LE", "-c", "2", "-q"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    started = False
    silence_time = 0
    audio_frames = []
    pre_roll = collections.deque(maxlen=15)
    start_time = time.time()

    # 动态噪声基线校准 (8帧)
    noise_samples = []
    for _ in range(8):
        data = proc.stdout.read(4096)
        if data:
            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(np.square(arr))))
            noise_samples.append(rms)

    # VAD 阈值可通过 env 调节 (默认值对齐同学 main2.py 在 RK809 板载 MIC 的实测: 600/400)
    VAD_MIN = int(os.environ.get("VOICE_VAD_MIN", "600"))
    VAD_DELTA = int(os.environ.get("VOICE_VAD_DELTA", "400"))
    VAD_DEBUG = os.environ.get("VOICE_VAD_DEBUG", "0") == "1"
    baseline = sum(noise_samples) / len(noise_samples) if noise_samples else 300
    threshold = max(VAD_MIN, baseline + VAD_DELTA)
    logging.info("VAD校准: baseline=%.0f threshold=%.0f (min=%d delta=%d)", baseline, threshold, VAD_MIN, VAD_DELTA)

    output_path = "/tmp/voice_record.wav"

    try:
        while True:
            if time.time() - start_time > timeout:
                logging.info("录音超时 (%ds)", timeout)
                break

            data = proc.stdout.read(4096)
            if not data:
                break

            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(np.square(arr))))

            # Debug: 实时打印 RMS vs threshold
            if VAD_DEBUG:
                logging.info("[VAD_DBG] rms=%.0f thresh=%.0f started=%s", rms, threshold, started)

            if not started:
                if rms > threshold:
                    started = True
                    audio_frames.extend(pre_roll)
                    audio_frames.append(data)
                    logging.info("[VAD] 触发! rms=%.0f > thresh=%.0f", rms, threshold)
                else:
                    pre_roll.append(data)
            else:
                audio_frames.append(data)
                if rms >= threshold:
                    silence_time = 0
                else:
                    silence_time += 4096.0 / (REC_RATE * 4)

                if silence_time > SILENCE_LIMIT:
                    logging.info("VAD: 停顿 %.1fs，录音结束", SILENCE_LIMIT)
                    break
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        subprocess.run(["sudo", "killall", "arecord"], stderr=subprocess.DEVNULL)

    if not started or not audio_frames:
        logging.debug("VAD: 无人说话 (started=%s, frames=%d)", started, len(audio_frames))
        return "SILENCE"

    # 拼接 → 降采样到 16kHz mono → 保存 WAV
    raw_bytes = b''.join(audio_frames)
    mono = audioop.tomono(raw_bytes, 2, 0.5, 0.5)
    resampled, _ = audioop.ratecv(mono, 2, 1, REC_RATE, ASR_RATE, None)

    with wave.open(output_path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(ASR_RATE)
        wf.writeframes(resampled)

    wav_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    duration = len(resampled) / (ASR_RATE * 2)
    logging.info("录音完成: %.1fs, %d bytes, %d帧", duration, wav_size, len(audio_frames))

    if wav_size < 500:
        logging.warning("录音文件太小 (%d bytes), 可能没录到声音", wav_size)
        return "SILENCE"

    return "SUCCESS"


def sound2text(client, file_path="/tmp/voice_record.wav"):
    # type: (object, str) -> str
    """百度短语音识别

    V4.5: 加入幻觉过滤 — 百度 ASR 对噪声 / 感叹词易乱识别，过短或纯感叹词直接舍弃，
    避免发给 DeepSeek 造成同音词污染和无意义轮询。
    """
    try:
        wav_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        logging.info("ASR 请求: %s (%d bytes)", file_path, wav_size)
        with open(file_path, 'rb') as fp:
            audio_data = fp.read()
            result = client.asr(audio_data, 'wav', ASR_RATE, {'dev_pid': 1537})
            logging.info("ASR 原始返回: err_no=%s, result=%s",
                         result.get('err_no'), str(result.get('result', ''))[:100])
            if result.get('err_no') == 0:
                text = result['result'][0] if 'result' in result and result['result'] else ''
                # V4.5 幻觉过滤：去标点 + 过短舍弃 + 感叹词舍弃
                text = text.strip().rstrip('。，！？.,!?')
                if len(text) < 2:
                    logging.info("[ASR] 过短舍弃: %r", text)
                    return ""
                if text in {"嗯", "啊", "哦", "呃", "嗯嗯", "啊啊", "哦哦", "嗯啊", "哎"}:
                    logging.info("[ASR] 感叹词舍弃: %r", text)
                    return ""
                logging.info("ASR 识别: %s", text)
                return text
            else:
                logging.warning("ASR 错误 (err_no=%s): %s", result.get('err_no'), result.get('err_msg', ''))
    except Exception as e:
        logging.error("ASR 异常: %s", e)
    return ""


# ===== Debug 输出 =====
def output_debug(energy, text):
    try:
        data = {"energy": float(energy), "threshold": 0, "text": text}
        with open("/dev/shm/voice_debug.json.tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.rename("/dev/shm/voice_debug.json.tmp", "/dev/shm/voice_debug.json")
    except Exception:
        pass


# ===== 主循环 =====
def main():
    logging.info("等待 %ds 让其他服务初始化...", STARTUP_DELAY)
    time.sleep(STARTUP_DELAY)

    # 清理残留 arecord/aplay 进程
    subprocess.run(["sudo", "killall", "-9", "arecord", "aplay"],
                   stderr=subprocess.DEVNULL)

    # 激活 ALSA mixer 通路 (板载 MIC + SPK)；板重启后 Path 会归零
    ensure_mixer_paths()

    # === 启动诊断 ===
    logging.info("===== 启动诊断 =====")
    logging.info("BAIDU_APP_ID: %s", "已配置(%s...)" % BAIDU_APP_ID[:4] if BAIDU_APP_ID else "未配置!")
    logging.info("BAIDU_API_KEY: %s", "已配置(%s...)" % BAIDU_API_KEY[:4] if BAIDU_API_KEY else "未配置!")
    logging.info("BAIDU_SECRET_KEY: %s", "已配置" if BAIDU_SECRET_KEY else "未配置!")
    logging.info("录音设备: %s, 播放设备: %s", DEVICE_REC, DEVICE_SPK)
    logging.info("VAD参数: SILENCE_LIMIT=%.1fs, WAKE_TIMEOUT=%ds, VAD_TIMEOUT=%ds",
                 SILENCE_LIMIT, WAKE_TIMEOUT, VAD_TIMEOUT)
    logging.info("====================")

    client = _init_baidu()
    if client is None:
        logging.error("百度 AipSpeech 初始化失败，语音守护进程退出")
        while True:
            output_debug(0, "百度API未配置")
            time.sleep(30)

    # === 麦克风自测 ===
    # 若 env 强制指定麦克风则跳过自测（修复 hw:2,0 exit=1 但 size=176KB 的误判 bug）
    if os.environ.get("VOICE_FORCE_MIC"):
        logging.info("VOICE_FORCE_MIC=%s: 跳过麦克风自测，强信任", DEVICE_REC)
        test_ok = True
    else:
        logging.info("麦克风自测: 录制1秒...")
        test_ok = False
        # 优先板载 hw:0,0 (RK809)，USB Webcam hw:2,0 作为降级，ES7243 mic-array hw:3,0 兜底
        _fallback_devs = [DEVICE_REC, "hw:0,0", "hw:2,0", "hw:3,0"]
        _tried = set()
        for dev in _fallback_devs:
            if dev in _tried:
                continue
            _tried.add(dev)
            try:
                ret = subprocess.run(
                    ["sudo", "arecord", "-D" + dev, "-r%d" % REC_RATE,
                     "-f", "S16_LE", "-c", "2", "-d", "1", "-q", "/tmp/mic_test.wav"],
                    timeout=5, capture_output=True)
                sz = os.path.getsize("/tmp/mic_test.wav") if os.path.exists("/tmp/mic_test.wav") else 0
                # 宽松判据: size > 1000 即视为成功 (arecord 可能 exit=1 但数据录到了)
                if sz > 1000:
                    logging.info("麦克风自测通过: %s (录制 %d bytes, exit=%d)", dev, sz, ret.returncode)
                    if dev != DEVICE_REC:
                        logging.info("切换录音设备: %s → %s", DEVICE_REC, dev)
                        globals()['DEVICE_REC'] = dev
                    test_ok = True
                    break
                else:
                    logging.warning("设备 %s 录音失败 (exit=%d, size=%d)", dev, ret.returncode, sz)
            except Exception as e:
                logging.warning("设备 %s 测试异常: %s", dev, e)

        if not test_ok:
            logging.error("所有麦克风设备测试失败！语音功能不可用")
            output_debug(0, "麦克风离线")

    # T5: L0 硬警报线程 (参考 main2.py hard_alarm_worker)
    try:
        _alarm_thread = threading.Thread(target=hard_alarm_worker, args=(client,), daemon=True)
        _alarm_thread.start()
    except Exception as e:
        logging.error("L0 警报线程启动失败: %s", e)

    # 开机音效
    speak(client, "教练已上线，随时准备指导", allow_interrupt=False)

    global _violation_mtime
    llm_reply_mtime = 0

    while True:
        # === 监听违规警报 (T5: 分派给 L0 hard_alarm 线程，零延迟 SIGKILL 抢占) ===
        try:
            if os.path.exists(VIOLATION_ALERT_FILE):
                ts = os.path.getmtime(VIOLATION_ALERT_FILE)
                if ts != _violation_mtime:
                    _violation_mtime = ts
                    with open(VIOLATION_ALERT_FILE, "r", encoding="utf-8") as f:
                        alert_text = f.read().strip()
                    if alert_text:
                        logging.info("违规警报 → L0 线程: %s", alert_text)
                        _violation_text_latest[0] = alert_text
                        _violation_event.set()
        except Exception:
            pass

        # === 监听 DeepSeek 回复，自动朗读 ===
        if not _is_muted[0]:
            try:
                if os.path.exists("/dev/shm/llm_reply.txt"):
                    ts = os.path.getmtime("/dev/shm/llm_reply.txt")
                    if ts != llm_reply_mtime:
                        llm_reply_mtime = ts
                        with open("/dev/shm/llm_reply.txt", "r", encoding="utf-8") as f:
                            reply_text = f.read().strip()
                        if reply_text:
                            logging.info("朗读教练回复: %s", reply_text[:50])
                            speak(client, reply_text)
            except Exception:
                pass

        # === 监听 chat_reply (对话回复)，自动朗读 ===
        if not _is_muted[0]:
            try:
                if os.path.exists("/dev/shm/chat_reply.txt"):
                    ts = os.path.getmtime("/dev/shm/chat_reply.txt")
                    if ts != getattr(main, '_chat_reply_mtime', 0):
                        main._chat_reply_mtime = ts
                        with open("/dev/shm/chat_reply.txt", "r", encoding="utf-8") as f:
                            chat_text = f.read().strip()
                        if chat_text:
                            logging.info("朗读对话回复: %s", chat_text[:50])
                            speak(client, chat_text)
            except Exception:
                pass

        # === 唤醒监听: 录音 → STT → 检查关键词 ===
        output_debug(0, "待机中..." if not _is_muted[0] else "静音待机...")
        status = record_with_vad(timeout=WAKE_TIMEOUT)

        if status == "SILENCE":
            continue

        if status == "SUCCESS":
            text = sound2text(client)
            if not text:
                continue

            output_debug(0, text)

            # 静音状态下只响应解除静音命令
            if _is_muted[0]:
                if any(w in text for w in ["解除静音", "你可以说话", "恢复对话", "可以说话", "继续", "恢复"]):
                    _is_muted[0] = False
                    _write_signal("/dev/shm/mute_signal.json", {"muted": False, "ts": time.time()})
                    speak(client, "已解除静音", allow_interrupt=False)
                else:
                    logging.info("静音中，忽略: %s", text)
                continue

            # 检查唤醒词
            is_wake = any(w in text for w in WAKE_WORDS)
            if not is_wake:
                logging.info("非唤醒语句，忽略: %s", text)
                continue

            # === 唤醒成功 → 进入对话模式 ===
            logging.info("唤醒词命中: %s", text)

            # 提取唤醒词后面的内容
            remaining = ""
            for w in WAKE_WORDS:
                if w in text:
                    remaining = text.split(w, 1)[-1].strip()
                    if remaining:
                        break

            if remaining and len(remaining) >= 2:
                # 唤醒词后面直接带了指令 — 先尝试系统命令
                logging.info("唤醒 + 指令: %s", remaining)
                if not _try_voice_command(client, remaining):
                    speak(client, "收到", allow_interrupt=False)
                    _deliver_to_fsm(remaining)
                continue

            # V4.5: 单轮问答模式 (抢话式) — 用户决策：长对话体验差，改为每次"教练"唤醒只接一轮
            # 原 while _silence_rounds < _MAX_SILENCE_ROUNDS 连续对话已移除
            speak(client, "我在", allow_interrupt=False)
            try:
                open("/dev/shm/chat_active", "w").close()
            except OSError:
                pass

            # 单次录音 → STT → 系统命令 or FSM 分派
            status2 = record_with_vad(timeout=VAD_TIMEOUT)
            if status2 == "SUCCESS":
                text2 = sound2text(client)
                if text2 and len(text2) >= 2:
                    logging.info("[对话] 用户: %s", text2)
                    if _try_voice_command(client, text2):
                        pass  # 系统命令已处理 (静音/切模式/飞书等)
                    else:
                        _deliver_to_fsm(text2)
                else:
                    speak(client, "没听清，请再次喊教练", allow_interrupt=False)
            else:
                speak(client, "没听到声音，请再次喊教练", allow_interrupt=False)

            try:
                os.remove("/dev/shm/chat_active")
            except OSError:
                pass
            logging.info("[对话] 单轮问答完成，退回待机")


def hard_alarm_worker(client):
    # type: (object) -> None
    """T5 · L0 级硬警报独立线程 (参考 main2.py hard_alarm_worker)

    主循环检测到 violation_alert.txt 更新 → 设置 _violation_event + 填 latest text
    本线程响应 Event: SIGKILL 任何正在播的 aplay → 写 voice_interrupt → 播放警报
    - V4.6: 移除静音尊重 (对齐 main2.py，违规警报无视静音强制播报)
    - allow_interrupt=False (警报播放中不被次级请求打断)
    - 守护线程 (daemon=True)，主进程退出时自动终止
    """
    logging.info("[hard_alarm] L0 警报线程启动")
    while True:
        if _violation_event.wait(timeout=0.5):
            _violation_event.clear()
            text = _violation_text_latest[0]
            if not text:
                continue
                
            logging.info("[hard_alarm] 触发 L0 警报 (强制穿透): %s", text)
            # 1) 立即 SIGKILL 任何正在播放的 aplay (抢占主循环 speak)
            subprocess.run(["killall", "-9", "aplay"], stderr=subprocess.DEVNULL)
            # 2) 通知主循环的 play_audio 循环: 其 aplay 已被杀
            try:
                open("/dev/shm/voice_interrupt", "w").close()
            except OSError:
                pass
            # 3) 强制播放警报 (allow_interrupt=False: 警报播完前不被新请求截断)
            try:
                speak(client, text, allow_interrupt=False)
            except Exception as e:
                logging.error("[hard_alarm] speak 失败: %s", e)


def _deliver_to_fsm(text):
    """将用户语音文字投递到 FSM 的 chat_input 信号文件"""
    try:
        tmp = CHAT_INPUT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.rename(tmp, CHAT_INPUT_FILE)
        logging.info("投递到 FSM: %s", text)
    except Exception as e:
        logging.error("投递失败: %s", e)


# ===== 语音调控命令 (Task 2) =====
def _try_voice_command(client, text):
    # type: (object, str) -> bool
    """检查是否是系统命令, 是则执行并返回True, 否则返回False让文字投递到FSM

    V4.5 修复: 先判"解除静音"再判"静音"。否则用户说"解除静音"会被"静音"子串命中，
    造成死锁无法解除。"解除静音"精确匹配优先。
    """
    # V4.5 先判"解除静音" (精确关键词)
    UNMUTE_WORDS = ["解除静音", "可以说话", "恢复对话", "继续", "恢复", "别装死"]
    if any(w in text for w in UNMUTE_WORDS):
        _is_muted[0] = False
        _write_signal("/dev/shm/mute_signal.json", {"muted": False, "ts": time.time()})
        speak(client, "好的，恢复正常对话", allow_interrupt=False)
        logging.info("命令: 解除静音")
        return True

    # 再判"静音"
    MUTE_WORDS = ["安静", "闭嘴", "静音", "别吵"]
    if any(w in text for w in MUTE_WORDS):
        _is_muted[0] = True
        _write_signal("/dev/shm/mute_signal.json", {"muted": True, "ts": time.time()})
        speak(client, "好的，进入静音模式，说解除静音可以恢复", allow_interrupt=False)
        logging.info("命令: 静音")
        return True

    # 状态快速查房（零延迟边缘阻断指令）
    if any(w in text for w in ["多少个", "几个", "完成度", "成绩", "报数"]):
        try:
            with open("/dev/shm/fsm_state.json", "r", encoding="utf-8") as f:
                d = json.load(f)
            counts = d.get("good", 0)
            speak(client, "报告，目前已经达标了%d个动作！继续加油！" % counts, allow_interrupt=False)
            logging.info("命令: 动作查询 (返回 %d)", counts)
        except Exception:
            speak(client, "目前好像还没有运动数据哦！", allow_interrupt=False)
        return True

    # 切换到深蹲
    if any(w in text for w in ["切换到深蹲", "深蹲模式", "做深蹲"]):
        _write_signal("/dev/shm/exercise_mode.json", {"mode": "squat", "ts": time.time()})
        speak(client, "已切换到深蹲模式", allow_interrupt=False)
        logging.info("命令: 切换到深蹲")
        return True

    # 切换到弯举
    if any(w in text for w in ["切换到弯举", "弯举模式", "做弯举"]):
        _write_signal("/dev/shm/exercise_mode.json", {"mode": "curl", "ts": time.time()})
        speak(client, "已切换到弯举模式", allow_interrupt=False)
        logging.info("命令: 切换到弯举")
        return True

    # 飞书智能代理: 规划、总结、提醒 分流
    push_type = None
    if any(w in text for w in ["总结", "汇报", "战报"]):
        push_type = "summary"
        speak_ack = "正在查阅历史并汇总战报，推送到飞书"
    elif any(w in text for w in ["警告", "提醒", "超载", "报警"]):
        push_type = "reminder"
        speak_ack = "正在发送系统超载警示提醒通告"
    elif any(w in text for w in ["规划", "计划", "发飞书", "发消息"]):
        push_type = "plan"
        speak_ack = "正在调取长期记忆，生成专业规划推送飞书"

    if push_type:
        speak(client, speak_ack, allow_interrupt=False)
        logging.info("命令: 飞书智能推送 (%s)", push_type)
        try:
            payload = json.dumps({"type": push_type, "prompt": text}).encode('utf-8')
            import urllib
            if hasattr(urllib, 'request'):
                req = urllib.request.Request(
                    "http://127.0.0.1:5000/api/feishu/push",
                    data=payload,
                    headers={"Content-Type": "application/json"})
                resp = urllib.request.urlopen(req, timeout=40)
                result = json.loads(resp.read().decode())
                if result.get("ok"):
                    speak(client, "飞书投递已完成！", allow_interrupt=False)
                else:
                    speak(client, "推送失败: %s" % result.get("error", "未知错误")[:20], allow_interrupt=False)
            else:
                # Python 2 fallback
                import urllib2
                req = urllib2.Request(
                    "http://127.0.0.1:5000/api/feishu/push",
                    data=payload,
                    headers={"Content-Type": "application/json"})
                resp = urllib2.urlopen(req, timeout=40)
                speak(client, "飞书投递已完成！", allow_interrupt=False)
        except Exception as e:
            logging.error("飞书推送异常: %s", e)
            speak(client, "服务器似乎忙碌，推送出错", allow_interrupt=False)
        return True

    # 修改疲劳上限 (如 "疲劳目标改为2000")
    import re
    m = re.search(r'(\d{3,5})', text)
    if m and any(w in text for w in ["疲劳", "目标", "上限"]):
        limit = int(m.group(1))
        _write_signal("/dev/shm/fatigue_limit.json", {"limit": limit, "ts": time.time()})
        speak(client, "疲劳上限已改为%d" % limit, allow_interrupt=False)
        logging.info("命令: 疲劳上限改为 %d", limit)
        return True

    return False


def _write_signal(path, data):
    # type: (str, dict) -> None
    """原子写入信号文件"""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.rename(tmp, path)
    except Exception as e:
        logging.error("写信号文件失败 %s: %s", path, e)


if __name__ == "__main__":
    main()
