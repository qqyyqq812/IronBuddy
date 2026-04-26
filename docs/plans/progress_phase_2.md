# Phase 2 Progress Report

**Phase**：2 — DeepSeek tool calls + 隐式 ack
**Date**：2026-04-27（automated overnight session）
**Status**：Code refactor done; live routing wire-up + SQL execution deferred [manual-pending]

---

## 完成的 Task

### ✅ Task 2.1：cognitive/deepseek_client.py 统一客户端
- `DeepSeekConfig` (slots) + `ToolResponse` (slots) + `DeepSeekClient`
- `chat()` 返回 str|None；`chat_with_tools()` 返回 ToolResponse；`from_config()` 自动加载 env→file
- 10 unit tests with full mock urllib (no real network)
- Commit `cf3a856`

### ✅ Task 2.2：voice/router.py + tools.py
- `tools.py`：8 tool 定义（OpenAI 兼容 schema）+ TOOL_ACK 模板 + DISPLAY_NAMES
- `router.py`：Action namedtuple + Tier A INSTANT_FALLBACK (7 keywords, 长度优先) + Tier B chat_with_tools 分发
- NEUTRAL_PROMPT 与 R2 SQL 对齐
- Pure module（不依赖 Flask 或 /dev/shm）— 实现纯函数式 routing
- 21 unit tests，包含空输入、Tier A 全 fallback、tool dispatch（str/dict args）、JSON malformed 容错、回退 chat content
- Commit `9ba015d`

### ✅ Task 2.3：voice_daemon._realize_action adapter
- 把 router Action 翻译为 live 副作用：speak / set_mute / stop_speaking / switch_exercise / switch_vision_mode / switch_inference_backend / set_fatigue_limit / start_mvc_calibrate / push_feishu_summary / shutdown / report_status
- `_format_status_report()` 读 `/dev/shm/fsm_state.json` 生成口头报告
- 15 unit tests 通过 exec snippet 加载 + mock _write_signal/speak_fn
- **Plan 偏离说明**：plan 要求"删除 _try_voice_command/_try_hardcode_chat/_try_deepseek_chat 三函数，改为调用 router.handle_user_text"。但 `_route_text` 是 99 行紧密耦合的链（M5/M7/gibberish/intent），整体替换无法在 automated session 单测验证（需现场 STT 测真实近音误识别）。改为分两步：
  - 本 task 完成 adapter 模块（已可单测）
  - Wire-up 留 [manual-pending]：早上人工把 `_route_text` 中"B 路 没听清"前面塞 fallback `action = handle_user_text(text, deepseek_client); _realize_action(action, speak_fn=speak)`
- Commit `f58d76c`

### ✅ Task 2.4：R1 — 移除 "做" + 加 "想做"
- `_EXPLICIT_CMD_MARKERS` 改：`"做"` 删除，`"想做"` 加入
- 4 unit tests 解析 marker 元组
- 现在 "现在适合做深蹲吗" 不再误进 A 路；"想做深蹲" 仍正确进 A 路
- Commit `6f3b939`

### ✅ Task 2.5：R2 — neutral system_prompt SQL migration
- `migrations/2026-04-26-neutral-system-prompt.sql`：BEGIN TRANSACTION → INSERT 新 prompt → 用 `MAX(id)` 把旧 active 全部降级 → COMMIT
- 新 prompt 不预设 "biceps preference" / "knee_caution" / "疲劳容忍"
- 在 in-memory sqlite 上跑通验证（旧 active=0，新 active=1）
- 6 unit tests 验证 SQL 形态
- **不执行 SQL**：早上人工跑 `sqlite3 data/ironbuddy.db < migrations/2026-04-26-neutral-system-prompt.sql`
- Commit `a3837d7`

### ✅ Task 2.6：R3 — shm 双写 race
- streamer 三路 (`/api/exercise_mode` / `/api/switch_inference_mode` / `/api/fatigue_limit`) 现在并写：
  - canonical：`/dev/shm/<name>.json`（保留兼容现有 FSM）
  - intent：`/dev/shm/intent_<name>.json`（新；FSM 加 watcher 后可独立观察 UI 起源请求）
- 所有 intent 携带 `"src": "ui"` tag
- 提取 `_atomic_write_json` helper（消除 6 个 inline tmp+rename 块）
- 5 source-text tests
- **Plan 偏离说明**：plan 要求 "FSM 加 watcher 把 intent 转成 state"。FSM 端的 intent watcher 是 main_claw_loop 主循环里的读盘逻辑，需要 simulator + FSM 联动测试，留 [manual-pending]。
- Commit `faeefca`

---

## 跳过的 Step

| Task | 跳过的 Step | 原因 |
|---|---|---|
| 2.3 | _route_text wire-up | M5/M7/gibberish 链需 STT 测试 |
| 2.3 | 手测剧本 T1 各句 | 需要真 voice_daemon 运行 |
| 2.5 | sqlite3 migration 执行 | 不允许动主 DB |
| 2.6 | FSM intent watcher | 需要 main_claw_loop + simulator 联动 |

---

## 待手测项 (`grep "manual-pending" git log`)

```
8eba1bd refactor(voice): wire state-machine + turn + ArecordGate (S1+S6 fixes)
6dfd8fd feat(ui): use turn_id to dedupe chat bubbles (S1 fix)
f02eabb fix(M3): symmetry=1.0 in training to match inference
a467479 docs(P0): add §10.1 Phase 0 validation log placeholder
f58d76c feat(voice): add _realize_action adapter (router → shm writes)
a3837d7 feat(R2): write neutral active system_prompt migration
```

早上手测顺序：
1. 跑 P0 simulator 三类（depend on M3 重训）
2. 跑 SQL migration
3. 启动 voice_daemon → 喊"教练, 切到深蹲" → 看 `/dev/shm/exercise_mode.json` 是否更新（验证 _realize_action 路径还没 wire-up，但 _try_voice_command 旧路径仍工作）
4. 把 _realize_action wire 进 _route_text（手动改：在"3) A 路: 命令意图"分支前加 router 尝试）

---

## 测试结果

```
tests/test_deepseek_client.py             10 passed
tests/test_voice_router.py                21 passed
tests/test_realize_action.py              15 passed
tests/test_voice_command_intent.py         4 passed
tests/test_migration_neutral_prompt.py     6 passed
tests/test_streamer_intent_writes.py       5 passed
============================== 61 passed ======================================
```

---

## 疑虑 / Risk Notes

1. **_realize_action 没 wire-up**：今天的提交里 `_realize_action` 是一个孤儿函数（没有调用方）。如果早上 wire 到 `_route_text` 出错（typo/参数顺序错），整个语音命令路径会断。建议早上做最小 wire-up：在 `_route_text` 的 "B 路保守化拦截" 之前加：

   ```python
   try:
       _action = handle_user_text(text, _DEEPSEEK)
       if _action and _action.kind != "silent":
           _realize_action(_action, speak_fn=lambda t: speak(client, t, priority=PRIO_USER_ACK))
           return True
   except Exception as _e:
       logging.warning("[ROUTER] new path failed: %s", _e)
   # fall through to existing M5/M7/A路/B路
   ```

   即使新 router 失败也回退老路径，零风险。
2. **R2 SQL 用 `datetime('now')`**：sqlite 默认 UTC。如果想要本地时间，改 `datetime('now', 'localtime')`。当前 prompt 表 ts 是 ISO 字符串，UTC 也能 work。
3. **R3 intent_*.json 未被任何东西消费**：仅 streamer 写入，FSM 未读。这些文件目前是观察用 telemetry，不影响行为。
4. **DeepSeek 客户端没换调用**：`voice_daemon._try_deepseek_chat`、`cognitive/deepseek_direct.py` 等 4 处旧调用 **未替换**。新 client 只是新增。要全部迁移需要逐处审视参数差异，留给手测后做。

---

## 下一步

进入 Phase 3（自动疲劳 + MVC + 拍摄准备）。
