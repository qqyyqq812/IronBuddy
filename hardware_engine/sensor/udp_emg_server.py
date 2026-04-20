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

# ===== V4.6 硬件域对齐（Hardware Domain Alignment）=====
# 廉价 ESP32 硬件 → MIA (Delsys) 信号域的线性校准：MIA_signal = alpha * User_signal + beta
# 由 tools/hardware_domain_calibrate.py 产出 JSON；缺失时 graceful 降级恒等变换
_DOMAIN_CALIB = {'target': (1.0, 0.0), 'comp': (1.0, 0.0)}  # (alpha, beta)
_CALIB_JSON = os.path.join(os.path.dirname(__file__), 'domain_calibration.json')
try:
    with open(_CALIB_JSON, 'r') as _cf:
        _cd = json.load(_cf)
        _m = _cd.get('calibration', {}).get('method_primary', 'stretch')
        _c = _cd['calibration'][_m]
        _DOMAIN_CALIB['target'] = (float(_c['target']['alpha']), float(_c['target']['beta']))
        _DOMAIN_CALIB['comp']   = (float(_c['comp']['alpha']),   float(_c['comp']['beta']))
    logging.info(
        '[DOMAIN_CALIB] 加载 (method=%s): target α=%.3f β=%+.3f, comp α=%.3f β=%+.3f',
        _m, _DOMAIN_CALIB['target'][0], _DOMAIN_CALIB['target'][1],
            _DOMAIN_CALIB['comp'][0],   _DOMAIN_CALIB['comp'][1])
except (IOError, OSError, ValueError, KeyError) as _e:
    logging.warning('[DOMAIN_CALIB] 未加载 %s (%s)，使用恒等变换', _CALIB_JSON, _e)

# ===== V4.7 动态 MVC 校准（Hardware-level individual MVC）=====
# 默认回退 400（保持 V4.6 之前硬编码行为）；若 mvc_values.json 存在且值合理（50-2000），则使用个体化值
_MVC_VALUES = {"target": 400.0, "comp": 400.0}
_MVC_JSON = os.path.join(os.path.dirname(__file__), 'mvc_values.json')
if os.path.exists(_MVC_JSON):
    try:
        with open(_MVC_JSON, 'r') as _mf:
            _md = json.load(_mf)
        for _k in ('target', 'comp'):
            _v = float(_md.get(_k, 400.0))
            if 50.0 <= _v <= 2000.0:
                _MVC_VALUES[_k] = _v
        logging.info('[MVC] 加载 mvc_values.json target=%.1f comp=%.1f',
                     _MVC_VALUES['target'], _MVC_VALUES['comp'])
    except Exception as _e:
        logging.warning('[MVC] mvc_values.json 加载失败，回退 400: %s', _e)

# MVC 动态校准运行时状态
# request_path: /dev/shm/mvc_calibrate.request  (外部触发)
# result_path:  /dev/shm/mvc_calibrate.result   (完成后写回给 API)
_MVC_REQUEST_PATH = '/dev/shm/mvc_calibrate.request'
_MVC_RESULT_PATH = '/dev/shm/mvc_calibrate.result'
_MVC_CAL_WINDOW_SEC = 3.0   # 3 秒采集窗口
_MVC_MIN_RMS = 50.0         # 下限钳位（防止空气测值）
_MVC_MAX_RMS = 2000.0       # 上限钳位（防止干扰尖峰）

# 校准状态（由 DSP 线程读写；io_dumper 线程触发/收尾）
_mvc_cal_active = False        # True 时 DSP 线程记录峰值
_mvc_cal_start_ts = 0.0
_mvc_cal_peak = [0.0, 0.0]     # (target_peak_rms, comp_peak_rms) 原始 rms 值（非百分比）

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
                # V4.6: 归一化 → 硬件域对齐 → clip
                ch_key = 'target' if ch == 0 else ('comp' if ch == 1 else None)
                # V4.7: 个体化 MVC 分母（fallback 400）
                _mvc_base = _MVC_VALUES.get(ch_key if ch_key is not None else 'target', 400.0)
                rms_raw_pct = (rms / _mvc_base) * 100.0
                if ch_key is not None:
                    _a, _b = _DOMAIN_CALIB[ch_key]
                    rms_raw_pct = _a * rms_raw_pct + _b  # 映射到 MIA Delsys 域
                rms_mapped = max(0, min(100, int(round(rms_raw_pct))))
                if rms_mapped < 4:
                    rms_mapped = 0
                CURRENT_RMS_PCT[ch] = rms_mapped

                # V4.7: 动态 MVC 校准 —— 3 秒窗口内记录 rms 峰值（原始未归一化）
                if _mvc_cal_active and ch_key is not None:
                    _idx = 0 if ch_key == 'target' else 1
                    if rms > _mvc_cal_peak[_idx]:
                        _mvc_cal_peak[_idx] = rms

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

def _check_mvc_request():
    """V4.7: 检测 MVC 动态校准请求并驱动 3 秒峰值采集状态机。
    由 io_dumper_worker 以 33Hz 调用（开销极小）。

    状态机：
        idle → request 文件出现 → 进入 active（记录 start_ts，清零 peak）
        active → 经过 _MVC_CAL_WINDOW_SEC 秒 → 封口写 json + result + 热更新
    """
    global _mvc_cal_active, _mvc_cal_start_ts
    now = time.time()

    # 1) 未在采集 → 检测 request 文件
    if not _mvc_cal_active:
        if os.path.exists(_MVC_REQUEST_PATH):
            try:
                os.remove(_MVC_REQUEST_PATH)
            except OSError:
                pass
            # 清理旧 result（避免 API 端读到上一次结果）
            try:
                os.remove(_MVC_RESULT_PATH)
            except OSError:
                pass
            _mvc_cal_peak[0] = 0.0
            _mvc_cal_peak[1] = 0.0
            _mvc_cal_start_ts = now
            _mvc_cal_active = True
            logging.info('[MVC] 收到校准请求，启动 %.1fs 峰值采集窗口', _MVC_CAL_WINDOW_SEC)
        return

    # 2) 采集中 → 未到期则直接返回
    if now - _mvc_cal_start_ts < _MVC_CAL_WINDOW_SEC:
        return

    # 3) 到期 → 封口
    peak_target = _mvc_cal_peak[0]
    peak_comp = _mvc_cal_peak[1]
    # 钳位到合理区间，避免异常值污染 json
    peak_target = max(_MVC_MIN_RMS, min(_MVC_MAX_RMS, peak_target)) if peak_target > 0 else 400.0
    peak_comp = max(_MVC_MIN_RMS, min(_MVC_MAX_RMS, peak_comp)) if peak_comp > 0 else 400.0

    # 热更新内存值（下一拍 DSP 立即使用新分母）
    _MVC_VALUES['target'] = float(peak_target)
    _MVC_VALUES['comp'] = float(peak_comp)

    # 落盘 mvc_values.json（下次启动即加载）
    try:
        tmp = _MVC_JSON + '.tmp'
        with open(tmp, 'w') as _wf:
            json.dump({
                'target': float(peak_target),
                'comp': float(peak_comp),
                'ts': now,
                'window_sec': _MVC_CAL_WINDOW_SEC,
            }, _wf)
        os.rename(tmp, _MVC_JSON)
    except Exception as _e:
        logging.error('[MVC] 写入 mvc_values.json 失败: %s', _e)

    # 写 result 文件供 API 轮询读取
    try:
        with open(_MVC_RESULT_PATH, 'w') as _rf:
            json.dump({
                'target': float(peak_target),
                'comp': float(peak_comp),
                'ts': now,
            }, _rf)
    except Exception as _e:
        logging.error('[MVC] 写入 result 失败: %s', _e)

    logging.info('[MVC] 校准完成 target=%.1f comp=%.1f', peak_target, peak_comp)
    _mvc_cal_active = False


def io_dumper_worker():
    """
    低频稳固生产者线程（约 33Hz）
    从 DSP 全局状态中采样平滑后的包络，并进行 JSON 写盘
    """
    while True:
        now = time.time()

        # V4.7: 每拍检查一次 MVC 校准请求（幂等且极廉价）
        try:
            _check_mvc_request()
        except Exception as _e:
            logging.error('[MVC] _check_mvc_request 异常: %s', _e)

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

            # V7.23: 心跳改原子 JSON + atomic rename, 避免 vision 进程读到残缺时戳或竞态
            _hb_tmp = '/dev/shm/emg_heartbeat.tmp'
            with open(_hb_tmp, 'w') as f:
                json.dump({"ts": now, "connected": True}, f)
            os.rename(_hb_tmp, '/dev/shm/emg_heartbeat')
        except Exception:
            pass

        # V7.15: 下发压控从 33Hz 提到 50Hz (0.03→0.02), 让 UI 肌电曲线更流畅
        time.sleep(0.02)

if __name__ == "__main__":
    t_recv = threading.Thread(target=dsp_receiver_worker, daemon=True)
    t_dump = threading.Thread(target=io_dumper_worker, daemon=True)
    
    t_recv.start()
    t_dump.start()
    
    t_recv.join()
    t_dump.join()
