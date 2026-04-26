# IronBuddy V7.23 全链路架构索引

> 从顶到底的代码地图。后续 Agent 改动前必须参照本文件定位文件与契约。

---

## 1. 顶层视图

```
┌─────────────────────────────────────────────────────────────┐
│                      用户                                   │
│  (喊"教练" | 做深蹲/弯举 | 浏览器访问 :5000 | Settings)     │
└─────────────────────────────────────────────────────────────┘
              ↓ 声音  ↓ 动作  ↓ HTTP   ↓ 配置
┌─────────────────────────────────────────────────────────────┐
│                    板端 (toybrick RK3399ProX)                │
│  ┌─────────┬─────────┬────────┬─────────┬──────────────┐    │
│  │ vision  │streamer │  fsm   │   emg   │    voice     │    │
│  │ (NPU/   │(Flask+  │(深蹲/  │(UDP:    │(百度 TTS/STT │    │
│  │  Cloud) │ MJPEG)  │ 弯举   │ 8080    │+VAD+ 唤醒)   │    │
│  │         │         │ +GRU+  │ 接收    │              │    │
│  │         │         │ API)   │         │              │    │
│  └────┬────┴────┬────┴───┬────┴────┬────┴──────┬───────┘    │
│       └─────────┴────────┴─────────┴───────────┘            │
│                   IPC: /dev/shm/*.json                      │
└─────────────────────────────────────────────────────────────┘
              ↑ rsync ↑ ssh
┌─────────────────────────────────────────────────────────────┐
│                    WSL (开发机)                              │
│  bash scripts/start_validation.sh ← 唯一上板入口            │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 五进程拓扑

| 进程 | 启动脚本 | 源码 | 作用 |
|---|---|---|---|
| **vision** | `start_all_services.sh L38` | `hardware_engine/ai_sensory/cloud_rtmpose_client.py` | 视觉推理 + 模拟 EMG（骨架驱动） |
| **streamer** | `start_all_services.sh L42` | `streamer_app.py` | Flask 后端 + MJPEG:8080 + 25+ API |
| **fsm** | `start_all_services.sh L46` | `hardware_engine/main_claw_loop.py` | 深蹲/弯举 FSM + GRU 推理 + DeepSeek 触发 |
| **emg** | `start_all_services.sh L50` | `hardware_engine/sensor/udp_emg_server.py` | UDP:8080 接收真实 ESP32 EMG |
| **voice** | `start_all_services.sh L52` → `start_voice_with_env.sh` | `hardware_engine/voice_daemon.py` | 百度 AipSpeech TTS/STT + VAD + 唤醒词 |

启动封装：
```bash
launch() {
    # setsid + nohup + disown + < /dev/null 完全脱离控制终端
    setsid nohup "$@" > "/tmp/${name}.log" 2>&1 < /dev/null &
    echo "$!" > "/tmp/ironbuddy_${name}.pid"
    disown 2>/dev/null || true
}
```

---

## 3. IPC 拓扑 `/dev/shm/`

### 视觉 / EMG
| 文件 | 写入方 | 读取方 | 格式 |
|---|---|---|---|
| `result.jpg` | vision | streamer (MJPEG) | JPEG 二进制 |
| `skeleton.json` | vision | fsm | `{"kpts": [[x,y,conf]...], "ts": ...}` |
| `muscle_activation.json` | vision (模拟) / emg (真实) | streamer (UI) | `{"quadriceps": 50.0, ...}` |
| `emg_heartbeat` | emg | vision | **V7.23**: `{"ts": ..., "connected": true}` 原子 JSON |

### FSM / LLM
| 文件 | 写入方 | 读取方 | 格式 |
|---|---|---|---|
| `fsm_state.json` | fsm | streamer (UI) | `{"exercise": "squat", "reps": 5, "fatigue": 800}` |
| `vision_mode.json` | UI | vision | `{"mode": "local_npu" / "cloud_rtmpose"}` |
| `user_profile.json` | UI | fsm | `{"exercise": "squat" / "bicep_curl"}` |
| `fatigue_limit.json` | UI/voice | fsm | `{"limit": 1500}`（消费即删） |

### 语音互斥（V7.23 新增 voice_speaking）
| 文件 | 写入方 | 读取方 | 语义 |
|---|---|---|---|
| `llm_inflight` | fsm._ds_wrapper | voice 主循环 | API 调用中，禁麦 |
| `voice_speaking` | voice.play_audio / voice._llm_reply_watcher | voice 主循环 | TTS 合成/播放中，禁麦 |
| `voice_interrupt` | UI / fsm | voice.play_audio | 强制打断正在播放的 aplay |
| `chat_active` | voice | UI | 对话态，UI 显示"对话中" |
| `mute_signal.json` | UI/voice | 全局 | `{"muted": true/false, "ts": ...}` |

### LLM 管道
| 文件 | 写入方 | 读取方 | 格式 |
|---|---|---|---|
| `llm_reply.txt` | fsm (疲劳触发) | voice._llm_reply_watcher | 纯文本 |
| `llm_reply.txt.seq` | fsm | voice | 递增整数 |
| `chat_input.txt` | UI | fsm._chat_handler | 纯文本 |
| `chat_reply.txt` + `.seq` | fsm._chat_handler | voice._chat_reply_watcher | 纯文本 + 递增 |

---

## 4. 单一上板路径（唯一）

### 脚本链：`scripts/start_validation.sh` (WSL)

```
[1] 校验 ~/.ssh/id_rsa_toybrick 存在（否则从 /mnt/c/temp/id_rsa 复制）
[2] 板卡连通性测试（5s timeout）
[3] 云端 RTMPose 健康检查 via curl http://localhost:6006/health
    └─ 未就绪则 SSH 到云端启动 rtmpose_http_server.py
[4] rsync 全量同步（排除 .git/*.tar.gz/docs/hardware_ref/.agent_memory/data）
[5] scp 强制上传 models/extreme_fusion_gru.pt
[6] SSH 远程执行 start_all_services.sh
[7] 输出推流地址 http://10.18.76.224:5000/
```

### 板端启动：`scripts/start_all_services.sh`

```
[1] 读 .api_config.json → export 所有 API Key
[2] amixer 重置音频通路 (Playback Path=2, Capture MIC Path=1)
[3] launch vision   → cloud_rtmpose_client.py
[4] launch streamer → streamer_app.py
[5] launch fsm      → main_claw_loop.py
[6] launch emg      → udp_emg_server.py
[7] launch voice    → start_voice_with_env.sh → voice_daemon.py
[8] cloud_tunnel.sh （可选，失败降级本地 NPU）
```

---

## 5. 关键文件索引

### 语音端
- [hardware_engine/voice_daemon.py](../hardware_engine/voice_daemon.py)
  - `play_audio()` L675 — 播放 WAV（V7.23: 加 voice_speaking 信号）
  - `SpeechManager` L390-500 — TTS 队列 + PTT 半双工
  - `_llm_reply_watcher()` L1666 — LLM 回复触发 TTS
  - `record_with_vad()` L756 — VAD 录音
  - `_is_wake_word()` — 唤醒词模糊匹配
  - 主循环 L1270-1400 — SLEEP → WAKE → dialog → SLEEP

### FSM / LLM
- [hardware_engine/main_claw_loop.py](../hardware_engine/main_claw_loop.py)
  - `_ds_wrapper()` L847 — DeepSeek 触发 wrapper (管理 llm_inflight)
  - `_deepseek_fire_and_forget()` L700 — API 调用 + 写 llm_reply.txt
  - `_chat_handler()` L873 — UI 聊天入口
  - 疲劳累积 L362 — 标准深蹲 +214.3 / 次

### 视觉 / EMG
- [hardware_engine/ai_sensory/cloud_rtmpose_client.py](../hardware_engine/ai_sensory/cloud_rtmpose_client.py)
  - `_generate_emg_from_angle()` L293 — 骨架驱动模拟
  - `_is_emg_sensor_live()` — **V7.23** 心跳时戳判定
- [hardware_engine/sensor/udp_emg_server.py](../hardware_engine/sensor/udp_emg_server.py)
  - 主循环 L86-191 — UDP 接收 + 心跳写入

### Web / 配置
- [streamer_app.py](../streamer_app.py) — Flask 主后端
  - Settings API — **V7.23** Key 掩码显示
- [templates/index.html](../templates/index.html) — PWA 前端

### 启动
- [scripts/start_validation.sh](../scripts/start_validation.sh) — **唯一上板入口**
- [scripts/start_all_services.sh](../scripts/start_all_services.sh) — 板端 5 进程编排
- [scripts/start_voice_with_env.sh](../scripts/start_voice_with_env.sh) — voice 专用 env 注入
- [scripts/start_collect.sh](../scripts/start_collect.sh) — **V7.23** 精简为 thin wrapper（停 voice+FSM）
- [scripts/probe_enable.sh](../scripts/probe_enable.sh) — UI probe 热更新
- [scripts/cloud_tunnel.sh](../scripts/cloud_tunnel.sh) — 云端 SSH 隧道

---

## 6. 历史踩坑速查（摘自 decisions.md）

### V7.11 每组只触发一次 API
- 症状：一组内每个标准动作都触发 API
- 修复：`_this_set_triggered` 标志 + "下一组"才重置

### V7.17 M11 单轮顽疾
- 症状：唤醒+指令处理后，下一次喊"教练"仍被当作"继续对话"
- 修复：`dialog_exit` 前 `killall -9 arecord` + `_wait_sm_idle(3.0s)`

### V7.18 EMG 双模竞态
- 症状：传感器断连后，vision 一直等 2s 才回滚
- 修复（本次再次加强）：心跳 1s 超时 + JSON 原子写

### V7.22 禁麦门禁
- 症状：API 调用期间用户说话被误录
- 修复：`/dev/shm/llm_inflight` 信号
- **V7.23 补强**：增加 `voice_speaking` 信号覆盖 TTS 播报期

### V4.9 aplay 三段式释放
- 症状：长句 TTS 被第二条 aplay 抢占，出现"加速尖锐"
- 修复：wait(10s) → terminate + 0.3s + kill

---

## 7. 测试入口

```bash
# 全链路启动
bash scripts/start_validation.sh

# 单独测语音
ssh toybrick@10.18.76.224 'cd /home/toybrick/streamer_v3 && python3 hardware_engine/voice_daemon.py'

# 可视化 (开发机)
streamlit run tools/dashboard.py

# 数据采集
python tools/collect_training_data.py --mode bicep_curl

# GRU 训练
python tools/train_model.py
```

---

## 8. 板端环境约束（红线）

- Python **3.7** —— 禁止 `X | None`, `match/case`, `:=`, `pandas`
- 进程管理 —— 必须 `setsid+nohup+disown`, `pgrep` 用 bracket trick（`[c]loud`）
- 硬件 —— HDMI 需 `startx --nocursor`，音频 `amixer` 每次开机必刷 Playback Path
- NPU 置信度 —— `MIN_KPT_CONF` 必须 ~0.08（不能照搬云端 0.5）

详见 [.claude/rules/toybrick_board_rules.md](../.claude/rules/toybrick_board_rules.md)。
