import socket
import struct
import json
import os
import time
import threading
import logging
from collections import deque
import math

# 配置日志输出格式，方便 Director 后台看门狗截获
logging.basicConfig(level=logging.INFO, format='%(asctime)s - [UDP_EMG] - %(message)s')

UDP_IP = "0.0.0.0"
UDP_PORT = 8080

# 全局状态，用于解耦 DSP 高频线程和 IO 低频写盘线程
CURRENT_RMS_PCT = 0
IS_CONNECTED = False
LAST_ALIVE_TS = 0

class BiquadFilter:
    def __init__(self, b, a):
        self.b0, self.b1, self.b2 = b[0], b[1], b[2]
        self.a1, self.a2 = a[1], a[2] # a0 always 1.0
        self.z1, self.z2 = 0.0, 0.0

    def process(self, x):
        y = self.b0 * x + self.z1
        self.z1 = self.b1 * x - self.a1 * y + self.z2
        self.z2 = self.b2 * x - self.a2 * y
        return y

def get_highpass_20hz():
    # 2nd order Butterworth Highpass 20Hz @ 1000Hz Fs
    return BiquadFilter([0.91496914, -1.82993828, 0.91496914], [1.0, -1.82269492, 0.83718165])

def get_notch_50hz():
    # 2nd order IIR Notch 50Hz @ 1000Hz Fs, Q=30
    return BiquadFilter([0.99479123, -1.89220537, 0.99479123], [1.0, -1.89220537, 0.98958247])

def get_lowpass_150hz():
    # 2nd order Butterworth Lowpass 150Hz @ 1000Hz Fs
    return BiquadFilter([0.13110643, 0.26221287, 0.13110643], [1.0, -0.74778917, 0.27221493])


def dsp_receiver_worker():
    global CURRENT_RMS_PCT, IS_CONNECTED, LAST_ALIVE_TS
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_IP, UDP_PORT))
    sock.settimeout(0.5)
    logging.info(f"[*] 🚀 生物电 DSP 流水线挂载完毕: UDP_EMG 无锁监听端口 {UDP_PORT}")
    
    # 实例化滤波器流水线
    hp_filter = get_highpass_20hz()
    notch_filter = get_notch_50hz()
    lp_filter = get_lowpass_150hz()
    
    # 积分包络窗口 (100ms)
    window_size = 100
    rms_ring = deque(maxlen=window_size)
    sum_sq = 0.0
    
    timeout_warn_emitted = False

    while True:
        try:
            data, addr = sock.recvfrom(1024)
            if timeout_warn_emitted:
                logging.info(f"🟢 [恢复] UDP 数据流已于 {addr} 闪电重连。")
                timeout_warn_emitted = False
            
            IS_CONNECTED = True
            LAST_ALIVE_TS = time.time()
            
            if len(data) > 0:
                try:
                    val = float(data.decode('ascii').strip())
                except ValueError:
                    continue
                
                # --- DSP Pipeline ---
                # 1. Highpass (Remove Baseline Wander & DC Offset)
                y1 = hp_filter.process(val)
                # 2. Notch (Remove 50Hz Mains Noise)
                y2 = notch_filter.process(y1)
                # 3. Lowpass (Remove High-Freq Noise)
                y3 = lp_filter.process(y2)
                
                # 4. Rectification & RMS Envelope
                y_sq = y3 * y3
                if len(rms_ring) == window_size:
                    sum_sq -= rms_ring[0]
                rms_ring.append(y_sq)
                sum_sq += y_sq
                
                rms = math.sqrt(max(0, sum_sq) / len(rms_ring))
                
                # 5. Mapping to 0-100% (Empirical scale for 12-bit ADC EMG swings)
                # 满发力时，滤波后振幅 RMS 大约在 200~600 左右
                rms_mapped = min(100, int((rms / 400.0) * 100))
                
                # 引入非常轻微的静息阈值防止数值抖动
                if rms_mapped < 4:
                    rms_mapped = 0
                
                # 更新共享变量供 IO 线程异步读取
                CURRENT_RMS_PCT = rms_mapped

        except socket.timeout:
            if not timeout_warn_emitted:
                logging.warning("🚨 [心跳报警] UDP 长达 500ms 阻断！传感器可能脱落。")
                timeout_warn_emitted = True
            IS_CONNECTED = False
            CURRENT_RMS_PCT = 0
            
        except Exception as e:
            logging.error(f"DSP 流水线异常: {e}")
            time.sleep(1)

def io_dumper_worker():
    """
    低频稳固生产者线程（约 33Hz）
    从 DSP 全局状态中采样平滑后的包络，并进行 JSON 写盘
    """
    while True:
        now = time.time()

        if not IS_CONNECTED:
            # No real sensor data — don't overwrite simulated EMG from vision pipeline
            # Remove heartbeat so vision knows to generate simulated data
            try:
                os.remove('/dev/shm/emg_heartbeat')
            except OSError:
                pass
            time.sleep(0.5)
            continue

        pct = CURRENT_RMS_PCT
        warnings = []

        # 将该通道数据强力扩散至全部四肢反馈，便于观察
        acts = {
            "quadriceps": pct,
            "glutes": pct,
            "calves": pct,
            "biceps": pct
        }
        out = {"activations": acts, "warnings": warnings, "exercise": "squat"}

        try:
            with open('/dev/shm/muscle_activation.json.tmp', 'w') as f:
                json.dump(out, f)
            os.rename('/dev/shm/muscle_activation.json.tmp', '/dev/shm/muscle_activation.json')

            with open('/dev/shm/emg_heartbeat', 'w') as f:
                f.write(str(now))
        except Exception:
            pass

        # 精确下发压控：1000Hz 抽样为 33Hz
        time.sleep(0.03)

if __name__ == "__main__":
    t_recv = threading.Thread(target=dsp_receiver_worker, daemon=True)
    t_dump = threading.Thread(target=io_dumper_worker, daemon=True)
    
    t_recv.start()
    t_dump.start()
    
    t_recv.join()
    t_dump.join()
