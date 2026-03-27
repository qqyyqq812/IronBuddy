# 第 5 章 Draft：深蹲状态机与测试运行逻辑

> 本文档基于 `hardware_engine/main_claw_loop.py` 源码编写，所有参数均与代码一致。

---

## 一、架构设计原则

系统将"动作判定"与"大模型评判"做了彻底的分层解耦：
- **边缘端实时判定**：所有关于"人是否在画面中"、"是否正在蹲"、"蹲到位了没"的判定全部在板端完成，不依赖网络。即使云端断连，计数和警报功能也完全可用。
- **云端延迟评判**：大模型仅在用户**主动请求**时才介入，接收的是高度精简的统计信息（标准次数/违规次数），而非原始坐标数据。

---

## 二、SquatStateMachine 核心设计

### 2.1 关键参数（源码定义）

| 参数 | 值 | 含义 |
|------|---|------|
| `ANGLE_STANDARD` | 90° | 膝盖角度低于此值 = 标准深蹲 |
| `TREND_WINDOW` | 8 帧 | 角度历史滑窗容量上限 16 帧 |
| `IDLE_RANGE` | 20° | 最近 4 帧角度波动小于此值 = 静止 |
| `IDLE_FRAMES` | 25 帧 | 连续 25 帧被判定为静止才切入 IDLE（约 3 秒） |

### 2.2 角度计算

系统从 YOLOv5-Pose 输出的 17 个关键点中提取三个点：
- **髋关节** (keypoint 11)
- **膝关节** (keypoint 13)
- **踝关节** (keypoint 15)

通过 `math.acos()` 计算三点构成的夹角（膝盖角度），单位为度。

### 2.3 平滑处理

原始角度经过 **5 帧均值滤波**消除瞬时抖动：
```python
smooth_n = min(5, len(self._angle_history))
angle = sum(self._angle_history[-smooth_n:]) / smooth_n
```

---

## 三、趋势检测算法

### 3.1 `_get_trend()` 方法

取最近 6 帧数据，计算相邻帧间角度差值的平均值 `avg_delta`：

```python
recent = self._angle_history[-6:]
deltas = [recent[i+1] - recent[i] for i in range(len(recent)-1)]
avg_delta = sum(deltas) / len(deltas)
```

判定规则：
- `avg_delta < -2.5` → `"falling"`（角度在减小，用户正在下蹲）
- `avg_delta > 2.5` → `"rising"`（角度在增大，用户正在起身）
- 其他 → `"stable"`（角度无明显变化）

### 3.2 稳定性检测

独立于趋势判断，系统同时维护一个稳定帧计数器 `_idle_counter`：
```python
recent_range = max(_angle_history[-4:]) - min(_angle_history[-4:])
if recent_range < IDLE_RANGE:  # 20°
    _idle_counter += 1
else:
    _idle_counter = 0
is_stable = _idle_counter >= IDLE_FRAMES  # 25 帧 ≈ 3 秒
```

---

## 四、状态流转图

```
NO_PERSON ──(检测到人)──→ IDLE
    ↑                       │
    │                       │ trend == "falling"
    │                       ↓
    │                   DESCENDING ──(is_stable)──→ IDLE
    │                       │
    │                       │ trend == "rising" (转折点)
    │                       │ + 1.5s 冷却期通过
    │                       ↓
    │                   ASCENDING ──(is_stable)──→ IDLE
    │                       │
    │                       │ trend == "falling"
    │                       ↓
    │                   DESCENDING (连续深蹲)
    │
    └──(无人检测)── 任意状态
```

### 各状态转移条件

| 当前状态 | 触发条件 | 目标状态 | 附加动作 |
|---------|---------|---------|---------|
| `NO_PERSON` | 检测到人且 `is_stable` | `IDLE` | — |
| `NO_PERSON` | 检测到人且 `trend == "falling"` | `DESCENDING` | 记录初始下蹲角度 |
| `IDLE` | `trend == "falling"` | `DESCENDING` | 初始化 `_min_angle_in_rep` |
| `DESCENDING` | `trend == "rising"` + 冷却期 > 1.5s | `ASCENDING` | **执行判定**（见下文） |
| `DESCENDING` | `is_stable` | `IDLE` | 下蹲中途停止，不计数 |
| `ASCENDING` | `is_stable` | `IDLE` | 完成一次动作循环 |
| `ASCENDING` | `trend == "falling"` | `DESCENDING` | 连续深蹲，重新开始下一次 |

---

## 五、深蹲质量判定

在 `DESCENDING → ASCENDING` 转折点，系统读取本次动作中记录的最低角度 `_min_angle_in_rep`：

```python
bottom = self._min_angle_in_rep
if bottom < self.ANGLE_STANDARD:  # 90°
    self.good_squats += 1
    # 日志: "🔥 标准深蹲！（最低XX°）有效计数：N"
else:
    self.failed_squats += 1
    self.trigger_buzzer_alert()
    # 日志: "⚠️ 半蹲违规！（最低XX°≥90°）累计违规：N"
```

**防重计冷却期**：相邻两次计数间隔必须 > 1.5 秒（`_last_count_time`），防止单次动作中由于传感器抖动被重复计算。

---

## 六、蜂鸣器触发机制

违规判定后立即调用 `trigger_buzzer_alert()`：
```python
def trigger_buzzer_alert(self):
    now = time.time()
    if now - self._last_buzzer_time < 2.0:  # 2 秒冷却
        return
    self._last_buzzer_time = now
    os.system("bash /home/toybrick/hardware_engine/peripherals/buzzer_beep.sh &")
```

蜂鸣器全程通过 `sysfs` GPIO 操作（交替拉低/拉高 GPIO 153 引脚），执行为非阻塞后台进程，不影响主循环运行。

---

## 七、前端状态同步

每帧处理完毕后，状态机通过 `sync_to_frontend()` 将当前状态写入共享内存 `/dev/shm/fsm_state.json`：

```json
{
  "state": "DESCENDING",
  "good": 3,
  "failed": 1,
  "angle": 85.2,
  "chat_active": false
}
```

前端网页通过 Flask `/state_feed` 接口轮询此文件，实时更新界面上的状态标签、计数和角度数据。

---

## 八、大模型触发方式

系统**不存在**任何自动触发大模型的逻辑。训练评判仅在以下两种情况下发送：

1. **前端按钮**：用户点击网页上的"📝 生成本组点评"按钮 → 前端 POST `/trigger_deepseek` → `streamer_app.py` 写入 `/dev/shm/trigger_deepseek` 信号文件 → 主循环检测到后组装 prompt 并异步发送。
2. **语音唤醒**：用户说出"教练"等唤醒词 → `voice_daemon` 录音并转文字 → 写入 `/dev/shm/chat_input.txt` → 主循环轮询检测到后组装对话 prompt 发送。

发送的 prompt 示例：
```
你是健身教练。学员本组完成 3 个标准深蹲、1 次半蹲。
请直接给出点评和下一组的鼓励。要求：纯一段文字，禁止 Markdown 和 Emoji，口语化，40字以内。
```
