import os
import json
import time
import csv
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [SYNC PROBE] %(message)s')

def run_probe():
    FSM_FILE = "/dev/shm/fsm_state.json"
    EMG_FILE = "/dev/shm/muscle_activation.json"
    OUT_CSV = "/home/toybrick/timing_correlation_log.csv"
    
    # 因为跑在 WSL/宿主机上测试，如果当前不是板子环境而是 WSL 挂载目录，需要改写成相对目录
    if not os.path.isdir("/dev/shm"):
        FSM_FILE = "./fsm_state.json"
        EMG_FILE = "./muscle_activation.json"
        OUT_CSV = "./timing_correlation_log.csv"
    
    logging.info(f"🚀 初始化底层毫秒级同步探针。测算输出文件: {OUT_CSV}")
    
    # 写入 CSV 表头
    try:
        with open(OUT_CSV, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp_ms", 
                "fsm_state", 
                "fsm_angle", 
                "emg_quadriceps", 
                "emg_glutes", 
                "emg_calves", 
                "emg_biceps"
            ])
    except Exception as e:
        logging.error(f"无法初始化输出文件: {e}")
        return

    logging.info("⏳ 探针已就绪，保持 50Hz (20ms) 轮询抓取状态...")
    
    try:
        while True:
            t_ms = int(time.time() * 1000)
            
            # 读取 FSM 侧
            state = "NO_PERSON"
            angle = -1.0
            try:
                if os.path.exists(FSM_FILE):
                    with open(FSM_FILE, 'r') as f:
                        fsm_d = json.load(f)
                        state = fsm_d.get("state", "NO_PERSON")
                        angle = fsm_d.get("angle", -1.0)
            except Exception:
                pass
            
            # 读取 Raw EMG 侧
            q, g, c, b = 0, 0, 0, 0
            try:
                if os.path.exists(EMG_FILE):
                    with open(EMG_FILE, 'r') as f:
                        emg_d = json.load(f)
                        acts = emg_d.get("activations", {})
                        q = acts.get("quadriceps", 0)
                        g = acts.get("glutes", 0)
                        c = acts.get("calves", 0)
                        b = acts.get("biceps", 0)
            except Exception:
                pass
                
            # 落盘
            try:
                with open(OUT_CSV, 'a', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([t_ms, state, angle, q, g, c, b])
            except Exception:
                pass
                
            # 20ms 一次轮询
            time.sleep(0.02)
            
    except KeyboardInterrupt:
        logging.info("🛑 探针侦测退出，数据记录完毕。")

if __name__ == "__main__":
    run_probe()
