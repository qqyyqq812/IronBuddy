# Agent Local Memory: IronBuddy (Embedded-Fullstack)

> Last updated: 2026-04-15 (Sprint 4: 全面修复—停止逻辑/UI重设计/HDMI互斥/飞书规划/语音V4/音箱修复)

## Quick Reference
- 读取 `_entity_graph.md` 获取完整代码拓扑和架构图
- APP入口: 板端 http://10.105.245.224:5000/ (一体化前端，非独立admin页)

## Architecture (2026-04-14 最终)

### 视觉推理 (双模式，异步非阻塞)
- **Local模式** (默认): YOLOv5-Pose RKNN NPU `pose-5s6-640-uint8.rknn` @ /home/toybrick/deploy_rknn_yolo/
  - 推理~107ms/帧，后台线程异步执行，不阻塞视频流
  - 置信度阈值必须 ≤0.08 (量化模型obj_conf最高约0.2，不能用默认0.35)
  - 切换: 写 `/dev/shm/vision_mode.json {"mode":"local"}` 或调 `/api/switch_vision`
- **Cloud模式**: RTMPose-m ONNX on RTX 5090, SSH隧道连接
- 关键Bug已修: `local_yolo_pose.py` keypoint xy不用sigmoid(只conf用), 默认conf=0.08

### 视频输出 (双通道，2026-04-15)
- **HDMI直连** (方案A, 推荐): `cv2.imshow("IronBuddy", frame)` 全屏, DISPLAY=:0, ENABLE_HDMI=1
  - 零延迟: 直接写framebuffer, 无编码/网络/解码开销, 30fps+
  - 需要: X11启动 (`startx -- -nocursor`), xhost授权, Xauthority复制
- **MJPEG独立端口** (方案B): vision进程内嵌HTTP server on :8080, 绕过Flask
  - `_ThreadedHTTPServer` + `_MJPEGHandler`, /stream (MJPEG流) + /snapshot (单帧)
  - 前端自动: 先试8080直连, 失败回退Flask /video_feed
- **Flask /video_feed** (已降级为后备): gen_frames()有10秒超时保护
- **为什么HDMI能减少卡顿** (汇报关键点):
  1. 网页视频链路: cv2.imencode→文件I/O→Flask读取→HTTP响应→TCP/IP→浏览器解码→DOM渲染 (6步)
  2. HDMI链路: cv2.imshow→X11 framebuffer→DRM/KMS→HDMI信号 (2步, 全硬件加速)
  3. 网页占用: Flask GIL竞争(12个API轮询 vs 视频流争抢Python线程)
  4. HDMI零额外CPU: 显示控制器DMA直读显存, 不消耗CPU周期
  5. 接入HDMI后: 网页可关闭视频只保留控制面板, Flask CPU占用从30%降到<5%

### LLM 后端
- **DeepSeek Direct** (推荐): `cognitive/deepseek_direct.py` — requests直连, SSE流式, deque(6)历史
  - API Key存在板端 `.api_config.json`, 通过APP设置Tab配置
  - 启动时自动注入: FSM和Voice进程启动时读取.api_config.json注入环境变量
- **OpenClaw**: WebSocket网关 Board→WSL:18789 (需SSH隧道, 已废弃)
- 切换: `LLM_BACKEND=direct` 环境变量 / APP一键启动自动读配置
- **后续计划**: 迁移到OpenAI SDK (base_url="https://api.deepseek.com"), 与参考项目统一

### 语音方案 (2026-04-15 决策)
- **采用**: 百度 AipSpeech (TTS+STT云端, 参考 docs/hardware_ref/main2.py 已验证)
  - TTS: `client.synthesis()` → WAV → `aplay -Dplughw:0,0`
  - STT: `arecord` + 自适应VAD录音 → `client.asr()` 云端识别
  - 唤醒词: 录音→STT→检查"教练"关键词 (不需要本地热词引擎)
  - 需要: 百度 APP_ID/API_KEY/SECRET_KEY (在APP设置Tab配置)
- **抛弃 Vosk**: glibc ABI不兼容, libvosk.so缺少`_ZNSt7__cxx1119basic_ostringstreamIcSt11char_traitsIcESaIcEEC1Ev`符号, Debian 10 ARM64无法修复
- **抛弃 edge-tts**: 依赖微软云端, 板端网络不稳定时完全不可用, 延迟200-500ms
- **参考项目关键模式**: ALSA静音(ctypes), 动态噪声基线VAD, aplay可中断播放

### 一体化APP (templates/index.html, ~2500行, PWA化)
- **PWA meta**: standalone模式, 无Google Fonts依赖, 系统字体栈
- **左侧**: 视频流(MJPEG+watchdog重连) + FSM监控 + 训练统计 + AI教练对话 + **EMG波形图(Canvas)**
- **Header模式切换**: 运动(深蹲/弯举) + 视觉(云端/本地) + **推理(纯视觉/视觉+传感)**
- **右侧可折叠侧边栏**, 4个Tab:
  - 控制台: 5服务状态(pgrep+zombie过滤) + 一键启动/停止(两步kill) + 系统信息
  - 终端日志: 实时轮询5个/tmp/*.log文件, 绿字黑底
  - 数据管理: 训练CSV列表 + 训练历史
  - 设置: 用户参数 + 视觉模式 + **DeepSeek API Key配置** + LLM后端选择
- **底部状态栏**: 总次数/合格率/训练时长/视觉模式/板子状态
- **APP化**: overscroll-behavior:none, touch-action:manipulation, 防下拉刷新, 44px触控区

### 服务启停 (APP本地直接启动，无SSH)
- streamer_app.py 运行在板端，直接用pgrep/pkill/nohup本地管理进程
- 5服务: vision/streamer/fsm/emg/voice
- `_SERVICE_LAUNCHERS` dict定义各服务启动命令+日志路径
- FSM/Voice启动时自动从.api_config.json注入DEEPSEEK_API_KEY
- **停止**: 两步kill (SIGTERM → 0.8s等待 → SIGKILL强杀残留), bracket trick避免pgrep self-match
- **状态检测**: pgrep + ps state过滤zombie进程

### 推理模式 (新增)
- **纯视觉** (pure_vision): 只用if-else角度判断，不跑GRU，不显示EMG波形
- **视觉+传感** (vision_sensor): 运行GRU推理 + EMG波形显示
- 信号文件: `/dev/shm/inference_mode.json` {"mode":"pure_vision"/"vision_sensor"}
- API: `GET/POST /api/inference_mode`, `POST /api/switch_inference_mode`
- FSM在main_claw_loop.py中每帧读取信号文件决定是否跑GRU

### API Config持久化
- 文件: `PROJECT_ROOT/.api_config.json` (板端本地)
- 端点: `GET/POST /api/admin/api_config`
- GET返回masked key (前6+后4字符)

## Sprint 4 修复记录 (2026-04-15)
### 已修复
- [x] 停止服务无效: nohup改为temp script启动, pgrep全部加bracket trick, kill -9进程树
- [x] HDMI黑屏: 板子重启后X11需重启 (`startx -- -nocursor` + xhost + Xauthority)
- [x] 网页HDMI互斥: CSS .hdmi-placeholder.active + feed.src='' 完全停止流
- [x] 音箱无声: Playback Path重启后回OFF, 需每次设SPK_HP (numid=1 val=6)
- [x] DeepSeek点评: FSM启动时需注入DEEPSEEK_API_KEY环境变量
- [x] 语音V4: 百度AipSpeech TTS+STT, 麦克风自测(hw:2,0→3,0→0,0 fallback)
- [x] UI精简: header去掉切换按钮只留状态标签, 删configModal, 设置统一到sidebar
- [x] 飞书: 不再实时推, 改为 /api/feishu/send_plan 手动/语音触发
- [x] HDMI全屏直输出 (cv2.imshow fullscreen, X11, 零延迟)
- [x] MJPEG独立端口:8080 (vision内嵌, 不走Flask)
- [x] EMG波形UI, 纯视觉/传感模式切换, APP化 (PWA)

### 待完成 (Sprint 5 — 下一个窗口)
- [ ] **语音唤醒不工作**: voice_daemon 启动了但喊"教练"没反应，需debug录音→STT→匹配全链路
- [ ] **语音调控**: 参考 docs/hardware_ref/main2.py 实现: 1.语音静音 2.语音修改疲劳上限 3.语音切换训练模式
- [ ] **渲染效果恢复**: 蹲到底/弯举到顶闪烁, 疲劳颜色渐变, 骨架联动glow (GitHub HEAD版有，当前被删)
  - `git show HEAD:templates/index.html` 中有 .rig-glow, pulse, fatigue颜色逻辑
- [ ] **违规检测不准**: 几乎所有动作都判为标准。已加关键点置信度过滤(MIN_KPT_CONF=0.05)，但量化模型精度本身差
- [ ] **音箱不响**: voice_daemon已改Playback Path=6(SPK_HP), 但测试中仍无声, 需板端实测aplay硬件
- [ ] **网页无视频**: 拔掉HDMI后网页也没视频, MJPEG 8080端口可能没自动回退
- [ ] **删除文字聊天框**: 用户只要语音交互，不要打字。飞书推送也改为语音触发
- [ ] 数据持久化: SQLite训练记录表
- [ ] 开机自启动: systemd服务

### 关键经验 (debug)
- nohup不能包裹 `cd dir && cmd` (nohup只能包裹单个可执行文件), 解决: 写temp script
- pgrep -f 在 shell=True 下会匹配 bash -c 的 cmdline, 必须用 bracket trick: `[c]loud_rtm...`
- 板子重启后: Playback Path 回 OFF, X11 不启动, 所有服务需重启
- voice_daemon V3(旧) vs V4(新): 注意部署后检查版本号, 旧版可能残留运行

## Training Data (2026-04-12 实录)
- `data/bicep_curl/golden/` 80KB, `lazy/` 86KB, `bad/` 84KB
- `data/squat/golden/` 14KB+83KB

## Critical Config
- Board SSH: `toybrick@10.105.245.224` key: `~/.ssh/id_rsa_toybrick`
- Cloud SSH: `root@connect.westd.seetacloud.com:14191` key: `~/.ssh/id_cloud_autodl`
- APP URL: http://10.105.245.224:5000/ (板端直接访问)
- 启动流程 (APP内一键): 从APP控制台Tab点"一键启动"，自动读.api_config.json
- Legacy: `bash start_validation.sh` (WSL执行，已废弃)
- GitHub: `git@github.com:qqyyqq812/IronBuddy.git`

## Dev Hints
- Board Python 3.7: 不支持 `X | None` 语法, 无 pandas, capture_output OK
- 板端hostname: debian10.toybrick (用于本地检测判断)
- 视觉客户端: 进程起来后检查/dev/shm/pose_data.json frame_idx是否在增长
- NPU: rknnlite OK, 但量化模型精度差; 坐标xy不用sigmoid, conf才sigmoid
- 启动调试: `tail -f /tmp/vision_local.log /tmp/fsm_loop.log /tmp/streamer.log`
- HDMI启动: `startx -- -nocursor`, `xhost +local:`, 复制root的.Xauthority到toybrick
- 音频硬件: `aplay -Dplughw:0,0`, `arecord -Dhw:0,0 -r44100 -f S16_LE -c 2`
- person_score阈值: FSM中 < 0.05 (量化模型person score最高约0.1-0.15)

## 飞书集成 (已配置, 已验证通过)
- **方式**: 自建应用API (非webhook), 使用 tenant_access_token
- **凭证** (存于板端 .api_config.json):
  - app_id: cli_a934a567cab85bd9
  - app_secret: RWNhCMzy38RIvjDohHPlPWKoFr1uoBDS
  - chat_id: oc_a4a35f2c4d59d81428191aea7fb6787e
- **发送代码**: `deepseek_direct.py` deliver() 方法, `streamer_app.py` /api/feishu/send_plan
- **触发**: 手动API调用或语音"帮我推送健身规划" (待实现)
- **已验证**: 板端→飞书直推成功, message_id 有返回

## 百度语音 (已配置, TTS验证通过, STT待验证)
- **凭证** (存于板端 .api_config.json):
  - APP_ID: 7636724
  - API_KEY: JNv5k2FTRleHVuMANkCT2xK3
  - SECRET_KEY: GvcyfTtRVDg3tnFwvSiGT0uqgU2cCNGm
- **TTS**: client.synthesis()验证通过(67KB WAV), aplay播放exit=0
- **STT**: 待板端实测
- **麦克风自测**: hw:2,0 通过(176KB/秒)

## 项目结构说明
- `coursework/嵌入式系统` → 软链接到 `projects/embedded-fullstack` (同一个项目)
- GitHub: `git@github.com:qqyyqq812/IronBuddy.git`, 本地有大量未提交改动(~7400行)
- 参考项目: `docs/hardware_ref/main2.py` (别人的车载智能副驾, 百度AipSpeech+DeepSeek, 已验证)
