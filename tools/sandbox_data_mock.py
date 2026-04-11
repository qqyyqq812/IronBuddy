import os
import time
import json
import math
import argparse

def generate_mock_data(mode):
    print(f"🚀 沙盒模拟器启动。模式: {'[黄金标准]' if mode == 'golden' else '[代偿犯规]'}")
    fps = 30
    delay = 1.0 / fps
    frame_idx = 0

    while True:
        # 建立一个 90 帧(3秒) 的标准化深蹲周期
        cycle_frame = frame_idx % 90
        
        # 1. 模拟视觉角度 (170度 -> 80度 -> 170度)
        if cycle_frame < 30:
            # 下落期
            angle = 170 - ( cycle_frame / 30.0 ) * 90
        elif cycle_frame < 45:
            # 谷底停滞期
            angle = 80
        elif cycle_frame < 75:
            # 起身期
            angle = 80 + ( (cycle_frame - 45) / 30.0 ) * 90
        else:
            # 站立休息
            angle = 170

        # 用数学巧妙构建让 main_claw_loop 算出正确 Angle 的假坐标
        # Knee 设为原点(0,0)，Hip 为(0, 100)，Ankle 设定夹角
        kpts = [[0, 0, 0.99] for _ in range(17)]
        kpts[11] = [500, 600, 0.99] # Hip
        kpts[13] = [500, 500, 0.99] # Knee
        rad = math.radians(angle)
        kpts[15] = [500 + 100 * math.sin(rad), 500 + 100 * math.cos(rad), 0.99] # Ankle

        pose_data = {
            "objects": [
                {
                    "score": 0.99,
                    "kpts": kpts
                }
            ]
        }

        # 2. 模拟肌电数据
        target_glute = 10
        comp_back = 10

        if angle < 140:
            # 进入受力区
            if mode == 'golden':
                # 黄金深蹲：臀大肌完美发力，后腰竖脊肌只起到轻微稳定作用
                target_glute = 75 + (140 - angle) * 0.2
                comp_back = 15 + (140 - angle) * 0.1
            else:
                # 代偿深蹲：臀肌失忆，完全靠后腰竖脊肌硬拉起身
                target_glute = 20 + (140 - angle) * 0.05
                comp_back = 80 + (140 - angle) * 0.3

        # 为了防止数据绝对死板，混入噪音 (Gaussian Noise)
        import random
        target_glute = max(0, min(100, target_glute + random.uniform(-5, 5)))
        comp_back = max(0, min(100, comp_back + random.uniform(-5, 5)))

        # 我们将 target 映射到 glutes，代偿映射到 biceps (暂时代指下背)
        muscle_data = {
            "activations": {
                "quadriceps": 0,
                "glutes": target_glute,
                "calves": 0,
                "biceps": comp_back 
            },
            "warnings": [],
            "exercise": "squat"
        }

        # 3. 高速写入 /dev/shm
        try:
            with open("/dev/shm/pose_data.json.tmp", "w") as f:
                json.dump(pose_data, f)
            os.rename("/dev/shm/pose_data.json.tmp", "/dev/shm/pose_data.json")

            with open("/dev/shm/muscle_activation.json.tmp", "w") as f:
                json.dump(muscle_data, f)
            os.rename("/dev/shm/muscle_activation.json.tmp", "/dev/shm/muscle_activation.json")

            # 写入当前高精度 Float 时钟
            with open("/dev/shm/emg_heartbeat", "w") as f:
                f.write(str(time.time()))

        except Exception as e:
            print(f"写入共享内存失败: {e}")

        # 可以在界面或者日志输出一个简谱
        if frame_idx % 15 == 0:
            print(f"帧 {frame_idx:4d} | 角度: {angle:3.0f}° | 臀肌: {target_glute:3.0f}% | 下腰: {comp_back:3.0f}%")

        frame_idx += 1
        time.sleep(delay)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IronBuddy 5D 沙盒数据伪造器")
    parser.add_argument("--mode", type=str, choices=['golden', 'lazy'], required=True, 
                        help="golden: 录制纯粹标准动作; lazy: 录制全代偿犯规动作")
    args = parser.parse_args()
    
    # 向全局发信，告知主引擎我们要保存成哪些 CSV
    try:
        with open("/dev/shm/record_mode", "w") as f:
            f.write(args.mode)
    except Exception:
        pass

    try:
        generate_mock_data(args.mode)
    except KeyboardInterrupt:
        print("\n沙盒模拟器安全退出！")
        try:
            os.remove("/dev/shm/record_mode")
        except:
            pass
