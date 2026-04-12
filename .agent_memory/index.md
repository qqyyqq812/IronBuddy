# Agent Local Memory: IronBuddy (Embedded-Fullstack)

> Last updated: 2026-04-12 (GRU训练闭环+可视化面板)

## Quick Reference
- 读取 `_entity_graph.md` 获取完整代码拓扑和架构图
- 读取 `GRU训练闭环指导手册.md` 获取完整的数据采集→训练→部署流程

## Architecture (2026-04-12)
- **视觉推理**: Cloud RTMPose-m ONNX on RTX 5090 via direct HTTPS (~100ms RTT)
- **板端 NPU**: RKNN 量化模型精度不可用(conf<0.3)，已弃用
- **通信**: /dev/shm 共享内存 IPC (atomic rename)
- **LLM**: DeepSeek via OpenClaw WebSocket (Board→WSL:18789)
- **EMG**: 模拟数据由视觉管线自动生成；真实传感器通过 UDP:8080 + emg_heartbeat 标记接管
- **GRU训练**: 7D特征 → 滑动窗口(30帧) → CompensationGRU → 相似度+分类+阶段
- **可视化**: TensorBoard (训练曲线) + Streamlit (数据探索/评估/实时推理)

## Sprint Status
- [x] Cloud RTMPose 部署 + SSH 直连隧道
- [x] FSM 状态机 (深蹲+弯举)
- [x] EMG 模拟同步（与骨架角度联动）
- [x] 语音守护 (TTS缓存 + Google ASR + retry)
- [x] DeepSeek 对话 + 教练点评 (`<think>` 剥离)
- [x] 疲劳 1500 自动重置
- [x] 前端标签动态切换 + CSS骨架 10Hz 同步
- [x] 数据采集工具 (collect_training_data.py, --auto 模式, bicep_curl 支持)
- [x] 批量采集脚本 (batch_collect.sh, 6组一键)
- [x] 数据验证工具 (validate_data.py)
- [x] 训练脚本集成 TensorBoard (train_model.py)
- [x] Streamlit 可视化面板 (dashboard.py, 4标签页)
- [ ] **GRU 实际训练** (工具就绪，等采集真实数据)
- [ ] 传感器实物对接 (ESP32 → UDP:8080)

## Critical Config
- Board: `toybrick@10.105.245.224` key: `~/.ssh/id_rsa_toybrick`
- Cloud: `root@connect.westd.seetacloud.com:14191` key: `~/.ssh/id_cloud_autodl`
- Start: `bash start_validation.sh` (从WSL执行)
- Dashboard: `streamlit run tools/dashboard.py`
- GitHub: `git@github.com:qqyyqq812/IronBuddy.git`

## 数据流 (采集→训练→推理)
```
摄像头 → cloud_rtmpose → pose_data.json ─┐
ESP32  → udp_emg_server → muscle_activation.json ─┤
                                                    ↓
                              collect_training_data.py → CSV
                                                    ↓
                              validate_data.py → 质量检查
                                                    ↓
                              train_model.py → extreme_fusion_gru.pt
                                                    ↓
                              main_claw_loop.py → 实时推理 → 前端
```

## Dev Hints
- Board Python 3.7: 不支持 `X | None` 语法, 无 pandas
- edge-tts 在板端不稳定 → 使用 ~/tts_cache/ 预缓存MP3
- 采集工具的 --auto 模式可在非交互终端(SSH)运行
