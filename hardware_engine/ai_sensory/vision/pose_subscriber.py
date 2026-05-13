"""
轻量级姿态评判引擎 (Agent 2 IPC Microservice)
专责监听 Linux /dev/shm/pose_data.json，执行快速数学运算，并吐出动作结果。
"""
import os
import json
import time
import sys
from pathlib import Path
import numpy as np

# 获取本脚本所在目录并注入 path，保证 angle_calculator 顺利导入
current_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(current_dir))
from angle_calculator import calc_angle, RepCounter

def load_exercise_config(exercise_name: str) -> dict:
    """从 config/exercises.json 加载指标配置"""
    config_path = current_dir.parent.parent / "config" / "exercises.json"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            configs = json.load(f)
            return configs.get(exercise_name)
    except Exception as e:
        print(f"[Pose Subscriber] 无法加载配置文件: {e}")
        return None

def run_subscriber(exercise="squat"):
    print(f"🚀 [Pose Subscriber] 微服务已启动！监控动作: {exercise}")
    
    config = load_exercise_config(exercise)
    if not config:
        return
        
    kp_indices = config["keypoints"]
    kp_keys = list(kp_indices.keys())
    idx_a, idx_b, idx_c = kp_indices[kp_keys[0]], kp_indices[kp_keys[1]], kp_indices[kp_keys[2]]
    
    counter = RepCounter(
        threshold_down=config["threshold_down"],
        threshold_up=config["threshold_up"],
        hold_frames=config.get("hold_frames", 3)
    )

    POSE_DATA_PATH = "/dev/shm/pose_data.json"
    STATUS_OUT_PATH = "/dev/shm/squat_status.json"
    
    last_mtime = 0
    
    while True:
        # 匹配 C++ 29FPS 的高频写入，设置 0.03s (30Hz) 的非空转挂起
        time.sleep(0.03)
        
        if not os.path.exists(POSE_DATA_PATH):
            continue
            
        try:
            mtime = os.path.getmtime(POSE_DATA_PATH)
            if mtime == last_mtime:
                continue
            last_mtime = mtime
            
            with open(POSE_DATA_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            objects = data.get("objects", [])
            if not objects:
                continue
                
            # 简化版：永远提取画面里的第一个人的骨架数据
            person = objects[0]
            kpts = person.get("kpts", [])
            if len(kpts) < 17:
                continue
                
            # Numpy 化张量
            kpts_xy = np.array([[pt[0], pt[1]] for pt in kpts])
            kpts_conf = np.array([pt[2] for pt in kpts])
            
            # 判断关键置信度是否足以支撑评判
            if (kpts_conf[idx_a] > 0.5 and 
                kpts_conf[idx_b] > 0.5 and 
                kpts_conf[idx_c] > 0.5):
                
                pt_a = kpts_xy[idx_a]
                pt_b = kpts_xy[idx_b]
                pt_c = kpts_xy[idx_c]
                
                # 开始快速向量计算
                angle = calc_angle(pt_a, pt_b, pt_c)
                state_info = counter.update(angle)
                
                # 序列化此帧的结果并旁路投递给前端与大模型中枢
                out_payload = {
                    "exercise": exercise,
                    "count": state_info["count"],
                    "state": state_info["state"],
                    "is_good_form": state_info["is_good_form"],
                    "angle": round(angle, 1),
                    "just_completed": state_info["just_completed"],
                    "timestamp": time.time()
                }
                
                # 原子写入到状态节点
                with open(STATUS_OUT_PATH + ".tmp", "w", encoding="utf-8") as fout:
                    json.dump(out_payload, fout, ensure_ascii=False)
                os.rename(STATUS_OUT_PATH + ".tmp", STATUS_OUT_PATH)

        except json.JSONDecodeError:
            pass # 文件写入一半被截断，跳过此帧
        except Exception as e:
            # print(f"[Pose Subscriber] Runtime error: {e}")
            pass

if __name__ == "__main__":
    run_subscriber()
