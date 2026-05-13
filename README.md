# IronBuddy — Edge-Native AI Fitness Coach

[![Status](https://img.shields.io/badge/status-V7.3x-blue)]() [![Hardware](https://img.shields.io/badge/board-RK3399ProX-orange)]() [![Cloud](https://img.shields.io/badge/cloud-RTMPose%20%2F%20Qdrant-green)]()

边缘侧 AI 健身教练，集成 **视觉骨架推理 + 表面肌电（sEMG）特征 + GRU 三分类教练点评 + DeepSeek LLM 对话 + 百度语音 STT/TTS** 的全栈系统。

* **板端**：Toybrick RK3399ProX（ARM CPU + NPU），跑 YOLOv5-Pose RKNN 推理 + FSM 计数 + GRU 教练点评
* **云端**：RTX GPU 节点，托管 RTMPose 高精度姿态推理 + Qdrant 向量 RAG + bge-m3 嵌入服务
* **客户端**：Flask + PWA Web UI（控制台 / EMG 波形 / 训练记录 / 设置）
* **传感器**：ESP32 BLE → UDP 传输 sEMG 原始波形

---

## 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│  ESP32 sEMG 贴片 ──BLE──► WiFi 网关 ──UDP:8080──┐            │
│                                                  │            │
│  HDMI 显示屏 ◄──┐                                ▼            │
│                │            ┌──────────────────────────┐    │
│  USB 摄像头 ───┴──────────► │  Toybrick RK3399ProX    │    │
│                              │  (板端 5 进程协作)        │    │
│                              │  /dev/shm/*.json IPC     │    │
│                              └──────┬──────┬────────────┘    │
│                                     │      │                 │
│                              ┌──────▼──┐ ┌─▼────────────┐    │
│                              │ Cloud   │ │ Cloud RAG     │   │
│                              │ RTMPose │ │ Qdrant+bge-m3 │   │
│                              │ (GPU)   │ │ (GPU)         │   │
│                              └─────────┘ └───────────────┘   │
│                                  │              │             │
│                              ┌───▼──────────────▼──┐         │
│                              │  DeepSeek LLM API   │         │
│                              │  百度 AipSpeech     │         │
│                              │  飞书 OpenCloud     │         │
│                              └─────────────────────┘         │
└─────────────────────────────────────────────────────────────┘
```

详细架构：[docs/technical/architecture.md](docs/technical/architecture.md) 
核心决策与踩坑：[docs/technical/decisions.md](docs/technical/decisions.md)

---

## 仓库布局

```
embedded-fullstack/
├── streamer_app.py            # Flask 主入口（5 服务控制 + REST API + 视频流）
├── hardware_engine/           # 板端运行时
│   ├── main_claw_loop.py      # FSM 主循环（深蹲 / 弯举 + GRU 推理）
│   ├── ai_sensory/            # 视觉引擎（local YOLOv5-Pose + cloud RTMPose）
│   ├── cognitive/             # 教练大脑（DeepSeek / ADP / RAG / 知识库）
│   ├── sensor/                # sEMG UDP 服务 + MVC 标定
│   ├── voice_daemon.py        # 百度 ASR/TTS + 自适应 VAD
│   ├── persistence/db.py      # SQLite 持久化（训练记录、reps、教练日志）
│   ├── fatigue_model.py       # 累积疲劳剂量模型
│   └── training_plan.py       # 训练计划生成
├── templates/                 # PWA 前端（index.html + database.html）
├── tools/                     # 训练 / 数据采集 / 探针
│   ├── train_gru_three_class.py            # 深蹲 GRU 训练
│   ├── train_gru_three_class_bicep.py      # 弯举 GRU 训练
│   ├── collect_training_data.py            # 数据采集
│   ├── ironbuddy_operator_console.py       # 操作员控制台
│   └── dashboard.py                         # Streamlit 可视化面板
├── tests/                     # pytest 测试套件（70+ 测试）
├── scripts/                   # 部署 / 隧道 / 启动脚本
├── hardware_firmware/         # ESP32 sEMG 固件
├── deploy/                    # 板端部署元数据
├── systemd/                   # 板端 systemd 服务文件
├── data/                      # 知识库 (coach_kb) + 动作元数据 (custom_actions)
├── models/                    # 入口模型 (yolov8n-pose.pt)
└── docs/                      # 技术文档 + 验收指南
```

---

## 快速开始（开发主机 / WSL）

### 1. 环境准备

```bash
git clone https://github.com/qqyyqq812/IronBuddy.git
cd IronBuddy
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt   # 注：requirements 见各模块 header
```

### 2. 配置敏感凭证

```bash
cp .env.example .env
# 编辑 .env，填入：
#   - 百度 AipSpeech (BAIDU_APP_ID/API_KEY/SECRET_KEY)
#   - DeepSeek API key
#   - 云端 SSH host (CLOUD_SSH_HOST=root@your-gpu-host.com)
#   - 飞书 OpenCloud (FEISHU_APP_ID/SECRET/CHAT_ID)
#   - 腾讯 ADP knowledge base (ADP_APP_KEY)
```

### 3. 启动主服务（仅 Web UI + 模拟数据）

```bash
python3 streamer_app.py
# 浏览器打开 http://localhost:5000/
```

### 4. 完整链路启动（板端 + 云端）

> ⚠️ 这一步**需要真实的板端硬件 + 云端 GPU 节点**，单机克隆本仓库**无法跑完整功能**。

见 [DEPLOYMENT.md](DEPLOYMENT.md)。

---

## 主要功能

| 模块 | 状态 | 说明 |
|------|------|------|
| FSM 即时计数（深蹲 / 弯举） | ✅ 稳定 | 角度阈值驱动，0 延迟 |
| GRU 三分类教练点评（深蹲） | ✅ 已上板 | `extreme_fusion_gru.pt`，7D 输入 |
| GRU 三分类教练点评（弯举） | 🟡 调优中 | 个性化数据集采集中 |
| sEMG 实时波形 + MVC 标定 | ✅ 稳定 | ESP32 BLE → UDP → 7D 特征 |
| 视觉双模（本地 NPU / 云 RTMPose） | ✅ 稳定 | `/dev/shm/vision_mode.json` 热切 |
| DeepSeek LLM 对话 | ✅ 稳定 | SSE 流式 + 触发器（疲劳 / "教练" 语音 / APP 按钮） |
| 百度语音 STT/TTS | ✅ 稳定 | 自适应 VAD + ALSA 直驱 |
| Qdrant 向量 RAG | ✅ 在线 | bge-m3 嵌入 + 学术文献 / ADP 知识库 |
| 累积疲劳剂量模型 | ✅ 稳定 | 单次 + 跨次累积 |
| 飞书 OpenCloud 提醒 | ✅ 稳定 | 训练前后 interactive card 推送 |

---

## 开发文档

* **架构与决策**：[docs/technical/decisions.md](docs/technical/decisions.md)（10 大决策 + 9 条踩坑）
* **深蹲 GRU 权威指南**：[docs/验收表/深蹲神经网络权威指南.md](docs/验收表/深蹲神经网络权威指南.md)
* **弯举 GRU 权威指南**：[docs/验收表/弯举神经网络权威指南.md](docs/验收表/弯举神经网络权威指南.md)
* **语音模块权威指南**：[docs/验收表/语音模块权威指南.md](docs/验收表/语音模块权威指南.md)
* **数据采集与训练**：[docs/technical/数据采集与训练指南.md](docs/technical/数据采集与训练指南.md)
* **sEMG 特征工程**：[docs/technical/sEMG泛化实现指南.md](docs/technical/sEMG泛化实现指南.md)
* **部署指南**：[DEPLOYMENT.md](DEPLOYMENT.md)

---

## 已知限制

* **板端 Python 3.7**：不支持 `X | None` 类型注解、`match/case`、`:=` 海象运算符；无 `pandas`
* **板端 NPU 置信度量化**：person_score 通常在 0.1~0.2，`MIN_KPT_CONF` 阈值已适配（不能照搬云端浮点 >0.5）
* **音频重置魔咒**：板端开机时 Playback Path 自动置 OFF，每次启动需 `amixer SPK_HP (numid=1 val=6)` 回拨
* **云端 SSH 隧道**：依赖手动启动（`scripts/cloud_tunnel.sh` / `scripts/rag_tunnel.sh`），暂未做自动重连

---

## License

MIT — see [LICENSE](LICENSE)
