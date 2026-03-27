# IronBuddy — 基于瑞芯微端侧算力的视感融合 3D 智能健身教练引擎

> **参赛项目** | 2026 "ISEE-瑞芯微" 浙江大学移动创新竞赛
> **硬件平台** | Toybrick TB-RK3399ProX (NPU + BLE 5.0)
> **当前版本** | V3.0-dev (代码就绪，传感器选型中)

---

## 一、项目简介

IronBuddy 是一套运行在国产嵌入式 AI 芯片上的**全栈智能健身私教系统**。它通过摄像头视觉骨骼检测、穿戴式 sEMG 肌电传感器和 IMU 姿态传感器三路数据的**跨模态融合 (Sensor Fusion)**，实现对用户健身动作的精准评估和实时语音指导。

### 核心技术亮点
- **端侧 NPU 推理**：YOLO 姿态检测模型经 RKNN 量化后在 RK3399ProX NPU 上实时运行，无需云端 GPU。
- **视感融合仲裁**：当视觉因遮挡失效时，IMU 绝对倾角自动接管；sEMG 捕捉真实肌电放电信号，拒绝"假发力"。
- **大模型私教灵魂**：DeepSeek API 驱动的 AI 教练，内置 20 年执教经验的严厉人格 (`SOUL.md`)，基于多模态数据口语化点评。
- **分屏实时证据链**：APP 同屏展示 3D 骨架热力图 + EMG 原始波形滚动曲线，摘掉传感器信号即消失，物理因果不可伪造。

---

## 二、系统架构

```
[ 手机端 APP ]                         [ 边缘计算中枢：RK3399ProX ]

📱 原生 APP        <-- HTTP/WS -->   🟢 streamer_app.py (Flask 推流中台)
 - 3D 骨架热力图                            ↑ 读 fsm_state.json
 - EMG 实时波形                             ↑ 读 result.jpg
 - 教练语音交互                             │
                                    🧠 main_claw_loop.py (FSM + LLM)
                                     ├─ 读 /dev/shm/pose_data.json  (NPU 视觉)
                                     ├─ 读 /dev/shm/sensor.json     (蓝牙传感器)
                                     └─ ws → DeepSeek API (经 SSH 隧道)

🩹 EMG+IMU 臂环    --- BLE 5.0 --->  🔵 ble_wearable.py (蓝牙采集守护进程)
 (如 gForcePro+)                      └─ 写 /dev/shm/sensor.json
```

**数据流**：NPU 吐骨骼坐标 + 蓝牙守护进程吐 EMG/IMU → FSM 多模态融合决策 → 推流中台打包 → 手机 APP 渲染 3D + 波形。

---

## 三、项目结构

```
embedded-fullstack/
├─ hardware_engine/         核心算法中枢
│   ├─ main_claw_loop.py      FSM 状态机 + 大模型结算
│   ├─ voice_daemon.py         Vosk 离线语音唤醒
│   ├─ cognitive/              教练人格 (SOUL.md) + OpenClaw 桥接
│   ├─ ai_sensory/             NPU 骨骼数据订阅
│   └─ sensor/                 麦克风 + 蓝牙传感器 (待开发)
├─ biomechanics/            生物力学引擎
│   ├─ lifting_3d.py           2D→3D 关节提升
│   ├─ muscle_model.py         肌肉激活建模
│   └─ exercise_profiles.json  动作配置库
├─ streamer_app.py          Flask 推流中台 (MJPEG + API)
├─ templates/               前端模板 (index + history)
├─ models/                  模型权重 (YOLO + Vosk, 不进 Git)
├─ deploy/                  部署配置 (supervisord + 板端网络)
├─ scripts/                 运维脚本
├─ tests/                   测试与压测
├─ sandbox/                 独立实验脚本
├─ backups/                 V1 快照归档
└─ docs/
    ├─ presentations/         答辩演示 (Marp + 用户手册)
    ├─ technical/             技术沉淀 (迭代记录 + 任务看板)
    ├─ hardware_ref/          板端参考资料 (不进 Git)
    └─ handover/              会话交接存档
```

---

## 四、团队分工

| 成员 | 职责领域 | 核心交付物 |
|------|----------|-----------|
| **队长** | 视觉 AI 管线、FSM 融合算法、大模型对接、项目统筹与答辩 | `hardware_engine/`, `biomechanics/`, 答辩 PPT |
| **队友** | 蓝牙协议解析、EMG/IMU 数据净化、手机原生 APP 全栈 | `ble_wearable.py`, `mobile_app/` |

**协作接口**：两人通过 `/dev/shm/sensor.json` (共享内存 JSON) 解耦，各自独立开发互不阻塞。

---

## 五、快速部署

```bash
# 1. 同步代码到板端
rsync -avz ./ toybrick@<BOARD_IP>:/home/toybrick/ --exclude='.git' --exclude='models/'

# 2. 部署 Vosk 模型 (首次)
ssh toybrick@<BOARD_IP> "bash scripts/deploy_vosk.sh"

# 3. 启动全链路
cd tests && ./start_validation.sh

# 4. 访问
# 主页: http://<BOARD_IP>:5000/
# 历史: http://<BOARD_IP>:5000/history
```

---

## 六、版本演进

| 版本 | 状态 | 核心交付 |
|------|------|---------|
| V1 | ✅ | 2D 姿态检测 + FSM 深蹲计数 + DeepSeek 教练 |
| V2 | ✅ | 3D Lifting + 肌肉热力图 + 累积激活模式 |
| V2.2 | ✅ 代码就绪 | Vosk 离线 ASR + MJPEG 推流 + 富 Prompt |
| V2.5 | ✅ 代码就绪 | 训练历史页 + SOUL 教练人格 + supervisord |
| **V3.0** | 🔨 **开发中** | **视感融合 (EMG+IMU) + 手机 APP + 竞赛答辩** |
