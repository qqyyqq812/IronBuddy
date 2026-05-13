import socket
import struct
import time
import math

UDP_IP = "127.0.0.1"
UDP_PORT = 5005

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
print(f"[*] 疯狂打桩机 MOCK EMG CLIENT 已就绪... 目标: {UDP_IP}:{UDP_PORT} (500Hz)")
t = 0
try:
    while True:
        # 伪造平滑的肌肉活动信号
        v1 = int(abs(math.sin(t)) * 1000)
        v2 = int(abs(math.cos(t*1.5)) * 1000)
        v3 = int(abs(math.sin(t*0.5)) * 1000)
        v4 = int(abs(math.cos(t*2)) * 1000)
        
        # 打包成 8 字节二进制子弹发送
        data = struct.pack('<HHHH', v1, v2, v3, v4)
        sock.sendto(data, (UDP_IP, UDP_PORT))
        
        t += 0.01
        time.sleep(0.002) # 发送速率限制
except KeyboardInterrupt:
    print("\n停止打桩。")
