import os
import json
import time
import queue
import threading
import logging

try:
    from vosk import Model, KaldiRecognizer
    VOSK_AVAILABLE = True
except Exception as e:
    logging.error(f"⚠️ Vosk 导入引发系统底层 ABI 不兼容，已进入优雅降级保护：{e}")
    VOSK_AVAILABLE = False

class ASRWorker:
    """
    轻量级脱机语音识别中台 (Agent 1 听觉神经元)
    使用 Pub/Sub 模式接入 MicrophoneController 的分发队列。
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(ASRWorker, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, model_path="/home/toybrick/streamer/hardware_engine/model/vosk-model-small-cn", sample_rate=16000):
        if self._initialized:
            return
            
        self.model_path = model_path
        self.sample_rate = sample_rate
        
        self.caption_queue = queue.Queue(maxsize=10)
        self.is_running = False
        self.worker_thread = None
        
        logging.basicConfig(level=logging.INFO, format='[ASR Engine] %(message)s')
        if not VOSK_AVAILABLE:
            logging.warning("⚠️ Vosk 库核心缺失，听觉皮层将装载为一个[空转马达]，仅作展示维持。")
            self.caption_queue.put("⚠️ 算力板底层环境较旧，ASR 字幕引擎处于降级挂起状态。")
            self._initialized = True
            return

        if not os.path.exists(self.model_path):
            logging.error(f"❌ 找不到轻量级声学大脑: {self.model_path}")
            return
            
        logging.info("🧠 正在加载 Vosk 脱机声学神经网络模型...")
        try:
            self.model = Model(self.model_path)
            self.recognizer = KaldiRecognizer(self.model, self.sample_rate)
            self.recognizer.SetWords(False)
            logging.info("✅ Vosk 脱机声学大脑装载完毕！")
            self._initialized = True
        except Exception as e:
            logging.error(f"❌ 模型加载崩溃: {e}")

    def start_listening(self, mic_controller):
        """挂载 Pub/Sub 推流订阅，不抢夺其他客户端的声音"""
        if not self._initialized or self.is_running:
            return
            
        self.audio_queue = mic_controller.register_client(max_buffer=30)
        self.is_running = True
        self.worker_thread = threading.Thread(target=self._process_audio_stream, daemon=True)
        self.worker_thread.start()
        logging.info("🎧 ASR 神经网已注册收听麦克风广播，开启极速转写模式！")

    def _process_audio_stream(self):
        while self.is_running:
            if not VOSK_AVAILABLE:
                # 优雅降级下的假空转循环，抛弃收到的音频
                try:
                    self.audio_queue.get(timeout=1.0)
                except queue.Empty:
                    pass
                continue
                
            try:
                audio_array = self.audio_queue.get(timeout=1.0)
                data = audio_array.tobytes()
                
                if self.recognizer.AcceptWaveform(data):
                    result = self.recognizer.Result()
                    text_dict = json.loads(result)
                    spoken_text = text_dict.get("text", "").replace(" ", "")
                    
                    if spoken_text:
                        logging.info(f"📝 识别反馈: {spoken_text}")
                        if self.caption_queue.full():
                            try:
                                self.caption_queue.get_nowait()
                            except queue.Empty:
                                pass
                        self.caption_queue.put(spoken_text)
            except queue.Empty:
                time.sleep(0.05)
            except Exception as e:
                logging.error(f"ASR 处理中断: {e}")

    def get_latest_caption(self, timeout=0.1):
        try:
            return self.caption_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def release(self):
        if not self.is_running:
            return
        logging.info("🧹 正在切断 ASR 听觉皮层...")
        self.is_running = False
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=1.0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
