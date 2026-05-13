import asyncio
import json
import logging
import os
import struct
from bleak import BleakClient, BleakScanner

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [BLE EMG] - %(message)s')

SERVICE_UUID = "19b10000-e8f2-537e-4f6c-d104768a1214"
CHAR_UUID = "19b10001-e8f2-537e-4f6c-d104768a1214"

WINDOW_SIZE = 50   # 50 采样点滑窗 (50ms @ 1kHz)
NUM_CHANNELS = 4

class EMGProcessor:
    def __init__(self):
        self.history = [[] for _ in range(NUM_CHANNELS)]
        self.sum_hist = [0.0] * NUM_CHANNELS
        self.calibrated = False
        self.dc_offsets = [0.0] * NUM_CHANNELS
        self.min_env = [9999.0] * NUM_CHANNELS
        self.max_env = [1.0] * NUM_CHANNELS
        self.calibration_samples = 0
        self.calib_limit = 500

    def process_packet(self, data: bytes):
        if len(data) != 8:
            return None
        
        vals = struct.unpack('<HHHH', data)
        activations = [0] * NUM_CHANNELS
        
        for i in range(NUM_CHANNELS):
            val = vals[i]
            
            if not self.calibrated:
                self.dc_offsets[i] += val
                self.history[i].append(val)
            else:
                # 均值漂移去直流 + 全波整流
                centered = val - self.dc_offsets[i]
                rectified = abs(centered)
                
                # MAV (Mean Absolute Value) 即滑动绝对平均，平替 RMS 以提升效能
                self.history[i].append(rectified)
                self.sum_hist[i] += rectified
                if len(self.history[i]) > WINDOW_SIZE:
                    old_val = self.history[i].pop(0)
                    self.sum_hist[i] -= old_val
                
                mav = self.sum_hist[i] / len(self.history[i])
                
                # 动态阈值漂移跟踪
                if mav < self.min_env[i]:
                    self.min_env[i] = mav
                # 缓慢衰减 max_env 适应疲劳放松
                self.max_env[i] = max(mav, self.max_env[i] * 0.9995)
                
                m_diff = self.max_env[i] - self.min_env[i]
                if m_diff < 5:
                    m_diff = 5
                
                # 映射 0-100%
                act = ((mav - self.min_env[i]) / m_diff) * 100.0
                activations[i] = int(max(0, min(100, act)))

        if not self.calibrated:
            self.calibration_samples += 1
            if self.calibration_samples >= self.calib_limit:
                for i in range(NUM_CHANNELS):
                    self.dc_offsets[i] /= self.calib_limit
                self.calibrated = True
                self.history = [[] for _ in range(NUM_CHANNELS)]
                logging.info(f"✅ 基线偏移已校准: {[round(x, 1) for x in self.dc_offsets]}")
            return None

        return activations

async def scan_and_connect():
    processor = EMGProcessor()
    
    def notification_handler(sender, data):
        activations = processor.process_packet(data)
        if activations:
            try:
                # 高频写入共享内存
                with open("/dev/shm/emg_state.json.tmp", "w", encoding="utf-8") as f:
                    json.dump({"activations": activations}, f)
                os.rename("/dev/shm/emg_state.json.tmp", "/dev/shm/emg_state.json")
            except Exception:
                pass

    while True:
        try:
            logging.info("🔍 正在扫描 ESP32 EMG 设备...")
            devices = await BleakScanner.discover(timeout=3.0)
            target_device = None
            for d in devices:
                if d.name and "EMG" in d.name:  # 假设 Worker1 命名含有 EMG
                    target_device = d
                    break
                
            if not target_device:
                # 备用：按服务 UUID 扫描
                for d in devices:
                    if SERVICE_UUID in d.metadata.get("uuids", []):
                        target_device = d
                        break

            if target_device:
                logging.info(f"🔗 发现目标设备 {target_device.address}，尝试连接...")
                async with BleakClient(target_device.address) as client:
                    logging.info(f"✅ 连接成功！开始订阅 GATT Notify: {CHAR_UUID}")
                    await client.start_notify(CHAR_UUID, notification_handler)
                    
                    # 持续监听直到断开
                    while await client.is_connected():
                        await asyncio.sleep(1)
            else:
                logging.warning("⚠️ 未发现传感器设备，重试中...")
                await asyncio.sleep(2)
                
        except Exception as e:
            logging.error(f"❌ 蓝牙连接异常: {e}")
            await asyncio.sleep(3)

if __name__ == "__main__":
    try:
        asyncio.run(scan_and_connect())
    except KeyboardInterrupt:
        logging.info("🛑 BLE 服务主动终止")
