<div align="center">
  <h1>🏋️ IronBuddy V3 </h1>
  <p><strong>智能康复与深蹲评估边缘双轨(Edge Compute)教练系统</strong></p>
  <i>Powered by <strong>RK3399ProX NPU</strong> & <strong>ESP32 UDP Sensor Network</strong></i>
</div>

---

## 🚀 项目概述 (Project Overview)
**IronBuddy V3** 是一个为现代智能家居和康复医学级评估设计的复合多模态（Multi-modal Fusion）体测系统。
基于传统的单通道深蹲相机，本项目创造性地拓展了**皮肤微秒级肌电传感网络 (sEMG)**，并结合后期微层神经网络计算（Late Fusion Matrix），实现对运动动作表层（几何骨骼点）与里层（肌肉爆发时延、代偿转移）的彻底透视。不仅能捕捉动作，更能判断你在深蹲疲倦时是否发生了**“脊柱代偿借力”**！

## 🧩 核心架构与物理拓扑图 (Architecture)

```mermaid
graph TD
    subgraph Edge NPU 板端中枢 (RK3399Pro)
        A[摄像头输入] --> B["RTMPose/Yolo (AI_Sensory)"]
        B -- 抽取3D/2D骨架矩阵 --> D((FSM 融合状态中枢 / main_claw))
        
        C[UDP肌电网络] --> E["Biquad/FFT DSP滤波网"]
        E -- 1000HzRMS肌肉激活特征 --> D
        
        D -- 下发评级/代偿侦测 --> F(Flask 流媒体/WebSocket控制中台)
    end
    
    subgraph 远端渲染终端 (Web Client)
        F --> G[推流展示屏 / 2.5D CSS 物理态引擎]
    end

    subgraph 下位机矩阵 (ESP32)
        H[贴片传感器] --> C
    end
```

## 🛠️ 项目代码树解构 (Code Structure)

```text
embedded-fullstack/
├── docs/                      # 大赛报告、答辩 PPT (Marp) 与并行的 Agent技术文卷
│   └── parallel/              # Agent 1,2,3 的分配工作区 (见下文协同栈)
├── hardware_engine/           # RK3399Pro NPU 后端推理的终极大脑
│   ├── ai_sensory/            # [Agent 2 辖区] 重型视觉框架 (RTMPose 推流)
│   ├── sensor/                # [Agent 3 辖区] Biquad与肌电FFT捕集，时序对齐探针
│   ├── voice_daemon.py        # [Agent 1 辖区] 驱动底层麦克风与 TTS 生成
│   └── main_claw_loop.py      # [总经理枢纽] FSM 动作融合评分机制
├── hardware_firmware/         # 板端代码
│   └── esp32_emg/             # 原生物质波采样下发固件 (ADC -> UDP)
├── streamer_app.py            # 高并发脱 GIL 重束缚的高速 Web 中转器
└── templates/                 # 赛博朋克深色仪表盘面板 (CSS 动效追踪)
```

## 🏃 极速运行指南 (Quick Start)
要在 RK3399Pro 板端一键唤起整个分布式中枢与 WebUIServer：

```bash
# 激活 V3 推流全景应用网络并注入环境变量
sh scripts/start_validation.sh

# 系统成功跑通后，请在浏览器中打开:
http://<板端IP>:5000/
# 即可收看高刷率的骨架重绘制界面
```

---
*Created dynamically by the Agentic Operations Center.*
