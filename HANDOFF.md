# IronBuddy V7.30 重构 — 交接索引

> **One-page handoff**：从这里开始，能 5 分钟内续上昨晚的工作。
> 给任何 AI agent（Claude / Codex / Cursor / 新 session）粘贴这个文件就够了。

---

## 1. 背景一句话

IronBuddy（板端 AI 健身教练）做了一晚自动重构，目标是修 **多模态 bug + 语音模块状态化 + DeepSeek tool calling**，最终为比赛展示视频拍摄做准备。所有改动都是 **离线代码层重构**，没有任何硬件验证。**早上需要手动测试 + wire-up 决策**。

**北极星**：拍摄成功 + 拿奖。不追求实用产品。所有"完美主义"建议都低于"能拍下来"。

---

## 2. 三个权威文档（不要重读源码，先读这几个）

| 文档 | 作用 |
|---|---|
| [docs/plans/2026-04-26-ironbuddy-refactor-design.md](docs/plans/2026-04-26-ironbuddy-refactor-design.md) | 设计稿（13 个 bug + 4 phase 拆解 + 测试清单 T1-T20） |
| [docs/plans/2026-04-26-ironbuddy-refactor-implementation.md](docs/plans/2026-04-26-ironbuddy-refactor-implementation.md) | 逐 task 实施指南（昨晚跟着这个跑） |
| [docs/plans/progress_overall.md](docs/plans/progress_overall.md) | 昨晚的执行总结 + risk + 早上推荐顺序 |

---

## 3. 改了什么（24 commit，43 文件，+3268 / -65 行）

### 新建模块（看这些就够）

| 文件 | 用途 |
|---|---|
| [hardware_engine/voice/state.py](hardware_engine/voice/state.py) | `VoiceStateMachine`：3 状态（LISTEN/DIALOG/BUSY）显式状态机 |
| [hardware_engine/voice/recorder.py](hardware_engine/voice/recorder.py) | `VADConfig`（frozen） + `ArecordGate`（SIGSTOP/SIGCONT 麦控） |
| [hardware_engine/voice/turn.py](hardware_engine/voice/turn.py) | `Turn` + `TurnWriter`：UI 气泡去重协议（写 `/dev/shm/voice_turn.json`） |
| [hardware_engine/voice/tools.py](hardware_engine/voice/tools.py) | 8 个 DeepSeek tool 定义 + ack 模板 |
| [hardware_engine/voice/router.py](hardware_engine/voice/router.py) | Tier A regex fallback + Tier B tool dispatch（替代 414 行 if 链） |
| [hardware_engine/cognitive/deepseek_client.py](hardware_engine/cognitive/deepseek_client.py) | 统一 DeepSeek 客户端（`chat()` + `chat_with_tools()`） |
| [migrations/2026-04-26-neutral-system-prompt.sql](migrations/2026-04-26-neutral-system-prompt.sql) | R2 修补：去掉 prompt 里的"偏好肌群/疲劳容忍/膝盖" 偏置 |
| [scripts/reset_demo_state.sh](scripts/reset_demo_state.sh) | 拍摄间幕状态清零 |
| [scripts/prepare_subvideo_squat.sh](scripts/prepare_subvideo_squat.sh) | 子视频 1（深蹲）拍摄准备 |
| [scripts/prepare_subvideo_curl.sh](scripts/prepare_subvideo_curl.sh) | 子视频 2（弯举）拍摄准备 |
| [scripts/dryrun_main_video_checklist.md](scripts/dryrun_main_video_checklist.md) | 主视频 T0→T8 手测清单 |
| [tests/manual/T1-T20_checklist.md](tests/manual/T1-T20_checklist.md) | 早上手测填表（PASS/FAIL/SKIP） |

### 改动现有文件

| 文件 | 改了什么 | bug ID |
|---|---|---|
| [hardware_engine/main_claw_loop.py](hardware_engine/main_claw_loop.py) | 加 Ang_Vel 推理归一化；squat/curl rep 增量包到 `_mode_cache != "vision_sensor"` 守卫；fatigue trigger 写 auto_trigger.json | M1, M2, P3.1 |
| [hardware_engine/voice_daemon.py](hardware_engine/voice_daemon.py) | 注入 voice subsystem（state/turn/gate）；`_dialog_enter/_exit` 驱动状态机；wake 创建 turn；`record_with_vad` 加 active_speech_cap；新增 `_realize_action` adapter；新增 `_auto_trigger_watcher` thread；R1 修 `_EXPLICIT_CMD_MARKERS`（删"做"加"想做"） | S1, S6, R1, P3.1, P3.2 |
| [streamer_app.py](streamer_app.py) | `/api/chat_input` + `/api/chat_reply` 返 turn_id；新 `/api/voice_turn`；3 路由双写 `intent_*.json` | S1, R3 |
| [templates/index.html](templates/index.html) | chat-poll 用 turn_id 去重 + `updateChatBubbleText` 原地更新 | S1 |
| [tools/train_gru_three_class.py](tools/train_gru_three_class.py) | 注释掉 `comp[:,5] *= U(0.5,0.75)` symmetry 偏置 | M3 |
| [CLAUDE.md](CLAUDE.md) | 把"快速开始"的 GRU 训练命令更新到新 trainer | — |

### 测试（15 文件，125 个 unit test 全绿）

```bash
$ python3 -m pytest tests/ -v
============================= 125 passed in 0.70s =============================
```

测试**全部是离线的**：用 mock subprocess、mock urllib、AST 静态分析、文本匹配。**没有任何硬件、网络、DB 调用**。

---

## 4. ⚠️ 早上必做的 12 个 manual-pending（按优先级）

### P0：模型/DB（必须先做，T1-T20 测试依赖）

```bash
# 1) 重训 squat 模型（M3 修补依赖，旧模型按错误偏置训练的）
python3 tools/train_gru_three_class.py --epochs 20

# 2) 跑 R2 SQL migration
sqlite3 data/ironbuddy.db < migrations/2026-04-26-neutral-system-prompt.sql
sqlite3 data/ironbuddy.db "SELECT id, ts, substr(prompt_text,1,80), active FROM system_prompt_versions WHERE active=1"
# 期望：1 行，新 neutral prompt
```

### P1：simulator 三类验证（设计稿 §10.1）

```bash
# 启动 FSM
python3 hardware_engine/main_claw_loop.py 2>&1 | tee /tmp/fsm.log &
sleep 3
echo '{"mode":"vision_sensor","ts":'$(date +%s)'}' > /dev/shm/inference_mode.json
echo '{"exercise":"squat"}' > /dev/shm/user_profile.json

# 三类 simulator 各跑 30s（squat + curl 各三次共 6 次）
for L in standard compensating non_standard; do
    timeout 30 python3 tools/simulate_emg_from_mia.py --label $L
    cat /dev/shm/fsm_state.json
done
```

期望：standard→good++，compensating→comp++，non_standard→failed++。

### P2：启动全栈手测（T1-T20）

参照 [tests/manual/T1-T20_checklist.md](tests/manual/T1-T20_checklist.md) 逐项填表。关键 7 项：
- T1：「教练 现在适合做深蹲吗」→ DeepSeek 回，自动回 LISTEN
- T4：连喊 3 次"教练 切 X" → UI 三对独立气泡（turn_id 测试）
- T5：触发自动播报 → 期间环境噪音不录（ArecordGate 测试）
- T9：「现在适合做深蹲吗」 → TIER B chat，不再"没听清"（R1 测试）
- T14：自动播报期间噪音 → 不录
- T16：弯举切换 → MVC 引导 → "开始" → 倒数
- T18：「请关机」 → 隐式 ack → 关闭

### P3：决定 router wire-up（不急，可拍完再做）

新 [hardware_engine/voice/router.py](hardware_engine/voice/router.py) + `_realize_action` adapter **没有 wire 进 `_route_text`**。原因：99 行的 M5/M7/gibberish/intent 链需现场 STT 测试才能安全替换。

**当前行为**：旧 `_try_voice_command` 仍在跑，所有命令路径不变。
**新行为可选启用**：在 [hardware_engine/voice_daemon.py](hardware_engine/voice_daemon.py) 的 `_route_text`（约 L1217）"M5 两句硬编码闲聊"之前加：

```python
try:
    from hardware_engine.voice.router import handle_user_text
    _action = handle_user_text(text, _DEEPSEEK_CLIENT)  # 需要先实例化
    if _action and _action.kind != "silent":
        _realize_action(_action,
            speak_fn=lambda t: speak(client, t, priority=PRIO_USER_ACK))
        return True
except Exception as _e:
    logging.warning("[ROUTER] new path failed: %s", _e)
# fall through to existing M5/M7/A/B chain
```

---

## 5. 4 处 plan 偏离（手术式 vs 整体重写）

| Task | Plan 期望 | 实际做法 | 为什么偏离 |
|---|---|---|---|
| 1.2 | `record_with_vad` 搬到 recorder.py | 仅 VADConfig + ArecordGate；保留原函数 | 原函数与 voice_daemon 模块全局强耦合 |
| 1.4 | main() 整体重写为状态机 | surgical 注入 5 个 hook 点 | 515 行含大量 watcher 协调，重写无法离线测 |
| 2.3 | 删 _try_voice_command 三函数 | 加 `_realize_action` adapter，保留旧链 | M5/M7 链需 STT 现场测试 |
| 2.6 | UI 写 intent，FSM 加 watcher | streamer 双写 canonical+intent；FSM watcher 留 manual | watcher 进 main_claw_loop 主循环风险大 |

---

## 6. 一键状态总览

```bash
# 看 24 个 commit
git log --oneline 1208966..HEAD

# 看 13 个 manual-pending
git log --oneline 1208966..HEAD | grep "manual-pending"

# 跑 unit tests
python3 -m pytest tests/ -v 2>&1 | tail -3
```

---

## 7. 给 Codex（或新 Claude 窗口）的启动 prompt

复制下面这段贴到新 session：

> 我在 `/home/qq/projects/embedded-fullstack` 工作。这是 IronBuddy 板端 AI 健身教练项目。昨晚（2026-04-27）我让一个 Claude session 跑了一晚的 V7.30 重构（24 commits, 43 files, +3268 lines），现在要做手动测试 + wire-up 决策。
>
> **先读 `HANDOFF.md`**（项目根目录），它是单文件交接索引，5 分钟看完就知道全貌。
>
> 然后按 §4 列的优先级做事：先重训 squat 模型，再跑 SQL migration，再做 T1-T20 手测。
>
> 约束：
> - 板端 Python 3.7 兼容（不能用 `X | None` / `dataclass(slots=)` / match-case / pandas / walrus）
> - 拍摄成功 + 拿奖 > 实用产品
> - 全部隐式确认，不加三层确认
> - 前端 4025 行不重写，DB schema 不动
>
> 任何不确定就停下问我。优先做能让 T1-T20 PASS 的事。

---

## 8. 注意事项

- **claude-mem 没有这次重构的内容**：开新窗口看不到任何代码上下文，必须靠 git + HANDOFF.md。所以这个文件**不要删**。
- **所有 progress 报告在 `docs/plans/progress_phase_*.md`**：详情看那里，HANDOFF 只是索引。
- **git 干净**：本会话所有改动已 commit，工作区无未提交改动（除会话开始前就已存在的 `.agent_memory/` / `tools/rknn-toolkit_source` 子模块 / `presentation/` / `docs/report/latex/` 杂项）。
- **未 push**：`origin/main` 落后 35 个 commit。push 之前先确认手测 PASS。
