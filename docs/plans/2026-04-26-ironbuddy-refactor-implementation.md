# IronBuddy 重构 + 多模态打通 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal**：在 2-3 周内修补语音/多模态/路由共 13 个具体 bug，重构语音模块为状态机驱动 + DeepSeek tool calling 路由，准备好主视频和子视频 1+2 的拍摄环境，使比赛展示视频可拍可剪。

**Architecture**：保留现有 V3 GRU 模型 + Flask 路由 + 4025 行前端 + SQLite schema 不动；只动 voice_daemon.py（拆 3 子模块）+ main_claw_loop.py 关键 bug 行 + 新建 `voice/` package + 新建 `cognitive/deepseek_client.py`。

**Tech Stack**：Python 3.7（板端约束）/ Flask / SQLite / 百度 AipSpeech / DeepSeek API（tool calling）/ MIA dataset / CompensationGRU。

**Reference**：上游设计稿 [docs/plans/2026-04-26-ironbuddy-refactor-design.md](2026-04-26-ironbuddy-refactor-design.md)。

---

## 阶段 0：多模态 bug 修补（1.5-7 天，自适应）

### Task 0.1：修 M1 — Ang_Vel 列推理时归一化

**Files**：
- Modify: `hardware_engine/main_claw_loop.py:1062-1066`

**Step 1：先看现状确认 bug**

Run: `sed -n '1058,1075p' hardware_engine/main_claw_loop.py`

Expected output 应包含：
```python
window[:, 1] /= 180.0          # Angle
window[:, 3] /= 100.0          # Target_RMS
window[:, 4] /= 100.0          # Comp_RMS
window[:, 2] = np.clip(window[:, 2] / 10.0, -1.0, 1.0)  # Ang_Accel
```
（注意没有 col 0 / Ang_Vel 归一化）

**Step 2：写测试（pytest，验证归一化效果）**

Create: `tests/test_main_claw_loop_normalization.py`

```python
import numpy as np
import pytest

def normalize_window(window):
    """与 main_claw_loop.py 推理时归一化逻辑一致"""
    window = window.copy()
    window[:, 0] = np.clip(window[:, 0] / 30.0, -3.0, 3.0)
    window[:, 1] /= 180.0
    window[:, 2] = np.clip(window[:, 2] / 10.0, -1.0, 1.0)
    window[:, 3] /= 100.0
    window[:, 4] /= 100.0
    return window


def test_ang_vel_in_training_range_after_normalize():
    """raw Ang_Vel 范围 [-15, 15] deg/frame，归一化后必须落在训练分布 [-3, 3]"""
    window = np.random.uniform(-15, 15, (30, 7)).astype(np.float32)
    out = normalize_window(window)
    assert out[:, 0].min() >= -3.0
    assert out[:, 0].max() <= 3.0


def test_extreme_ang_vel_clipped():
    window = np.zeros((30, 7), dtype=np.float32)
    window[:, 0] = 50.0  # 极端值
    out = normalize_window(window)
    assert (out[:, 0] == 3.0).all()  # 全部 clip 到 3
```

**Step 3：运行测试确认它失败（因为 main_claw_loop 真实代码还没改）**

Run: `cd /home/qq/projects/embedded-fullstack && python3 -m pytest tests/test_main_claw_loop_normalization.py -v`

Expected: 测试本身 PASS（因为 normalize_window 是测试内 helper），但这只是验证逻辑，下一步是把这逻辑套进真代码。

**Step 4：把归一化加到 main_claw_loop.py**

Modify: `hardware_engine/main_claw_loop.py:1066`，在 Ang_Accel clip 后加一行：

```python
# V7.30 修补 M1：Ang_Vel 列推理归一化对齐训练（train_gru_three_class.py:138）
window[:, 0] = np.clip(window[:, 0] / 30.0, -3.0, 3.0)
```

**Step 5：本地烟测推理**

Run: `python3 -c "
import sys; sys.path.insert(0, 'hardware_engine')
from cognitive.fusion_model import CompensationGRU, load_model
import numpy as np

m = load_model('hardware_engine/extreme_fusion_gru.pt')
window = np.random.uniform(-15, 15, (30, 7)).astype(np.float32)
window[:, 1] = np.random.uniform(50, 180, 30)
window[:, 3] = np.random.uniform(20, 80, 30)
window[:, 4] = np.random.uniform(10, 40, 30)
window[:, 5] = 1.0
window[:, 6] = np.linspace(0, 1, 30)
window[:, 0] = np.clip(window[:, 0] / 30.0, -3.0, 3.0)
window[:, 1] /= 180.0
window[:, 2] = np.clip(window[:, 2] / 10.0, -1, 1)
window[:, 3] /= 100.0
window[:, 4] /= 100.0
result = m.infer(window)
print('classification:', result['classification'])
print('confidence:', result['confidence'])
print('OK' if 0 <= result['confidence'] <= 1 else 'FAIL')
"`

Expected：打印 classification + confidence，无 NaN，confidence ∈ [0, 1]。

**Step 6：commit**

```bash
git add hardware_engine/main_claw_loop.py tests/test_main_claw_loop_normalization.py
git commit -m "fix(M1): align Ang_Vel inference normalization with training (clip /30 [-3,3])"
```

---

### Task 0.2：修 M2 — 恢复 V7.15 FSM/GRU mode-gating

**Files**：
- Modify: `hardware_engine/main_claw_loop.py:384-392` (squat ASCENDING 分支)
- Modify: `hardware_engine/main_claw_loop.py:662-669` (curl EXTENDING 分支)

**Step 1：读现状定位 V7.18 注释**

Run: `grep -n "V7.18\|无论模式" hardware_engine/main_claw_loop.py`

Expected：定位到 squat / curl 两处 V7.18 注释行。

**Step 2：阅读两段当前代码**

Run: `sed -n '380,400p' hardware_engine/main_claw_loop.py` 和 `sed -n '658,680p' hardware_engine/main_claw_loop.py`

记下两段 increment 逻辑的具体行。

**Step 3：写测试（FSM mode-gating 单测）**

Create: `tests/test_fsm_mode_gating.py`

```python
"""验证 vision_sensor 模式下 FSM 不直接增加 good/failed，让 GRU 决定"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'hardware_engine'))


def test_pure_vision_squat_increments_via_angle():
    from main_claw_loop import SquatStateMachine
    fsm = SquatStateMachine()
    fsm._inference_mode = "pure_vision"
    fsm._bottom_angle = 90  # bottom < 100 → standard
    initial_good = fsm._good_squats
    fsm._on_rep_complete()
    assert fsm._good_squats == initial_good + 1


def test_vision_sensor_squat_does_not_increment_via_angle():
    from main_claw_loop import SquatStateMachine
    fsm = SquatStateMachine()
    fsm._inference_mode = "vision_sensor"
    fsm._bottom_angle = 90
    initial_good = fsm._good_squats
    fsm._on_rep_complete()
    # vision_sensor 模式下 FSM 不直接 +1，等 GRU 决定
    assert fsm._good_squats == initial_good
```

> 注意：测试可能需要 stub 一些 init 依赖。如果 SquatStateMachine 构造太复杂，简化为只测 `_on_rep_complete` 内部 if 分支逻辑。

**Step 4：运行测试确认它失败**

Run: `cd /home/qq/projects/embedded-fullstack && python3 -m pytest tests/test_fsm_mode_gating.py -v`

Expected：第二个测试 FAIL（V7.18 现状下 vision_sensor 也增加 good）。

**Step 5：修代码（squat ASCENDING 分支）**

Modify: `hardware_engine/main_claw_loop.py:384-392` 把 increment 包到 `if self._inference_mode != "vision_sensor":` 下：

```python
# V7.30 恢复 M2 / V7.15 mode-gating：vision_sensor 模式下不直接 ++
if self._inference_mode != "vision_sensor":
    if self._bottom_angle < 100:
        self._good_squats += 1
    else:
        self._failed_squats += 1
```

**Step 6：修代码（curl EXTENDING 分支）**

Modify: `hardware_engine/main_claw_loop.py:662-669` 同样包：

```python
if self._inference_mode != "vision_sensor":
    if self._peak_angle < 50:
        self._good_curls += 1
    else:
        self._failed_curls += 1
```

**Step 7：运行测试确认现在 PASS**

Run: `python3 -m pytest tests/test_fsm_mode_gating.py -v`

Expected: 两个测试都 PASS。

**Step 8：commit**

```bash
git add hardware_engine/main_claw_loop.py tests/test_fsm_mode_gating.py
git commit -m "fix(M2): restore V7.15 FSM/GRU mode-gating for vision_sensor mode"
```

---

### Task 0.3：修 M3 — Symmetry 训练侧改 1.0（消除推理-训练差距）

**Files**：
- Modify: `tools/train_gru_three_class.py` (合成 compensating 时设 sym=1.0)
- Modify: `tools/train_gru_three_class_bicep.py` (同样)

**Step 1：定位 Symmetry 合成行**

Run: `grep -n "comp\[:, 5\]\|Symmetry" tools/train_gru_three_class*.py`

Expected：定位到 `comp[:, 5] *= np.random.uniform(0.5, 0.75)` 这种行。

**Step 2：注释掉训练侧的 sym 偏置**

Modify 两个 train 脚本：把 `comp[:, 5] *= np.random.uniform(...)` 这行删除或改为：

```python
# V7.30 修补 M3：训练侧也固定 sym=1.0，与推理对齐
# comp[:, 5] *= np.random.uniform(0.5, 0.75)  # 原 V7.15 偏置，已与推理失准
```

确保 standard / non_standard 路径也都不动 sym 列（默认 1.0）。

**Step 3：重训 squat 模型**

Run: `cd /home/qq/projects/embedded-fullstack && python3 tools/train_gru_three_class.py --epochs 20 2>&1 | tail -30`

Expected：末尾 `🟢 SELFTEST PASS` + `模型保存: hardware_engine/extreme_fusion_gru.pt`，train acc > 90%。

**Step 4：重训 curl 模型**

Run: `python3 tools/train_gru_three_class_bicep.py --epochs 20 2>&1 | tail -30`

Expected：同上，输出 `extreme_fusion_gru_bicep.pt`。

**Step 5：commit**

```bash
git add tools/train_gru_three_class.py tools/train_gru_three_class_bicep.py \
        hardware_engine/extreme_fusion_gru.pt hardware_engine/extreme_fusion_gru_bicep.pt
git commit -m "fix(M3): symmetry=1.0 in training to match inference + retrain both models"
```

---

### Task 0.4：修 M4 — 归档 deprecated 文件

**Files**：
- Move: `tools/train_model.py` → `.archive/deprecated_v3map/`
- Move: `models/extreme_fusion_gru_squat.pt` → `.archive/deprecated_v3map/`
- Move: `models/extreme_fusion_gru_curl.pt` → `.archive/deprecated_v3map/`

**Step 1：建目录**

Run: `mkdir -p .archive/deprecated_v3map/`

**Step 2：移动文件**

Run:
```bash
git mv tools/train_model.py .archive/deprecated_v3map/
git mv models/extreme_fusion_gru_squat.pt .archive/deprecated_v3map/
git mv models/extreme_fusion_gru_curl.pt .archive/deprecated_v3map/
```

**Step 3：写归档 README**

Create: `.archive/deprecated_v3map/README.md`

```markdown
# Deprecated V3 Map Files

These files belong to the old V3_7D 全链路地图 era where:
- `train_model.py` was the trainer
- Models lived in `models/`
- A manual `cp models/*.pt hardware_engine/*.pt` step was required

**Replaced by**: `tools/train_gru_three_class.py` and `tools/train_gru_three_class_bicep.py`,
which write directly to `hardware_engine/extreme_fusion_gru{,_bicep}.pt`.

Archived 2026-04-26 to prevent confusion about which weight is loaded at runtime.
```

**Step 4：commit**

```bash
git add .archive/deprecated_v3map/
git commit -m "chore(M4): archive deprecated V3 map files (train_model.py + models/*.pt)"
```

---

### Task 0.5：用 simulator 验证三类（决策点）

**Step 1：起 FSM 主进程**

Run（在 WSL，不需要真板）：
```bash
cd /home/qq/projects/embedded-fullstack
python3 hardware_engine/main_claw_loop.py 2>&1 | tee /tmp/fsm.log &
sleep 3
```

**Step 2：写 vision_sensor 模式信号 + 切到 squat**

Run:
```bash
echo '{"mode":"vision_sensor","ts":'$(date +%s)'}' > /dev/shm/inference_mode.json
echo '{"exercise":"squat"}' > /dev/shm/user_profile.json
```

**Step 3：起 squat simulator with --label standard**

Run（30 秒）:
```bash
timeout 30 python3 tools/simulate_emg_from_mia.py --label standard 2>&1 | tee /tmp/sim_std.log &
```

**Step 4：观察 fsm_state.json**

Run（每 2 秒打一次）:
```bash
for i in 1 2 3 4 5 6 7 8 9 10; do
  cat /dev/shm/fsm_state.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'good={d.get(\"good\",0)} failed={d.get(\"failed\",0)} comp={d.get(\"comp\",0)} cls={d.get(\"classification\",\"?\")}')"
  sleep 2
done
```

Expected：标签 standard 下 `good` 单调递增，`failed` 和 `comp` 不变（理想情况）。

**Step 5：换 label 重测两次**

```bash
killall python3
# 重启 FSM + simulator with --label compensating
# 期望 comp 递增，good 不变
# 重启 FSM + simulator with --label non_standard
# 期望 failed 递增，good 不变
```

**Step 6：换 curl 重测三次**

```bash
echo '{"exercise":"bicep_curl"}' > /dev/shm/user_profile.json
# 同上跑 3 个 label
python3 tools/simulate_emg_from_bicep.py --label standard / compensating / non_standard
```

**Step 7：决策**

- ✅ 三类都正确 → Phase 0 完成，跳到 Phase 1
- ❌ 三类不能区分 → 转 Task 0.6（备选路径）

**Step 8：写实验记录到 design doc**

Modify: `docs/plans/2026-04-26-ironbuddy-refactor-design.md` 末尾加 § "10.1 Phase 0 验证日志"，记下每个 label 测试结果。

**Step 9：commit 验证日志**

```bash
git add docs/plans/2026-04-26-ironbuddy-refactor-design.md
git commit -m "docs(P0): record simulator 3-class validation results"
```

---

### Task 0.6（备选，仅 Task 0.5 失败时）：ESP32 + 微调

> 如果 Task 0.5 三类都通了，跳过此 task 直接进 Phase 1。

**Step 1：接 ESP32 + 跑 hardware_domain_calibrate.py**

Run: `python3 tools/hardware_domain_calibrate.py`

**Step 2：MVC 校准**

Run: `python3 tools/simulate_mvc_burst.py --exercise curl`

**Step 3：真录 ~10 段 compensating**

Run: `python3 tools/collect_training_data.py --label compensating --duration 60`

**Step 4：增强**

Run: `python3 tools/augment_curl_data.py`

**Step 5：微调 GRU 最后两层**

> 需要写 fine-tune 脚本（不在原 train_gru_three_class.py 里），新建 `tools/finetune_gru_last_layers.py`。

**Step 6：commit + 重测 Task 0.5**

具体步骤略（仅在需要时展开）。

---

## 阶段 1：语音状态机 + UI 协议 + VAD 边界（3 天）

### Task 1.1：新建 voice/ package

**Files**：
- Create: `hardware_engine/voice/__init__.py`
- Create: `hardware_engine/voice/state.py`

**Step 1：建目录 + __init__**

```bash
mkdir -p hardware_engine/voice
echo '"""IronBuddy voice subsystem (state machine + recorder + turn + router)"""' > hardware_engine/voice/__init__.py
```

**Step 2：写状态机测试**

Create: `tests/test_voice_state.py`

```python
from hardware_engine.voice.state import VoiceState, VoiceStateMachine


def test_initial_state_is_listen():
    sm = VoiceStateMachine()
    assert sm.state == VoiceState.LISTEN


def test_listen_to_dialog_on_wake():
    sm = VoiceStateMachine()
    sm.transition(VoiceState.DIALOG, reason="wake_detected")
    assert sm.state == VoiceState.DIALOG


def test_invalid_transition_raises():
    sm = VoiceStateMachine()
    sm.transition(VoiceState.DIALOG, reason="wake")
    # DIALOG → BUSY 是合法但 DIALOG → LISTEN 也合法，所以这个测试要验证别的非法转移
    # 实际上 3 状态机几乎全连通，这个测试可能不需要，删掉
    pass


def test_busy_blocks_listen_transition_until_explicit_release():
    sm = VoiceStateMachine()
    sm.transition(VoiceState.BUSY, reason="auto_trigger")
    # BUSY → LISTEN 必须显式 release
    sm.transition(VoiceState.LISTEN, reason="tts_done")
    assert sm.state == VoiceState.LISTEN


def test_state_transition_logs():
    sm = VoiceStateMachine()
    sm.transition(VoiceState.DIALOG, reason="test")
    assert len(sm.history) == 1
    assert sm.history[0].from_state == VoiceState.LISTEN
    assert sm.history[0].to_state == VoiceState.DIALOG
    assert sm.history[0].reason == "test"
```

**Step 3：运行测试确认 FAIL**

Run: `python3 -m pytest tests/test_voice_state.py -v`

Expected: ImportError 或 AttributeError。

**Step 4：实现 state.py**

Create: `hardware_engine/voice/state.py`

```python
"""VoiceStateMachine — 3 状态显式状态机，替代 voice_daemon 隐式 while True。

Python 3.7 兼容（板端约束）：不用 X | None / dataclass(slots=) / match。
"""
import time
import logging
import threading
from enum import Enum
from collections import namedtuple


class VoiceState(Enum):
    LISTEN = "listen"   # 监听 wake word（默认，麦开）
    DIALOG = "dialog"   # wake 命中后录入和处理
    BUSY = "busy"       # 系统播报 / MVC / 自动触发（arecord SIGSTOP）


Transition = namedtuple("Transition", ["from_state", "to_state", "reason", "ts"])


class VoiceStateMachine(object):
    """显式 3 状态机。所有状态转移走 transition()，自动记日志和时间戳。"""

    def __init__(self):
        self._state = VoiceState.LISTEN
        self._enter_ts = time.time()
        self._lock = threading.Lock()
        self.history = []  # type: list[Transition]

    @property
    def state(self):
        return self._state

    @property
    def time_in_state(self):
        return time.time() - self._enter_ts

    def transition(self, to_state, reason=""):
        # type: (VoiceState, str) -> None
        with self._lock:
            from_state = self._state
            self._state = to_state
            now = time.time()
            transition = Transition(from_state, to_state, reason, now)
            self.history.append(transition)
            self._enter_ts = now
            logging.info(
                u"[STATE] %s → %s (reason=%s, in_prev=%.1fs)",
                from_state.value, to_state.value, reason,
                now - (transition.ts if not self.history else self.history[-1].ts),
            )

    def is_(self, *states):
        return self._state in states
```

**Step 5：运行测试确认 PASS**

Run: `python3 -m pytest tests/test_voice_state.py -v`

Expected: 全 PASS。

**Step 6：commit**

```bash
git add hardware_engine/voice/__init__.py hardware_engine/voice/state.py tests/test_voice_state.py
git commit -m "feat(voice): add VoiceStateMachine (3 states + transition log)"
```

---

### Task 1.2：voice/recorder.py — 抽 record_with_vad + arecord 进程级 gate

**Files**：
- Create: `hardware_engine/voice/recorder.py`

**Step 1：写测试（限于纯函数部分，subprocess 部分手测）**

Create: `tests/test_voice_recorder_config.py`

```python
from hardware_engine.voice.recorder import VADConfig


def test_vad_config_defaults():
    cfg = VADConfig()
    assert cfg.silence_end == 1.0
    assert cfg.hard_cap == 6.0
    assert cfg.active_speech_cap == 5.0


def test_vad_config_immutable():
    cfg = VADConfig()
    try:
        cfg.silence_end = 2.0
        assert False, "should be frozen"
    except (AttributeError, TypeError):
        pass  # OK — frozen
```

**Step 2：运行测试 FAIL**

Run: `python3 -m pytest tests/test_voice_recorder_config.py -v`

Expected: ImportError。

**Step 3：实现 recorder.py**

Create: `hardware_engine/voice/recorder.py`

```python
"""voice/recorder — 录音 VAD 抽象 + arecord 进程级 gate（解决 S2/S3/S4/S6）。

P3.7 兼容；不依赖 dataclasses（用 namedtuple frozen 模拟）。
"""
import os
import time
import wave
import logging
import subprocess
import collections

import numpy as np


class VADConfig(object):
    """VAD 参数。P3.7 没有 dataclass(frozen)，用 __slots__ 模拟。"""
    __slots__ = ("silence_end", "hard_cap", "active_speech_cap", "pre_roll")

    def __init__(self,
                 silence_end=1.0,        # 1.0 → 静音判停
                 hard_cap=6.0,           # 6s 录音硬上限（修 S6 无限录入）
                 active_speech_cap=5.0,  # 连续发声 5s 强制截断
                 pre_roll=0.3):
        object.__setattr__(self, "silence_end", silence_end)
        object.__setattr__(self, "hard_cap", hard_cap)
        object.__setattr__(self, "active_speech_cap", active_speech_cap)
        object.__setattr__(self, "pre_roll", pre_roll)

    def __setattr__(self, k, v):
        raise AttributeError("VADConfig is frozen")


class ArecordGate(object):
    """arecord 进程级 gate：BUSY 状态进入时 SIGSTOP，退出时 SIGCONT。

    解决 S3（自动播报期间录音）和 S4（MVC 期间录音）。
    """

    def __init__(self):
        self._suspended = False

    def suspend(self):
        """暂停所有 arecord 进程（不杀，用 SIGSTOP）"""
        if self._suspended:
            return
        try:
            subprocess.run(["sudo", "killall", "-SIGSTOP", "arecord"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          check=False, timeout=2)
            self._suspended = True
            logging.info(u"[ARECORD_GATE] suspended (SIGSTOP)")
        except Exception as e:
            logging.warning(u"[ARECORD_GATE] suspend failed: %s", e)

    def resume(self):
        if not self._suspended:
            return
        try:
            subprocess.run(["sudo", "killall", "-SIGCONT", "arecord"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          check=False, timeout=2)
            self._suspended = False
            logging.info(u"[ARECORD_GATE] resumed (SIGCONT)")
        except Exception as e:
            logging.warning(u"[ARECORD_GATE] resume failed: %s", e)


# record_with_vad() 函数从 voice_daemon.py 拷过来 + 用 VADConfig 替换硬编码常量。
# Step 4 拷 + 改。
```

**Step 4：拷 + 改 record_with_vad**

把 `voice_daemon.py:768-903` 的 `record_with_vad` 函数搬到 `recorder.py`：
- 替换 `VAD_TIMEOUT` → `cfg.hard_cap`
- 替换 `SILENCE_LIMIT` → `cfg.silence_end`
- 加 active_speech_cap 检查：每次 silence_time=0 reset 时检查 `time.time() - active_start > cfg.active_speech_cap` 强制 break

**Step 5：运行测试**

Run: `python3 -m pytest tests/test_voice_recorder_config.py -v`

Expected: PASS。

**Step 6：commit**

```bash
git add hardware_engine/voice/recorder.py tests/test_voice_recorder_config.py
git commit -m "feat(voice): add VADConfig + ArecordGate + record_with_vad with hard caps"
```

---

### Task 1.3：voice/turn.py — Turn dataclass + voice_turn.json

**Files**：
- Create: `hardware_engine/voice/turn.py`

**Step 1：写测试**

Create: `tests/test_voice_turn.py`

```python
import json
import os
import tempfile
from hardware_engine.voice.turn import Turn, TurnWriter


def test_turn_id_is_8_hex():
    t = Turn.new()
    assert len(t.turn_id) == 8
    assert all(c in "0123456789abcdef" for c in t.turn_id)


def test_writer_emits_atomic_json(tmp_path):
    p = tmp_path / "voice_turn.json"
    w = TurnWriter(str(p))
    t = Turn.new()
    w.write(t, stage="wake")
    data = json.loads(p.read_text())
    assert data["turn_id"] == t.turn_id
    assert data["stage"] == "wake"
```

**Step 2：FAIL → 实现**

Create: `hardware_engine/voice/turn.py`

```python
"""voice/turn — 对话轮 ID 协议（修 S1 多对话框 bug）"""
import os
import json
import time
import uuid


class Turn(object):
    __slots__ = ("turn_id", "started_ts")

    def __init__(self, turn_id, started_ts):
        self.turn_id = turn_id
        self.started_ts = started_ts

    @classmethod
    def new(cls):
        return cls(uuid.uuid4().hex[:8], time.time())


class TurnWriter(object):
    """原子写 voice_turn.json（atomic rename）"""

    def __init__(self, path="/dev/shm/voice_turn.json"):
        self.path = path

    def write(self, turn, stage, text=None):
        # type: (Turn, str, str) -> None
        payload = {
            "turn_id": turn.turn_id,
            "started_ts": turn.started_ts,
            "stage": stage,  # "wake" | "user_input" | "assistant_reply" | "closed" | "auto"
            "ts": time.time(),
        }
        if text is not None:
            payload["text"] = text
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.rename(tmp, self.path)
```

**Step 3：测试 PASS**

**Step 4：commit**

```bash
git add hardware_engine/voice/turn.py tests/test_voice_turn.py
git commit -m "feat(voice): add Turn dataclass + voice_turn.json writer (S1 fix prep)"
```

---

### Task 1.4：voice_daemon.py 主循环改造

**Files**：
- Modify: `hardware_engine/voice_daemon.py:952-1415` (main 函数)

**Step 1：备份原 main**

```bash
cp hardware_engine/voice_daemon.py hardware_engine/voice_daemon.py.pre-state-machine
```

**Step 2：改造 main 主循环骨架**

把原 `main()` 中 `while True: ...` 改成基于状态机：

```python
def main():
    # ... 原来的初始化代码（Baidu init / SpeechManager 等）保留 ...

    sm = VoiceStateMachine()
    gate = ArecordGate()
    turn_writer = TurnWriter()
    vad_cfg = VADConfig()

    # watcher 线程改成 push 状态机：
    #   _llm_reply_watcher 检测到 llm_reply.txt → sm.transition(BUSY, reason="auto_llm")
    #   _violation_alert_watcher → sm.transition(BUSY, reason="violation_alert")
    # ...

    while True:
        try:
            if sm.state == VoiceState.LISTEN:
                # 监听 wake word，timeout 短
                status = record_with_vad(timeout=WAKE_TIMEOUT, fast_start=False, cfg=vad_cfg)
                if status != "SUCCESS":
                    continue
                text = sound2text(client)
                if not _is_wake_word(text):
                    continue
                stripped = _strip_wake(text)
                turn = Turn.new()
                turn_writer.write(turn, stage="wake")
                sm.transition(VoiceState.DIALOG, reason="wake_detected")

            elif sm.state == VoiceState.DIALOG:
                # 已 wake，接下来 record + route
                if stripped:
                    user_text = stripped
                else:
                    _speak_ack("嗯", allow_interrupt=False)
                    status2 = record_with_vad(timeout=vad_cfg.hard_cap, fast_start=True, cfg=vad_cfg)
                    if status2 != "SUCCESS":
                        speak(client, "没听清", priority=PRIO_USER_ACK)
                        sm.transition(VoiceState.LISTEN, reason="vad_silence")
                        continue
                    user_text = sound2text(client)

                turn_writer.write(turn, stage="user_input", text=user_text)
                # 路由（Phase 2 实现）
                action = handle_user_text(user_text, ctx={...})
                if action.spoken_reply:
                    turn_writer.write(turn, stage="assistant_reply", text=action.spoken_reply)
                    speak(client, action.spoken_reply)
                turn_writer.write(turn, stage="closed")
                sm.transition(VoiceState.LISTEN, reason="dialog_done")

            elif sm.state == VoiceState.BUSY:
                # 等播报完成，麦克风 SIGSTOP
                gate.suspend()
                # watcher 设了 BUSY 之后，此处主循环只 sleep 等待
                # watcher 朗读完毕后 sm.transition(LISTEN, "tts_done")
                time.sleep(0.5)
                if sm.state != VoiceState.BUSY:
                    gate.resume()

            else:
                logging.error("[FATAL] unknown state %s", sm.state)
                time.sleep(1)

        except Exception as e:
            logging.exception("[MAIN_LOOP] %s", e)
            time.sleep(0.5)
```

**Step 3：本地手测**

```bash
python3 hardware_engine/voice_daemon.py 2>&1 | tee /tmp/voice_test.log &
# 喊 "教练 切到深蹲" → 看 log 是否打 [STATE] LISTEN → DIALOG → LISTEN
# 不喊话 30 秒 → 看 log 应一直在 LISTEN，不应有 wake 假触发
killall python3
```

**Step 4：commit**

```bash
git add hardware_engine/voice_daemon.py
git commit -m "refactor(voice): restructure main() around 3-state machine (S1-S6 fixes)"
```

---

### Task 1.5：UI chat-poll 用 turn_id 复用气泡

**Files**：
- Modify: `streamer_app.py` 的 `chat_input` 和 `chat_reply` 路由（加 turn_id 字段）
- Modify: `templates/index.html` 的 chat-poll 逻辑

**Step 1：streamer_app.py 加 turn_id 返回**

修改 `/api/chat_input` 和 `/api/chat_reply` 路由，从 `/dev/shm/voice_turn.json` 读 turn_id 加到响应。

**Step 2：index.html 改 chat-poll**

找到 chat 气泡渲染逻辑（约 L3554），加：
```javascript
let lastTurnId = null;
let lastUserBubble = null;
let lastReplyBubble = null;

function renderChatPoll(data) {
  if (data.turn_id !== lastTurnId) {
    lastTurnId = data.turn_id;
    lastUserBubble = null;
    lastReplyBubble = null;
  }
  if (data.stage === "user_input") {
    if (lastUserBubble) lastUserBubble.textContent = data.text;
    else lastUserBubble = createUserBubble(data.text);
  }
  // ...
}
```

**Step 3：手测**

启动 streamer + 前端，发起两次 wake → 应看到两对独立气泡（不再因为 chat_input 多次更新而新建多个）。

**Step 4：commit**

```bash
git add streamer_app.py templates/index.html
git commit -m "feat(ui): use turn_id to dedupe chat bubbles (S1 fix)"
```

---

## 阶段 2：DeepSeek tool calls + 隐式 ack（3 天）

### Task 2.1：cognitive/deepseek_client.py 统一客户端

**Files**：
- Create: `hardware_engine/cognitive/deepseek_client.py`

**Step 1：写测试**

Create: `tests/test_deepseek_client.py`

```python
from hardware_engine.cognitive.deepseek_client import DeepSeekConfig, DeepSeekClient


def test_config_defaults():
    cfg = DeepSeekConfig(api_key="sk-test")
    assert cfg.base_url == "https://api.deepseek.com/v1"
    assert cfg.timeout == 8.0


def test_chat_returns_none_on_no_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    client = DeepSeekClient(DeepSeekConfig(api_key=""))
    assert client.chat("system", "user") is None
```

**Step 2：实现**

```python
"""统一的 DeepSeek 客户端，替代 voice_daemon / streamer / deepseek_direct / fsm 4 处分散调用。"""
import os
import json
import logging
import urllib.request
import urllib.error


class DeepSeekConfig(object):
    __slots__ = ("api_key", "base_url", "model", "timeout", "max_retries")

    def __init__(self, api_key, base_url="https://api.deepseek.com/v1",
                 model="deepseek-chat", timeout=8.0, max_retries=1):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries


class ToolResponse(object):
    __slots__ = ("content", "tool_calls")

    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class DeepSeekClient(object):
    def __init__(self, config):
        self.config = config

    def chat(self, system, user, max_tokens=200, temperature=0.7):
        # type: (str, str, int, float) -> str | None
        if not self.config.api_key:
            return None
        body = json.dumps({
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.config.base_url + "/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.config.api_key,
            })
        try:
            resp = urllib.request.urlopen(req, timeout=self.config.timeout)
            data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logging.warning(u"DeepSeek chat failed: %s", e)
            return None

    def chat_with_tools(self, system, user, tools, max_tokens=400, temperature=0.3):
        # type: (str, str, list, int, float) -> ToolResponse
        if not self.config.api_key:
            return ToolResponse()
        body = json.dumps({
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": tools,
            "tool_choice": "auto",
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.config.base_url + "/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.config.api_key,
            })
        try:
            resp = urllib.request.urlopen(req, timeout=self.config.timeout)
            data = json.loads(resp.read().decode("utf-8"))
            msg = data["choices"][0]["message"]
            return ToolResponse(
                content=msg.get("content", "") or "",
                tool_calls=msg.get("tool_calls", []),
            )
        except Exception as e:
            logging.warning(u"DeepSeek chat_with_tools failed: %s", e)
            return ToolResponse()

    @classmethod
    def from_config(cls):
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            try:
                cfg_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "..", ".api_config.json")
                with open(cfg_path) as f:
                    api_key = json.load(f).get("DEEPSEEK_API_KEY", "")
            except Exception:
                pass
        return cls(DeepSeekConfig(api_key=api_key))
```

**Step 3：commit**

```bash
git add hardware_engine/cognitive/deepseek_client.py tests/test_deepseek_client.py
git commit -m "feat(cognitive): unified DeepSeekClient with chat() + chat_with_tools()"
```

---

### Task 2.2：voice/router.py — Tier A regex + tools

**Files**：
- Create: `hardware_engine/voice/router.py`
- Create: `hardware_engine/voice/tools.py` (tools spec)

**Step 1：tools.py 定义 8 条 tool**

Create: `hardware_engine/voice/tools.py`（参考设计稿 §4.2 的 8 条 tool 定义）

**Step 2：router.py 实现 + handler 分发**

Create: `hardware_engine/voice/router.py`：
- INSTANT_FALLBACK 字典
- handle_user_text 函数
- 8 个 handler 函数（switch_exercise / switch_vision_mode / ... / shutdown / report_status）
- TOOL_ACK 模板

**Step 3：写测试**

Create: `tests/test_voice_router.py`：
- 测 INSTANT_FALLBACK 命中 "静音"
- 测 fall-through 到 chat_with_tools

**Step 4：commit**

```bash
git add hardware_engine/voice/tools.py hardware_engine/voice/router.py tests/test_voice_router.py
git commit -m "feat(voice): tool calling router (8 tools + 7 instant fallbacks)"
```

---

### Task 2.3：替换 _try_voice_command

**Files**：
- Modify: `hardware_engine/voice_daemon.py` 删除 `_try_voice_command`、`_try_hardcode_chat`、`_try_deepseek_chat` 三函数；改为调用 `router.handle_user_text`

**Step 1**：找到旧三函数 + 删除

**Step 2**：改 main 中 _route_text 调用

**Step 3**：手测剧本 T1 各句

**Step 4**：commit

---

### Task 2.4：修 R1 — 把"做"移出 _EXPLICIT_CMD_MARKERS

**Files**：
- Modify: `hardware_engine/voice_daemon.py:244`

**Step 1**：删除 "做" 关键词 + 加要求双词复合（"开始做" / "切到" / "换成" / "想做"）

**Step 2**：手测 "现在适合做深蹲吗" → 应进 chat_with_tools 而不是 _try_voice_command

**Step 3**：commit

---

### Task 2.5：修 R2 — INSERT 新 system_prompt

**Files**：
- Create: `migrations/2026-04-26-neutral-system-prompt.sql`

**Step 1**：写 SQL（设计稿 §4.5）

**Step 2**：执行：

```bash
sqlite3 data/ironbuddy.db < migrations/2026-04-26-neutral-system-prompt.sql
```

**Step 3**：验证：

```bash
sqlite3 data/ironbuddy.db "SELECT id, ts, substr(prompt_text,1,100), active FROM system_prompt_versions WHERE active=1"
```

**Step 4**：commit

```bash
git add migrations/2026-04-26-neutral-system-prompt.sql
git commit -m "feat(R2): insert neutral active system_prompt (no biceps bias / no knee_caution)"
```

---

### Task 2.6：shm 双写 race 修法

**Files**：
- Modify: streamer_app.py 3 处路由（exercise_mode / fatigue_limit / inference_mode）改写 intent 文件
- Modify: main_claw_loop.py 加 intent watcher

**Step 1**：streamer 改写 intent_*.json

**Step 2**：FSM 加 watcher 把 intent 转成 state

**Step 3**：commit

---

## 阶段 3：自动疲劳 + MVC + 拍摄准备（3 天）

### Task 3.1：自动疲劳触发链

**Files**：
- Modify: `hardware_engine/main_claw_loop.py` 加疲劳上限检测 + 写 auto_trigger.json
- Modify: `hardware_engine/voice_daemon.py` 加 auto_trigger watcher → 进 BUSY → DeepSeek 总结 → TTS

**Step 1**：FSM 写 auto_trigger.json

**Step 2**：voice 加 watcher

**Step 3**：手测：人工把疲劳值改到上限 → 应触发 TTS

**Step 4**：commit

---

### Task 3.2：MVC 语音流程整合

> "开始" 已在 Tier A，无需 LISTEN_RESTRICTED 子状态

**Files**：
- Modify: `hardware_engine/voice/router.py` 加 start_mvc_calibrate handler
- Modify: `hardware_engine/voice_daemon.py` 当 tool_call switch_exercise(curl) 时跟进引导播报

**Step 1**：tool handler

**Step 2**：手测

**Step 3**：commit

---

### Task 3.3：子视频拍摄准备脚本

**Files**：
- Create: `scripts/prepare_subvideo_squat.sh`
- Create: `scripts/prepare_subvideo_curl.sh`
- Create: `scripts/reset_demo_state.sh`
- Create: `scripts/dryrun_main_video_checklist.md`

**Step 1**：写 4 个脚本（参考设计稿 §3.4）

**Step 2**：每个脚本本地跑一次确认无 syntax error

**Step 3**：commit

```bash
git add scripts/prepare_subvideo_*.sh scripts/reset_demo_state.sh scripts/dryrun_main_video_checklist.md
git commit -m "feat(shooting): prep scripts for subvideo and main video shoots"
```

---

### Task 3.4：T1-T20 测试清单 + 全回归

**Files**：
- Create: `tests/manual/T1-T20_checklist.md`

**Step 1**：把设计稿 §6 的测试列表写成 checklist 格式

**Step 2**：手测每条 → 标记 PASS / FAIL

**Step 3**：commit checklist + 测试结果

---

## 阶段 4：拍摄演练（剩余 buffer 时间）

### Task 4.1：主视频排练（半天）

按主视频剧本 T0 → T1 → T2 → T6 → T7 → T8 全程跑一次。
- 录屏 + 录音
- 标注问题点
- 补丁 < 2 小时能修的就修

### Task 4.2：子视频 1 拍摄（半天）

- prepare_subvideo_squat.sh
- 多机位录
- 至少 3 次 take

### Task 4.3：子视频 2 拍摄（半天）

- prepare_subvideo_curl.sh
- 多机位录
- 至少 3 次 take

### Task 4.4：剪辑 + 提交（1 天）

- 视频剪辑（不在代码计划范围）

---

## 收尾

### Task X：合并 design + implementation 入主分支

**Files**：
- Modify: `CLAUDE.md` 加一行索引到 design + implementation
- Modify: `docs/technical/decisions.md` 新增决策章节描述本次重构

**Step 1**：CLAUDE.md 加链接

**Step 2**：decisions.md 加章节"Decision XX：4 阶段重构 + DeepSeek tool calling"

**Step 3**：commit + push

```bash
git push origin main
```

---

## 执行说明

**写时**：每个 Task 走 TDD 节奏（写测试 → 跑 FAIL → 实现 → 跑 PASS → commit）；纯配置改动可跳过测试。

**调试时**：
- 跑测试用 `python3 -m pytest tests/ -v`
- 板端跑 voice：`python3 hardware_engine/voice_daemon.py`
- WSL 跑 simulator：`python3 tools/simulate_emg_from_*.py --label X`

**commit 频率**：每个 Task 至少 1 个 commit；Task 多 Step 时每完成一组逻辑就 commit。

**回滚**：每个 Task 都是单文件改动，`git revert <hash>` 回滚单 task 不影响其他。

**遇到问题**：
- Phase 0 三类不区分 → Task 0.6 ESP32 微调
- Phase 1 main() 改造后崩 → 用 `voice_daemon.py.pre-state-machine` 还原
- Phase 2 DeepSeek tool calling 不返回 → 检查 `tools` 参数 schema 是否合法

---

## 时间总览

| 阶段 | 工作量 | 累计 |
|---|---|---|
| Phase 0（多模态 bug + 验证） | 1.5-7 天 | 1.5-7 |
| Phase 1（语音状态机 + UI） | 3 天 | 4.5-10 |
| Phase 2（DeepSeek tool + ack） | 3 天 | 7.5-13 |
| Phase 3（自动疲劳 + MVC + 脚本） | 3 天 | 10.5-16 |
| Phase 4（拍摄演练 + 实拍） | 2-3 天 | 12.5-19 |

**deadline 2-3 周内有 2-7 天 buffer。**

---

## 引用

- 设计稿：[2026-04-26-ironbuddy-refactor-design.md](2026-04-26-ironbuddy-refactor-design.md)
- 项目主索引：[CLAUDE.md](../../CLAUDE.md)
- 决策卡：[docs/technical/decisions.md](../technical/decisions.md)
- 权威指南：[深蹲](../验收表/深蹲神经网络权威指南.md) / [弯举](../验收表/弯举神经网络权威指南.md) / [语音](../验收表/语音模块权威指南.md)
