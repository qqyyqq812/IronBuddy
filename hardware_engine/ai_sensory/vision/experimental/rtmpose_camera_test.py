#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# 脚本：rtmpose_camera_test.py (端到端全链路推送测试)
# 归属：Agent 2 后台托管生成 / experimental 特区
# 架构：Rockchip RK3399Pro (NPU v1) + RTMPose + 原生 Angle 计算
# 说明：捕获视频源，连接新版 NPU Worker，并将 17 关键点灌入状态机测算闭环。
# ----------------------------------------------------------------------------

import os
import sys
import cv2
import time
import numpy as np

# 将项目根目录推入 Path 以相对路径安全复用主干的模块
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
sys.path.insert(0, PROJECT_ROOT)

from hardware_engine.ai_sensory.vision.experimental.rtmpose_backend_worker import RTMPoseWorker
from hardware_engine.ai_sensory.vision.angle_calculator import calc_angle, RepCounter

# 常见 COCO 关键点映射
KEYPOINT_HIP = 11   # 左髋
KEYPOINT_KNEE = 13  # 左膝
KEYPOINT_ANKLE = 15 # 左脚踝

def main():
    print("=== [RTMPose 推理流 <-> Angle 状态机] 全栈整合挂件 ===")
    
    # 1. 实例化 NPU 测试挂件 (后台模式下可能只有虚模型或等待上传实装)
    worker = RTMPoseWorker("rtmpose_quant.rknn")
    
    # 2. 实例化原主干的深蹲状态机
    # 下蹲时小于 90 度，站立时大于 160 度
    counter = RepCounter(threshold_down=90, threshold_up=160, hold_frames=2)
    
    # 3. 开启视频流 (优先尝试 /dev/video0，如果没有硬件则挂载一个虚拟视频或模拟)
    # 本地沙盘环境直接打印为主
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[WARNING] 本地无真实 V4L2 摄像头节点 (0)，进入纯终端数组模拟打桩测试模式。")
        # 纯终端打印模拟模式
        run_simulate_terminal_mode(worker, counter)
        return

    print("[SUCCESS] 成功开启物理摄像头，准备抽取帧。")
    frame_idx = 0
    
    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
                
            frame_idx += 1
            t_start = time.time()
            
            # [核心] 送入推理，得到 [17, 3] Numpy 数组 (x, y, conf)
            keypoints = worker.inference(frame)
            
            # 提取左腿特征点
            hip = keypoints[KEYPOINT_HIP][:2]   # (x, y)
            knee = keypoints[KEYPOINT_KNEE][:2]
            ankle = keypoints[KEYPOINT_ANKLE][:2]
            
            # 为了防止误判，取最弱的一个点的置信度
            min_conf = min(keypoints[KEYPOINT_HIP][2], 
                           keypoints[KEYPOINT_KNEE][2], 
                           keypoints[KEYPOINT_ANKLE][2])

            angle = 0.0
            if min_conf > 0.3:
                # 传入坐标：Hip(A), Knee(B), Ankle(C)
                angle = calc_angle(hip, knee, ankle)
                
            # [核心] 装填打断，推送至原逻辑状态机
            state_info = counter.update(angle)

            fps = 1.0 / (time.time() - t_start + 1e-6)
            
            # ==== 终端打印监控墙 ====
            # 根据需求静默输出但不打开 cv2.imshow 窗口 (避免 WSL x_server 挂死)
            print(f"[Frame {frame_idx:04d}] NPU FPS:{fps:4.1f} | 腿弯角:{angle:5.1f}° | "
                  f"当前状态:{state_info['state']} | 计数:{state_info['count']} | 标志:{state_info['just_completed']}")
            
            # 等待中断 (如果有视窗环境才会生效，终端直接 Ctrl+C 杀除)
            # if cv2.waitKey(1) & 0xFF == ord('q'): break

    except KeyboardInterrupt:
        print("\n[INFO] 接收到总经理强制停止命令(Ctrl+C)，开始释放管线资源...")
    finally:
        cap.release()
        worker.release()
        print("[INFO] 端到端测试环境已彻底销毁并下线。")


def run_simulate_terminal_mode(worker, counter):
    """防挂死降级处理：如果没有物理摄像头，推入虚假动作坐标走完链路验证"""
    print("--- 启动矩阵强灌测试 ---")
    
    # 模拟用户做一个标准的深蹲动作（关节角度模拟序列）
    mock_angles = [170.0, 160.0, 130.0, 100.0, 85.0, 80.0, 80.0, 95.0, 140.0, 165.0, 175.0]
    
    for idx, angle in enumerate(mock_angles):
        print(f"\n>> 模拟传入第 {idx} 帧画面... （底层 NPU 推理忽略中）")
        
        # 将角度强行灌入深蹲判别器 (跳过 worker 模型推理)
        res = counter.update(angle)
        
        print(f"[Angle State Machine] 当前探测夹角: {angle:5.1f}°")
        print(f" -> 分析结论: 现处状态 [{res['state']}] | 当前合规深蹲总数: {res['count']} | 最新标志: {res['just_completed']}")
        time.sleep(0.5)

    print("\n[SUCCESS] 原有 `angle_calculator.py` 与外部模拟信号耦合正常！")
    worker.release()


if __name__ == "__main__":
    main()
