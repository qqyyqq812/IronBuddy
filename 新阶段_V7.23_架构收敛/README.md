# 新阶段 V7.23 统一工作规范

> **本文件是 `/home/qq/projects/embedded-fullstack/` 项目根目录下的最高权重规范。**
> 后续任何 Agent 进入本项目工作前，必须先读完本目录三份文件（`README.md` / `ARCHITECTURE_INDEX.md` / `UPDATE_LOG.md`）。

---

## 🎯 目标导向（唯一，放弃之前的枝节验收表）

本阶段只抓两个效果：

1. **语音端测试成功**
   - 喊一次"教练" = 进入一次对话（说 1 句 → API 回一次 → 回 SLEEP）
   - 疲劳满 = 自动 API 调用 + TTS 语音播报
   - **不允许其他触发方式**（没有 UI 点按、没有聊天窗口直发、没有其他唤醒词）

2. **机电传感器伪造成功**
   - 模拟模式：骨架角度驱动 7 通道 EMG（`cloud_rtmpose_client.py::_generate_emg_from_angle`）
   - 真实模式：ESP32 → UDP:8080 → `udp_emg_server.py`
   - 两者切换判定：心跳时戳 `< 1.0s` → 真实；否则 → 模拟
   - **两种模式不得冲突**，任意时刻只有一方覆写 `/dev/shm/muscle_activation.json`

---

## 🚫 绝对禁止（后续所有 Agent 必须遵守）

### 上板入口禁令
- ✅ **唯一允许**：`bash scripts/start_validation.sh`
- ❌ 禁止新增任何 `rsync` / `scp` / `ssh ... <<EOF` 的上板脚本
- ❌ 禁止在 `start_all_services.sh` 以外启动板端进程
- ❌ `start_collect.sh` 已精简为"只关闭 voice+FSM"的 thin wrapper，**必须前置执行 `start_validation.sh`**

### 代码改动禁令
- ❌ 禁止修改已稳定的业务逻辑：FSM 计数、GRU 推理、飞书推送、视觉切换、OpenClaw Gateway
- ❌ 禁止新增"回退方式" / "备选路径" / 新环境变量
- ❌ 禁止绕过 `.api_config.json` 读取凭证（单一配置源）
- ❌ 禁止在 `voice_daemon.py` 的半双工 PTT 机制外另开录音/播音通道

### 文档禁令
- ❌ 禁止创建 `handoff_*.md` / `EXECUTION_PLAN_*.md` / 演讲稿 / 临时调研报告
- ❌ 禁止在项目根目录散落 `.md` / `.py` / `.log` 文件
- ❌ 禁止修改 `docs/GitHub_Golden_Pool/` 内的任何文件（只读展示池）

### Agent 行为禁令
- ❌ 禁止"并行多 Agent 改同一文件"，改动前必须在 `UPDATE_LOG.md` 声明意图
- ❌ 禁止静默修改不记录日志
- ❌ 禁止清空 / 重写 `UPDATE_LOG.md` 的历史

---

## 📋 语音模块核心契约（V7.23 修复后）

### 唤醒流程（唯一）

```
[SLEEP]
  ↓ record_with_vad(6s, wake)
  ↓ sound2text() → text
  ↓ is_wake_word(text)?
  ├─ 否 → 回 [SLEEP]
  └─ 是 → _dialog_enter()
          ↓ _speak_ack("嗯", 0.3s TTS)
          ↓ _mic_allowed.wait()           ← 等播报结束
          ↓ record_with_vad(6s, fast_start=True)  ← 二次 VAD
          ↓ _route_text(text2)
          ├─ 硬编码命令 → 本地执行 + TTS 回报
          └─ 闲聊 → DeepSeek API + TTS 播报
          ↓ killall -9 arecord
          ↓ _dialog_exit()
          ↓ 回 [SLEEP]（必须重新喊"教练"才能再次唤醒）
```

### 疲劳触发路径（跨进程）

```
main_claw_loop._ds_wrapper
  ↓ touch /dev/shm/llm_inflight   ← 禁麦门禁
  ↓ killall -9 arecord
  ↓ await DeepSeek API
  ↓ 写 /dev/shm/llm_reply.txt + seq++
  ↓ remove /dev/shm/llm_inflight

voice_daemon._llm_reply_watcher (轮询 0.2s)
  ↓ 检测到 seq 变化
  ↓ [V7.23 新增] _mic_allowed.clear()       ← 预占麦克风
  ↓ [V7.23 新增] touch /dev/shm/voice_speaking
  ↓ _speak_llm(txt) 入队 SpeechManager
  ↓ killall -9 arecord（冗余保险）

SpeechManager worker
  ↓ text2sound() → WAV
  ↓ play_audio(WAV)
      ↓ [V7.23 冗余] touch /dev/shm/voice_speaking
      ↓ aplay
      ↓ [V7.23] remove /dev/shm/voice_speaking
  ↓ _mic_allowed.set()
```

### 互斥信号（三重保护）

| 信号 | 管理方 | 生命周期 |
|---|---|---|
| `/dev/shm/llm_inflight` | `main_claw_loop._ds_wrapper` | API 调用起 → API 返回止 |
| `/dev/shm/voice_speaking` | `voice_daemon.play_audio` + `_llm_reply_watcher` | TTS 合成起 → aplay 完成止 |
| `_mic_allowed` (threading.Event) | `voice_daemon.SpeechManager` | TTS 入队起 → aplay 完成止 |

voice_daemon 主循环 L1285 检查：`llm_inflight OR voice_speaking` 任一存在 → 阻塞 0.2s。
主循环 L1280 `_mic_allowed.wait()` → 进程内 PTT 半双工。

---

## 🔌 EMG 契约（V7.23 修复后）

### 心跳格式（原子 JSON）

```json
{"ts": 1713666000.123, "connected": true}
```

- `udp_emg_server.py`：每 DSP 周期通过 `.tmp` + `os.rename` 原子写入
- `cloud_rtmpose_client.py`：读取 → `time.time() - hb.ts < 1.0` 判定真实传感器在线

### 切换逻辑

```
[DSP cycle]
  ↓ 收到 UDP 包? 
  ├─ 是 → 解析 → 写 muscle_activation.json + 写 emg_heartbeat (JSON+ts)
  └─ 否（0.5s 无更新）→ 停止覆写 muscle_activation.json

[Vision cycle]
  ↓ _is_emg_sensor_live()?
  ├─ 是（1s 内有心跳）→ return，不覆写 muscle_activation.json
  └─ 否 → 骨架角度 → 模拟 EMG 覆写 muscle_activation.json
```

---

## 📁 目录地位

本目录 3 份文件为项目最高权重：

| 文件 | 作用 | 变更频率 |
|---|---|---|
| **README.md** | 统一规范（本文件） | 阶段升级时才改 |
| **ARCHITECTURE_INDEX.md** | 顶到底全链路架构索引 | 架构变动时 |
| **UPDATE_LOG.md** | 实时更新日志 | 每次 Agent 改动后追加 |

### 新窗口 Agent 上下文喂料

```
将以下三份文件一次性喂给新 Agent：
  1. 新阶段_V7.23_架构收敛/README.md          （规范 + 禁令）
  2. 新阶段_V7.23_架构收敛/ARCHITECTURE_INDEX.md （架构地图）
  3. 新阶段_V7.23_架构收敛/UPDATE_LOG.md        （历史改动脉络）
```

---

## 🔧 快速命令备忘

```bash
# 唯一上板入口
bash scripts/start_validation.sh

# 停掉 voice + FSM 进入采集模式
bash scripts/start_collect.sh

# 板端日志
ssh toybrick@10.18.76.224 'tail -f /tmp/voice.log'
ssh toybrick@10.18.76.224 'tail -f /tmp/npu_main.log'
ssh toybrick@10.18.76.224 'tail -f /tmp/mainloop.log'

# 手动清理残留信号（紧急场景）
ssh toybrick@10.18.76.224 'sudo rm -f /dev/shm/voice_speaking /dev/shm/llm_inflight'
```

---

## 📌 项目信息（备忘）

- 板端: `toybrick@10.18.76.224:/home/toybrick/streamer_v3/`
- 云端 GPU: `ssh -p 42924 root@connect.westd.seetacloud.com`
- DeepSeek API: `.api_config.json::DEEPSEEK_API_KEY`
- 百度语音: `.api_config.json::BAIDU_*`
- 飞书 Bot: `.api_config.json::FEISHU_*`

**所有密钥放 `.api_config.json`，UI Settings Tab 显示掩码（V7.23 新增）**。
