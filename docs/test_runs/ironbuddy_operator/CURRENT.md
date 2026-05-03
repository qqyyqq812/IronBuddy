# IronBuddy 当前调试状态

本文件是 IronBuddy 长时间调试的**唯一事实入口**。新开的 Codex、
Claude Code 或人工复盘窗口先读这里，再进入具体 run 目录。

> 项目根目录的 `HANDOFF.md` 是 V7.30 重构期的旧索引（保留作历史），
> 调试期所有"先读我"的位置都以本文件为准。

更新时间：2026-05-03 00:36 CST（服务在线；双波形 Sensor Lab 已验证，raw ADC 20Hz 快刷，等待 ESP32 持续发包）。

## 板端锁

| 字段 | 当前值 |
|---|---|
| `lock_owner` | `free` |
| `lock_taken_at` | -- |
| `lock_reason` | -- |
| `next_release` | -- |

拿锁动作：把 `lock_owner` 改成 `lane_a` / `lane_b` / `claude_code`，
填上 `lock_taken_at`（CST 时间）和一句话 reason，再做副作用动作。
释放时改回 `free` 并清空其他三栏。免锁动作（只读 API、SSH、日志、
`/dev/shm`）不需要写这里。规则见 `AGENT_LANES.md` L52–66。

## 当前结论

- 调试工作台主入口是 `docs/test_runs/ironbuddy_operator/`。
- 给操作者看的极简入口是
  `docs/test_runs/ironbuddy_operator/OPERATOR_FINAL_GUIDE.md`。
- token 过多或多窗口迁移时，先读
  `docs/test_runs/ironbuddy_operator/WINDOW_MIGRATION.md`。
- Lane A 使用 `tools/ironbuddy_operator_console.py`，负责语音、主网页、
  API、数据库、飞书和拍摄主线。
- Lane B 使用 `tools/ironbuddy_sensor_lab.py`，负责真实 EMG、
  Sensor/GRU 和视觉+传感融合验证。
- 不开两套完整板端服务。板端核心服务共享一套，测试 UI 外置到本机。
- Claude Code 新窗口默认先做阶段审查和工作流补强，不直接抢板端锁。
- 当前板端 IP 是 `10.244.190.224`。

## 当前进度

- Lane B Sensor Lab 已建成，默认地址是 `http://127.0.0.1:8766/`。
- Sensor Lab 当前 run 是
  `docs/test_runs/ironbuddy_sensor_lab/20260502-141338/`，已显式指向
  当前板端 `10.244.190.224`；旧 `20260502-120043` run 使用过期
  `10.235.20.224`，只作历史参考。
- Lane A operator console 当前 run 是
  `docs/test_runs/ironbuddy_operator/20260502-190758/`，scenario 为
  `rag_feishu_cloud_retest`，用于 RAG/飞书/OpenCloud 模块现场复测。
  `20260502-134526` 是早期 sha256 一致性 run，保留作历史证据。
- 最近本机验证过 Sensor Lab 页面和 `/api/status` 可访问。
- 2026-05-02 12:44 CST 已验证板端 `10.244.190.224` 在线：
  `/api/fsm_state` 返回 `NO_PERSON`、`squat`、`pure_vision`、计数为 0。
- SSH 已验证五个核心进程在线：vision、streamer、fsm、emg、voice。
- Lane A 仍以 `docs/test_runs/ironbuddy_operator/` 下的 run 记录为主，
  后续语音和拍摄主线继续从 operator console 进入。
- 2026-05-02 14:06 CST，Lane A 已完成本地核心板端文件与板端
  `/home/toybrick/streamer_v3/` 同名文件的 `sha256sum` 一致性比对：
  9/9 MATCH，0 mismatch，0 missing。证据见当前 operator run 的
  `events.jsonl` 末尾 `sha256_check` 事件。
- 2026-05-02 15:31 CST，Lane A 已将展示闭环修复部署到板端并释放锁：
  更新 `voice_daemon.py`、`main_claw_loop.py`、`streamer_app.py`、
  `templates/index.html`；远端备份在
  `/home/toybrick/streamer_v3/.deploy_backups/lane_a_20260502_152634/`。
  板端 `py_compile` 通过，受影响的 streamer/mainloop/voice 已重启；
  vision 和 emg 保持运行。`/api/chat_events` 返回 `ok=true`，
  `mainloop.log` 显示 DeepSeek Direct 已就绪。下一步是现场复测气泡顺序。
- 2026-05-02 17:04 CST，Lane A 已部署语音/MVC 复测修复到板端并释放锁：
  远端备份在
  `/home/toybrick/streamer_v3/.deploy_backups/lane_a_20260502_165933_voice_emg/`；
  `hardware_engine/voice_daemon.py` 远端 sha256 为
  `d08244ab6013fcbe953ffd5e3c8f8a1ef35676c5b9b0986f793b241c6ae988bc`。
  当前 operator run 是
  `docs/test_runs/ironbuddy_operator/20260502-165439/`，scenario 为
  `voice_emg_retest`，停在 `voice_emg_start`；Sensor Lab run 是
  `docs/test_runs/ironbuddy_sensor_lab/20260502-165520/`。板端
  `voice_daemon` PID `16657` 在线，`/api/fsm_state` 和
  `/api/admin/voice_diag` 均可访问；Sensor Lab 当前判断
  `real_emg=false`、`udp_online=false`、`sensor_simulated=true`。
- 2026-05-02 18:09 CST，Lane B 已在本机 Arduino IDE 2.3.8 完成
  `C:\arduino_work\WiFiUDPClient\WiFiUDPClient.ino` 的 ESP32 上传：
  板型 `ESP32 Dev Module`，端口 `COM13`，USB 串口为
  `USB-SERIAL CH340`，Upload Speed `115200`。上传日志显示
  `Connected to ESP32 on COM13`、芯片 `ESP32-D0WD-V3`、多段
  `Hash of data verified`、`Hard resetting via RTS pin`。此前
  `No serial data received` 的直接原因是上传时还接着外设/面包板；
  拔掉所有外设、只保留 USB 后自动下载成功，未按 BOOT。
- 2026-05-02 18:40 CST，Lane B 离线验收准备完成：新增只读预检工具
  `tools/ironbuddy_lane_b_readiness.py`、现场验收单
  `docs/test_runs/ironbuddy_sensor_lab/LANE_B_ACCEPTANCE_READY.md` 和回归测试
  `tests/test_lane_b_readiness.py`。在手机热点未上线状态下，本地离线预检
  `python3 tools/ironbuddy_lane_b_readiness.py` 返回 `status: READY`；
  联网后再运行 `python3 tools/ironbuddy_lane_b_readiness.py --probe-board`
  做只读 HTTP/SSH 探测。
- 2026-05-02 19:10 CST，Lane A 已将 RAG-lite、飞书 interactive card
  和 OpenCloud 状态接口模块部署到板端并释放锁：远端备份在
  `/home/toybrick/streamer_v3/.deploy_backups/lane_a_20260502_190322_rag_feishu_cloud/`；
  上传文件为 `voice_daemon.py`、`streamer_app.py`、
  `hardware_engine/integrations/feishu_client.py`、
  `hardware_engine/cognitive/coach_knowledge.py`、`data/coach_kb/*.json`
  和 `scripts/opencloud_reminder_daemon.py`。远端 `py_compile` 通过；
  仅重启 streamer 和 voice，当前 PID 分别为 `23555`、`23556`，
  vision/mainloop/emg 保持运行。烟测通过：
  `/api/coach/capabilities`、`/api/coach/rag_query`、
  `/api/feishu/card_push` dry-run、`/api/opencloud/status` 均返回可用；
  飞书 dry-run 为 interactive card，OpenCloud 状态不返回密钥值。

- 2026-05-02 22:50 CST，Lane A 已部署 RAG 展示口径、语音播报打断、
  UI 静音止播和音量控制修复到板端并释放锁：远端备份在
  `/home/toybrick/streamer_v3/.deploy_backups/lane_a_20260502_222754_rag_voice_control/`；
  上传文件为 `hardware_engine/voice_daemon.py`、
  `hardware_engine/cognitive/coach_knowledge.py`、
  `data/coach_kb/ironbuddy_manual.json`、`streamer_app.py`、
  `templates/index.html`。远端 `py_compile` 通过；仅重启 streamer
  和 voice，当前 PID 分别为 `30768`、`30769`，vision/mainloop/emg
  保持运行。烟测通过：`/api/coach/capabilities` 回复不直接念出
  唤醒词，`/api/openclaw/status` 返回 `OpenClaw 云端提醒`，
  `/api/tts_volume` 可设置音量，`/api/admin/voice_diag` 显示语音在线，
  `/api/chat_events` 可用；`/tmp/voice_daemon.log` 已观察到 UI 静音触发
  `external_mute` 并中断当前 TTS。当前复测台在
  `docs/test_runs/ironbuddy_operator/20260502-225052/`，scenario 为
  `rag_feishu_cloud_retest`，前两步后台预检已自动标为通过，现场从
  `教练功能介绍` 开始。
- 2026-05-03 00:35 CST，Lane A 已部署 RAG/voice control fix 并释放锁：
  远端备份在
  `/home/toybrick/streamer_v3/.deploy_backups/lane_a_20260503_0024_rag_voice_fix/`；
  上传文件为 `hardware_engine/voice_daemon.py`、`streamer_app.py`、
  `hardware_engine/cognitive/coach_knowledge.py`。远端 `py_compile`
  通过；仅重启 streamer 和 voice，当前 PID 分别为 `27052`、`27747`，
  vision/mainloop/emg 保持运行。烟测通过：`/api/fsm_state` 可用；
  `/api/coach/capabilities` 返回 4 句固定自然介绍，不含“拍摄/演示”；
  `/api/tts_volume` 设置/读取 `vol=9`；`/api/mute` 解除静音按当前音量
  恢复 mixer；`/api/admin/voice_diag` 显示语音在线且读取新
  `/tmp/voice_daemon.log`；本地验证 `python3 -m py_compile
  hardware_engine/voice_daemon.py streamer_app.py tools/ironbuddy_operator_console.py`
  和 `pytest tests/test_coach_knowledge.py tests/test_voice_daemon_integration.py
  tests/test_streamer_voice_turn.py tests/test_operator_console_scenarios.py
  tests/test_feishu_opencloud_api_source.py -q` 为 `60 passed`。下一步使用
  新 operator 场景 `rag_voice_control_fix_retest` 现场复测，不复用旧
  `20260502-225052`。
- 2026-05-03 01:26 CST，Lane A 已部署复测反馈修复并释放锁：远端备份在
  `/home/toybrick/streamer_v3/.deploy_backups/lane_a_20260503_0121_voice_feedback_fix/`；
  上传文件为 `hardware_engine/voice_daemon.py`、`streamer_app.py`、
  `hardware_engine/cognitive/coach_knowledge.py`、`templates/index.html`。
  远端 `py_compile` 通过；仅重启 streamer 和 voice，当前 PID 分别为
  `26225`、`26226`，vision/mainloop/emg 保持运行。烟测通过：
  `/api/coach/capabilities` 返回固定 4 句介绍且不含“拍摄/演示/教练/MVC”；
  `/api/mute` 解除静音返回 `replay_requested=true` 并写
  `/dev/shm/replay_last_tts.json`；`/api/admin/voice_diag` 显示语音在线；
  `/tmp/voice_daemon.log` 已观察到 `MuteReplay` 补播路径。本地验证
  `python3 -m py_compile hardware_engine/voice_daemon.py streamer_app.py
  tools/ironbuddy_operator_console.py` 和 `pytest tests/test_coach_knowledge.py
  tests/test_voice_daemon_integration.py tests/test_streamer_voice_turn.py
  tests/test_operator_console_scenarios.py tests/test_feishu_opencloud_api_source.py -q`
  为 `62 passed`。当前本机 operator console 在 tmux session
  `ironbuddy_operator_console`，地址 `http://127.0.0.1:8765/`，新 run 是
  `docs/test_runs/ironbuddy_operator/20260503-012648/`。
- 2026-05-03 11:25 CST，用户重启宿主机后板端 IP 已切换为
  `10.244.190.224`，本机入口已同步更新：`tools/ironbuddy_operator_console.py`、
  `tools/ironbuddy_sensor_lab.py`、`tools/ironbuddy_lane_b_readiness.py` 和
  当前调试文档均默认指向新 IP。只读验证通过：`/api/fsm_state`、
  `/api/coach/capabilities`、`/api/admin/voice_diag` 可访问；SSH 看到五个核心
  进程在线：vision PID `581`、streamer PID `603`、mainloop PID `615`、
  EMG PID `616`、voice PID `617`。本机 operator console 已在 tmux session
  `ironbuddy_operator_console` 重新启动，地址 `http://127.0.0.1:8765/`，
  新 run 是 `docs/test_runs/ironbuddy_operator/20260503-112505/`。
- 2026-05-03 12:57 CST，Lane A 已部署录制导向修复并释放锁：远端备份在
  `/home/toybrick/streamer_v3/.deploy_backups/lane_a_20260503_125252_recording_rehearsal/`；
  上传文件为 `hardware_engine/voice_daemon.py`、`hardware_engine/main_claw_loop.py`、
  `streamer_app.py`、`templates/index.html`、
  `hardware_engine/cognitive/coach_knowledge.py` 和
  `scripts/opencloud_reminder_daemon.py`。远端 `py_compile` 通过；仅重启
  streamer/mainloop/voice，当前 PID 分别为 `6885`、`6886`、`6887`，
  vision/emg 保持 `581`、`616`。本地验证通过：
  `python3 -m py_compile hardware_engine/voice_daemon.py hardware_engine/main_claw_loop.py streamer_app.py tools/ironbuddy_operator_console.py scripts/opencloud_reminder_daemon.py`；
  相关 pytest 为 `72 passed` 和扩展集 `54 passed`。板端 smoke 通过：
  `/api/fsm_state` 恢复 `squat/pure_vision`、`state_feed.angle_diag` 可见、
  `/api/demo/rag_status`、`/api/demo/opencloud_records`、`/api/demo/code_graph`
  可用，主网页包含“后台调试 / 知识库 / 云端记忆 / 代码结构图”入口。
  本机 operator console 已切到 `recording_rehearsal`，地址
  `http://127.0.0.1:8765/`，新 run 是
  `docs/test_runs/ironbuddy_operator/20260503-125650/`。
- 2026-05-03 13:08 CST，Lane A 修复主网页“一键停止后服务状态灯不熄灭”
  并释放锁：远端备份在
  `/home/toybrick/streamer_v3/.deploy_backups/lane_a_20260503_130420_stop_ui_status_fix/`。
  更新 `streamer_app.py` 和 `templates/index.html`。新契约是：一键停止
  保留 streamer 控制台运行，vision/fsm/emg/voice 熄灭；前端在请求失败或
  连接中断时也立即把服务灯置为离线/未运行，不保留旧绿色状态。同时
  `/api/admin/start` 的 voice 启动改为走 `scripts/start_voice_with_env.sh`，
  避免绕过 `.api_config.json` 凭证。验证通过：`python3 -m py_compile
  streamer_app.py`；`/api/admin/stop` 返回 `streamer: 控制台保留运行`；
  当前全服务在线：vision `22321`、streamer `29949`、fsm `22511`、
  emg `22775`、voice `31157`；`/api/admin/voice_diag` 显示
  `voice_boot_status.status=queued`，欢迎词已入队。
- 2026-05-03 16:52 CST，Lane A 已部署 FSM 录制计数收口修复并释放锁：
  远端备份在
  `/home/toybrick/streamer_v3/.deploy_backups/lane_a_20260503_164736_fsm_rep_accounting/`；
  仅更新 `hardware_engine/main_claw_loop.py`，远端 `py_compile` 通过，
  只重启 main loop，当前 PID `2020`。本地验证通过：
  `python3 -m py_compile hardware_engine/main_claw_loop.py streamer_app.py`；
  `pytest tests/test_main_claw_loop_angle_safety.py tests/test_fsm_mode_gating.py
  tests/test_auto_trigger_chain.py -q` 为 `27 passed`。板端 smoke 通过：
  `/api/fsm_state` 已暴露 `rep_in_progress`、`last_rep_result`、
  `last_finalize_reason`、`last_drop_reason`、`total_reps`；main loop
  启动时已从 `.api_config.json` 注入 DeepSeek 环境，日志显示
  `DeepSeek Direct 已就绪`。本轮未重启 streamer/voice/vision/emg。
- 2026-05-02 22:56 CST，Lane B 已把本机 Sensor Lab 升级为
  "GRU 标注验收台"，只改本机 `tools/ironbuddy_sensor_lab.py`、
  `tests/test_sensor_lab_display.py` 和 `docs/technical/ironbuddy_sensor_lab.md`，
  未改 `streamer_app.py`、未重启板端服务、未 reset 或清 `/dev/shm`。
  当前本机 Lab 进程 PID `10168` 监听 `http://127.0.0.1:8766/`，
  当前 run 是 `docs/test_runs/ironbuddy_sensor_lab/20260502-225507/`。
  页面新增三类标签组测：`standard`、`compensating`、`non_standard`；
  点击"开始本组"会调用现有板端 API 同步
  `/api/user_profile`、`/api/exercise_mode`、`/api/switch_inference_mode`
  和 best-effort `/api/test_capture/start`，然后做 5 秒 baseline，
  按 rep 记录真值、GRU 预测、confidence、similarity、target/comp peak、
  计数 delta、命中结果和错分归因。波形已改为 raw snapshot fallback，
  并增加 `fsm.exercise` 与 `muscle_activation.exercise` mismatch 诊断。
  验证通过：`python3 -m py_compile tools/ironbuddy_sensor_lab.py`、
  `pytest tests/test_sensor_lab_display.py tests/test_lane_b_readiness.py -q`
  为 `7 passed`；`python3 tools/ironbuddy_lane_b_readiness.py --probe-board`
  返回 `OK=25 WARN=1 FAIL=0`，唯一 WARN 是还未现场完成动作所以
  `historical_gru` 未产生结果。当前只读状态已看到 `udp_online=true`、
  `real_emg=true`、`fsm.exercise=bicep_curl`，但尚未点击开始本组，
  因此 `inference_mode=pure_vision`、GRU classification 仍为 `unknown`。
- 2026-05-02 23:43 CST，Lane B 已拿锁并部署一个只读 raw EMG debug
  snapshot 到板端 `hardware_engine/sensor/udp_emg_server.py`，随后释放锁。
  远端备份在
  `/home/toybrick/streamer_v3/.deploy_backups/lane_b_20260502_233509_emg_debug/`；
  只重启了 EMG 进程，当前 `udp_emg_server.py` PID 为 `7685`，UDP
  `0.0.0.0:8080` 监听正常，主 Flask/FSM/vision/voice 未重启。新增
  `/dev/shm/emg_debug_snapshot.json`，字段包含 raw ADC、filtered、RMS、
  MVC、pct、domain、exercise 和 packet_count；Sensor Lab 已接入该文件，
  当 0-100 activation 饱和时会显示 RMS/ADC 诊断，不再只显示贴顶平线。
  同时修复 Sensor Lab "开始本组"遇到陈旧 `/dev/shm/test_capture.session`
  409 时自动 stop/clear 后重试；现场遗留的 session `384` 已用
  `/api/test_capture/clear` 清掉，当前 `/api/test_capture/status` 为
  `active=false`。当前关键结论：板端曾收到一包 ESP32 UDP
  `raw_values=[1792, 961]`、`rms=[213.85, 114.68]`、`pct=[24, 20]`，
  但随后 EMG 日志显示 `UDP 长达 500ms 阻断`，当前 debug snapshot 已过期
  约 6 分钟，`udp_online=false`、`real_emg=false`；所以此刻不是 Lab
  没工作，而是 ESP32 没有持续发包。
- 2026-05-03 00:19 CST，用户重启后已复查并补齐 Lane B 快速原始波形：
  本地 Sensor Lab 进程 PID `8613` 监听 `http://127.0.0.1:8766/`；
  板端五个核心进程在线：vision PID `588`、mainloop PID `623`、
  EMG PID `26237`、streamer PID `30768`、voice PID `30769`。
  板端 `hardware_engine/sensor/udp_emg_server.py` 与本机 sha256 一致：
  `bc09fdfbd183032f425cd86c5488f6214e408eeb38205baa6e791329db39f386`，
  板端 `python3 -m py_compile hardware_engine/sensor/udp_emg_server.py`
  通过。Sensor Lab 页面现在有两个独立波形框：
  `滤波后 / RMS / 激活` 和 `原始 ADC 快速波形`；raw ADC 通过
  `/api/emg_fast` 从 `/dev/shm/emg_raw_waveform.json` 快速读取，前端
  `setInterval(loadFastWave, 120)` 刷新。合成 UDP 260 包已验证链路：
  `/api/emg_fast` 返回 `ok=true`、`samples=420`、最近点
  `[1777738615.1877313, 2659.0, 2048.0]`。当前真实 ESP32 仍没有持续发包；
  EMG 日志显示合成包后又出现 `UDP 长达 500ms 阻断`，所以若 raw 框
  `age` 继续变大，不是网页没工作，而是板端没有收到新 UDP。
- 2026-05-03 00:36 CST，Lane B Sensor Lab 已按用户要求进一步提高 raw
  ADC 刷新率：本地 `tools/ironbuddy_sensor_lab.py` 中 raw 前端轮询改为
  `RAW_REFRESH_MS = 50`（约 20Hz），远端 `/dev/shm/emg_raw_waveform.json`
  SSH 读取循环从 `sleep 0.05` 改为 `sleep 0.03`；滤波后 / RMS / 激活
  面板仍走较慢的 `/api/status` 路径，两块 canvas 保持独立。当前本机
  Lab 已放入 tmux 会话 `ironbuddy_sensor_lab`，PID `23397`，监听
  `http://127.0.0.1:8766/`。页面源码已读回确认 `filteredChart`、
  `rawChart`、`const RAW_REFRESH_MS = 50`、
  `setInterval(loadFastWave, RAW_REFRESH_MS)` 和 `fast 20Hz` 均存在。
  合成 UDP 后 `/api/emg_fast` 返回 `ok=true`、`samples=420`；之后
  `age_s` 继续增长仍表示真实 ESP32 未持续发包。

## Lane A sha256sum 一致性结果

本结果只做文件哈希比对；未部署、未重启、未 reset 状态、未清理
`/dev/shm`。

| file | local_sha256 | board_sha256 | status |
|---|---|---|---|
| `hardware_engine/main_claw_loop.py` | `70537ac2cd364518ff3aaea8c7a5ad7db8c12a583b8929d6c3a646e97fcb64cc` | `70537ac2cd364518ff3aaea8c7a5ad7db8c12a583b8929d6c3a646e97fcb64cc` | MATCH |
| `hardware_engine/voice_daemon.py` | `a5194cc35a9c37aeefc3743dbc31abc9ce533d0629c4467c5921270db036f757` | `a5194cc35a9c37aeefc3743dbc31abc9ce533d0629c4467c5921270db036f757` | MATCH |
| `hardware_engine/voice/recorder.py` | `4685db96e686e6bd2d962ad300df83d32003fd2075366c21063ebd3da2d43fea` | `4685db96e686e6bd2d962ad300df83d32003fd2075366c21063ebd3da2d43fea` | MATCH |
| `hardware_engine/cognitive/fusion_model.py` | `7e39dbffa1d91d323e527b1db0c4239cd797f2cbeeb17316790c91aeef9e51f1` | `7e39dbffa1d91d323e527b1db0c4239cd797f2cbeeb17316790c91aeef9e51f1` | MATCH |
| `scripts/start_voice_with_env.sh` | `c98de7591a80c842160ba958cfd23d11d6c6e183e1ba7c7920731f089a44c326` | `c98de7591a80c842160ba958cfd23d11d6c6e183e1ba7c7920731f089a44c326` | MATCH |
| `tools/train_gru_three_class.py` | `66e41adeafb9f40658aa9bb12ea94a34213650915be6345a2b3628cc9cb37cb2` | `66e41adeafb9f40658aa9bb12ea94a34213650915be6345a2b3628cc9cb37cb2` | MATCH |
| `tools/simulate_emg_from_bicep.py` | `e51ce43eb7c335b20ffc8f9151ffd1a39a74758a71bbd5e75a3d1362bc823f9a` | `e51ce43eb7c335b20ffc8f9151ffd1a39a74758a71bbd5e75a3d1362bc823f9a` | MATCH |
| `streamer_app.py` | `480cf8c7d841899e3a17300f701e68e91006e42d2da91ad910f5e2b6afa66a74` | `480cf8c7d841899e3a17300f701e68e91006e42d2da91ad910f5e2b6afa66a74` | MATCH |
| `templates/index.html` | `215f251c75c90f0767ff1db3c41a907cdabe6e0216c92d764d4877a091adae73` | `215f251c75c90f0767ff1db3c41a907cdabe6e0216c92d764d4877a091adae73` | MATCH |

- 2026-05-03 14:32 CST，Claude Code 已部署 V7.36 Stage 1-3 并释放锁：
  远端备份在 `.deploy_backups/claude_code_20260503_142604_v736_stages_1to3/`；
  上传文件 `streamer_app.py`、`templates/index.html`、
  `hardware_engine/ai_sensory/cloud_rtmpose_client.py`，板端 sha256
  3/3 MATCH，`py_compile` 通过。仅重启 streamer (PID `31660`) 和
  vision (PID `5397`) — voice/mainloop/emg 保持
  `25798/25601/25723`。烟测通过：`/api/cloud_handshake_status` 上线
  返回 `phase=ready, backend=local, detail="local backend at startup"`；
  切到 cloud 后正确写入 `phase=failed, detail="3 consecutive errors:
  HTTPConnectionPool(host='127.0.0.1', port=6006)..."`（板端配置的
  cloud URL 仍是 127.0.0.1:6006，需要用户配真实云端地址）；切回 local
  恢复 `phase=ready, backend=local`。主网页 HTML 烟检：no
  `demoShowcaseContainer`、有 `codeGraphMount`/`operatorIframe`/
  `feedbackNote`/`logTerminalDetails`、`--bg-primary`/`.btn-primary`/
  `cloud_handshake_status` 均存在。本地 pytest `231 passed`。代码
  commits: `fa6b276` (handshake endpoint) → `6d85048` (shm writer) →
  `41f4fcc` (frontend poll) → `d828934` (tab cleanup) → `cdf5f08`
  (Linear tokens)。下一步：用户在主网页现场点云端 GPU 切换看 toast；
  Stage 4-6 不阻塞录制，可分批继续。
- 2026-05-03 14:50 CST，Claude Code 已部署 V7.36 Stage 4-6 并释放锁：
  远端备份在 `.deploy_backups/claude_code_20260503_144044_v736_stages_4to6/`；
  上传文件 `streamer_app.py`、`templates/index.html`、
  `tools/build_code_graph.py`、`tools/__init__.py`、
  `data/code_graph/graph.json`，板端 sha256 5/5 MATCH，
  `py_compile` 通过。仅重启 streamer (PID `26338`) — vision/voice/
  mainloop/emg 保持 `5397/25798/25601/25723`。烟测通过：
  `/api/code_graph` 返回 `ok=true, nodes=98, edges=26, commit=6febca2`；
  `/api/cloud_handshake_status` 仍返回 `phase=ready, backend=local`；
  主网页 HTML 包含 `force-graph` CDN script、`codeGraphMount`、
  `operatorIframe`、`feedbackNote`、`127.0.0.1:8765`、`submitFeedback`、
  `/api/code_graph` 调用。本机 `pytest` 256 passed。代码 commits:
  `d2726b6` (build_code_graph) → `508192d` (code_graph endpoint +
  force-graph render) → `1c709df` (operator console theme) →
  `1af5116` (submitFeedback wired)。下一步：用户重启本机 operator
  console 看主题对齐；现场点击调试 tab 看代码图能否拉取 + 节点交互；
  现场反馈区粘贴截图 + 备注，确认能写到 operator run 的
  `events.jsonl` + `uploads/`。
- 2026-05-03 17:25 CST，Claude Code 已部署 V7.37 Stage 1+2+4+6 并释放锁：
  远端备份 `.deploy_backups/claude_code_20260503_171142_v737_stages_1246/`；
  上传 `streamer_app.py`、`templates/index.html`、`templates/database.html`、
  `scripts/opencloud_reminder_daemon.py`，板端 sha256 4/4 MATCH，
  `py_compile` 通过。仅重启 streamer (PID `8539`)，
  vision/mainloop/emg/voice 保持 `16141/16240/16314/16363`。烟测通过：
  `/api/openclaw/status` 返回 `weekly_hour=20, morning_hour=9,
  next_push_mode=weekly, configured.feishu_app_id=true`；
  `/api/openclaw/insights` 返回 `ok=true, weekly_training=
  {sessions:52, good:619, failed:231}, llm_triggers=
  [疲劳满值自动:8, voice_chat:5]`；`/api/openclaw/once {weekly,
  send:true}` **真发到飞书** msg_id
  `om_x100b504568d288a0b26944e3090c0ca`，4 区块文本含训练统计 +
  高频提问 + LLM 学习方向 + footer；`/api/db/tables` 各表
  `last_ts` 字段返回今天 09:12 等真实时间。代码 commits: `19cfab8`
  (Stage 1) → `2693a24` (Stage 2) → `8913ae1` (Stage 4) →
  `cfb86b7` (Stage 6)，270+ pytest 全绿。下一步：
  Stage 3 systemd unit (need sudo on board) + Stage 5 RAG citation
  (needs voice_daemon, blocked by lane_a) + Stage 7 GitHub push
  (secret-scanned)。当前 `IRONBUDDY_WEEKLY_HOUR` 默认 20，按用户要求
  应改 17；下次部署 systemd unit 时把 env 设为 17。
- 2026-05-03 18:30 CST，Claude Code 已部署 V7.37 Stage 3+7 并释放锁。
  Stage 7 (GitHub repo push)：`origin/main` 从 `9f1c0a9` (V7.17) 升级到
  `da764aa` (V7.37 Stage 7 secret hardening)，55 commits fast-forward。
  事前用 `git filter-repo` 清理本地 history 中 4 个 100MB+ pptx 与
  20MB png（这些 commit 未 push 过，不算改公开历史）；
  同时把硬编码 SSH 密码从 `deploy_to_cloud.py` 移到 env，untrack 8 个
  `*.bak_pre_v45/db.bak_*` 与 `.agent_memory/raw/`，扩 `.gitignore` 加
  `data/runtime/`、`docs/test_runs/2*/`、`presentation/*.pptx`。
  ⚠ 已 push 的旧 commit `61fa524` (2026-04-11) 仍然包含原密码字面值，
  必须**rotate 远端 SSH 密码**（不强 push history 重写）。
  Stage 3 (systemd unit)：`/etc/systemd/system/ironbuddy-openclaw.service`
  已 enable + active，daemon PID 由 systemd 管理（current 2594）。
  Environment=`TZ=Asia/Shanghai` 让 board 默认 UTC 也能按字面 17:00 推送；
  `WEEKLY_HOUR=17 / DOW=6 (周日) / MORNING_HOUR=9 / EVENING_HOUR=21`。
  daemon loop 启动时写 `data/runtime/opencloud_schedule.json`，streamer
  优先读它（systemd-injected env 不可见 streamer 进程）。烟测通过：
  `/api/openclaw/status` 返回 `weekly_hour=17, morning_hour=9,
  next_push=evening @ 2026-05-03 21:00 Sun`，6 个核心进程在线
  (vision/streamer/mainloop/emg/voice + openclaw daemon)。代码 commits:
  `0c5a00e` (Stage 7 hardening) → `da764aa` (filter-repo 后 SHA) →
  `0269836` (Stage 3 systemd + TZ + schedule.json)。

## 当前阻塞

- 语音链路仍是最高风险项，需要现场实测验证 ASR、唤醒、TTS 和
  voice busy 释放是否稳定。
- RAG/飞书/OpenCloud 模块已上板并通过只读 smoke；下一步需要按
  `20260502-190758` run 的 `rag_feishu_cloud_retest` 步骤做现场复测，
  尤其关注气泡顺序、功能介绍、健身知识问答、飞书真推送和回归项。
- 真实 EMG/Arduino UDP 的当前阻塞已经收敛到"ESP32 没有持续发 UDP 包"：
  板端 EMG 服务可监听 UDP 8080，Sensor Lab 可显示 raw ADC 快速波形，
  合成 UDP 已证明从板端到网页链路可用；raw 面板现在约 20Hz 刷新，
  但真实 ESP32 包不持续时 `/api/emg_fast.age_s` 会持续变大、heartbeat
  会消失。下一步先在
  Arduino Serial Monitor `9600` 看 ESP32 是否持续打印 WiFi/ADC/UDP send；
  若没有持续输出，优先处理 ESP32 供电、热点、Wi-Fi 重连和接线。接回外设
  时仍必须避免影响 `GPIO0`、`GPIO2`、`GPIO12`、`GPIO15`、`EN/RST` 等
  启动相关脚。
- 本地核心板端文件与板端运行目录一致性已验证，9/9 MATCH；这项不再是
  当前阻塞。

## 已归档（2026-05-02）

- `docs/plans/2026-04-29-guided-live-test-handoff.md` 与
  `docs/plans/2026-04-29-report-refactor-handoff.md` 已移至
  `.archive/docs_handoff_2026-04-29/`。前者被 operator console 取代，
  后者等拍摄主线收尾再起。原因和恢复指引见该目录下的 `README.md`。

## 下一步

1. 先让 Claude Code 读 `CLAUDE_SYNC.md` 做一次协作/审查反馈。
2. 开 Lane A 新窗口继续语音和主拍摄流程，只用 operator console 记录结果。
3. Lane B 现场验收从 `http://127.0.0.1:8766/` 开始：先确认页面同时有
   `滤波后 / RMS / 激活` 和 `原始 ADC 快速波形` 两个框；原始框右上角
   `fast ... age ...s` 应小于 3 秒。然后选择
   `标准` / `代偿` / `不标准`，点击"开始本组"，静息 5 秒 baseline，
   必要时点击 MVC，然后每类做 3-5 个弯举。开始动作前必须先确认页面
   信号诊断里的 `debug_age` 小于 3 秒，并且 ADC/RMS 与原始 ADC 都在持续刷新；
   如果显示 `raw snapshot 已过期`，先回 Arduino Serial Monitor 查
   ESP32 持续发包。每组结束后看 correct/total、confusion、可疑 rep
   和归因建议。若网页不通，重新启动：
   `IRONBUDDY_BOARD_IP=10.244.190.224 python3 tools/ironbuddy_sensor_lab.py`。
4. 每个阶段交付后，让 Claude Code 做客观审查。

## 快速命令

从仓库根目录启动 Lane A：

```bash
python3 tools/ironbuddy_operator_console.py
```

从仓库根目录启动 Lane B：

```bash
python3 tools/ironbuddy_sensor_lab.py
```

如需显式指定当前板端 IP：

```bash
IRONBUDDY_BOARD_IP=10.244.190.224 python3 tools/ironbuddy_operator_console.py
IRONBUDDY_BOARD_IP=10.244.190.224 python3 tools/ironbuddy_sensor_lab.py
```

连通性验证：

```bash
curl --noproxy '*' -m 5 -sS http://10.244.190.224:5000/api/fsm_state
ssh -i ~/.ssh/id_rsa_toybrick -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=5 toybrick@10.244.190.224 'pgrep -af "[s]treamer_app|[m]ain_claw_loop|[u]dp_emg_server|[v]oice_daemon|[c]loud_rtmpose_client"'
```

## 必读顺序

新窗口按这个顺序接手：

1. `CLAUDE.md`
2. `docs/test_runs/ironbuddy_operator/OPERATOR_FINAL_GUIDE.md`
3. `docs/test_runs/ironbuddy_operator/CURRENT.md`
4. `docs/test_runs/ironbuddy_operator/AGENT_LANES.md`
5. `docs/test_runs/ironbuddy_operator/WINDOW_MIGRATION.md`
6. `docs/test_runs/ironbuddy_operator/CLAUDE_SYNC.md`
7. `docs/technical/ironbuddy_operator_console.md`
8. `docs/technical/ironbuddy_sensor_lab.md`
