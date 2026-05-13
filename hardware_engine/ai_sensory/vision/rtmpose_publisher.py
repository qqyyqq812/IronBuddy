#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rtmpose_publisher.py
────────────────────
Vision main loop for IronBuddy.

Modes
─────
  Local NPU (default)
    python rtmpose_publisher.py

  Cloud GPU via HTTP (AutoDL RTX 5090)
    python rtmpose_publisher.py --cloud
    # or:
    USE_CLOUD_RTMPOSE=1 python rtmpose_publisher.py

  When --cloud is active the script delegates entirely to cloud_rtmpose_client.main()
  which handles camera capture, HTTP inference, SHM writes, and NPU fallback internally.
  The local code below is only used in NPU mode.
"""
import argparse
import os
import sys
import time
import json
import cv2
import math
import numpy as np

# 安全引入本端与其他文件夹内的模块
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../../"))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# 动态引入（容忍非 RK3399 宿主环境缺失依赖）
try:
    from hardware_engine.ai_sensory.vision.experimental.rtmpose_backend_worker import RTMPoseWorker
except ImportError as e:
    print(f"[RTMPose Publisher] 载入后端 Worker 失败, 可能丢失模型依赖: {e}")
    RTMPoseWorker = None

from hardware_engine.ai_sensory.vision.filters import PoseSmoother

SHM_POSE_JSON = "/dev/shm/pose_data.json"
SHM_RESULT_JPG = "/dev/shm/result.jpg"
SHM_EMG_JSON = "/dev/shm/muscle_activation.json"
JPEG_QUALITY = 65


def _compute_angle(a, b, c):
    """Compute angle at point b given points a, b, c."""
    ba = [a[0] - b[0], a[1] - b[1]]
    bc = [c[0] - b[0], c[1] - b[1]]
    dot = ba[0]*bc[0] + ba[1]*bc[1]
    mag_ba = math.sqrt(ba[0]**2 + ba[1]**2)
    mag_bc = math.sqrt(bc[0]**2 + bc[1]**2)
    if mag_ba * mag_bc == 0:
        return 180.0
    cos_a = max(-1.0, min(1.0, dot / (mag_ba * mag_bc)))
    return math.degrees(math.acos(cos_a))


def _generate_emg_from_angle(angle, exercise="squat"):
    """Generate simulated EMG data synchronized with pose angle.
    Temporary: will be replaced by real sensor data later."""
    import random
    noise = lambda: random.uniform(-3, 3)

    if exercise == "squat":
        if angle < 140:
            depth_factor = (140 - angle) / 70.0  # 0 at 140, 1 at 70
            target_glute = min(100, 40 + depth_factor * 55 + noise())
            comp_back = min(100, 10 + depth_factor * 20 + noise())
            quad = min(100, 50 + depth_factor * 40 + noise())
            calf = min(100, 15 + depth_factor * 15 + noise())
        else:
            target_glute = max(0, 5 + noise())
            comp_back = max(0, 3 + noise())
            quad = max(0, 8 + noise())
            calf = max(0, 3 + noise())
    else:  # bicep_curl
        if angle < 140:
            contraction = (140 - angle) / 90.0
            target_glute = max(0, 3 + noise())  # not used for curl
            comp_back = max(0, 5 + contraction * 10 + noise())
            quad = max(0, 3 + noise())
            calf = min(100, 60 + contraction * 35 + noise())  # maps to biceps
        else:
            target_glute = max(0, 2 + noise())
            comp_back = max(0, 2 + noise())
            quad = max(0, 2 + noise())
            calf = max(0, 5 + noise())

    return {
        "activations": {
            "quadriceps": round(max(0, quad), 1),
            "glutes": round(max(0, target_glute), 1),
            "calves": round(max(0, calf), 1),
            "biceps": round(max(0, comp_back), 1)
        },
        "warnings": [],
        "exercise": exercise,
        "simulated": True
    }


def _write_emg_json(data):
    """Write simulated EMG to shared memory (atomic rename)."""
    try:
        target = SHM_EMG_JSON if os.path.exists("/dev/shm") else "/tmp/muscle_activation.json"
        with open(target + ".tmp", "w") as f:
            json.dump(data, f)
        os.rename(target + ".tmp", target)
    except Exception:
        pass

def draw_skeleton(img, kpts):
    """
    在帧上画出带颜色的火柴人，支持多色展示并高亮左腿关节供调试。
    """
    skeleton = [
        (15, 13), (13, 11), (16, 14), (14, 12), (11, 12),
        (5, 11), (6, 12), (5, 6), (5, 7), (6, 8), (7, 9), (8, 10),
        (1, 2), (0, 1), (0, 2), (1, 3), (2, 4), (3, 5), (4, 6)
    ]
    
    # 画关节
    for i, pt in enumerate(kpts):
        x, y, conf = pt
        if conf > 0.1:
            color = (0, 255, 0)
            if i in [11, 13, 15]: # 突出显示左腿的关键点，给深蹲裁判员视觉反馈
                color = (0, 100, 255) 
                cv2.circle(img, (int(x), int(y)), 8, color, -1)
            else:
                cv2.circle(img, (int(x), int(y)), 4, color, -1)
            
    # 画连线
    for (u, v) in skeleton:
        if kpts[u][2] > 0.1 and kpts[v][2] > 0.1:
            pt1 = (int(kpts[u][0]), int(kpts[u][1]))
            pt2 = (int(kpts[v][0]), int(kpts[v][1]))
            cv2.line(img, pt1, pt2, (200, 100, 0), 3)
            
    return img

def _parse_args():
    parser = argparse.ArgumentParser(description="IronBuddy RTMPose Publisher")
    parser.add_argument(
        "--cloud",
        action="store_true",
        default=os.environ.get("USE_CLOUD_RTMPOSE", "0") == "1",
        help="Offload inference to cloud GPU via HTTP (AutoDL port 6006). "
             "Also enabled by env var USE_CLOUD_RTMPOSE=1.",
    )
    return parser.parse_known_args()[0]


def main():
    args = _parse_args()

    # ── Cloud mode: delegate entirely to the HTTP client ─────────────────────
    if args.cloud:
        print("[RTMPose Publisher] Cloud mode enabled – handing off to cloud_rtmpose_client.")
        try:
            from hardware_engine.ai_sensory.cloud_rtmpose_client import main as cloud_main
            cloud_main()
        except ImportError as e:
            print(f"[RTMPose Publisher] cloud_rtmpose_client import failed: {e}")
            print("[RTMPose Publisher] Falling back to local NPU mode.")
        else:
            return  # cloud_main() returned (e.g. KeyboardInterrupt handled inside)

    # ── Local NPU mode ────────────────────────────────────────────────────────
    print("[RTMPose Publisher] 初始化长效视觉主站...")
    
    # 1. 装载 NPU 模型
    worker = None
    if RTMPoseWorker is not None:
        try:
            model_path = os.path.join(current_dir, "experimental", "rtmpose_quant.rknn")
            worker = RTMPoseWorker(model_path)
            print("[RTMPose Publisher] RTMPose NPU 硬件加速器挂载成功。")
        except Exception as e:
            print(f"[RTMPose Publisher] RKNN Worker 加载受阻: {e}")
            
    # 2. 建立避震系统 (一欧元滤波器)
    smoother = PoseSmoother(mincutoff=0.8, beta=0.015, dcutoff=1.0)

    # 3. 挂载摄像头捕获
    import glob
    video_dev_paths = glob.glob('/dev/v4l/by-id/*index0')
    video_dev = video_dev_paths[0] if len(video_dev_paths) > 0 else '/dev/video5'
    print(f"[RTMPose Publisher] 尝试挂载物理相机设备: {video_dev}")
    cap = cv2.VideoCapture(video_dev)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 强制清空 OpenCV 底层 V4L2 积压缓冲
    simulate_mode = False
    
    if not cap.isOpened():
        print("[WARNING] 未识别到本地物理相机 (/dev/video0)，切入虚拟沙盒模拟数据源提供模式。")
        simulate_mode = True
        
    try:
        frame_idx = 0
        while True:
            t_now = time.time()
            frame_idx += 1
            
            # --- 获取原始数据与帧 ---
            if simulate_mode:
                time.sleep(0.05) # ~20 fps 发射频率
                frame = np.zeros((480, 640, 3), dtype=np.uint8)
                cv2.putText(frame, f"Simulate Engine ON - FPIdx: {frame_idx}", (20, 40), 
                            cv2.FONT_HERSHEY_DUPLEX, 0.8, (0, 0, 255), 2)
                            
                # 生成虚假的跳动人骨骼数据
                # 让它的关节有随机的噪声偏移，以此用来观测咱们的一欧元滤波器是否有效
                kpts = np.zeros((17, 3))
                jitter = np.random.randn() * 15 # 高频抖点噪声
                
                # 动态模拟一个在不停上下深蹲的老伙计，带噪声
                angle_anim = math.sin(t_now * 3.0) * 100
                
                # Left Leg 
                kpts[11] = [320 + np.random.randn()*3, 150 + np.random.randn()*3, 0.9] # Hip
                kpts[13] = [350 + jitter, 250 + angle_anim + jitter, 0.9] # Knee (受噪声荼毒最深)
                kpts[15] = [320 + np.random.randn()*3, 400 + np.random.randn()*3, 0.9] # Ankle
                raw_kpts = np.copy(kpts)
                
            else:
                # 已经设置 BUFFERSIZE=1，底层硬件出帧即为最新，无需用 grab() 强行清空，
                # 否则会导致每帧强制原地阻塞等待 4 个硬件节拍！
                ret, frame = cap.read()
                if not ret:
                    time.sleep(0.01)
                    continue
                    
                orig_h, orig_w = frame.shape[:2]

                if worker and hasattr(worker, 'rknn') and worker.rknn:
                    # 推理出来的是相对于输入尺寸 (如 192x256) 的像素坐标
                    raw_kpts = worker.inference(frame)
                    
                    # 后处理：把小分辨率里的点拓扑映射回源图的真实坐标
                    # 假设 rtmpose 默认尺寸长宽（宽192, 高256）
                    scale_x = orig_w / 192.0
                    scale_y = orig_h / 256.0
                    raw_kpts[:, 0] *= scale_x
                    raw_kpts[:, 1] *= scale_y
                else:
                    raw_kpts = np.zeros((17, 3))
                    
            # --- 核心：通过平滑矩阵进行过滤除噪 ---
            smoothed_kpts = smoother.process(raw_kpts, timestamp=t_now)
            
            # --- 序列化构建 JSON (严格对接原版的对象外壳格式以兼容状态机) ---
            out_json = {
                "timestamp": t_now,
                "frame_idx": frame_idx,
                "objects": [
                    {
                        "score": 0.99, 
                        "kpts": smoothed_kpts.tolist()
                    }
                ]
            }
            
            # IPC 写入节点
            try:
                # 首先确保 /dev/shm 路径有效，若在 windows 调试也可 fallback 至 /tmp
                target_json = SHM_POSE_JSON if os.path.exists("/dev/shm") else "/tmp/pose_data.json"
                target_jpg = SHM_RESULT_JPG if os.path.exists("/dev/shm") else "/tmp/result.jpg"

                with open(target_json + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(out_json, f, ensure_ascii=False)
                os.rename(target_json + ".tmp", target_json)

                # Generate simulated EMG from skeleton angle (synced with vision)
                kpts_list = smoothed_kpts.tolist() if hasattr(smoothed_kpts, 'tolist') else smoothed_kpts
                # Detect exercise from user_profile if available
                _exercise = "squat"
                try:
                    if os.path.exists("/dev/shm/user_profile.json"):
                        with open("/dev/shm/user_profile.json", "r") as uf:
                            _exercise = json.load(uf).get("exercise", "squat")
                except Exception:
                    pass
                # Compute angle for EMG generation
                l_score = kpts_list[11][2] + kpts_list[13][2] + kpts_list[15][2]
                r_score = kpts_list[12][2] + kpts_list[14][2] + kpts_list[16][2]
                if _exercise == "bicep_curl":
                    if l_score > r_score:
                        _emg_angle = _compute_angle(kpts_list[5], kpts_list[7], kpts_list[9])
                    else:
                        _emg_angle = _compute_angle(kpts_list[6], kpts_list[8], kpts_list[10])
                else:
                    if l_score > r_score:
                        _emg_angle = _compute_angle(kpts_list[11], kpts_list[13], kpts_list[15])
                    else:
                        _emg_angle = _compute_angle(kpts_list[12], kpts_list[14], kpts_list[16])
                # Only write EMG if no real sensor data (check heartbeat)
                _has_real_emg = os.path.exists("/dev/shm/emg_heartbeat")
                if not _has_real_emg:
                    _write_emg_json(_generate_emg_from_angle(_emg_angle, _exercise))
            except Exception as e:
                pass
            
            # --- MJPEG 回传管线更新 ---
            # 画上骨头然后直接给到 web 前端串流渲染
            drawn_frame = draw_skeleton(frame, smoothed_kpts.tolist())
            
            # 也可以在这里使用 cv2 输出一个小红点作为原跳动点，小绿点作为过滤点做学术验证演示
            if simulate_mode:
                cv2.circle(drawn_frame, (int(raw_kpts[13][0]), int(raw_kpts[13][1])), 3, (0,0,255), -1) # 红点乱跳
                cv2.circle(drawn_frame, (int(smoothed_kpts[13][0]), int(smoothed_kpts[13][1])), 6, (0,255,0), -1) # 绿点稳健
            
            encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
            try:
                ret, buf = cv2.imencode('.jpg', drawn_frame, encode_param)
                if ret:
                    with open(target_jpg + ".tmp", "wb") as f:
                        f.write(buf.tobytes())
                    os.rename(target_jpg + ".tmp", target_jpg)
            except Exception as e:
                pass
                
            # 心跳报告
            if frame_idx % 60 == 0:
                print(f"| RTMPose Publisher | 心跳正常, 已发送 {frame_idx} 帧. 时间戳: {t_now:.2f} |")

    except KeyboardInterrupt:
        print("\n[INFO] 收回 V4L2 摄像头访问权限，关停视觉主泵。")
    finally:
        cap.release()
        if worker:
            worker.release()
            
if __name__ == "__main__":
    main()
