import os
import time
import queue
import threading
import subprocess
import numpy as np
import logging

class MicrophoneController:
    """
    麦克风单例驱动引擎 (Agent 1 硬件基建)
    支持 USB Webcam 内置麦克风或 ALSA 系统默认麦克风的健壮采集。
    [v2 架构升级] 支持Pub/Sub（发布订阅）分发机制。
    消除队列争抢 (Stealing) Bug，确保 NPU/ASR 和 HTTP 监听流能同时拿到完美的声音片段。
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(MicrophoneController, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, device="hw:3,0", sample_rate=16000, channels=1, chunk_size=1024):
        if self._initialized:
            return
            
        self.device = device
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_size = chunk_size
        
        self.process = None
        self.subscribers = [] # 允许多个引擎(如ASR和Web流)同时挂载听觉
        self.is_running = False
        self.capture_thread = None
        
        logging.basicConfig(level=logging.INFO, format='[Audio Sensor] %(message)s')
        self._start_capture()
        self._initialized = True

    def register_client(self, max_buffer=15):
        """开辟一个独立的订阅通道"""
        q = queue.Queue(maxsize=max_buffer)
        self.subscribers.append(q)
        return q

    def _start_capture(self):
        if self.is_running:
            return
        logging.info(f"正在挂载音频节点: {self.device} (Rate: {self.sample_rate}Hz)")
        cmd = [
            "arecord",
            "-D", self.device,
            "-f", "S16_LE",
            "-c", str(self.channels),
            "-r", str(self.sample_rate),
            "-t", "raw",
            "-q"
        ]
        try:
            self.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=self.chunk_size * 2)
            self.is_running = True
            self.capture_thread = threading.Thread(target=self._dispatch_stream, daemon=True)
            self.capture_thread.start()
            logging.info("✅ 麦克风 PCM 数据广播泵 (Pub/Sub) 已启动！")
        except Exception as e:
            logging.error(f"❌ 无法启动内核级音频流: {e}")
            self.is_running = False

    def _dispatch_stream(self):
        """后台分发器：把波形复制给所有的订阅队列"""
        bytes_per_sample = 2
        read_total_bytes = self.chunk_size * bytes_per_sample * self.channels

        while self.is_running and self.process and self.process.stdout:
            try:
                raw_data = self.process.stdout.read(read_total_bytes)
                if not raw_data:
                    logging.warning("⚠️ 内核音频流遭遇断崖...")
                    time.sleep(1.0)
                    continue
                    
                audio_array = np.frombuffer(raw_data, dtype=np.int16)
                
                # 同步广播给所有的监听神经元 (Aagent/ASR/Web)
                for q in self.subscribers:
                    if q.full():
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            pass
                    q.put(audio_array)
                    
            except Exception as e:
                logging.error(f"音频流矩阵分发过程发生撕裂: {e}")
                time.sleep(0.5)

    def release(self):
        if not self.is_running:
            return
        logging.info("🧹 正在进入防雷撤离程序...")
        self.is_running = False
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
        if self.capture_thread and self.capture_thread.is_alive():
            self.capture_thread.join(timeout=0.5)
        logging.info("🛡️ ALSA 硬件资源已安全遣散！")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

if __name__ == "__main__":
    print("[Agent 1] 开始多客户端声卡分发测试...")
    try:
        with MicrophoneController(device="hw:3,0", chunk_size=2048) as mic:
            q1 = mic.register_client()
            q2 = mic.register_client()
            for i in range(5):
                aud1 = q1.get(timeout=1.0)
                aud2 = q2.get(timeout=1.0)
                print(f"[{i+1}/5] ASR端振幅: {np.max(np.abs(aud1))} | Web端振幅: {np.max(np.abs(aud2))}")
                time.sleep(0.2)
    except Exception as e:
        print(e)
