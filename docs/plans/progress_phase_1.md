# Phase 1 Progress Report

**Phase**：1 — 语音状态机 + UI 协议 + VAD 边界
**Date**：2026-04-27（automated overnight session）
**Status**：Code refactor done; voice_daemon + UI behavior verification deferred [manual-pending]

---

## 完成的 Task

### ✅ Task 1.1：voice/ package + VoiceStateMachine
- `hardware_engine/voice/__init__.py` + `state.py`
- `VoiceState` enum (LISTEN / DIALOG / BUSY) + `VoiceStateMachine`
- `Transition` namedtuple history, `time_in_state`, thread-safe transitions
- 8 unit tests covering initial state, all transition pairs, history, time tracking
- Commit `ddacd8e`

### ✅ Task 1.2：voice/recorder.py — VADConfig + ArecordGate
- Frozen `VADConfig` (silence_end=1.0, hard_cap=6.0, active_speech_cap=5.0, pre_roll=0.3)
- `apply_to_voice_daemon(mod)` 桥接器，把新默认值注入 voice_daemon 模块全局
- `ArecordGate` SIGSTOP/SIGCONT 控制（idempotent + safe noop resume）
- 9 unit tests with `monkeypatch` on `subprocess.run`
- **Plan 偏离说明**：`record_with_vad()` 主体未搬到 recorder.py（与 voice_daemon 模块全局强耦合 + 130 行）。改为 surgical 桥接 — Task 1.4 把 active_speech_cap 直接加到 voice_daemon 的原函数里。
- Commit `e365a3a`

### ✅ Task 1.3：voice/turn.py — Turn + TurnWriter
- 8-hex uuid Turn + immutable `__slots__`
- `TurnWriter` 写 `/dev/shm/voice_turn.json`（write-tmp + atomic rename）
- 5 valid stages: wake / user_input / assistant_reply / closed / auto
- `extra={"trigger_source": "fatigue_max"}` 支持后续 Phase 3 自动触发场景
- 9 unit tests
- Commit `cf3f277`

### ✅ Task 1.4：voice_daemon.py main() 集成
- 加 imports：VoiceState/StateMachine/VADConfig/ArecordGate/Turn/TurnWriter
- 模块级 singletons：`_voice_sm`、`_arecord_gate`、`_turn_writer`、`_current_turn`
- `_start_turn` / `_emit_turn_stage` / `_close_turn` 三个 helper
- `_dialog_enter`/`_dialog_exit` 同步驱动状态机 + 关闭 turn
- Wake 检测后 → `_start_turn(stage="wake")`
- `_publish_chat_input_raw` → emit user_input
- `_publish_chat_reply` → emit assistant_reply
- main() init → `VADConfig().apply_to_voice_daemon()`，覆盖旧 VAD_TIMEOUT=12 / SILENCE_LIMIT=1.2
- `record_with_vad` 加 `ACTIVE_SPEECH_CAP` 长独白硬截断（S6 修补）
- 10 AST 集成测试覆盖每个 hook 点
- **Plan 偏离说明**：515 行 `main()` body 没整体重写（plan 给的是 illustrative pseudo-code，照搬会破坏现有 watcher 协调）。surgical 注入 5 个 hook 点 + VAD config bridge，达成同样语义。
- Commit `8eba1bd`

### ✅ Task 1.5：UI chat-poll turn_id 去重
- streamer_app.py：`_read_voice_turn()` helper + `/api/chat_input` + `/api/chat_reply` 都返回 `turn_id` + `stage`
- 新增 `/api/voice_turn` 路由暴露 raw JSON（备用）
- index.html：`appendChatBubble` 返回元素 + `updateChatBubbleText` 原地更新
- chat-poll 追踪 `lastTurnId` / `currentUserBubble` / `currentReplyBubble`
  - 同 turn_id → 更新现有气泡
  - turn_id rotate → 重置引用，创建新气泡
  - 空 turn_id → 兜底走旧 append 路径
- 7 source-text + JS 检查测试
- Commit `6dfd8fd`

---

## 跳过的 Step

| Task | 跳过的 Step | 原因 |
|---|---|---|
| 1.4 | 本地手测 voice_daemon | 需要音频设备（ALSA + AipSpeech） |
| 1.5 | 启动 streamer + 前端 | 需要 cv2 + 板端可视化 |

---

## 待手测项 (`grep "manual-pending" git log`)

```
8eba1bd refactor(voice): wire state-machine + turn + ArecordGate (S1+S6 fixes) [manual-pending]
6dfd8fd feat(ui): use turn_id to dedupe chat bubbles (S1 fix) [manual-pending]
```

具体测试步骤（早上）：
1. `python3 hardware_engine/voice_daemon.py 2>&1 | tee /tmp/voice.log`
   - 看 startup 行打印 `VAD参数: SILENCE_LIMIT=1.0s, WAKE_TIMEOUT=6s, VAD_TIMEOUT=6s, ACTIVE_SPEECH_CAP=5.0s`
   - 喊 "教练，切到深蹲" → log 看到 `[STATE] listen -> dialog (reason=dialog_enter, ...)`
   - 看 `/dev/shm/voice_turn.json` 内容是否符合预期 (turn_id + stage 切换)
2. 启动 streamer + 浏览器打开
   - 喊 "教练，今天天气好吗" → 应只看到 1 个用户气泡，不重复
   - 喊第二轮 "教练，再问一次" → 应看到 2 对独立气泡
3. 长说话场景：连续讲 5+ 秒，看 voice_daemon log 应打 "[VAD] 长独白截断 (>5.0s)"

---

## 测试结果

```
tests/test_voice_state.py                     8 passed
tests/test_voice_recorder.py                  9 passed
tests/test_voice_turn.py                      9 passed
tests/test_voice_daemon_integration.py       10 passed
tests/test_streamer_voice_turn.py             7 passed
============================== 43 passed ======================================
```

---

## 疑虑 / Risk Notes

1. **voice_daemon main() 没整体重写**：plan 给的是 illustrative pseudo-code（基于状态机的 if/elif/else），但现有 main() 已经有大量 watcher / SpeechManager 协调，整体重写风险太大。改用 surgical 注入 = "把状态机和 turn writer 织进去"。状态机现在主要起 **观察** 作用（记录历史），不主动驱动主循环 —— 这是有意为之。Phase 2/3 的 `BUSY` 状态利用会进一步驱动 main loop（比如 watcher 转 BUSY 时主循环 SIGSTOP arecord）。
2. **active_speech_cap 截断行为**：在 record_with_vad 里加了一行 `if (time.time() - speech_start) > ACTIVE_SPEECH_CAP: break`。在 fast_start=True 模式下也生效。需要在板端真录 5+ 秒长 monologue 验证不误截。
3. **turn_id 空字符串兜底**：如果 voice_daemon 没启动 / voice_turn.json 不存在，前端会回退到旧 append-only 行为，不破坏现有功能。
4. **VAD 超时降低 12s → 6s**：可能对录音迟疑的用户造成误终止。如果手测发现"用户还在想就被砍了"，调 VADConfig hard_cap 回 8 即可（VADConfig 在 voice_daemon.py:_init 处实例化，参数化简单）。
5. **streamer_app.py 改动后未做 import 测试**：环境无 cv2 + flask。语法层 OK（编辑工具检查通过），但运行时如果 `_read_voice_turn` 字段名打错或类似低级错误会爆。早上启动 streamer 看第一秒报错即知。

---

## 下一步

进入 Phase 2（DeepSeek tool calls + 隐式 ack）。
