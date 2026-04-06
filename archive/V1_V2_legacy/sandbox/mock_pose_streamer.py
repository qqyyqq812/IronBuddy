"""
模拟人类深蹲轨迹的虚拟 NPU (Mock NPU IPC)
由于靶板断连，该脚本将在本地宿主机生成极速的假 `/dev/shm/pose_data.json` 与 `/dev/shm/result.jpg`，
用来驱动并验证刚写好的 `pose_subscriber.py` 判定组件。
"""
import os
import json
import time
import math
import cv2
import numpy as np

SHM_PATH = "/dev/shm"

def ensure_mock_env():
    global SHM_PATH
    # 为了在 WSL2 或 Docker 里模拟 /dev/shm 内存盘
    if not os.path.exists(SHM_PATH):
        try:
            os.makedirs(SHM_PATH, exist_ok=True)
            print("🚨 提示：正在非标准 Linux 的环境里挂载伪装的 /dev/shm。")
        except Exception:
            SHM_PATH = "/tmp"

def generate_mock_pose(frame_index):
    """
    用简单的正弦波伪装人腿部的弯曲过程。
    假设人每 100 帧完成一次深蹲 (29FPS，一次全过程约 3~4 秒)。
    这里主要模拟右边: 髋(12), 膝(14), 踝(16)。但在 `pose_subscriber` 读的是右边还是左边？
    `exercises.json` 配置为 {hip: 11, knee: 13, ankle: 15}，即 COCO 的左侧。
    """
    HIP_IDX = 11
    KNEE_IDX = 13
    ANKLE_IDX = 15
    
    # 周期 100帧。0-50下蹲，50-100起立。
    # 站立：夹角接近 170°，半蹲：约 90°，深蹲：约 70°
    progress = (math.cos(frame_index * math.pi / 50) + 1) / 2  # 0->1->0 的波峰
    angle = 75 + 100 * progress # 从 175度 -> 75度 -> 175度
    
    # 将角度反算为三维坐标（极简化：固定 髋(100, 100), 踝(100, 300)）
    # 膝盖的 x,y 会由于角度改变而偏移
    hip = [100.0, 100.0, 0.9]
    ankle = [100.0, 300.0, 0.9]
    
    # 假设大腿小腿等长 (L=100)
    L = 100.0
    # 根据余弦定理反推 knee 的坐标：膝盖往 x 的反方向（身后）突出
    # 这里为了快一点，我们可以直接写一个定式。不过也可以直接传入 angle 让 angle_calculator 反过来算。
    # 夹角 α。如果髋是原点
    alpha = np.radians(angle) / 2
    knee_x = 100.0 - L * math.sin(alpha)
    knee_y = 100.0 + L * math.cos(alpha)
    knee = [knee_x, knee_y, 0.85]
    
    # 生成 17 个关键点的默认空壳
    kpts = [[0.0, 0.0, 0.0] for _ in range(17)]
    kpts[HIP_IDX] = hip
    kpts[KNEE_IDX] = knee
    kpts[ANKLE_IDX] = ankle
    
    # 为了测试动作是否标准，我们在某几个循环制造"下不去"的姿态。
    # 例如：每第三次深蹲（frame_index > 200 && < 300）只蹲到 100度。
    if 200 <= frame_index < 300:
        angle_bad = 105 + 70 * progress
        beta = np.radians(angle_bad) / 2
        kpts[KNEE_IDX] = [100.0 - L * math.sin(beta), 100.0 + L * math.cos(beta), 0.85]
    
    payload = {
        "source": "mock_engine",
        "objects": [
            {
                "id": 1,
                "score": 0.99,
                "bbox": [50.0, 50.0, 100.0, 250.0],
                "kpts": kpts
            }
        ]
    }
    return payload

def start_mocking():
    ensure_mock_env()
    print("🎬 开始播送虚拟用户的深蹲轨迹至内存盘！")
    
    frame_idx = 0
    img_canvas = np.zeros((480, 640, 3), dtype=np.uint8)
    
    json_path = os.path.join(SHM_PATH, "pose_data.json")
    img_path = os.path.join(SHM_PATH, "result.jpg")
    
    while True:
        # 每秒 30 帧速度抛投
        time.sleep(1/30.0)
        
        # 1. 投射假图片给 Web 流
        cv2.putText(img_canvas, f"MOCK FRAME: {frame_idx}", (10, 30), 
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
        cv2.imwrite(img_path, img_canvas)
        img_canvas.fill(0) # 清屏准备下一帧
        
        # 2. 投射假骨骼点给我们新造的业务逻辑引擎
        pose_data = generate_mock_pose(frame_idx)
        # 保证原子性
        with open(json_path + ".tmp", "w") as f:
            json.dump(pose_data, f)
        os.rename(json_path + ".tmp", json_path)
        
        # 给终端报个数
        if frame_idx % 30 == 0:
            print(f"> 已平滑发送 Mock NPU 帧: {frame_idx}")
            
        frame_idx += 1
        
        # 防止死循环太多，跑够 10 秒自动退出（刚好做 3次半深蹲）
        if frame_idx > 300:
            print("Mock 数据发送完毕！")
            break

if __name__ == "__main__":
    start_mocking()
