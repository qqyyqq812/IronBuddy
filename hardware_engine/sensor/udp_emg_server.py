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
# 双通道: ch0=目标肌肉(股四头肌/肱二头肌), ch1=代偿肌肉(臀肌/背阔肌)
CURRENT_RMS_PCT = [0, 0]
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
    
    # 双通道 DSP 流水线
    filters = []
    for _ in range(2):
        filters.append({
            'hp': get_highpass_20hz(),
            'notch': get_notch_50hz(),
            'lp': get_lowpass_150hz(),
            'ring': deque(maxlen=100),
            'sum_sq': 0.0,
        })

    timeout_warn_emitted = False
    pkt_count = 0

    while True:
        try:
            data, addr = sock.recvfrom(1024)
            if timeout_warn_emitted:
                logging.info(f"🟢 [恢复] UDP 数据流已于 {addr} 闪电重连。")
                timeout_warn_emitted = False

            IS_CONNECTED = True
            LAST_ALIVE_TS = time.time()

            if len(data) == 0:
                continue

            raw = data.decode('ascii').strip()
            parts = raw.split()

            # 支持: "123.4 567.8"(双通道) 或 "123.4"(单通道兼容)
            vals = []
            for p in parts:
                try:
                    vals.append(float(p))
                except ValueError:
                    pass

            if not vals:
                continue

            # 首包打日志
            pkt_count += 1
            if pkt_count == 1:
                logging.info(f"📡 首包来自 {addr}: [{raw}] → {len(vals)}通道")

            # 对每个通道走 DSP
            for ch in range(min(len(vals), 2)):
                f = filters[ch]
                val = vals[ch]
                y1 = f['hp'].process(val)
                y2 = f['notch'].process(y1)
                y3 = f['lp'].process(y2)

                y_sq = y3 * y3
                if len(f['ring']) == f['ring'].maxlen:
                    f['sum_sq'] -= f['ring'][0]
                f['ring'].append(y_sq)
                f['sum_sq'] += y_sq

                rms = math.sqrt(max(0, f['sum_sq']) / max(1, len(f['ring'])))
                rms_mapped = min(100, int((rms / 400.0) * 100))
                if rms_mapped < 4:
                    rms_mapped = 0
                CURRENT_RMS_PCT[ch] = rms_mapped

            # 单通道时,两个通道都用同一个值
            if len(vals) == 1:
                CURRENT_RMS_PCT[1] = CURRENT_RMS_PCT[0]

        except socket.timeout:
            if not timeout_warn_emitted:
                logging.warning("🚨 [心跳报警] UDP 长达 500ms 阻断！传感器可能脱落。")
                timeout_warn_emitted = True
            IS_CONNECTED = False
            CURRENT_RMS_PCT[0] = 0
            CURRENT_RMS_PCT[1] = 0

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

        target_pct = CURRENT_RMS_PCT[0]  # ch0: 目标肌肉
        comp_pct   = CURRENT_RMS_PCT[1]  # ch1: 代偿肌肉
        warnings = []

        # 读当前运动类型决定映射
        exercise = "squat"
        try:
            if os.path.exists('/dev/shm/user_profile.json'):
                with open('/dev/shm/user_profile.json', 'r') as f:
                    exercise = json.load(f).get('exercise', 'squat')
        except Exception:
            pass

        if exercise == "bicep_curl":
            acts = {
                "quadriceps": comp_pct,
                "glutes": comp_pct,
                "calves": 0,
                "biceps": target_pct
            }
        else:
            acts = {
                "quadriceps": target_pct,
                "glutes": target_pct,
                "calves": 0,
                "biceps": comp_pct
            }
        out = {"activations": acts, "warnings": warnings, "exercise": exercise}

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
