import cv2
import socket
import struct
import json
import time
import threading

# --- 配置参数 ---
CLOUD_IP = "connect.westd.seetacloud.com" # 这个会在 sendto 时通过 DNS 解析 
CLOUD_PORT = 20000        # 云测监听的公网暴露端口或内网转发端口
LOCAL_IP = "0.0.0.0" 
LOCAL_PORT = 20001        # 回传接收端口

# 初始化接收端的 Socket
sock_recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock_recv.bind((LOCAL_IP, LOCAL_PORT))

# 初始化发送端 Socket
sock_send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

# 全局状态
latest_keypoints = None
network_latency = 0.0

def udp_receive_thread():
    global latest_keypoints, network_latency
    print(f"[Network] 板端独立接收线程启动，监听 {LOCAL_PORT} 端口...")
    while True:
        try:
            data, _ = sock_recv.recvfrom(65535)
            payload = json.loads(data.decode('utf-8'))
            
            latest_keypoints = payload.get("keypoints", [])
            network_latency = payload.get("latency_ms", 0.0)
            
            # 此处直接刷新 UI 或写入共享内存
            # print(f"  --> 收到 seq: {payload.get('seq_id')}, 推理耗时: {network_latency} ms")
        except Exception as e:
            print(f"[Error] 回传接收异常: {e}")

def main_claw_loop():
    print("[Camera] 启动边缘推流器...")
    cap = cv2.VideoCapture(0) # 或接入 IronBuddy 的全局摄像头句柄
    
    # 降低分辨率以极致压缩 UDP 带宽
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
    
    seq_id = 0
    t0 = time.time()
    frames = 0
    
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
            
        # JPEG 高压 (Quality=70)
        encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 70]
        _, img_encode = cv2.imencode('.jpg', frame, encode_param)
        jpeg_bytes = img_encode.tobytes()
        
        # 封装头部 seq_id
        header = struct.pack('<I', seq_id)
        packet = header + jpeg_bytes
        
        # 强制发送 (UDP 容忍丢包)
        # 注意: 生产环境需要将云侧真实 IP 解析好，此处作为 Demo 直接发送
        try:
            # 解析云端真实 IP 地址 (可以提前解析好以防 DNS 耗时)
            cloud_addr = (socket.gethostbyname(CLOUD_IP), CLOUD_PORT)
            sock_send.sendto(packet, cloud_addr)
        except Exception as e:
            pass # 发生掉线时不阻塞镜头
            
        # 帧率计算打印
        seq_id += 1
        frames += 1
        if time.time() - t0 >= 1.0:
            fps = frames / (time.time() - t0)
            print(f"[Sync] 上行 FPS: {fps:.1f} | 骨架点就绪状态: {latest_keypoints is not None} | 云端延迟: {network_latency}ms")
            t0 = time.time()
            frames = 0
            
        # 模拟主控制循环节拍
        time.sleep(0.01)

if __name__ == "__main__":
    t = threading.Thread(target=udp_receive_thread, daemon=True)
    t.start()
    
    try:
        main_claw_loop()
    except KeyboardInterrupt:
         print("关闭传感器...")
