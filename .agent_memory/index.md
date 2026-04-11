# Agent Local Memory: IronBuddy (Embedded-Fullstack)

> Last updated: 2026-04-11 (V3.0 sprint complete)

## Quick Reference
读取 `_entity_graph.md` 获取完整代码拓扑和架构图。

## Architecture (2026-04-11)
- **视觉推理**: Cloud RTMPose-m ONNX on RTX 5090 via **direct** SSH tunnel (Board→Cloud, ~100ms RTT)
- **板端 NPU**: RKNN 量化模型精度不可用(conf<0.3)，已弃用
- **通信**: /dev/shm 共享内存 IPC (atomic rename)
- **LLM**: DeepSeek via OpenClaw WebSocket (Board→WSL:18789)
- **EMG**: 模拟数据由视觉管线同步生成（角度驱动），真实传感器通过 emg_heartbeat 标记接管

## Sprint Status
- [x] Cloud RTMPose 部署 + SSH 直连隧道
- [x] FSM 状态机 (深蹲+弯举)
- [x] EMG 模拟同步（与骨架角度联动）
- [x] 语音守护 (mono fix, Google ASR)
- [x] DeepSeek 对话 + 教练点评
- [x] 疲劳 1500 自动重置 + 自动触发 API
- [x] 鬼影过滤（真实置信分数 > 0.15）
- [x] 小人发力闪烁 + 疲劳渐变渲染
- [ ] **GRU 神经网络训练** (代码就绪，需采集数据)
- [ ] 传感器实物连接测试

## Critical Config
- Board: `toybrick@10.105.245.224` key: `~/.ssh/id_rsa_toybrick`
- Cloud: `root@connect.westd.seetacloud.com:14191` key: `~/.ssh/id_cloud_autodl`
- Cloud model: `/root/ironbuddy_cloud/rtmpose_m.onnx`
- Webcam mic: `hw:Webcam,0` **mono** (CHANNELS=1)
- Start: `tclsh ~/projects/embedded-fullstack/start_validation.tcl`
- Stop: `tclsh ~/projects/embedded-fullstack/stop_validation.tcl`

## Dev Hints
- 修改代码后需重读 `_entity_graph.md` 更新拓扑
- Board Python 3.7: 不支持 `X | None` 语法, 无 pandas
- 云端 ONNX 模型 54MB, GPU 推理 ~10ms
