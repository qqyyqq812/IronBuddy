# IronBuddy Sprint 5 交接提示词

> 生成日期: 2026-04-15
> 前序会话: Sprint 1-4 (共5个窗口, ~170条用户消息)

---

## 下一个窗口的提示词 (直接复制粘贴)

```
项目: projects/embedded-fullstack (IronBuddy AI健身教练系统)
请先读取 projects/embedded-fullstack/.agent_memory/index.md 了解完整状态。

## 当前系统状态 (2026-04-15 Sprint 4 结束)

板子 toybrick@10.105.245.224 已联通，APP运行在板端 http://10.105.245.224:5000/
GitHub: git@github.com:qqyyqq812/IronBuddy.git (本地有~7400行未提交改动)

### 已完成的功能
- 一体化APP (templates/index.html, PWA化, Settings Tab统一管理)
- 本地YOLOv5-Pose NPU推理 + HDMI全屏直输出 + MJPEG独立端口8080
- DeepSeek直连 (API Key已配, 启动时自动注入环境变量)
- 百度AipSpeech语音V4 (TTS验证通过, 麦克风hw:2,0自测通过)
- 飞书推送 (自建应用API, 板端→飞书直推已验证)
- 一键启停 (temp script方式, export环境变量, bracket trick pgrep)
- HDMI/网页互斥 (有HDMI时网页隐藏视频流)
- 关键点置信度过滤 (MIN_KPT_CONF=0.05, 防止噪声角度)

### Sprint 5 待解决 (优先级顺序)

1. **语音唤醒不工作** [高] — voice_daemon V4启动了,百度AipSpeech就绪,麦克风通过,但喊"教练"完全没反应。需要debug: arecord录音→VAD检测→百度ASR识别→关键词匹配 全链路。
   - 检查: tail -f /tmp/voice_daemon.log 看是否有录音/识别输出
   - 可能原因: arecord进程没启动, VAD阈值太高, 或百度ASR返回空

2. **语音调控功能** [高] — 参考 docs/hardware_ref/main2.py (别人的项目):
   - 语音静音/解除静音 ("安静"/"解除静音")
   - 语音修改疲劳值上限 ("疲劳目标改为2000")
   - 语音切换训练模式 ("切换到弯举")
   - 语音触发飞书推送 ("帮我推送健身规划")
   
3. **渲染效果恢复** [中] — 当前UI删掉了骨架联动动画和闪烁效果。需从Git历史恢复:
   - `git show HEAD:templates/index.html` 有: .rig-glow, pulse, fatigue颜色渐变
   - 蹲到底/弯举到顶时闪烁绿光
   - 疲劳值增加时背景从绿→橙→红渐变
   
4. **违规检测不准** [中] — 几乎所有动作都判为标准。已加关键点置信度过滤,但量化模型精度差。
   - 测试时角度值:1°,15°,87° 等明显不合理的也被认为"好球"
   - 需要: 更严格的角度合理范围检查 (如<10°或>170°直接过滤)

5. **音箱不响** [中] — Playback Path已设SPK_HP(6),TTS合成成功,但实际不出声。需板端实测:
   - `sudo aplay -Dplughw:0,0 /tmp/test_tts.wav` 手动测试
   - 如果手动也不响→硬件连接问题; 如果手动响→voice_daemon的播放逻辑有bug

6. **删除文字聊天框** [低] — 用户只要语音交互,不要打字框。飞书按钮也删,改语音触发。

7. **网页无视频** [低] — 拔HDMI后网页也没视频。MJPEG 8080端口在线但前端reconnect可能有bug。

### 关键文件
- streamer_app.py: Flask后端,所有API,服务启停(temp script+export)
- templates/index.html: 前端(~2500行,PWA,Settings Tab统一)
- hardware_engine/voice_daemon.py: 语音V4(百度AipSpeech)
- hardware_engine/main_claw_loop.py: FSM状态机+GRU推理+DeepSeek调用
- hardware_engine/ai_sensory/cloud_rtmpose_client.py: 视觉+HDMI+MJPEG
- hardware_engine/cognitive/deepseek_direct.py: DeepSeek直连+飞书推送
- docs/hardware_ref/main2.py: 参考项目(别人的车载副驾,语音调控方案)
- .agent_memory/index.md: 完整项目状态和架构记录

### 用户关键要求
- 最终目标是做展示汇报PPT
- 所有交互只通过语音(不要打字框)
- 飞书推送通过语音触发
- 需要恢复之前的渲染动画效果
- coursework/嵌入式系统 是 projects/embedded-fullstack 的软链接(同一目录)
```

---

## 测试结果摘要 (Sprint 4)

| 测试项 | 结果 | 备注 |
|--------|------|------|
| A1 一键启动 | 通过 | EMG纯视觉下仍显示在线(小问题) |
| A2 一键停止 | 通过 | 停止后左侧数据未清空 |
| A3 停止→再启动 | 通过 | |
| B1 HDMI大屏 | 通过 | |
| B2 网页HDMI互斥 | 通过 | 用户不需要提示文字 |
| B3 无HDMI时网页视频 | 失败 | 拔HDMI后网页也无视频 |
| C1 深蹲检测 | 失败 | 1500疲劳后DeepSeek掉线 |
| C2 违规检测 | 失败 | 几乎全标准,音箱不响 |
| C3 重置统计 | 通过 | |
| D1 生成点评 | 失败 | 无响应 |
| D2 文字对话 | 删除 | 用户要求只用语音 |
| E1 语音唤醒 | 失败 | 进程活着但无反应 |
| F1 飞书推送 | 改需求 | 改为语音触发 |
