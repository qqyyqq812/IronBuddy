# IronBuddy V4 架构升级 — 深度研究报告
> 🔍 搜索轮次：3 轮并行广度搜索 | 精读维度：端云协同、WebRTC、智能相机 | 生成时间：2026-04-07

## 核心发现
针对 RK3399Pro 强行剥壳跑 RTMPose 所引发的维度崩溃与算力极限（SimCC的 BatchMatMulV2/重组降维过程对 Cortex-A72 CPU 与老旧 v1 NPU 极度不友好），业界通用的解决方法不是死磕单机算力榨取，而是进行**物理架构解耦**。

主要有以下三种升级捷径，能在**不更换 RK3399Pro 核心板**的约束下，斩获 30FPS 超清流畅且精准的骨架捕捉。

## 详细分析

### 维度 1：端云协同与算力卸载 (Cloud-Offloading + WebRTC)
既然我们拥有 `AutoDL RTX 5090` 这样的顶级算力资源，让 RK3399Pro 仅作为“眼”，让 5090 作为“脑”是最高性价比的方案。
- **推流协议改造**：目前的 Flask HTTP / MJPEG 是一帧张发图片，属于 TCP 阻塞流，天然带来高延迟卡顿。应当用 Python `aiortc` 库引入 **WebRTC** 协议，通过 UDP 将纯视频串流毫秒级推送给 AutoDL。
- **并行回传**：RTX 5090 接到视频流后，单张 RTMPose 推理仅需 <5ms。提取出纯文本的 `JSON 骨架点 (kpts)` 后，通过 WebSockets 双工通道直接广播回前端网页或板端 FSM。
- **结论**：这是业界的 Media Bridge 标准范式，彻底消灭板端发热与算力崩溃瓶颈。

### 维度 2：感知硬件升级 (Smart Edge Camera)
如果不希望严重依赖自习室的网络去连云端（避免断网导致无法健身）：
- **外置算力大脑**：可以外接类似 **Luxonis OAK-D** 的智能深度摄像头。
- **原理**：这些设备内部集成了 Intel Myriad X 或类似的 VPU 张量芯片。它们在摄像头硬件底层就把 Pose Estimation 算完了，并通过 USB 只向主板发送高频的 `[17, 3]` 坐标矩阵。
- **结论**：对于系统而言，摄像头直接插拔即用，RK3399Pro 无需跑任何视觉运算，专心跑深蹲的这套 Web UI 和 FSM 状态机即可，系统流畅度彻底质变。

### 维度 3：降级感知模型 (YOLOv8-Pose 方案)
如果要求零成本且纯离线：
- RTMPose 的 SimCC 头部对 NPU v1 支持极差是业界痛点。相反，官方的 `rknn_model_zoo` 里对 **YOLOv8-Pose** 的支持进行了极其深度的底层 C++ 算子优化。
- **结论**：放弃高精度的 RTMPose，改用基于回归坐标的 YOLOv8-pose INT8 量化模型，能避免大量复杂的维度排布（NCHW/NHWC）争端。

## 关键对比

| 方案 | 核心技术 | 流畅度 | 核心优势 | 最大局限 |
|---|---|---|---|---|
| **端云 WebRTC 协同** | 发送视频到 RTX 5090 | 极高 (30FPS+) | 零成本，可部署极致模型 | 极度依赖外网 UDP 穿透环境 |
| **OAK-D 智能相机** | 更换带独立 NPU 的摄像头 | 极高 (30/60FPS) | 纯离线稳如老狗，RK3399 零负载 | 需要预算采购新硬件（约千元） |
| **降级 YOLOv8-Pose** | 替换轻量级底座 | 尚可 (~15FPS) | 无需花钱，无需改网络架构 | 骨架精度、抗遮挡能力显著下降 |

## 进一步研究建议
由于您已拥有 AutoDL 服务器储备，极其建议向 **维度 1 (WebRTC + WebSocket 并行回传)** 演进。建议下一步优先研究 `aiortc` 在 WSL2 与 AutoDL 之间的 P2P 穿透打洞测试。

## 参考来源
1. WebRTC Ultra Low Latency Python AIortc Implementation
2. Luxonis OAK-D On-Device Pose Tracking Documentation
