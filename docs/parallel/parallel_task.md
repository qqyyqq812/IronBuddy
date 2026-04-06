# IronBuddy V3 宏观调度台 (Macro TODO)

> **全栈重塑突击周**
> 下表罗列了多路大军的交汇进度点，每次 `/para-join` 全权由总线指挥官 (Director) 核销状态。

| 分配专员 | 进攻阵地 | 阶段指标 | 当前状态 |
| :--- | :--- | :--- | :--- |
| **Agent 1** (通信组) | `voice_daemon.py` | 解决麦克风死锁盲区，恢复虚拟教练大模型应答系统。 | 🔴 待开营 |
| **Agent 2** (视觉组) | `ai_sensory/` | 移除 YOLOv5 旧产线，接入高精度 NPU 版 RTMPose 管道并实现骨骼滤波去抖点。 | 🔴 待开营 |
| **Agent 3** (算法组) | 独立探针 / `udp_emg_server.py`| 实现**晚期多模态融合网络 (Late Fusion MMF)**：构建 MLP 动作级代偿判决。 | 🔴 待开营 |

## 📦 主线里程碑 (Global Milestones)
- [x] 完成本机的代码资产剥离（黄金版本确认）。
- [x] 完成靶机的超大型污染文件（G级别 log与tar）删除。
- [x] 完成极净 `.gitignore` 和工业极 `README.md` 的编制与全网分发。
- [ ] MMF 多模态骨架与肌电协同体系落地（Wait for Agent 2, 3）。
- [ ] 项目整体通过 24 小时耐久压测跑通验证，录制决赛演示短片。
