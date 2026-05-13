import cv2
import socket
import struct
import json
import numpy as np
import time
import threading
from queue import Queue

# --- 配置参数 ---
UDP_IP = "0.0.0.0"
UDP_PORT = 20000        # 云端监听端口
CLIENT_IP = None        # 客户端 IP（自动从收到的包中提取）
CLIENT_PORT = 20001     # 板端的接收端口

# 初始化网络
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))

# 帧缓冲队列
frame_queue = Queue(maxsize=10)

def udp_receive_thread():
    global CLIENT_IP
    print(f"[Network] 云侧接收端启动，监听 {UDP_IP}:{UDP_PORT} ...")
    while True:
        try:
            # 假设极简分包协议或高压单包协议 (64KB payload max)
            data, addr = sock.recvfrom(65535)
            CLIENT_IP = addr[0]  # 更新给下推线程
            
            # 前 4 字节是 seq_id (unsigned int)
            seq_id = struct.unpack('<I', data[:4])[0]
            # 后面的全是 jpeg 字节流
            jpeg_bytes = np.frombuffer(data[4:], dtype=np.uint8)
            frame = cv2.imdecode(jpeg_bytes, cv2.IMREAD_COLOR)
            
            if frame is not None:
                if frame_queue.full():
                    frame_queue.get()  # 丢弃最老的一帧，保持低延迟
                frame_queue.put((seq_id, frame))
        except Exception as e:
            print(f"[Error] UDP 接收异常: {e}")

def run_inference():
    print("[Inference] RTMPose 推理总线启动...")
    # 这里是挂载真实 RTMPose Pipeline 的地方
    # from mmpose.apis import inference_topdown, init_model
    # model = init_model('config.py', 'checkpoint.pth', device='cuda:0')
    
    while True:
        seq_id, frame = frame_queue.get()
        start_t = time.time()
        
        # --- [Mock 推理层] 替换为实际 RTMPose 调用 ---
        # result = inference_topdown(model, frame)
        # keypoints = result[0].pred_instances.keypoints[0].tolist()
        
        # 极速模拟计算 (假设 15ms 延迟)
        time.sleep(0.015) 
        mock_keypoints = [[x, y, 0.9] for x, y in zip(np.random.randint(0, 320, 17), np.random.randint(0, 240, 17))]
        # ---------------------------------------------
        
        # JSON 封包回传
        output = {
            "seq_id": seq_id,
            "latency_ms": round((time.time() - start_t) * 1000, 2),
            "keypoints": mock_keypoints
        }
        json_str = json.dumps(output).encode('utf-8')
        
        if CLIENT_IP is not None:
            # 将骨架点发回客户端
            try:
                sock.sendto(json_str, (CLIENT_IP, CLIENT_PORT))
            except Exception as e:
                pass # 忽略偶发网络端点不可达
                
if __name__ == "__main__":
    recv_t = threading.Thread(target=udp_receive_thread, daemon=True)
    recv_t.start()
    
    # 主线程进行 GPU 推理
    run_inference()
