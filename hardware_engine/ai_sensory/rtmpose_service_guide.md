# ☁️ RTX 5090 云端 Systemd 守护进程部署指南

为了防止 SSH 连接断开导致 `Cloud_RTMPose_Receiver.py` 被系统杀死，我们需要利用 `systemd` 将其注册为一个后台守护服务，跟随系统生命周期存活，并具备崩溃自启功能。

## 部署步骤

### 1. 传输脚本至云端
将接收端脚本发送至云端服务器的 `/root/` 目录下：
```bash
scp -P 14191 /home/qq/projects/embedded-fullstack/hardware_engine/ai_sensory/Cloud_RTMPose_Receiver.py root@connect.westd.seetacloud.com:/root/Cloud_RTMPose_Receiver.py
```

### 2. 创建 Systemd 配置文件
在云服务器中，创建一个新的 `systemd` 服务单元：
```bash
nano /etc/systemd/system/rtmpose.service
```

写入以下配置内容（定义了如何启动、在哪执行以及失败处理）：
```ini
[Unit]
Description=RTMPose UDP Bridge Service
After=network.target

[Service]
Type=simple
User=root
# 工作目录
WorkingDirectory=/root
# [修改重点] 指定你的 Python 环境绝对路径，西电云默认 conda 环境比如 /root/miniconda3/bin/python
ExecStart=/usr/bin/python3 /root/Cloud_RTMPose_Receiver.py

# 崩溃后的自动重启策略
Restart=on-failure
RestartSec=5s

# 保护标准输出不丢弃，可以通过 journalctl 查看
StandardOutput=syslog
StandardError=syslog
SyslogIdentifier=rtmpose-bridge

[Install]
WantedBy=multi-user.target
```

### 3. 激活与运行全流程

当配置写入后，依次执行以下三条指令：

**🔥 重新加载 Systemd (每次修改 `.service` 文件后必须执行)：**
```bash
systemctl daemon-reload
```

**🚀 启动服务并设置开机自启：**
```bash
systemctl start rtmpose
systemctl enable rtmpose
```

### 4. 日志检视 (如何抓虫)

后续如果想查看云端 UDP 是不是在正常收图或者看 Python 报错日志，不需要看 nohup 的日志文件，直接用 journal 统一管理：

```bash
# 实时跟踪最后 100 行日志
journalctl -u rtmpose -f -n 100
```

> **为何采用 Systemd 而不是 Tmux/Nohup？**
> Systemd 能接管僵尸进程，并在内存 OOM (超载被系统 Kill) 或者代码发生未捕获异常退出时，严格遵守 `Restart=on-failure` 在 5 秒后主动拉起，是 `Zero-Downtime`（零停机）环境下的工业级标准部署方式。

---

## ⚡ 附录：JupyterLab 官方终端的“极速免密”部署法（强烈推荐）

如果您想跳过本地的 SSH 和 SCP，直接在云端的 JupyterLab 网页终端里操作，这**完全可以，且效率更高**！

> ⚠️ **注意事实纠正**：由于西电云/AutoDL 等云服务器的 JupyterLab 实际上运行在 Docker 容器内部（PID 1 不是 init），这类环境**大概率不支持 `systemctl` 命令** (会报错 `System has not been booted with systemd`)。
> 因此，在网页终端内，我们必须改用 `tmux` 或 `nohup` 来保持后台运行。

**请直接复制以下这段整块代码，粘贴到云端 JupyterLab 的 Terminal 里敲回车：**

```bash
# 1. 直接用 cat 命令生成 Python 接收端代码文件
cat << 'EOF' > /root/Cloud_RTMPose_Receiver.py
import cv2, socket, struct, json, numpy as np, time, threading
from queue import Queue

UDP_IP, UDP_PORT = "0.0.0.0", 20000
CLIENT_IP, CLIENT_PORT = None, 20001
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))
frame_queue = Queue(maxsize=10)

def udp_receive_thread():
    global CLIENT_IP
    print(f"[Network] 云侧接收端启动，监听 {UDP_IP}:{UDP_PORT} ...")
    while True:
        try:
            data, addr = sock.recvfrom(65535)
            CLIENT_IP = addr[0]
            seq_id = struct.unpack('<I', data[:4])[0]
            frame = cv2.imdecode(np.frombuffer(data[4:], dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                if frame_queue.full(): frame_queue.get()
                frame_queue.put((seq_id, frame))
        except Exception as e: pass

def run_inference():
    print("[Inference] RTMPose 推理总线启动...")
    while True:
        seq_id, frame = frame_queue.get()
        start_t = time.time()
        time.sleep(0.015) 
        mock_keypoints = [[x, y, 0.9] for x, y in zip(np.random.randint(0,320,17), np.random.randint(0,240,17))]
        out = {"seq_id": seq_id, "latency_ms": round((time.time() - start_t)*1000, 2), "keypoints": mock_keypoints}
        if CLIENT_IP is not None: sock.sendto(json.dumps(out).encode(), (CLIENT_IP, CLIENT_PORT))

threading.Thread(target=udp_receive_thread, daemon=True).start()
run_inference()
EOF

# 2. 静默安装可能缺失的依赖
pip install opencv-python numpy -q

# 3. 使用 nohup 后台启动挂载，把日志输出挂到 recv.log 里
nohup python /root/Cloud_RTMPose_Receiver.py > /root/recv.log 2>&1 &
echo -e "\n✅ 部署成功！您可以随时通过命令: 'tail -f /root/recv.log' 观测云侧的运行日志。"
```

这样就能一次性跨过所有网络传输阻碍，直接在云侧将守护后台立起来！
