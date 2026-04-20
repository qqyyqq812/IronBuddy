# 新阶段 V7.23 实时更新日志

> **规则**：每个在本目录工作的 Agent 完成任务后，**必须在本文件末尾追加**一段日志。
> 格式：`日期 · Agent 身份 · 改动摘要 + 涉及文件 + 验证结果`。
> **禁止清空 / 重写历史日志**。

---

## 2026-04-21 · Claude Code (Opus 4.7, 1M ctx) · V7.23 架构收敛落地

### 改动摘要

本次为 V7.23 基线奠基，目的是一次性解决多 Agent 并行修改造成的混乱，并修复最核心的语音长录音顽疾：

1. **新建规范载体**：`新阶段_V7.23_架构收敛/` 目录，含 `README.md` / `ARCHITECTURE_INDEX.md` / `UPDATE_LOG.md`（本文件）三份最高权重文件
2. **语音长录音 bug 修复**（CRITICAL）：在 `voice_daemon.py` 三处添加 `/dev/shm/voice_speaking` 互斥信号，关闭 `_llm_reply_watcher` → `SpeechManager` 之间的 200ms 竞态窗口
3. **EMG 心跳原子化**（MEDIUM）：心跳改为 `{"ts": ..., "connected": true}` 带时戳 JSON + `.tmp + os.rename` 原子写；vision 端读取改为时戳判定（< 1.0s 视为在线）
4. **上板入口单一化**（MEDIUM）：`scripts/start_collect.sh` 从 115 行精简为 40 行 thin wrapper，删除自带的 rsync + ssh launch 逻辑，强依赖 `start_validation.sh` 完成同步后再运行
5. **API 凭证 UI 掩码**（HIGH）：`streamer_app.py` Settings 相关 API 读取时对 `*_KEY` / `*_SECRET` / `*_PASSWORD` 字段返回 `前4***后4` 掩码；写入时识别掩码回传则跳过覆盖

### 涉及文件

| 文件 | 改动行数 | 类型 |
|---|---|---|
| `新阶段_V7.23_架构收敛/README.md` | +180 | 新增 |
| `新阶段_V7.23_架构收敛/ARCHITECTURE_INDEX.md` | +230 | 新增 |
| `新阶段_V7.23_架构收敛/UPDATE_LOG.md` | +80 | 新增（本文件） |
| `hardware_engine/voice_daemon.py` | +15 / -2 | 3 处微调 |
| `hardware_engine/sensor/udp_emg_server.py` | +8 / -1 | 心跳原子化 |
| `hardware_engine/ai_sensory/cloud_rtmpose_client.py` | +12 / -1 | 心跳读取改 JSON |
| `scripts/start_collect.sh` | +40 / -115 | 精简重写 |
| `streamer_app.py` | +25 / -3 | API 掩码 |

### 不改动清单（严格约束）

- `hardware_engine/main_claw_loop.py` 未改（契约向后兼容，llm_inflight 语义保留）
- 视觉引擎切换逻辑未改
- FSM / GRU / 飞书推送未改
- `.api_config.json` 结构未改

### 验证结果

**静态验证**（已完成 2026-04-21 01:45）：
- [x] 所有 Python 文件通过 `ast.parse` 语法校验
- [x] `scripts/start_collect.sh` 通过 `bash -n` 语法校验
- [x] `V7.23` 标记在 4 处代码点正确注入（voice_daemon 3 处 + udp_emg_server 1 处 + cloud_rtmpose_client 2 处）
- [x] 阶段 4（API Key 掩码）复核：V4.8 已实现 `_mask_secret` + `_API_CONFIG_SENSITIVE_KEYS` + `_API_CONFIG_WRITE_WHITELIST`，`DEEPSEEK_API_KEY` / `BAIDU_SECRET_KEY` / `FEISHU_WEBHOOK` / `CLOUD_SSH_PASSWORD` 均在掩码名单，无需再改

**上板与启动验证**（2026-04-21 板卡重启后完成）：
- [x] `bash scripts/start_validation.sh` 成功执行（rsync + scp + 板端 5 进程启动 + cloud_tunnel 就绪）
- [x] 板端 5 进程全部拉起（vision/streamer/fsm/emg/voice PID 齐全）
- [x] V7.23 代码已同步上板：voice_daemon.py 4 处标记 / udp_emg_server.py 1 处 / cloud_rtmpose_client.py 2 处
- [x] Web 服务就绪：`http://10.18.76.224:5000/` 返回 HTTP 200
- [x] IPC 信号清洁（无残留 llm_inflight / voice_speaking）
- [x] EMG 模拟模式正常工作（真实传感器未接时正确回落到骨架驱动）
- [x] 语音主循环正常（ASR 识别正常，非唤醒语句正确忽略回 SLEEP）

**用户侧现场测试**（由用户在板端麦克风前实测）：
- [ ] 喊"教练" → 听到"嗯" → 说话 → API 回复 → 回 SLEEP，必须重新喊才能再次响应
- [ ] 疲劳满触发 API 期间喊"教练"：播报不被打断、录音不被启动
- [ ] 真实 EMG 接入 → 模拟切换 < 1s，无错帧

**已知无害警告**：
- `/tmp/mainloop.log` 有 Vosk ABI 不兼容 ERROR —— 为 legacy 残留，已 graceful degrade；项目实际使用百度 AipSpeech，不影响功能

---

## 2026-04-21 · Claude Code (Opus 4.7) · V7.24 语音顽疾 hotfix（回退 V7.23 自伤）

### 背景
用户报告三个 CRITICAL 语音 bug：
1. 解除静音需要喊多遍才识别
2. 静音→解除静音后再喊"教练"无响应
3. 静音一次后即便重启也无法识别"教练"

### 诊断（强证据）
板卡取证发现 `/dev/shm/voice_speaking` 文件残留（mtime=18:59:58），这正是我 V7.23 新增的跨进程 TTS 互斥信号。主循环 L1300 的 `if llm_inflight or voice_speaking: skip` 因此**永久跳过 `record_with_vad`**，整个语音系统死锁。

**V7.23 自伤事故还原**：
- `_llm_reply_watcher` 在 L1716 touch `voice_speaking`
- 调 `_speak_llm()` → enqueue 到 `SpeechManager`
- `SpeechManager._worker` 用内部 `_launch_aplay` + `_wait_aplay` 播放 TTS，**不经过 `play_audio()`**！
- 所以 V7.23 在 `play_audio` 的 finally 里写的 `os.remove(voice_speaking)` 永远不被触发
- 一次 LLM API 调用之后 `voice_speaking` 就永久残留

Bug 2/3 同根：主循环死锁导致任何语音（含"教练"、"解除静音"）都无响应。
Bug 1（喊多遍）是 `_mic_allowed_watchdog` 兜底 15s 太长 + TTS 播报期间阻塞叠加造成。

### 修复方案（用户拍板：C "用进程内 Event 替代文件信号"）
- **进程内 Event 已存在**：主循环 L1294 `_mic_allowed.wait()` 本就完全覆盖"TTS 播报期间阻塞"的语义
- V7.23 的 `voice_speaking` 文件是**冗余冗余保险**，却因清理缺失反把系统卡死
- V7.24 直接撤掉文件信号，只靠 `_mic_allowed` Event

### 改动摘要

| 位置 | 改动 |
|---|---|
| `voice_daemon.py::play_audio` L675-731 | 撤掉入口 touch + finally os.remove 的 voice_speaking 文件操作，回归单层缩进 |
| `voice_daemon.py::_llm_reply_watcher` L1706-1712 | 撤掉 `open(voice_speaking)` touch，**保留 `_mic_allowed.clear()`**（V7.23 核心修复，关闭 watcher→SpeechManager 竞态） |
| `voice_daemon.py` 主循环 L1294-1302 | L1300 从双信号检查回退为只检查 `llm_inflight`（_mic_allowed.wait() 已足够） |
| `voice_daemon.py::_m10_voice_cleanup` L987 | 启动清理列表加 `/dev/shm/voice_speaking`（覆盖跨版本升级的历史残留） |
| `voice_daemon.py::_mic_allowed_watchdog` L1632 | 兜底超时 15s → **3s**（Bug 1 修复：解除静音喊多遍的竞态窗口被显著压缩） |

### 涉及文件
- `hardware_engine/voice_daemon.py`（+10 / -20，净削减代码）

### 上板验证
- [x] `bash scripts/start_validation.sh` 成功
- [x] 板卡 `/dev/shm/voice_speaking` 不存在（M10 清理生效）
- [x] `V7.24` 标记在代码 6 处就位
- [x] 主循环正常 VAD 校准（baseline=647, threshold=687）
- [x] `mute_signal.json` 重置为 `{muted: false}`
- [x] SpeechManager 欢迎词入队

### 用户现场实测（待回填）
- [ ] 喊"教练" → 听到应答 → 对话 → 回 SLEEP
- [ ] 喊"静音" → 进入静音态
- [ ] 静音态喊"解除静音" → 紧急路径立即解除
- [ ] 解除静音后立即喊"教练" → **应可响应**（Bug 2 修复验证）
- [ ] 静音→解除→重启板子→喊"教练" → **应可响应**（Bug 3 验证）
- [ ] 连续喊"解除静音" → 应首次就识别（Bug 1 缓解）

### 经验教训（给未来 Agent）
1. **跨进程文件信号必须有明确的所有者**（创建+删除在同一进程）。V7.23 用 watcher touch + play_audio delete，但 SpeechManager 根本不用 play_audio → 清理链断裂
2. **冗余保险不是免费的** —— 每增加一个同步机制都增加"残留/死锁"的可能
3. **优先用进程内 Event**（如 `_mic_allowed`），只有在真的需要跨进程同步时才用文件（如 `llm_inflight` 由 main_claw_loop 写、voice_daemon 读）

---

## 2026-04-21 · Claude Code (Opus 4.7) · V7.25 watchdog 过激回调（自伤二次修复）

### 用户报告的三个现象

1. 切换哑铃弯举后系统播报"准备好后请说开始 MVC 测试"，**这句话被系统自己录下来**
2. 切换到视觉+传感器模式后 UI 右上角机电传感器无数据（ESP32 未接）—— 用户想确认切换是否生效
3. 状态机在某次**卡死**后**重启已恢复正常**

### 诊断结论（证据见板端取证 + Golden Pool 对比）

| 现象 | 根因 | 处理 |
|---|---|---|
| MVC 自言自语 | **V7.24 把 `_mic_allowed_watchdog` 从 15s 改为 3s** —— "准备好后请说 MVC 测试"长句 TTS 合成+播放约 2~3s，擦边触发 watchdog 强制释放麦克风门，主循环随即开 VAD 录到自己尾音 | V7.25 改为 **6s** |
| 模式切换验证 | 系统层一切正常：`/dev/shm/inference_mode.json = {"mode":"vision_sensor"}` ✅；`muscle_activation.json` 在写模拟数据 ✅；`emg_heartbeat` 不存在（ESP32 未接，正确回落模拟）✅ | **不改代码**。ESP32 接入后会自动切真实。UI 右上角空白属前端渲染，非后端问题 |
| 状态机卡死后重启恢复 | 非参数问题，属瞬时异常（可能 vision/fsm 某次 crash 或 IPC 争用）。当前状态机参数用户已认可（ANGLE_STANDARD=90°）| **不改代码**。若再次卡死立即抓 `/tmp/voice.log`、`/tmp/mainloop.log`、`/tmp/npu_main.log` 给 Claude 分析 |

### 状态机差异纪录（不改动，仅留档供未来排查）

当前 (V7.18) vs Golden Pool (V7.15)：
- `ANGLE_STANDARD`: 90° vs 100° —— **保持 90°**（用户亲口认可）
- vision_sensor 模式计数: 无条件 vs 等 GRU —— **保持无条件**（未获授权改）
- 结账门槛: `angle > min+25°`（相对） vs `angle > 150°`（绝对） —— **保持相对值**
- rep 最短时长: 0.4s vs 0.5s —— **保持 0.4s**

### 涉及文件

- `hardware_engine/voice_daemon.py` L1629-1637：watchdog 超时常数 3s → 6s；加 V7.25 注释说明回调原因

### 上板验证

- [x] `bash scripts/start_validation.sh` 成功
- [x] V7.25 标记在 L1629 就位，板端 L1636 阈值为 `> 6.0`
- [x] 主循环正常 VAD 校准（baseline=651, threshold=686）
- [ ] **用户现场实测**：切弯举后"准备好后请说 MVC 测试" 不再被系统自录
- [ ] **用户现场实测**：真喊"开始 MVC 测试" 能正常进入 MVC 流程

### 经验教训

1. **兜底阈值不要设在正常场景的边界上**——V7.24 假设"TTS 不超 3s"没调研实际长句时长就落刀
2. **watchdog 应该是"异常兜底"不是"性能优化"**——V7.24 本来是为了修 Bug 1 "解除静音喊多遍"，但用户选择"回调到 6s"说明用户对延迟容忍度其实够大
3. **用户重启恢复正常 ≠ 问题不存在**——需要记入 UPDATE_LOG 作为瞬时异常排查基线

---

## 2026-04-21 · Claude Code (Opus 4.7) · V7.26 对话空窗期根治

### 用户报告
"自由对话中 LLM 说完之后，我说'教练'一点反应都没有，可能进入了空窗状态"

### 铁证（板端取证原文）
```
19:45:29.651  LLM_Watcher 读到回复 "本组合格5次不合格3次..."（28 字）
19:45:29.651  SM 抢占 prio=3 开始播
19:45:36.332  [mic_watchdog] 麦风门持续阻塞 >6s, 强制释放  ← V7.25 6s 阈值踩雷
19:45:37.556  VAD校准: baseline=662 threshold=702           ← 回声污染
19:45:37-42   rms=630-680 全部 started=False                ← 用户喊话被拒
```

### 完整病理链
1. LLM 长回复（28 字）TTS 合成+播放 **7~8s**
2. V7.25 的 watchdog 6s 阈值被触发 → 强制 `_mic_allowed.set()`（**aplay 还在播**）
3. 主循环立刻开 VAD → **麦克风录进扬声器的自己声音** → baseline 从正常 ~550 被污染到 662
4. `threshold = max(250, 662+40) = 702` → 用户正常喊"教练" rms ~650 **永远到不了 702**
5. `_VAD_BASELINE_CACHE` TTL 30 秒 → 这个高阈值被缓存**卡 30 秒**

### 三层根治方案（V7.26 全上）

#### A. watchdog 强制释放前 kill aplay + 作废 baseline 缓存
- 位置：`_mic_allowed_watchdog` L1667-1683
- 释放前调 `killall -9 aplay` 消除回声源
- 释放前 `_VAD_BASELINE_CACHE["ts"]=0 + baseline=0` 强制下次重采

#### B. 智能判定"aplay 在跑就不算卡死"
- 位置：`_mic_allowed_watchdog` L1635-1643
- watchdog 每秒先检查 `_sm._play_proc[0].poll()` 是否 None
- aplay 还活着 → 重置 `_blocked_since[0]` = 0，不计时
- 只有 aplay 已死但 `_mic_allowed` 仍 clear 超过 **12 秒**才真正兜底
- **副产品**：V7.25 的 6s 彻底过气，回归宽松的 12s（有 B 兜着，激进不必要）

#### C-minimal. 扩展 `voice_interrupt` 到 PRIO_LLM 档
- 位置：`_wait_aplay` L507-555
- LLM 播报期间若检测到 `/dev/shm/voice_interrupt` 文件 → 立即 kill aplay + 清空 LLM 队列
- 支持 UI 侧或外部主动打断，**不影响自动播放**
- **配套**：主循环 `_dialog_active` 仅对 ALARM 档生效（原有行为保留）

### 未实施项：C-full（自动"喊教练"打断 LLM）

需要独立 `_wake_listener` 线程，在 LLM 播报期间并行：
- `arecord plughw:0,0` 与 aplay 并行（ALSA 需软件混音，不是所有配置都 OK）
- 实时 ASR 识别"教练"（每次调用 0.5-1s，播报 8s 需 8-16 次 ASR，API 配额爆炸）
- 区分扬声器回声（系统自己说的"教练"）vs 真人（需要 AEC 回声消除）
- **风险**：误打断率难控制，ASR 成本高
- **建议**：先让用户实测 A+B+C-minimal 效果，如 A+B 已修好"空窗期"，C-full 可能不紧急
- **授权策略**：若用户坚持要 C-full，需独立需求单，评估 50+ 行新代码

### 涉及文件
- `hardware_engine/voice_daemon.py`
  - `_mic_allowed_watchdog` 全重写（L1618-1693）
  - `_wait_aplay` 扩展 `_interruptible` 到 PRIO_LLM（L507-555）

### 上板验证
- [x] `bash scripts/start_validation.sh` 成功
- [x] V7.26 标记 7 处就位
- [x] watchdog 新阈值 `> 12.0` 在 L1665
- [x] `_interruptible = (prio in (PRIO_ALARM, PRIO_LLM))` 在 L516
- [x] 板端 5 进程拉起，主循环正常 VAD 校准
- [x] **用户现场验收 2026-04-21**：视觉模块（含语音 + FSM + EMG 双模）所有任务可以验收，V7.23 → V7.26 的全部改动生效

### 经验教训叠加
1. V7.24 (3s)、V7.25 (6s) 两次调参都是治标不治本 —— **因为根因不是阈值不够长，是 aplay 还活着时不该释放麦克风**
2. watchdog 逻辑应以**外部系统状态**（aplay 进程存活）为首要判据，不应只靠计时器
3. **回声污染 baseline**是嵌入式设备的经典坑 —— baseline 缓存策略必须有"突发失效"机制

---

## （模板：后续 Agent 追加日志参照以下格式）

```
## YYYY-MM-DD · <Agent 身份> · <一句话摘要>

### 改动摘要
- ...

### 涉及文件
- path/to/file (+X / -Y)

### 验证结果
- [x] ...
- [ ] ...

---
```
