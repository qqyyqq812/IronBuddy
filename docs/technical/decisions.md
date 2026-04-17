# IronBuddy 架构决策记录

> 基于源码实读，2026-04-16 重写。每条决策标注文件路径和行号。

**系统定位**：RK3399ProX 板端 AI 健身教练，5 个独立 Python 进程通过 `/dev/shm` JSON 文件协同。

---

## §I. 双视觉模式（Cloud RTMPose ↔ 本地 YOLOv5-Pose NPU）

**决策**：视觉推理支持两种模式热切换，运行时写信号文件即可切换，无需重启。

### Local 模式（当前默认）

- 引擎：`hardware_engine/ai_sensory/local_yolo_pose.py` — `LocalYoloPose` 类
- 模型：YOLOv5-Pose RKNN uint8 量化 (`pose-5s6-640-uint8.rknn`)，640x640 输入
- NPU 加载：通过 `rknnlite.api.RKNNLite` 加载（`local_yolo_pose.py:200-210`）
- 输出解码：4 头（P3/P4/P5/P6，stride 8/16/32/64）或 3 头（P3/P4/P5），运行时探测输出数量自动选择锚框（`local_yolo_pose.py:216-225`）
- 置信度：`LOCAL_POSE_CONF = 0.08`（`cloud_rtmpose_client.py:171`）——量化模型 obj_conf 最高约 0.2，不能用云端默认的 0.35
- 关键修复：keypoint xy 坐标不过 sigmoid（只有 conf 才 sigmoid），见 `_decode_head()` L128-133
- 异步：后台线程 `_local_worker()` 执行推理，主循环不阻塞（`cloud_rtmpose_client.py:553-567`）

### Cloud 模式

- 引擎：RTMPose-m ONNX on RTX 5090（AutoDL 云端）
- 客户端：`cloud_rtmpose_client.py` 通过 HTTP POST JPEG 到 `CLOUD_INFER_URL`
- 默认地址：`http://127.0.0.1:6006/infer`（需 SSH 隧道）（L148-152）
- 异步：后台线程 `_cloud_worker()` 发送请求（L526-551），主循环读最新结果
- NPU 兜底：Cloud 超时时自动降级到板端 NPU（`_NPUFallback` 类，L334-378）

### 热切换协议

```python
# cloud_rtmpose_client.py:384-397
# 每 30 帧检查信号文件 /dev/shm/vision_mode.json
def _read_vision_mode(default):
    with open(SHM_VISION_MODE, "r") as f:
        data = json.load(f)
    mode = data.get("mode", default).lower()  # "local" 或 "cloud"
```

前端通过 `POST /api/switch_vision` 写信号文件（`streamer_app.py:265-283`）。

---

## §II. 三路视频输出（HDMI / MJPEG / Flask）

**决策**：三种输出通道并存，覆盖零延迟本地显示到远程浏览器。

### 1. HDMI 直连（零延迟）

```python
# cloud_rtmpose_client.py:252-258
if ENABLE_HDMI and _hdmi_ok[0]:
    cv2.imshow("IronBuddy", drawn)
    cv2.waitKey(1)
```

- 前置条件：`ENABLE_HDMI=1`，`DISPLAY=:0`，X11 已启动
- 启动时先做 X11 连通性检测（`xdpyinfo` 调用，L469-471），失败则静默禁用
- 全屏窗口：`cv2.setWindowProperty("IronBuddy", cv2.WND_PROP_FULLSCREEN, ...)` (L477-478)

### 2. 内嵌 MJPEG 服务器（:8080，零拷贝）

```python
# cloud_rtmpose_client.py:56-138
# vision 进程内嵌的 HTTP 服务器，绕过 Flask GIL
class _MJPEGHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # /stream — MJPEG multipart 流 (~20fps)
        # /snapshot — 单帧 JPEG
```

- 线程化：`_ThreadedHTTPServer` 每请求一线程（L110-128）
- 零拷贝：JPEG bytes 通过 `_mjpeg_frame` 共享内存列表传递（L239-240）
- 端口：`MJPEG_PORT = 8080`（L57）

### 3. Flask /video_feed（后备）

```python
# streamer_app.py:84-112
# MJPEG multipart 流，从 /dev/shm/result.jpg 读文件
def gen_frames():
    # 10 秒无新帧则终止流，让浏览器重连
    if time.time() - last_yield_time > 10.0:
        return
    time.sleep(0.1)  # ~10fps cap
```

- 帧去重：`/snapshot` 端点按 `st_mtime_ns` 缓存（L56-78）
- 性能低：受 Flask GIL + 文件 I/O 限制，仅作最终兜底

---

## §III. APP 架构（Flask + HTML5 + Canvas PWA）

**决策**：单页 HTML5 应用，Flask 做 API 层，纯前端渲染，无 JS 框架依赖。

### Flask 后端

- 入口：`streamer_app.py`，`Flask(__name__)`，`0.0.0.0:5000`（L1033-1036）
- 模板加载：绕过 Jinja2 缓存，直接 `open().read()` 返回 HTML（L23-36）
- PWA：`/manifest.json` 返回 standalone 配置（L39-52）
- API 数量：约 25 个路由端点（视频流、FSM 状态、LLM 对话、服务管理、配置等）

### 前端能力（`templates/index.html`，~2500 行）

| 区域 | 功能 |
|------|------|
| Header | 运动模式切换（深蹲/弯举）、视觉模式（云端/本地）、推理模式（纯视觉/视觉+传感） |
| 左侧主区 | MJPEG 视频流 + HUD 叠层（角度/状态/计数）、统计卡片、圆形仪表盘、疲劳条、AI 教练对话、EMG 波形（Canvas 实时绘制） |
| 右侧侧边栏 | 4 Tab：控制台（5 服务管理）、终端日志、数据管理（CSV 列表）、设置（API Key 等） |
| 底部状态栏 | 总次数/合格率/训练时长/视觉模式/板子温度 |
| 运动学骨架 | CSS 骨架人偶（.rig-body），角度驱动关节旋转 |

- PWA 化：`overscroll-behavior: none`, `touch-action: manipulation`, 44px 最小触控区
- 无 Google Fonts：系统字体栈 `system-ui, -apple-system, ...`
- HDMI 互斥：当 HDMI 激活时显示占位符，停止 MJPEG 流（`.hdmi-placeholder.active`）

---

## §IV. 服务管理（5 服务 + pgrep/pkill + SIGTERM/SIGKILL）

**决策**：5 个独立 Python 进程，APP 直接在板端本地管理（非 SSH）。

### 5 服务清单

```python
# streamer_app.py:553-559
SERVICE_SIGNATURES = {
    "vision":   "cloud_rtmpose_client.py",
    "streamer": "streamer_app.py",
    "fsm":      "main_claw_loop.py",
    "emg":      "udp_emg_server.py",
    "voice":    "voice_daemon.py",
}
```

### 启动流程（`/api/admin/start`，L669-769）

1. 检查是否已运行（pgrep + bracket trick 防自匹配：`'[c]loud_rtm...'`）
2. 写临时 shell 脚本（`/tmp/_launch_{name}.sh`），不能直接 `nohup cd && cmd`
3. FSM/Voice 启动时自动从 `.api_config.json` 注入环境变量（DeepSeek Key、百度语音凭证等）
4. `nohup /tmp/_launch_{name}.sh > /tmp/{name}.log 2>&1 &`
5. 等待 1.5s 后 pgrep 验证进程存活

### 停止流程（`/api/admin/stop`，L772-825）

```python
# 先收集所有匹配 PID
all_pids = [pgrep -f 匹配结果]
# 一次性 SIGKILL（SIGTERM 被发现不可靠）
_run_cmd("kill -9 {} 2>/dev/null".format(pid_list))
# 安全网：按签名 pkill 补杀
_run_cmd("pkill -9 -f '{}' 2>/dev/null".format(sig))
# 清理 voice 残留子进程
_run_cmd("killall -9 arecord aplay 2>/dev/null")
```

注意：当前实现直接 `kill -9`，注释写"no mercy -- TERM was unreliable"（L795）。

### 音频前置

```python
# 每次启动 FSM/Voice 时自动重置音箱通道
sf.write("sudo amixer -c 0 cset numid=1,iface=MIXER,name='Playback Path' 6 >/dev/null 2>&1\n")
```

---

## §V. LLM 集成（DeepSeek REST API + SSE）

**决策**：直连 DeepSeek API（绕过 OpenClaw WebSocket Gateway），SSE 流式获取响应。

### 实现

```python
# hardware_engine/cognitive/deepseek_direct.py:175-217
def _sync_chat(self, messages):
    payload = {"model": "deepseek-chat", "stream": True, "temperature": 0.7}
    resp = requests.post(url, headers=headers, json=payload, stream=True, timeout=55)
    for line in resp.iter_lines(decode_unicode=True):
        if line.startswith("data: "):
            # SSE 解析
```

- 异步适配：`ask()` 是 async 方法，通过 `loop.run_in_executor()` 包装同步 HTTP 请求（L77-83）
- 对话历史：`deque(maxlen=6)` 保留最近 3 轮对话（L34）
- `<think>` 块剥离：`result.split("</think>")[-1].strip()`（L214-215）
- OpenClaw 兼容：保持 `ask()` / `connect()` / `health_check()` 接口签名不变

### 触发时机（`main_claw_loop.py:750-791`）

| 触发条件 | 说明 |
|---------|------|
| 手动 | `/dev/shm/trigger_deepseek` 信号文件（前端按钮） |
| 自动 | `fsm.total_fatigue_volume >= 1500`（疲劳满值） |
| 冷却 | 自动触发间隔 > 30s（L754） |
| 锁定 | `_ds_lock[0]` 防并发（L543, L767） |

### 飞书推送

- `streamer_app.py:367-478`：DeepSeek 生成计划 → 飞书自建应用 API 推送
- 非 Webhook：使用 `tenant_access_token`（L440-451）
- 触发：`/api/feishu/send_plan` 手动调用或语音"推送飞书"

---

## §VI. 语音系统（Baidu AipSpeech + ALSA）

**决策**：百度 AipSpeech 云端 TTS+STT，板端 ALSA 录放音。

### 架构

```
录音 (arecord hw:2,0) → 自适应 VAD → 16kHz WAV → 百度 ASR → 文字
文字 → 百度 TTS (合成 WAV) → aplay plughw:0,0 → 音箱
```

### ALSA 错误静音（`voice_daemon.py:33-43`）

```python
# 通过 ctypes 直接操作 libasound.so 屏蔽 ALSA 垃圾日志
_asound = ctypes.cdll.LoadLibrary('libasound.so.2')
_asound.snd_lib_error_set_handler(_c_error_handler)
```

### VAD 录音（`voice_daemon.py:146-240`）

- 动态噪声基线校准：开头 8 帧计算 baseline RMS（L166-176）
- 阈值：`max(400, baseline + 250)`
- 停顿检测：RMS < threshold 持续 1.2s 即结束（SILENCE_LIMIT）
- 降采样：44100Hz 双声道 → `audioop.ratecv()` → 16000Hz 单声道（L223-224）
- 麦克风自测：开机遍历 `hw:2,0 → hw:3,0 → hw:0,0` 找可用设备（L305-323）

### 唤醒词

```python
# voice_daemon.py:58
WAKE_WORDS = ["教练", "教", "叫练", "交练", "焦练", "铁哥", "coach"]
```

非本地热词引擎，而是录音 → STT → 关键词匹配（L404-407）。

### 语音命令（`_try_voice_command()`，L469-545）

| 命令 | 关键词 | 信号文件 |
|------|--------|---------|
| 静音 | "安静/闭嘴/静音" | `/dev/shm/mute_signal.json` |
| 切换深蹲 | "深蹲模式" | `/dev/shm/exercise_mode.json` |
| 切换弯举 | "弯举模式" | `/dev/shm/exercise_mode.json` |
| 飞书推送 | "发飞书" | HTTP 调 `/api/feishu/send_plan` |
| 调疲劳上限 | "疲劳目标改为2000" | `/dev/shm/fatigue_limit.json` |

### 被抛弃的方案

- **Vosk**：glibc ABI 不兼容，`libvosk.so` 缺 `_ZNSt7__cxx11...` 符号（Debian 10 ARM64 无解）
- **edge-tts**：依赖微软云端，网络不稳定时完全不可用

---

## §VII. EMG 双模（模拟 ↔ 传感器）

**决策**：无传感器时自动生成模拟 EMG，有传感器时实时接管。

### 模拟模式（`cloud_rtmpose_client.py:293-331`）

```python
def _generate_emg_from_angle(angle, exercise="squat"):
    if angle < 140:
        d = (140 - angle) / 70.0
        return {"quadriceps": 50+d*40, "glutes": 40+d*55, ...}
```

- 由 vision 进程基于骨架角度生成
- 写入 `/dev/shm/muscle_activation.json`
- 仅在无传感器心跳时生效：`if os.path.exists("/dev/shm/emg_heartbeat"): return`（L313）

### 传感器模式（`hardware_engine/sensor/udp_emg_server.py`）

- UDP:8080 监听双通道 ASCII 浮点数据（L86-87）
- DSP 流水线：20Hz 高通 → 50Hz 陷波 → 150Hz 低通（`BiquadFilter`，L23-46）
- RMS 包络：100 样本滑动窗口（L110-118）
- 双线程：`dsp_receiver_worker`（1kHz 接收）+ `io_dumper_worker`（33Hz 写盘）
- 心跳：写 `/dev/shm/emg_heartbeat` 告知 vision 进程停止模拟（L190-191）
- 断连：500ms 无数据 → `IS_CONNECTED = False` → 不覆写模拟数据（L148-153）

---

## §VIII. FSM + GRU 双引擎

### FSM 状态机（`main_claw_loop.py`）

**深蹲 FSM**（`SquatStateMachine`，L69-272）：

```
状态流转（绝对阈值迟滞法）：
  STAND/IDLE → angle < 140 → DESCENDING（记录 min_angle）
  DESCENDING → angle > 145 → 结算:
    min_angle < 110 → good_squats++（标准）
    min_angle >= 110 → failed_squats++（违规, 触发蜂鸣）
```

- 角度计算：三点法（hip-knee-ankle），取左右腿置信度更高的一侧（L192-210）
- 5 帧平滑：`sum(history[-5:]) / 5`（L226-227）
- 角度合理性过滤：`< 20` 或 `> 175` 丢弃（L213-214）
- 关键点间距检查：髋-踝距离 < 30px 丢弃（L218-221）
- 疲劳计算：每次 good_squat 加 `1500/7 ≈ 214.3`，7 个动作即满（L252-253）

**弯举 FSM**（`DumbbellCurlFSM`，L275-457）同构，关键点换为 shoulder-elbow-wrist，阈值 `ANGLE_STANDARD = 50`。

### GRU 推理引擎（`cognitive/fusion_model.py`）

**模型结构**（`CompensationGRU`，L24-91）：

```python
# 7D 输入 → GRU(hidden=16) → 3 输出头
self.gru = nn.GRU(7, hidden_size=16, num_layers=1, batch_first=True)
self.golden_embed = nn.Parameter(torch.randn(16))  # 黄金标准嵌入
self.sim_head = nn.Sequential(Linear(17, 8), ReLU(), Linear(8, 1), Sigmoid())  # 相似度
self.cls_head = nn.Linear(16, 3)   # 3 分类: standard/compensating/non_standard
self.phase_head = nn.Linear(16, 4) # 4 阶段: standing/descending/bottom/ascending
```

**7D 特征向量**：

```python
# fusion_model.py:13
FEATURES_7D = ['Ang_Vel', 'Angle', 'Ang_Accel', 'Target_RMS', 'Comp_RMS',
               'Symmetry_Score', 'Phase_Progress']
```

**推理触发**（`main_claw_loop.py:665-683`）：

- 仅在 `inference_mode == "vision_sensor"` 且一个完整 rep 结束时触发
- 取最近 30 帧特征窗口（`_GRU_WINDOW_SIZE = 30`）
- 归一化：angle/180, RMS/100, ang_accel/10（clamp -1,1）
- 跳帧：设计有 `_GRU_INFER_EVERY = 3` 但当前只在 rep 结束时触发
- 向后兼容：支持 4D 旧模型（`_load_gru_model()` L29-53 先试 7D 再试 4D）

**训练**：

- 数据集：`SquatDataset` 滑动窗口，seq_len=30（L163-205）
- 损失：`0.4 * MSE(similarity) + 0.6 * CrossEntropy(classification)`（L295）
- 相似度目标：golden=1.0, lazy=0.5, bad=0.2（L283-289）
- 无 pandas 依赖：内建 `_SimpleDF` 类替代（L233-250）

---

## §IX. IPC 协议（/dev/shm JSON + atomic rename）

**决策**：所有进程间通信通过 `/dev/shm/` 下的 JSON 文件，原子 rename 保证一致性。

### 完整信号文件清单

| 文件 | 生产者 | 消费者 | 内容 |
|------|--------|--------|------|
| `pose_data.json` | vision | fsm | 17 关键点坐标+置信度 |
| `result.jpg` | vision | flask/mjpeg | 带骨架标注的 JPEG |
| `fsm_state.json` | fsm | flask/frontend | 状态/计数/角度/疲劳/EMG |
| `muscle_activation.json` | vision 或 emg | fsm/frontend | 肌肉激活百分比 |
| `llm_reply.txt` | fsm | flask/voice | DeepSeek 教练点评 |
| `chat_input.txt` | flask/voice | fsm | 用户文字/语音输入 |
| `chat_reply.txt` | fsm | flask/voice | DeepSeek 对话回复 |
| `vision_mode.json` | flask | vision | `{"mode":"local"/"cloud"}` |
| `inference_mode.json` | flask | fsm | `{"mode":"pure_vision"/"vision_sensor"}` |
| `mute_signal.json` | flask/voice | voice | `{"muted":true/false}` |
| `exercise_mode.json` | voice | fsm | `{"mode":"squat"/"curl"}` |
| `fatigue_limit.json` | voice | fsm | `{"limit":2000}` |
| `trigger_deepseek` | flask | fsm | 空文件，存在即触发 |
| `fsm_reset_signal` | flask | fsm | 空文件，存在即重置 |
| `violation_alert.txt` | fsm | voice | 违规播报文本 |
| `chat_active` | voice | fsm | 空文件，对话模式锁 |
| `emg_heartbeat` | emg | vision | 存在时 vision 不生成模拟 EMG |
| `user_profile.json` | flask | fsm/vision/emg | 用户参数（运动类型等） |
| `hdmi_status.json` | vision | flask | `{"active":true/false}` |
| `voice_debug.json` | voice | flask | VAD 能量/识别文本 |

### 原子写入模式

```python
# 所有写入都遵循此模式（示例：cloud_rtmpose_client.py:220-227）
tmp = SHM_POSE_JSON + ".tmp"
with open(tmp, "w") as f:
    json.dump(payload, f)
os.rename(tmp, SHM_POSE_JSON)  # POSIX atomic rename
```

---

## §X. 板端约束（Python 3.7 + NPU 置信度 + 音频硬件）

### Python 3.7 红线

- 禁止 `X | None` 语法 → 用 `Optional[X]` 或注释型标注 `# type: (str) -> bool`
- 禁止 `match/case`、`:=` 海象运算符
- 禁止 `pandas`（板端未安装，fusion_model.py 用内建 `_SimpleDF` 替代）
- 代码中随处可见 `# type: (X) -> Y` 注释（如 `voice_daemon.py:89`, `local_yolo_pose.py:66`）

### NPU 置信度适配

- 量化模型 person_score 最高约 0.1-0.2（云端浮点模型 > 0.9）
- FSM 阈值：`obj.get("score", 0) < 0.05`（`main_claw_loop.py:184`）
- Vision 阈值：`person_score > 0.08` 才写入 objects（`cloud_rtmpose_client.py:684`）
- 关键点置信度：`MIN_KPT_CONF = 0.05`（`main_claw_loop.py:192`）
- local_yolo_pose 默认：`conf_thresh=0.08`（`cloud_rtmpose_client.py:171`）

### 音频硬件

- 录音：es7243 阵列麦克风 `hw:2,0`（`voice_daemon.py:50`），44100Hz 双声道
- 播放：`plughw:0,0`（L51）
- 每次重启必须：`sudo amixer -c 0 cset numid=1,iface=MIXER,name='Playback Path' 6`（SPK_HP 通道）
- 掉电后 Playback Path 自动回 OFF（`toybrick_board_rules.md:18`）

### HDMI

- 需要 X11：`startx -- -nocursor` + `xhost +local:` + 手动复制 `.Xauthority`
- 检测：读 `/sys/class/drm/card0-HDMI-A-1/status`（`streamer_app.py:341`）

---

## §XI. 踩坑记录

### 坑 1：nohup 不能包裹复合命令

**现象**：`nohup cd /path && python3 script.py` 无效，进程立即退出。

**原因**：`nohup` 只能包裹单个可执行文件，不能包裹 shell 复合命令。

**解决**（`streamer_app.py:726-756`）：写临时 shell 脚本 `/tmp/_launch_{name}.sh`，nohup 执行脚本。

### 坑 2：pgrep -f 匹配自身

**现象**：`pgrep -f "cloud_rtmpose_client.py"` 在 `shell=True` 模式下匹配到 bash 自身的 cmdline。

**解决**：bracket trick `'[c]loud_rtmpose_client.py'`（`streamer_app.py:631-632`），正则 `[c]` 不匹配字面 `c` 在 `/proc/self/cmdline` 中。

### 坑 3：Playback Path 掉电归零

**现象**：板子重启后音箱无声，`aplay` 正常退出但没声音。

**原因**：RK3399ProX 音频 Codec 掉电后 `Playback Path` 寄存器回 OFF（0）。

**解决**：每次启动服务时强制写 `numid=1 val=6`（SPK_HP）。在 `_SERVICE_LAUNCHERS` 的启动脚本中注入（L731-732）。

### 坑 4：NPU keypoint xy 被错误 sigmoid

**现象**：local 模式下所有关键点都聚集在画面中央。

**原因**：YOLOv5-Pose 的 keypoint xy 坐标是直接解码（`kx * 2.0 - 0.5 + grid_offset`），不经过 sigmoid，只有 conf 才 sigmoid。

**解决**（`local_yolo_pose.py:128-133`）：

```python
kx = (block[6 + k*3, i, j] * 2.0 - 0.5 + j) * stride     # NO sigmoid
ky = (block[6 + k*3 + 1, i, j] * 2.0 - 0.5 + i) * stride  # NO sigmoid
kc = float(_sigmoid(block[6 + k*3 + 2, i, j]))              # sigmoid only on conf
```

### 坑 5：ALSA 错误日志污染

**现象**：使用 `arecord`/`aplay` 时大量 ALSA 警告刷屏。

**解决**（`voice_daemon.py:33-43`）：通过 ctypes 加载 `libasound.so.2`，注册空错误处理函数。

### 坑 6：Flask GIL 竞争导致视频卡顿

**现象**：12 个 API 轮询端点与 MJPEG 流争抢 Python GIL，前端视频严重卡顿。

**解决**：vision 进程内嵌独立 MJPEG 服务器（:8080），完全绕过 Flask。HDMI 模式下 Flask CPU 占用从 30% 降到 < 5%。

### 坑 7：Vosk 语音在 Debian 10 ARM64 不可用

**现象**：`import vosk` → `libvosk.so` 链接失败，缺少 `_ZNSt7__cxx1119basic_ostringstreamIcSt11char_traitsIcESaIcEEC1Ev`。

**原因**：Vosk 预编译二进制要求 glibcxx 新版本 ABI，Debian 10 的 libstdc++ 不提供。

**解决**：放弃 Vosk，采用百度 AipSpeech 云端方案（`voice_daemon.py:9`）。

### 坑 8：SIGTERM 对 Python 子进程树无效

**现象**：`kill -15` 后 Python 主进程退出，但 `arecord`/`aplay` 子进程成为孤儿。

**解决**（`streamer_app.py:795-811`）：直接 `kill -9`（注释："no mercy -- TERM was unreliable"），外加 `killall -9 arecord aplay` 兜底清理。

### 坑 9：量化模型角度跳变

**现象**：NPU 量化噪声导致角度在帧间剧烈跳变（如 90->20->150）。

**解决**：
- 5 帧平滑（`main_claw_loop.py:226-227`）
- 角度合理性过滤 20-175（L213-214）
- 关键点间距检查（L218-221）
- One-Euro 平滑滤波器（`cloud_rtmpose_client.py:489-491`）

---

## §XII. 当前状态与下一步

### 已完成（Sprint 4, 2026-04-15）

- [x] 双视觉模式热切换（Cloud RTMPose / Local YOLOv5-Pose）
- [x] 三路视频输出（HDMI / MJPEG:8080 / Flask）
- [x] 一体化 PWA 前端（控制台 + 日志 + 数据管理 + 设置）
- [x] 5 服务本地启停管理（pgrep + bracket trick + 脚本化启动）
- [x] DeepSeek 直连 + SSE 流式 + 飞书推送
- [x] 百度 AipSpeech 语音 TTS+STT + 自适应 VAD
- [x] EMG 双模（模拟/传感器）+ Canvas 波形
- [x] FSM 深蹲/弯举 + GRU 双引擎框架
- [x] 推理模式切换（纯视觉 / 视觉+传感）

### 已知问题（Sprint 5 待修）

来源：`architecture.md` Sprint 5 已知问题清单（L99-109）

- [ ] 语音唤醒不工作：voice_daemon 启动但喊"教练"无反应，需 debug 录音→STT→匹配全链路
- [ ] 违规检测不准：几乎所有动作判标准，量化模型精度本身差
- [ ] 音箱不响：amixer 已设 SPK_HP 但实测仍无声，需板端硬件实测
- [ ] 网页无视频：拔掉 HDMI 后 MJPEG:8080 没自动回退
- [ ] GRU 未正式训练：工具链就绪，等真实 EMG 数据
- [ ] 开机自启动：需 systemd 服务
- [ ] 数据持久化：当前训练记录用 JSON 文件，待迁移 SQLite

### 技术债

- `streamer_app.py` 体量过大（~1040 行），admin API 应抽出独立模块
- `main_claw_loop.py` 的 `SquatStateMachine` 和 `DumbbellCurlFSM` 有大量重复代码
- `_SimpleDF` 内建类是 pandas 的临时替代，DataFrame 操作不完整
- SSL 验证全局关闭（`ssl.CERT_NONE`，`session.verify = False`）
- 飞书 app_secret 硬编码在 `architecture.md`（应仅存于 `.api_config.json`）
