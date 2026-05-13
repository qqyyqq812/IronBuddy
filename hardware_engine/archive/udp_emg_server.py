import socket, struct, json, os, time

UDP_IP = "0.0.0.0"
UDP_PORT = 5005
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))
print(f"[*] 物理网关已开启: UDP_EMG_SERVER 纯净监听端口 {UDP_PORT}")

last_write = 0
while True:
    try:
        data, addr = sock.recvfrom(1024)
        if len(data) == 8:
            # 解包 4 个 uint16_t (小端序)
            emg_vals = struct.unpack('<HHHH', data)
            now = time.time()
            # 稳定 33Hz 写内存盘
            if now - last_write > 0.03: 
                # 强制转换为标准英文规范键，屏蔽一切中文编解码地雷
                acts = {
                    "quadriceps": min(100, int(emg_vals[0]/1000 * 100)), # 股四头肌
                    "glutes": min(100, int(emg_vals[1]/1000 * 100)),     # 臀大肌
                    "calves": min(100, int(emg_vals[2]/1000 * 100)),     # 小腿肌
                    "biceps": min(100, int(emg_vals[3]/1000 * 100))      # 肱二头肌 (抓捕代偿)
                }
                out = {"activations": acts, "warnings": [], "exercise": "squat"}
                with open('/dev/shm/muscle_activation.json.tmp', 'w') as f:
                    json.dump(out, f)
                os.rename('/dev/shm/muscle_activation.json.tmp', '/dev/shm/muscle_activation.json')
                
                # 手动更新时间戳文件，喂给 streamer_app 里的 150ms 掉线看门狗
                with open('/dev/shm/emg_heartbeat', 'w') as f:
                    f.write(str(now))
                    
                last_write = now
    except Exception as e:
        print(f"UDP 错误或接收异常: {e}")
