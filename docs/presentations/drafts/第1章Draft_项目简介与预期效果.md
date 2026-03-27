# 第 1 章：项目简介与预期效果

> 本章记录 IronBuddy 项目的整体定位、核心技术栈、硬件平台和最终预期效果。所有内容基于实际实现。

---

## 1.1 项目定位

IronBuddy 是一套运行在 Rockchip RK3399ProX 开发板上的**居家智能健身辅助系统**。核心理念是「免穿戴、零门槛」——用户只需站在摄像头前即可开始训练，无需佩戴任何传感器或手环。

系统利用板载 NPU 加速 YOLOv5-Pose 模型，实时提取人体 17 个骨骼关键点坐标（COCO 格式），通过自研的深蹲状态机完成动作质量判定，并经由 OpenClaw 网关调用 DeepSeek 大语言模型提供个性化的语音教练反馈。

## 1.2 核心技术关键词

| 技术 | 角色 | 说明 |
|------|------|------|
| **YOLOv5-Pose** | 2D 姿态估计 | 预训练 17 点人体骨骼模型，`.rknn` 格式 NPU 加速 |
| **NPU (RKNN)** | 推理加速 | RK3399ProX 内置 NPU，C++ 引擎驱动，15-37 FPS |
| **OpenClaw** | 大模型中转网关 | Node.js 守护进程，WebSocket 协议，管理 API Key |
| **DeepSeek API** | 智能教练 | 接收训练统计，生成个性化点评与建议 |
| **VideoPose3D** | 2D→3D Lifting | V2 新增，ONNX CPU 推理，支持肌肉激活估算 |

## 1.3 硬件平台

**主控板：Rockchip RK3399ProX**
- CPU: 双核 Cortex-A72 @1.8GHz + 四核 Cortex-A53 @1.4GHz
- NPU: 独立神经网络处理器，支持 INT8 量化推理
- RAM: 2GB LPDDR3
- OS: Debian 10 (aarch64)

**外设：**
- USB HD 720P 摄像头（UVC 协议免驱，`/dev/video5`）
- 有源蜂鸣器（GPIO 153 控制，低电平触发）
- USB 小音箱（复用 RK809 codec Card 0 SPK 通道）
- 板载麦克风（ALSA `plughw:0,0`，16kHz 采样率）

## 1.4 预期效果

1. **实时骨骼检测**：用户站在摄像头前，网页端实时显示骨骼叠加画面
2. **深蹲质量判定**：系统自动检测标准深蹲（膝盖角 <90°）和违规半蹲，实时计数
3. **即时声响警报**：违规动作触发蜂鸣器警报
4. **语音交互**：唤醒词"教练"触发语音对话，DeepSeek 生成教练点评并通过音箱播报
5. **V2 热力图**：实时显示 13 肌群累积激活百分比，检测肌肉代偿
6. **飞书推送**：语音中提及"飞书+发送"，系统自动推送训练计划到飞书

## 1.5 系统分层架构概览

```
┌───── 边缘感知层（RK3399ProX 板端）─────┐
│  C++ NPU 引擎 → /dev/shm → Python FSM  │
│  语音守护 → ASR → /dev/shm/chat_input   │
│  Streamer (Flask:5000) → HTTP API        │
│  蜂鸣器 + 音箱 + TTS                       │
├───── 指挥控制层（宿主机 WSL）──────────┤
│  OpenClaw Gateway (18789)                 │
│  SSH 反向隧道                              │
│  浏览器前端展示                             │
├───── 云端智能层 ─────────────────────┤
│  DeepSeek API → 教练点评/训练计划          │
│  飞书 WebHook → 消息推送                   │
└───────────────────────────────────────┘
```

---

## 附：关键文件清单

| 文件 | 位置 | 描述 |
|------|------|------|
| `main` | `yolo_test/build/` | C++ NPU 推理引擎（YOLOv5-Pose） |
| `main_claw_loop.py` | `hardware_engine/` | Python 主循环（FSM + DeepSeek + V2 管线） |
| `streamer_app.py` | 项目根目录 | Flask 推流中台（12 API 端点） |
| `openclaw_bridge.py` | `hardware_engine/cognitive/` | OpenClaw WebSocket 桥接 |
| `voice_daemon.py` | `hardware_engine/` | 唤醒式语音对话守护 |
| `index.html` | `templates/` | Web 仪表盘（含 SVG 热力图） |
| `start_validation.sh` | `tests/` | 一键启动脚本（代码同步 + 全服务拉起） |
