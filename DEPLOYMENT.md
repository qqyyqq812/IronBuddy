# Deployment Guide

> 本仓库代码包含 **三个独立运行环境**：开发主机（WSL/Linux）、板端（RK3399ProX）、云端（GPU 节点）。单独克隆本仓库**无法运行完整功能**，必须按下文准备硬件 + 部署外部服务。

---

## 1. 开发主机（WSL2 / Ubuntu）

### 1.1 依赖

* Python 3.10+ （开发主机；板端是 3.7）
* `pip install flask flask-cors aiohttp websockets pyserial opencv-python-headless numpy torch scikit-learn`
* （可选）`pip install streamlit` — 训练面板

### 1.2 启动

```bash
cp .env.example .env
# 填写所有 *_API_KEY、CLOUD_SSH_HOST、BAIDU_*、FEISHU_*、ADP_APP_KEY
python3 streamer_app.py
```

* Web UI：http://localhost:5000/
* 默认走"模拟模式"（无板端 / 无 sEMG 硬件），FSM 用 mock 关键点驱动

---

## 2. 板端（Toybrick RK3399ProX）

### 2.1 硬件准备

* Toybrick RK3399ProX 开发板 1 块（含 NPU）
* HDMI 显示屏 + USB 摄像头
* ESP32 sEMG 贴片传感器（固件见 `hardware_firmware/`）
* WiFi 网络（板端、开发主机、ESP32 同网段）

### 2.2 系统镜像

```
Linux 4.4.194 (Toybrick 官方 Debian 9 镜像)
Python 3.7（系统自带，请勿升级）
amixer (alsa-utils) — 用于音频路径管理
```

### 2.3 部署模型

* 拷贝 `pose-5s6-640-uint8.rknn` 到板端 `/home/toybrick/deploy_rknn_yolo/YOLOv5-Style/data/weights/`
* 拷贝训练好的 `extreme_fusion_gru.pt`（深蹲）到板端 `/home/toybrick/streamer_v3/hardware_engine/cognitive/`

> 模型本身因体积关系不入仓。深蹲 GRU 可用 `tools/train_gru_three_class.py` 训练；弯举见 `tools/train_gru_three_class_bicep_personal.py`。

### 2.4 同步与启动

```bash
# 在开发主机执行（板端 IP 默认 10.29.10.224，按需用 IRONBUDDY_BOARD_IP 覆盖）
export IRONBUDDY_BOARD_IP=<your-board-ip>
bash scripts/start_silent.sh
```

* 自动 rsync 代码到 `toybrick@<ip>:/home/toybrick/streamer_v3/`
* SSH 启动 `scripts/start_silent_services.sh`，拉起 5 个板端进程
* Web UI：`http://<board-ip>:5000/`

### 2.5 板端进程

| 进程 | 文件 | 用途 |
|------|------|------|
| vision | `hardware_engine/ai_sensory/cloud_rtmpose_client.py` 或 `local_yolo_pose.py` | 姿态推理 |
| fsm | `hardware_engine/main_claw_loop.py` | FSM 计数 + GRU 推理 |
| emg | `hardware_engine/sensor/udp_emg_server.py` | sEMG UDP 接收 + 特征 |
| voice | `hardware_engine/voice_daemon.py` | ASR/TTS + 自适应 VAD |
| streamer | `streamer_app.py` | Flask Web + 5 服务编排 |

进程间通过 `/dev/shm/*.json` + atomic rename 通信。

### 2.6 板端红线

* Python 3.7 限制：禁用 `X | None`、`match/case`、`:=`、`pandas`
* `pgrep -f` 必须用正则括号陷阱：`pgrep -f "[c]loud_rtm"`
* 停止服务必须 `SIGTERM 0.8s` 再 `kill -9`，避免僵尸进程
* HDMI 必须 `startx -- -nocursor` + `xhost +local:`
* 音频每次重启必须 `amixer SPK_HP (numid=1 val=6)` 回拨

---

## 3. 云端（GPU 节点 — RTX 3090/4090/5090）

### 3.1 服务清单

云端运行三个独立服务（**不在本仓库**，需独立部署）：

#### a. RTMPose HTTP 推理服务（端口 6006）

```bash
# 在云端 GPU 主机
git clone <your-rtmpose-server-repo>  # 或参考 hardware_engine/ai_sensory/deploy_to_cloud.py
cd ironbuddy_cloud
pip install mmpose mmengine opencv-python-headless
python rtmpose_http_server.py  # 0.0.0.0:6006
```

* 加载 `rtmpose-m_simcc-aicrowd_256x192.onnx`（约 50MB，需自行下载或训练）

#### b. Qdrant 向量数据库（端口 6333）

```bash
docker run -d --name qdrant -p 6333:6333 -v /root/qdrant_storage:/qdrant/storage qdrant/qdrant
```

#### c. bge-m3 嵌入服务（端口 8008）

```bash
pip install transformers torch sentence-transformers fastapi uvicorn
# 见 hardware_engine/cognitive/vector_knowledge.py 头注释（embedding server 启动方式）
uvicorn embedding_server:app --host 0.0.0.0 --port 8008
```

### 3.2 SSH 反向隧道（开发主机 / 板端 → 云端）

```bash
# 设置 .env.CLOUD_SSH_HOST + CLOUD_SSH_PORT
bash scripts/cloud_tunnel.sh   # 转发 6006 → 本地 6006 (RTMPose)
bash scripts/rag_tunnel.sh     # 转发 6333 + 8008 → 本地 (RAG + embedding)
```

* 隧道运行中，板端的 `cloud_rtmpose_client.py` 可直接访问 `http://localhost:6006/infer`
* `vector_knowledge.py` 可访问 `http://localhost:6333` (Qdrant) 和 `http://localhost:8008` (bge-m3)

---

## 4. 第三方 SaaS 凭证

### 4.1 必备

| 服务 | 凭证 | 用途 |
|------|------|------|
| **百度 AipSpeech** | `BAIDU_APP_ID`、`BAIDU_API_KEY`、`BAIDU_SECRET_KEY` | 语音 ASR + TTS |
| **DeepSeek** | `DEEPSEEK_API_KEY` | LLM 对话（SSE 流式） |

### 4.2 可选

| 服务 | 凭证 | 用途 |
|------|------|------|
| **飞书 OpenCloud** | `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_CHAT_ID` | 训练前后 interactive card 推送 |
| **腾讯 ADP** | `ADP_APP_KEY` | 健身知识库 RAG |

### 4.3 配置方式

* 优先用 `.env`（已 gitignore）+ 项目根 `.api_config.json`（已 gitignore）
* `python-dotenv` 或 shell `source .env` 加载
* `streamer_app.py` 启动时检测，缺失会降级到 mock 模式

---

## 5. 验证

```bash
# 在板端 SSH
ssh toybrick@<board-ip>
ls /dev/shm/*.json     # 应有 vision_mode、emg_features、fsm_state 等
ps aux | grep -E "cloud_rtm|main_claw|udp_emg|voice_daemon|streamer"  # 5 个进程
curl http://localhost:5000/api/health  # streamer 健康检查
curl http://localhost:6006/health      # 云端 RTMPose（经隧道转发）
```

---

## 6. 故障排查

| 症状 | 检查 |
|------|------|
| 视频流卡顿 | 摄像头是否 USB 直连；CAMERA_FOURCC=MJPG；CLOUD_TARGET_FPS=15 |
| EMG 无数据 | `ss -ulnp \| grep 8080`；ESP32 → 板端网络通；`mock_teammate_esp32.py` 测试 |
| 语音无响应 | `amixer SPK_HP` 是否 ON；`/dev/snd/` 权限 |
| GRU 推理慢 | NPU 是否启用；`MIN_KPT_CONF` 阈值是否过严 |
| 云端 RTMPose 不通 | SSH 隧道是否运行；`netstat -tnp \| grep 6006` |

---

详细架构与设计决策见 [docs/technical/decisions.md](docs/technical/decisions.md)。
