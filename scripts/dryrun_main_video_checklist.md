# 主视频拍摄 dry-run 清单

> 手测，不是自动化脚本。每条按顺序勾选，确认 PASS 才进下一条。如果 FAIL，记录症状并停拍排查，不要硬撑。

## 0. 环境就绪（拍摄前 30 分钟）

- [ ] 板端电源接好，HDMI 连接屏幕，摄像头开机
- [ ] WSL 已 ssh 到板端 / 板端 X server 已起 (`xhost +local:`)
- [ ] amixer 音频回拨已执行 (`amixer cset numid=1 6` 等)
- [ ] DeepSeek API key 在 `.api_config.json` 里有效
- [ ] 网络通 (`curl -m 3 https://api.deepseek.com/v1`)

## 1. 服务启动（拍摄前 10 分钟）

- [ ] `bash start_validation.sh` 一键启动 5 进程
- [ ] 30 秒后检查：`pgrep -f '[m]ain_claw_loop'` / `[s]treamer_app` / `[v]oice_daemon` / `[c]loud_rtm` / `[u]dp_emg_server` 都返回 PID
- [ ] 浏览器打开 `http://<board-ip>:5000`，UI 加载完整
- [ ] HDMI 屏看到摄像头实时画面 + 骨架点叠加

## 2. 状态归零

- [ ] 跑 `bash scripts/reset_demo_state.sh`
- [ ] UI 显示 good=0, failed=0, comp=0, fatigue=0/1500
- [ ] HDMI 上没有红色违规闪烁残留

## 3. 主视频剧本演练（T0 → T8）

按 `docs/plans/2026-04-26-ironbuddy-refactor-design.md` §6 测试清单：

### T0：开场静态镜头
- [ ] HDMI 显示空界面（无人在画面）
- [ ] UI 计数 0

### T1：第一段对话（深蹲建议询问）
- [ ] 喊"教练" → ack 播报 "嗯"
- [ ] 说 "现在适合做深蹲吗"
- [ ] 期望：DeepSeek 回复（短文本，不胡话）
- [ ] UI 出现 1 对气泡（我 + 教练），不重复
- [ ] 自动回 LISTEN 状态

### T2：切到深蹲 + 做几下
- [ ] 喊"教练 切到深蹲"
- [ ] ack "好，切到深蹲"
- [ ] HDMI 显示 exercise=squat
- [ ] 用户做 3 个标准深蹲
- [ ] UI 计数 good +3

### T6：触发自动总结
- [ ] 用户做到疲劳值 >= 1500（约 8-12 个深蹲）
- [ ] 期望：voice 自动播报 LLM 总结
- [ ] **关键**：播报期间环境噪音不应触发录音
- [ ] /dev/shm/auto_trigger.json 短暂出现（被 watcher 消费）

### T7：切到弯举 + MVC
- [ ] 喊"教练 切到弯举"
- [ ] ack "好，切到弯举" + "准备好后请说 开始 MVC 测试"
- [ ] 用户说 "开始"
- [ ] 期望：进入 MVC 流程 → 倒数 → 录峰值 → "测试结束"

### T8：闭幕
- [ ] 用户做 1 个弯举 → UI 计数变化
- [ ] 镜头淡出

## 4. 失败重拍准则

- 如果 T1 DeepSeek 回复明显胡话 / 含"作为AI模型"等字眼 → 检查 system_prompt 是否切到 neutral 版（id 应为最新 active=1 行）
- 如果 T2 喊话不响应 → 检查 voice_daemon log 看是否进 BUSY 卡住
- 如果 T6 触发不到自动播报 → 检查 _fatigue_limit / _ds_lock / connected 三个变量
- 如果 T7 切弯举后没听到 MVC 引导 → 检查 _realize_action 路径是否启用（早上 wire-up 后才生效）

## 5. 抢救机制（实拍中）

- 任何时候 UI 卡死：F5 刷新 → 90% 能恢复
- 任何时候 voice 卡 BUSY：板端跑 `bash scripts/reset_demo_state.sh`
- 任何时候 FSM 计数错乱：发送 `/api/reset` POST → 重置为 0
- 任何时候 整体崩：重启 5 进程 (`pkill -f voice_daemon|main_claw_loop|streamer_app` + `bash start_validation.sh`)

## 6. 拍摄完成 checklist

- [ ] 所有镜头落地（主视频 + subvideo squat + subvideo curl）
- [ ] OBS 录像文件保存
- [ ] 备份到云盘
- [ ] git commit + push 当前代码（含拍摄当天的 .pt 模型 + DB）
