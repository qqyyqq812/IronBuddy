# Phase 3 Progress Report

**Phase**：3 — 自动疲劳 + MVC + 拍摄准备
**Date**：2026-04-27（automated overnight session）
**Status**：Code refactor done; live trigger chain + shoot rehearsal deferred [manual-pending]

---

## 完成的 Task

### ✅ Task 3.1：自动疲劳触发链
- main_claw_loop.py: 在 fatigue_max / 手动按键触发 LLM 总结**之前**写 `/dev/shm/auto_trigger.json`
- voice_daemon.py: 新增 `_auto_trigger_watcher` thread（poll 0.5s），收到信号后：
  - state machine LISTEN → BUSY
  - ArecordGate.suspend()（避免 TTS 漏录到 mic）
  - 开 `auto` 阶段 Turn（extra={trigger_source, stats}）
  - 消费文件
- 8 AST 测试 + payload 验证
- Commit `a6bd8f2`

### ✅ Task 3.2：MVC 语音流程整合
- `_realize_action` 处理 `switch_exercise(curl)` 时额外播报 "准备好后请说 开始 MVC 测试"（隐式确认，不强制执行）
- squat 路径不变（无 MVC 引导）
- 2 新 unit test
- Commit `aa890e2`

### ✅ Task 3.3：拍摄准备脚本
- `scripts/reset_demo_state.sh`：拍摄间幕清理（killall 录音/播放进程 + 删 18 个 shm 信号文件 + 重置 fatigue_limit + mute）
- `scripts/prepare_subvideo_squat.sh`：reset + squat/pure_vision 写入 + 启动 checklist 打印
- `scripts/prepare_subvideo_curl.sh`：reset + curl/vision_sensor 写入 + MVC 校准提醒
- `scripts/dryrun_main_video_checklist.md`：T0→T8 主视频手测清单 + 失败模式排查 + 抢救机制
- 三个 .sh 都通过 `bash -n` syntax 检查
- Commit `d5b56f5`

### ✅ Task 3.4：T1-T20 测试清单
- `tests/manual/T1-T20_checklist.md`：所有 20 测试组织成 PASS/FAIL/SKIP 表格
- 包含 decision criteria（all pass → Phase 4；P0 任一 fail → block；总 ≥3 fail → halt）
- 早上由用户手动填写
- Commit `47866a8`

---

## 跳过的 Step

| Task | 跳过的 Step | 原因 |
|---|---|---|
| 3.1 | 人工把疲劳值改到上限 | 需要 FSM + simulator 配合 |
| 3.2 | 手测 MVC 流程 | 需要 voice_daemon + EMG |
| 3.4 | 整 task 手测填表 | 需要全栈运行 |

---

## 待手测项（Phase 3 部分）

```
a6bd8f2 feat(P3): auto fatigue trigger chain (FSM → auto_trigger.json → voice BUSY)
aa890e2 feat(P3): MVC follow-up after switch_exercise(curl)
47866a8 docs(P3): T1-T20 manual checklist
```

早上手测顺序参照 `tests/manual/T1-T20_checklist.md`：
1. 先跑 P0 simulator 三类（M3 重训前提）
2. 跑 R2 SQL migration
3. 启动全栈服务
4. T1-T20 顺序手测

---

## 测试结果（automated）

```
$ python3 -m pytest tests/ -v
============================= 125 passed in 0.70s =============================
```

每个 phase 测试覆盖：

| Phase | 测试文件 | 数量 |
|---|---|---|
| 0 | test_main_claw_loop_normalization / test_fsm_mode_gating / test_train_symmetry_alignment | 11 |
| 1 | test_voice_state / test_voice_recorder / test_voice_turn / test_voice_daemon_integration / test_streamer_voice_turn | 43 |
| 2 | test_deepseek_client / test_voice_router / test_realize_action / test_voice_command_intent / test_migration_neutral_prompt / test_streamer_intent_writes | 63 |
| 3 | test_auto_trigger_chain / + 2 in test_realize_action | 10 |

---

## 疑虑 / Risk Notes

1. **auto_trigger.json 写入时机和 LLM 总结同步**：FSM 在写 auto_trigger.json **之后**立即调用 `asyncio.create_task(_ds_wrapper(...))`。voice_daemon 的 watcher 0.5s 周期，可能在 `_ds_wrapper` 已经写 `llm_reply.txt` 之后才看到 auto_trigger。
    - 实际影响：state machine 转 BUSY 可能比 TTS 开始晚 ~0.5s。但这个 BUSY 转换 *主要* 用于 mic suspend；TTS 本身已经通过 `_mic_allowed.clear()` 互斥。所以是 belt-and-suspenders 而非关键路径。
    - 真实播放质量取决于现有 _llm_reply_watcher 行为（未变）。
2. **MVC 引导仅在新 router 路径生效**：当前 `_realize_action` 还没 wire 到 `_route_text` 主链。所以 T16 / T2.6 路径还是走老 `_try_voice_command` 的硬编码逻辑（已存在 MVC 提示）。一旦 manual wire-up 完成，新路径接管。
3. **shooting prep 脚本依赖 sudo killall**：reset_demo_state.sh 用了 sudo killall；如果用户没设 NOPASSWD sudo，会卡住。如果板端不能 sudo，改成 `killall` 即可（板端常用 root 运行）。
4. **T20 不是真"自动" ready**：脚本只是写入 shm 文件 + 打 checklist；实际 ready 需要 5 个 service 都已启动。脚本里 echo 的 pgrep 命令只是提醒，不阻断。

---

## Phase 3 完成 — 全 4 phase 收尾

详见 `docs/plans/progress_overall.md`（即将提交）。
