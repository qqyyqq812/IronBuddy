# IronBuddy (Embedded-Fullstack AI Fitness Coach)

边缘侧 AI 健身教练，集成视觉推理、肌电识别、LLM 对话、语音交互的完整全栈系统。

**硬件**：RK3399ProX（ARM CPU + NPU），**云服务**：RTX 5090 RTMPose 推理

## 关键文件导航

| 文件/目录 | 用途 |
|----------|------|
| `docs/technical/decisions.md` | ⭐ **架构决策 + 踩坑（10大决策 + 9条踩坑，源码驱动）** |
| `docs/technical/architecture.md` | 整体系统架构图 |
| `streamer_app.py` | Flask 主 APP（~1040行，5 服务控制 + API + 视频流）|
| `templates/database.html` | 一站式数据库可视化（`/database` 路由，8 张表）|
| `docs/验收表/深蹲神经网络权威指南.md` | ⭐ **深蹲 GRU 三分类权威（V7.15，唯一标准，取代 MIA数据集分析.md）** |
| `docs/验收表/语音模块权威指南.md` | ⭐ **语音模块权威（V7.14 杀青）** |
| `docs/验收表/V3_7D_全链路地图.md` | 弯举 GRU 管线（由弯举 AI 独立维护） |
| `hardware_engine/main_claw_loop.py` | 板端 FSM 主循环（深蹲/弯举 + GRU 推理）|
| `hardware_engine/ai_sensory/cloud_rtmpose_client.py` | 视觉引擎（Cloud RTMPose + Local YOLOv5-Pose NPU 双模）|
| `hardware_engine/ai_sensory/local_yolo_pose.py` | 本地 NPU 视觉推理（RKNN uint8）|
| `hardware_engine/voice_daemon.py` | 百度 AipSpeech TTS + STT + 自适应 VAD |
| `hardware_engine/cognitive/fusion_model.py` | CompensationGRU 模型（7D/4D 输入，3-head 输出）|
| `hardware_engine/sensor/udp_emg_server.py` | EMG 数据处理（模拟 ↔ 传感器双模）|
| `templates/index.html` | PWA 前端（~3500 行，控制台 + EMG 波形 + 日志 + 设置）|
| `docs/technical/数据采集与训练指南.md` | GRU 数据采集与训练 ⭐ |
| `docs/technical/sEMG泛化实现指南.md` | 肌电特征工程 ⭐ |
| `docs/technical/IronBuddy_Deployment_Guide.md` | 部署快速开始 |
| `tools/{collect_training_data,train_model,dashboard}.py` | 数据采集 / GRU 训练 / Streamlit 面板 |
| `.claude/rules/toybrick_board_rules.md` | 板端环境约束（Python 3.7, 无 pandas, ALSA, NPU）|

## 当前状态（Sprint 4, 2026-04-15）

- **架构**：5 进程 + `/dev/shm` JSON IPC（vision/streamer/fsm/emg/voice）
- **视觉**：本地 YOLOv5-Pose NPU（默认）↔ Cloud RTMPose RTX 5090（可切）
- **视频**：HDMI 直连 + MJPEG:8080 + Flask fallback 三路
- **LLM**：DeepSeek 直连 REST API + SSE 流式
- **语音**：百度 AipSpeech TTS/STT + ALSA 直驱
- **识别**：FSM 即时计数 + GRU 3-head 教练点评双引擎
- **待做**：语音唤醒调试、GRU 正式训练、systemd 自启动、SQLite 持久化

## 关键决策速查

1. **双视觉模式**：本地 NPU RKNN 为默认（~0.08 置信度阈值），Cloud RTMPose 为可选高精度备份，`/dev/shm/vision_mode.json` 热切换
2. **三路视频**：HDMI (cv2.imshow, 零延迟) + MJPEG (:8080, 内建) + Flask (/video_feed, legacy)
3. **LLM 直连**：DeepSeek REST API + SSE 流式（非 WebSocket），触发：疲劳 1500 / "教练" 语音 / APP 按钮
4. **服务管理**：pgrep bracket trick (`[c]loud_rtm`) + nohup 临时脚本 + SIGTERM(0.8s)+SIGKILL 双判定
5. **IPC 协议**：`/dev/shm/*.json` + atomic rename，20+ 信号文件覆盖视觉/EMG/FSM/聊天/违规
6. **EMG 双模**：模拟（骨架角速度驱动）↔ 传感器（ESP32 BLE→UDP:8080），7D 特征
7. **语音方案**：百度 AipSpeech（放弃 Vosk glibc 不兼容 + edge-tts 网络不稳）

## 快速开始

```bash
# 一键启动（WSL 执行）
bash start_validation.sh

# 仅启动可视化面板
streamlit run tools/dashboard.py

# 数据采集
python tools/collect_training_data.py --mode bicep_curl

# GRU 训练
python tools/train_model.py
```

## 开发注意

- **Board 限制**：Python 3.7（无 `X | None` 语法、无 pandas）
- **禁止**：生成 `handoff_*.md`, `EXECUTION_PLAN_*.md`, 演讲稿、调研报告
- **关键代码**：修改时更新 `docs/technical/decisions.md`
- **通信约定**：所有进程通过 `/dev/shm/*.json` + atomic rename 交换数据
