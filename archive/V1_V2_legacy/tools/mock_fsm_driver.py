#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import time
import json
import math

FSM_FILE = "/dev/shm/fsm_state.json"
MUSCLE_FILE = "/dev/shm/muscle_activation.json"

def write_json(path, data):
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.rename(tmp_path, path)
    except Exception as e:
        print(f"写入失败: {e}")

print("🚀 Mock FSM Driver 启动，正在往 /dev/shm 灌入深蹲数据...")

t = 0.0
dt = 0.1
rep_count = 0
prev_state = "IDLE"

while True:
    try:
        # 使用正弦波模拟深蹲动作 (周期约4秒)
        # 角度范围：180 (站立) 到 70 (深蹲底端)
        phase = (t % 4.0) / 4.0  # 0 to 1
        
        # 为了让停顿真实，我们在顶部(180)和底部(70)都有一定的驻留
        raw_sin = math.sin(phase * 2 * math.pi)  # -1 to 1
        
        # 映射到 180 -> 70 -> 180
        # 当 raw_sin = 1 (phase 0.25) 时为 70 (蹲到底)
        # 当 raw_sin = -1 (phase 0.75) 时为 180 (站立)
        mapped = 125 - 55 * raw_sin 
        
        angle = round(mapped)
        
        # 状态判定
        if angle > 165:
            state = "IDLE"
        elif angle < 85:
            state = "BOTTOM"
        else:
            # 根据趋势判断上下
            if raw_sin > 0 and phase < 0.5:
                state = "DESCENDING"
            else:
                state = "ASCENDING"
                
        # 计数逻辑
        if prev_state != "IDLE" and state == "IDLE":
            rep_count += 1
            
        prev_state = state
        
        # 肌肉张力模拟：角度越小（蹲得越深），激活度越高
        squat_depth_ratio = max(0, (180 - angle) / 110.0) # 0 to 1
        
        quad = int(squat_depth_ratio * 90 + 10)
        glute = int(squat_depth_ratio * 85 + 5)
        ham = int(squat_depth_ratio * 60 + 10)
        calf = int(squat_depth_ratio * 30 + 10)
        core = int(squat_depth_ratio * 40 + 20)
        
        fsm_data = {
            "state": state,
            "good": rep_count,
            "failed": 0,
            "angle": angle,
            "chat_active": False
        }
        
        muscle_data = {
            "activations": {
                "quadriceps": quad,
                "glutes": glute,
                "hamstrings": ham,
                "calves": calf,
                "erector_spinae": core,
                "abs": core
            },
            "warnings": [],
            "exercise": "squat",
            "rep_count": rep_count
        }
        
        if angle < 75:
            muscle_data["warnings"].append("膝盖张力过大")
            muscle_data["flash"] = ["quadriceps", "glutes"]
            
        write_json(FSM_FILE, fsm_data)
        write_json(MUSCLE_FILE, muscle_data)
        
        time.sleep(dt)
        t += dt
        
    except KeyboardInterrupt:
        print("\n退出 mock 驱动")
        break
