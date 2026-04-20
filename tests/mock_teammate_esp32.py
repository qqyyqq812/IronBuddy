#!/usr/bin/env python3
import socket
import struct
import time
import random
import math

# 配置：将这里的 IP 指向 RK3399ProX 的内网地址
TARGET_IP = "10.18.76.224"
TARGET_PORT = 5005

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

print("🚀 [PC-Mock] 开始启动物理外挂火力网！")
print(f"📡 目标标定: {TARGET_IP}:{TARGET_PORT}")
print("========================================")
print("按 Enter 键发射标准深蹲受力 (合法测试)")
print("按组合键 Ctrl+C 停止发射")
print("========================================")

t = 0
biceps_mode = False

try:
    while True:
        # 使用正弦波模拟一个标准的下蹲收缩肌电包络
        # 股四头肌 (0) 与臀大肌 (1) 同相共发力
        quad = int((math.sin(t) * 0.5 + 0.5) * 800)   # 波动在 0~800 (最大值1000代表100%)
        glute = int((math.sin(t - 0.2) * 0.5 + 0.5) * 750) 
        calves = int((math.sin(t) * 0.2 + 0.3) * 400) # 维持一定基础发力

        # 随机引入代偿违纪动作 (模拟手臂去拉借力点)
        if random.random() > 0.95:
            biceps_mode = True
        elif random.random() > 0.90:
            biceps_mode = False

        if biceps_mode:
            biceps = 900 # 肱二头肌突发爆表 (违规代偿)
            print("🚨 [PC模拟器] 正在发射肱二头肌违规代偿干扰波...")
        else:
            biceps = 100 # 正常

        # 加入 5% 的白噪声让数据看起来更有生物波真实感
        quad = min(1000, max(0, quad + random.randint(-50, 50)))
        glute = min(1000, max(0, glute + random.randint(-50, 50)))
        calves = min(1000, max(0, calves + random.randint(-50, 50)))
        biceps = min(1000, max(0, biceps + random.randint(-20, 20)))

        # 根据 udp_emg_server，解包格式为 '<HHHH'
        data = struct.pack('<HHHH', quad, glute, calves, biceps)
        sock.sendto(data, (TARGET_IP, TARGET_PORT))

        t += 0.1
        time.sleep(0.03) # ~33Hz

except KeyboardInterrupt:
    print("\n🛑 停火。已切断网段！")
    sock.close()
