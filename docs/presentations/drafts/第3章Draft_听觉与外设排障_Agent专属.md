# 第 3 章 Draft：听觉与外设系统技术详解

> 本文档基于以下源码编写：
> - `hardware_engine/voice_daemon.py`（301 行）
> - `hardware_engine/peripherals/tts_daemon.sh`（74 行）
> - `hardware_engine/peripherals/peripheral_daemon.sh`（26 行）
> - `hardware_engine/peripherals/buzzer_init.sh`（12 行）

---

## 一、蜂鸣器控制 (GPIO)

### 1.1 硬件接口

蜂鸣器通过 GPIO 153 引脚控制，使用 Linux `sysfs` 接口操作。

### 1.2 初始化脚本 `buzzer_init.sh`

```bash
GPIO=153
P="/sys/class/gpio/gpio${GPIO}"
_w() { echo toybrick | sudo -S sh -c "echo $2 > $1" 2>/dev/null; }
[ ! -d "$P" ] && _w /sys/class/gpio/export $GPIO && sleep 0.05
_w $P/direction out
_w $P/value 1   # 拉高 → 截止蜂鸣器（静音）
```

**要点**：
- 使用 `echo 153 > /sys/class/gpio/export` 将引脚暴露到用户空间
- 设置方向为 `out`（输出模式）
- 默认拉高（`value=1`）= 静音；拉低（`value=0`）= 蜂鸣
- 需要 `sudo` 权限，通过管道传入密码实现非交互执行

### 1.3 触发方式

由 `main_claw_loop.py` 中的 `trigger_buzzer_alert()` 调用 `buzzer_beep.sh`，该脚本交替拉低/拉高以产生蜂鸣声，设有 2 秒冷却期。

> **配置参考**：`RK3399proX/3399 gpio 参考及例程/Rockchip_Developer_Guide_IO_CN.pdf`

---

## 二、语音输入系统 `voice_daemon.py`

### 2.1 架构概述

`voice_daemon.py` 是一个独立的长驻守护进程，与主循环（`main_claw_loop.py`）完全解耦，通过文件 IPC 通信。

**启动延迟**：开机后等待 15 秒（`STARTUP_DELAY = 15`），避免与 NPU 引擎争夺 ALSA 设备。

### 2.2 麦克风自动探测

系统支持多个候选麦克风设备：
```python
MIC_CANDIDATES = ["plughw:0,0", "plughw:2,0", "plughw:3,0"]
```

`find_working_mic()` 函数按序尝试每个设备，用 `arecord` 录制 1 秒测试音频。首个成功的设备被选为工作麦克风。如果全部失败，进入待机模式（每 30 秒重试一次）。

**设备占用恢复**：如果录音时检测到设备 `busy`，等待 5 秒后重新检测可用麦克风。

### 2.3 唤醒词检测

系统维护一个包含 13 个变体的唤醒词列表，覆盖 ASR 的常见误识别：
```python
WAKE_WORDS = ["教练", "教", "叫练", "交练", "焦练", "iron", "buddy",
              "爱人", "巴蒂", "铁哥", "铁哥们", "铁头", "coach"]
```

检测流程：
1. 每 3 秒录制一个音频 chunk（16kHz, 单声道, S16_LE）
2. 计算音频能量 → 低于阈值 300 则判定为静音，跳过 ASR
3. 高于阈值 → 调用 Google Speech Recognition API 转写
4. 检查转写文本是否包含任何唤醒词
5. 命中 → 进入对话模式

**代理要求**：由于板端无法直连 Google API，在脚本开头设置了局域网代理：
```python
os.environ["http_proxy"] = "http://10.208.139.68:7890"
```

### 2.4 对话模式状态机

唤醒后系统进入 `conversation_mode = True`：

```
唤醒词命中
  ↓
killall 正在播放的所有音频进程（打断冗长播报）
  ↓
创建 /dev/shm/chat_active 信号文件
  ↓
TTS 播报 "我在，请说"
  ↓
提取唤醒词后的内容（如"教练，你觉得我蹲得怎么样"中的"你觉得我蹲得怎么样"）
  ↓
持续录音并 ASR，文字拼接到 accumulated_text
  ↓
连续 1-2 个 chunk 无语音 → 判定对话结束
  ↓
将 accumulated_text 写入 /dev/shm/chat_input.txt
  ↓
删除 chat_active 信号，退出对话模式
```

**多段拼接**：对话过程中每个 chunk 的 ASR 结果被逐步拼接到 `accumulated_text`，允许用户说较长的句子（跨多个 3 秒录音窗口）。

**实时草稿**：对话过程中实时写入 `/dev/shm/chat_draft.txt`，前端可展示用户正在说的内容。

---

## 三、语音输出系统 `tts_daemon.sh`

### 3.1 架构

独立的 Bash 守护进程（非 Python），轮询两个文件：
- `/dev/shm/llm_reply.txt`：训练点评回复
- `/dev/shm/chat_reply.txt`：对话回复

每 0.5 秒检查文件的 `mtime`，有变化则触发播报。

### 3.2 合成与播放

```bash
DEVICE="plughw:0,0"
VOICE="zh-CN-YunxiNeural"   # 年轻男性教练声线

speak() {
    text=$(echo "$1" | head -c 300)  # 截取前 300 字节
    edge-tts --text "$text" --voice "$VOICE" --write-media "$TTS_TMP"
    mpg123 -a "$DEVICE" -r 16000 -f 8000 -q "$TTS_TMP"
}
```

**关键技术点**：

| 参数 | 值 | 原因 |
|-----|---|------|
| `-a plughw:0,0` | ALSA 插件设备 | 启用软件重采样，绕过硬件采样率锁定 |
| `-r 16000` | 强制 16kHz | 匹配板载 I2S 时钟，避免变调 |
| `-f 8000` | 音量缩放 | 适配小音箱输出功率 |
| `head -c 300` | 截断 | 防止过长文本导致 TTS 超时 |

### 3.3 ALSA 路由

开机时由 `buzzer_init.sh` 和 `tts_daemon.sh` 执行：
```bash
amixer -c 0 sset 'Playback Path' SPK
```
将声卡输出路由切换至外接小音箱（SPK）。

---

## 四、外设旁路监听守护 `peripheral_daemon.sh`

### 4.1 功能

监听大模型回复文件 `/dev/shm/llm_reply.txt`，当回复文本中包含特定纠错关键词时，触发音箱警报音效。

### 4.2 关键词列表

```bash
grep -qE '错误|不正确|违规|纠正|警告|太浅|内扣' "$WATCH_FILE"
```

当大模型回复中出现上述任一关键词，调用 `speaker_alert.sh` 播放警报音（`alert_warning.wav`）。

### 4.3 设计意义

这是一种"旁路增强"机制：主流程（FSM）已经有蜂鸣器即时警报，而此守护进程提供了第二层基于大模型语义理解的音频反馈。两者互不干扰，粒度不同：
- FSM 蜂鸣器：基于角度阈值的**即时物理信号**
- 旁路监听：基于大模型**文本语义**的延迟音效

---

## 五、进程隔离与设备冲突处理

系统中有多个进程同时使用 ALSA 设备（麦克风和音箱），需要严格的时序隔离：

| 进程 | ALSA 用途 | 冲突处理 |
|------|---------|---------|
| `voice_daemon.py` | 录音（麦克风） | 延迟 15 秒启动；设备忙时等待 5 秒重试 |
| `tts_daemon.sh` | 播放（音箱） | 独占 `plughw:0,0`，播完释放 |
| `voice_daemon.py` | 播放 TTS 回应 | 唤醒时 `killall` 所有正在播放的进程 |

唤醒词触发时的强制打断机制避免了"用户说话时系统还在自顾自播报"的尴尬：
```python
os.system("killall aplay edge-tts mpg123 espeak 2>/dev/null")
```
