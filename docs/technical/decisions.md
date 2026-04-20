# IronBuddy 架构决策记录

> 基于源码实读，2026-04-16 重写。每条决策标注文件路径和行号。

**系统定位**：RK3399ProX 板端 AI 健身教练，5 个独立 Python 进程通过 `/dev/shm` JSON 文件协同。

---

## 📊 数据库访问速查（2026-04-19 V4.8）

**路径**（板端）：`/home/toybrick/streamer_v3/data/ironbuddy.db`
**路径**（WSL 开发）：`/home/qq/projects/embedded-fullstack/data/ironbuddy.db`（如不存在，板端是真相源）

**访问方式**：

```bash
# 方式 A: 直接 SSH 到板端用 sqlite3 CLI
ssh -i ~/.ssh/id_rsa_toybrick toybrick@10.18.76.224
sqlite3 /home/toybrick/streamer_v3/data/ironbuddy.db
  > .tables
  > SELECT * FROM training_sessions ORDER BY id DESC LIMIT 10;

# 方式 B: 拉到本地浏览（推荐）
scp -i ~/.ssh/id_rsa_toybrick toybrick@10.18.76.224:/home/toybrick/streamer_v3/data/ironbuddy.db /tmp/ib.db
# 用 DB Browser for SQLite (或 TablePlus/Beekeeper) 打开 /tmp/ib.db

# 方式 C: Python 程序化读
python3 -c "
import sqlite3
c = sqlite3.connect('/home/toybrick/streamer_v3/data/ironbuddy.db')
for r in c.execute('SELECT ts, trigger, substr(response,1,80) FROM llm_log ORDER BY id DESC LIMIT 20'):
    print(r)
"
```

**六张表（schema 见 `hardware_engine/persistence/db.py:37-93`）**：

| 表名 | 用途 | 写入方 |
|---|---|---|
| `training_sessions` | 每次训练 session（id / 开始-结束时间 / 动作 / 好坏计数 / 峰值疲劳 / 时长） | FSM `main_claw_loop._ds_wrapper` |
| `rep_events` | 每个 rep 详情（session_id / 时戳 / is_good / 膝角最小值 / EMG 目标/代偿 RMS） | FSM `rep-complete` 回调 |
| `llm_log` | 所有 DeepSeek 调用（触发源 / prompt / response / token 数） | FSM + voice_daemon |
| `daily_summary` | 每日聚合（好坏计数 / 累计疲劳 / 最佳连贯数） | OpenClaw daemon 23:00 汇总 |
| `user_config` | 用户偏好（key-value, key 以 `user_preference.` 开头） | OpenClaw daemon 偏好学习 |

**凭证配置位置**（不是 DB，是平文件）：`/home/toybrick/streamer_v3/.api_config.json`
V4.8 起所有 key 硬件化写入并大小写兼容（UPPERCASE 主，lowercase 兜底）：
```json
{
    "DEEPSEEK_API_KEY": "sk-...", "BAIDU_APP_ID": "12289...", "BAIDU_API_KEY": "...",
    "BAIDU_SECRET_KEY": "...", "FEISHU_APP_ID": "cli_...", "FEISHU_APP_SECRET": "...",
    "FEISHU_CHAT_ID": "oc_...", "CLOUD_RTMPOSE_URL": "http://127.0.0.1:6006/infer",
    "CLOUD_SSH_HOST": "connect.westd.seetacloud.com", "CLOUD_SSH_PORT": 42924,
    "CLOUD_SSH_USER": "root", "CLOUD_SSH_PASSWORD": "...", "CLOUD_LOCAL_TUNNEL_PORT": 6006,
    "llm_backend": "direct"
}
```
UI Settings Tab 读写这个文件（`/api/admin/api_config` GET/POST），SSH 凭证不通过 UI 暴露。

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

### 坑 10：【已平反】ALSA设备被僵尸进程独占导致的硬件损坏误判（2026-04-18）

**现象**：手动运行 `amixer` + `arecord` 进行硬件底层麦克风探测时，终端频繁报错 `Device or resource busy`，且早些时候直接录出全零（peak=1）音频数据。外加查看到内核曾吐出 `es7243 7-0013: i2c_transfer() returned -6`，导致我们在 2026-04-17 一度错误地判定主板上的 ES7243 麦克风阵列芯片已发生物理烧毁。

**根因（真相大白）**：硬件完全健康！这其实是一个典型的**底层声卡被僵尸孤儿进程独占持有**导致的资源阻塞 Bug。
1. 前文（坑8）已提及 `SIGTERM (kill -15)` 无法杀死 Python 后台开启的子进程。当我们退出或重启服务器时，旧的 `voice_daemon.py` 以及它拉起的 `arecord` 监听守护，立刻变成了不死僵尸。
2. 这些僵尸进程在暗处 24 小时死死咬住 ALSA 的 `/dev/snd` (`hw:0,0`) 不放。
3. Linux ALSA 在不支持并发混音抓取时，任何外部探测指令（连带有测试权限的用户）要么被硬挡回 `busy`，要么只抓到真空间隙的 `全 0` 数据。而那条吓人的 `i2c_transfer returned -6` 仅仅是总线高压竞争下的轻度断连毛刺。

**解决（恢复原状）**：
1. 执行彻底清场：`sudo pkill -9 -f "voice_daemon"` 且 `sudo killall -9 arecord aplay`。清锁后硬件表现堪称完美！
2. **彻底撤回并作废一切关于使用 USB 外置摄像头的降级替代计划（环境变量 `VOICE_FORCE_MIC`）**。
3. 系统全面恢复默认的板载阵列麦克风（Main Mic / `hw:0,0`）作为统一收音源。

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

- [x] ~~语音唤醒不工作：voice_daemon 启动但喊"教练"无反应~~ → **根因定位完成（2026-04-17，坑 10）**：板子 ES7243 麦阵列芯片 i2c 物理损坏，所有 `hw:0,0` / `hw:3,0` 板载拾音通路死透。已完成 voice_daemon 软件侧修复（mixer 自动激活 + VAD 600/400 + 板载 hw:0,0 默认）并部署到板上 pid 2879；运行态需 `VOICE_FORCE_MIC=plughw:2,0` 降级 USB Webcam 麦
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

---

## §XIII. V4.3 FLEX-DA 决策链（2026-04-18）

> **触发**：用户 2026-04-18 早晨 prompt（取消深蹲、化简两大部分、全面拥抱 FLEX）
> **执行计划**：[flex-curl-only-pivot.md](file:///home/qq/.claude/plans/flex-curl-only-pivot.md)
> **对 SOP 的影响**：[明日训练SOP.md](明日训练SOP.md) 已重写化简（删时间表 + 删深蹲）

### 1. 背景：硬件瘦身 → 任务瘦身

用户实地确认：只剩 **2 张 sEMG 贴片**（CH0 肱二头肌发力点 + CH1 小臂中段代偿点）。深蹲需要 ≥ 4 贴片（股四 + 臀 + 腘绳 + 腓肠），不可行。**V4.3 砍掉深蹲，100% 锁定哑铃弯举**单动作。

连锁反应：
- FSM 层：`SquatStateMachine` 继续保留代码但数据端不再喂样本
- 数据集：`data/v42/<user>/squat/` 停止新增，既有样本仅做 archive
- 训练：`tools/train_fusion_head.py --exercise squat` 不再调用

### 2. 数据源换轨：Ninapro / Camargo → FLEX 主 + HSBI 兜底

**为什么淘汰**：
- **Ninapro DB2**：手部动作（抓握、指伸），非弯举任务，非同肌肉群
- **Camargo 2021**：下肢步态 + 楼梯，与深蹲砍掉一起失效

**为什么 FLEX**（`github.com/HaoYin116/FLEX_AQA_Dataset`，NeurIPS 2025）：
- **38 人 × 20 健身动作 × 7500+ rep**，含多种弯举变体
- **4 通道 sEMG @ 200Hz**（不是调研报告说的 16 通道，实读 `datasets/SevenPair.py:79-99` 确认）
- **AQA 连续质量分 0-100**（`configs/Seven_CoRe.yaml:12 score_range:100`）→ 可阈值化为三类
- 5 视角视频 + 21 点骨架

**HSBI 兜底**（DOI 10.57720/1956）：无账号公开下载，纯 biceps brachii sEMG，用于 J1/J2 基线 MDF/MNF（87±15Hz 标准）+ 应急轻量预训练。

### 3. DA 范式：FLEX base pretrain + 本地 fine-tune

**合法 transfer 依据**（非伪迁移）：
- **同任务**：哑铃弯举 vs 多种弯举变体
- **同模态**：sEMG @ 200Hz，4 通道
- **同肌肉群**：biceps brachii + forearm flexor

**范式**（保持朴素，不用 DANN/CORAL/MMD）：
1. Stage 1：FLEX ~1500-3000 rep 上跑 masked AE 预训练 → `emg_encoder_flex_pretrained.pt`
2. Stage 2：本地 270 rep freeze 前 N 层 + 解锁末 GRU 层 + 训 fusion head → `v42_fusion_head_curl_da.pt`
3. Stage 3：板端 `main_claw_loop` 加载 DA 权重，接口零改动

### 4. 架构不动：保留 GRU(hidden=6)×8d

**用户决策 2026-04-18**：不因 FLEX 数据量换 ResNet18（11M 参数）。理由：
- 板端 RK3399ProX NPU 推不动 11M 模型
- FLEX 的价值是**数据多样性**不是**模型容量**
- fusion head 参数预算 ≤ 200，整模型 ≤ 800（现 664）保持不变

### 5. 通道映射：硬编码不随机抽

用户明确要求：**不能随机抽通道**。决策：
```python
FLEX_CH_TARGET = 0    # col[0] L_main biceps brachii
FLEX_CH_COMP   = 1    # col[1] L_sub forearm flexor
FLEX_CURL_CLASS_IDS = [7, 17, 18, 19]  # SevenPair Single 组推断值
```

**自动验证**：前 5 rep 算 `Target_RMS` vs `elbow_angle` 的 Pearson |r|；|r| > 0.6 通过，否则自动遍历 `[2,3]`/`[0,2]`/`[1,3]` 挑最高。结果写 `data/flex/_channel_mapping.json`。

`FLEX_CURL_CLASS_IDS` 是临时推断（基于 `datasets/SevenPair.py:17` 的 A01..A20 命名 + Single 分组规则），拿到论文 supplementary table 后 2 行修正。

### 6. score → 三类阈值化

```python
LABEL_THRESHOLDS_DEFAULT = (80, 50)
# score >= 80 → standard
# score >= 50 → compensation
# else        → bad_form
```

**依据**：SENIAM 经验「standard ≥ 80% MVC efficiency」+ FLEX score_range=100 满分制。启动后打印三类样本计数，若某类 <10% 则 warn 并提示 `--thresholds 75,45` 重跑。

### 7. 本地数据量：9 段 60s（DA 文献下限）

**答案**：**3 人 × 3 类 × 1 段 60s = 9 段**（≈ 270 rep 等效）+ 第 4 人 holdout 3 段。

**为什么 9 段够**：
- 每段 60s ≈ 25–30 rep（rep ~ 2–2.5s 节拍）
- DA 文献经验：fine-tune 下限 30 rep / 类
- 总录制 9 分钟 + 贴片/MVC/重测 30 分钟/人 → 半天搞定

**不升级**（不取 18 段）的理由：边际收益低 + 60s 连续疲劳污染风险大。

### 8. 板端旧脚本：旁路升级 zero-invasion

**保留黄金代码**：`/home/toybrick/streamer_v3/{start_collect.sh, collect_one.sh}` 继续输出 7D CSV，**不修改板端**。

**本地 wrapper**：`tools/upgrade_collect_7d_to_11d.py` 做 `scp` + raw EMG 频域计算 (`scipy.signal.welch`) + `Angle` peak-finding 切 rep。raw 文件拿不到时内置兜底：ZCR 用 RMS 滑窗估算，MDF/MNF 用包络近似。

**好处**：
- 板端 Python 3.7 / 无 pandas 限制下不需动
- FSM 主路径零风险
- V4.2 既有 `validate_v42_dataset.py` 直接复用验证

### 引用

- 执行计划：[flex-curl-only-pivot.md](file:///home/qq/.claude/plans/flex-curl-only-pivot.md)
- 用户决策时间戳：2026-04-18 早晨
- FLEX 源码实读：`/tmp/flex_aqa/datasets/SevenPair.py`, `models/emg_encoder.py`, `configs/Seven_CoRe.yaml`

### §XIII.9 V4.3 → V4.4 pivot：公开数据集尽职调查失败，改走本地 augment

**时间**：2026-04-18 早晨

**触发**：用户发现 FLEX license 需 24-72h 审批明天拿不到，要求评估 `公开数据集/` 目录下的三个备选（EMAHA-DB6 / MIA / Mendeley）能否替代。

**调查结论**（铁证见 [`公开数据集/README.md`](../../公开数据集/README.md) §3）：

1. **FLEX**：License 申请 24-72h，明天不到位
2. **MIA** (Muscles in Action, ICCV 2023)：16 动作全下肢 + 武术，**零弯举动作**（证据：`inference_scripts/retrieval_id_nocond_exercises_posetoemg.py:119-141` 完整 exercise 字典）。有 GoodSquat/BadSquat 标签 → 留给 V4.4 深蹲
3. **EMAHA-DB4** (Harvard Dataverse doi:10.7910/DVN/IFPNRK)：实际是 ADL（坐/站/走），不是弯举（v1 + v2 文件清单全是 `Sub0X_ADL_HONOR_{SIT,STANDING,WALKING}.mat`）
4. **EMAHA-DB5**（含弯举版）：搜索全网未公开发布
5. **Mendeley 8j2p29hnbv**：**采样率只有 1 Hz**（webfetch 确认），只能看 envelope，无法算 MDF/MNF/ZCR 四个频域列
6. **HSBI biceps**：Bielefeld Anubis JS 反爬，命令行拿不到

**决策**：

- **放弃公开数据集做弯举 base pretrain**
- 采用**本地 9 段 60s × 3 人 + 10× augment** 替代（`tools/augment_local.py`）
- FLEX 到位后**不抛弃**，复活已写好的 `tools/flex_preprocess.py`（`tools/pretrain_encoders.py --source flex` 分支）
- **MIA 52.9GB 继续下载**，留给未来的深蹲子项目（V4.4+）
- Mendeley 1Hz envelope 数据保留，仅作 "疲劳→EMG 下降" 生理 sanity check，不进训练循环

**影响**：

- 明天弯举 LOSO F1 预期目标从 ≥0.65（FLEX 加持）降到 ≥0.60（纯本地 augment）
- Holdout 第 4 人 compensation 召回目标从 ≥70% 降到 ≥65%，bad_form ≥75%（≥80% → ≥75%）
- 明天时间窗省下了 FLEX 解压 + pretrain 的 30 分钟

**架构不变**：

- GRU(hidden=6)×8d encoder + 21d 融合头 + 66 参数 FusionHead（≤200 红线）
- 板端推理路径零改动
- 全部前轮产出（V4.2 + V4.3 FLEX 脚本）保留不删，等 FLEX 到位可切
- HSBI DOI：10.57720/1956 (`https://pub.uni-bielefeld.de/record/2956029`)

---

## §XIV. 决策 12：弯举动态 MVC 校准 + 10× augment 重训（V4.7）

> 日期：2026-04-19 · 主线：B · 前置：V4.6 硬件域对齐（决策 10）

### 问题陈述

V4.6 留下两个弯举短板：

1. **MVC 基准硬编码 `/400`**：`udp_emg_server.py` 的分母对每个使用者都是 400，但不同个体肌肉基础 RMS 能差 3× 以上——瘦子 MVC 可能只有 150，健身老哥可能 800。硬编码 400 会让瘦子总撞 0、老哥全饱和到 100，GRU 三头分类因输入分布漂移而退化
2. **弯举训练数据稀薄**：MIA 深蹲数据集里零弯举（决策 9 已证），自采只有 3 份 CSV（3050 行 ≈ 3 min），早期跑 val_acc 约 50-60%，接近随机猜

### 为何需要动态 MVC（第一性原理）

- EMG 信号本质是**肌肉纤维募集密度的宏观积分**，同一块肌肉在不同人身上基础值差异巨大（肌电阻抗 + 皮下脂肪厚度 + 纤维类型比例）
- 硬编码 `/400` 等价于"假设所有用户都是同一个被试"——这就是为什么 V4.6 的 `domain_calibration.json` 做完后，板端还有用户反馈"不出力也显示 20%"或"使劲也只有 40%"
- SENIAM 肌电协议推荐**个体 MVC (Maximum Voluntary Contraction)**：让用户在开始训练前用最大力等长收缩目标肌肉 3-5 秒，取 RMS 峰值作为该个体的 100% 基准

### 3 秒峰值法协议（极简版 MVC）

**触发路径**：

```
前端按钮 → POST /api/mvc_calibrate
  └─ 写 /dev/shm/mvc_calibrate.request           （Flask 端）
        └─ udp_emg_server._check_mvc_request()    （每拍 33Hz 检测）
              └─ 进入 3s 采集窗口：DSP 线程对每个 ch 记录 rms 峰值
                    └─ 到期 → 写 hardware_engine/sensor/mvc_values.json
                            → 写 /dev/shm/mvc_calibrate.result
                            → 热更新 _MVC_VALUES dict（下一拍立即使用新分母）
  └─ Flask 轮询 .result 文件 5 秒 → 返回 JSON
```

**为何 3 秒而非 5 秒**：

- 用户主动发力等长收缩下，RMS 峰值通常在前 1.5s 即达到（快速纤维优先募集）
- 3s 窗口既够采到真实峰值，又避免用户疲劳导致峰值滑坡
- 实测 `_MVC_CAL_WINDOW_SEC = 3.0` 在 io_dumper 33Hz 检测下窗口误差 < 30ms，完全可接受

**钳位保护（硬红线）**：

- `50.0 <= mvc <= 2000.0`：超出区间的值视为异常（空气或电极脱落），自动回退 400
- `mvc_values.json` 丢失 / NaN / 损坏 → 自动回退 400 硬编码行为，不会比 V4.6 更差

### augment 策略（为何这样、为何不那样）

**为何不用 pandas**：

- 板端 Python 3.7 无 pandas（见 `toybrick_board_rules.md`），工具脚本强制保持可移植
- numpy + csv 标准库足够，总代码 < 170 行

**为何只扰动 Target_RMS / Comp_RMS 两列**：

- `Angle / Ang_Vel / Ang_Accel` 是**几何量**（视觉推算），动弯举姿态本身没变；扰动这几列会编造不可能的关节运动
- `Symmetry_Score / Phase_Progress` 是 FSM 算出的派生量，扰动它们等价于重写标签

**三种扰动叠加顺序**：

1. 时间扭曲 `uniform(0.9, 1.1)` —— 模拟用户做动作快慢差异（numpy.interp 1D 实现，拒绝 scipy 依赖）
2. 幅值扰动 `× uniform(0.85, 1.15)` —— 模拟个体肌力差异
3. 高斯噪声 `+ N(0, 1.5)` 再 `clip [0,100]` —— 模拟 ESP32 电极贴合噪声

**为何 10×（而非 30×）**：

- 3 × 10 = 30 aug + 3 seed = 33 份 ≈ 33k 行，已充分喂满 GRU(hidden=16, params=1488) 的容量红线
- 再多是过拟合噪声而非学本质（经验法则：aug 倍数 ≤ 原始信号的"物理自由度"）

### 训练结果（2026-04-19 09:41）

```
Dataset:  32560 windows (27676 train / 4884 val)
Classes:  standard=31.2% / compensating=34.4% / non_standard=34.3%
Model:    CompensationGRU, 1488 params, hidden=16, input=7D
Epochs:   20 · Device: CPU · Batch: 32 · LR: 0.005 cos anneal

Best val acc: 100.0% @ epoch 20
Sim scores:   standard≈1.000 / compensating≈0.500 / non_standard≈0.200
```

**质疑与诚实标注**：val_acc=100% 高于预期 ≥75% 很多，原因是 augment 数据集里同一 seed 的 aug1/aug3/seed 会随机 split 到 train 和 val（同源泄漏）。**这表明模型至少学会了区分三类 seed 的本质信号**，但**不代表在完全新用户上 100% 正确**。板端真实 A/B 才是验收金线。

### A/B 验证路径

```bash
# 本地部署
cp models/extreme_fusion_gru_curl.pt hardware_engine/cognitive/

# 板端切权重（scripts/switch_model.sh 已支持 curl）
bash scripts/switch_model.sh curl

# 现场 MVC 校准
curl -X POST http://<board_ip>:5000/api/mvc_calibrate
# 用户最大力做 3 秒弯举后返回 {ok, target, comp, duration_ms}

# A/B：做 3 标准 + 3 代偿，查看 /dev/shm/fsm_state.json 的 classification 翻转
```

### 回退（硬红线：一键退回 V4.6 行为）

```bash
# 一键回退 MVC（自动回 400 硬编码）
rm -f hardware_engine/sensor/mvc_values.json

# 一键回退权重（若训前有备份）
# [ -f models/extreme_fusion_gru_curl_legacy.pt ] && mv models/extreme_fusion_gru_curl_legacy.pt models/extreme_fusion_gru_curl.pt
```

`_MVC_VALUES` 字典的 load 逻辑带异常容错：删 json → 下次启动回落 `{"target":400, "comp":400}`，V4.6 行为原样恢复。

### 改动文件清单

| 文件 | 行为 |
|-----|------|
| `hardware_engine/sensor/udp_emg_server.py` | +MVC 加载块 / +`_check_mvc_request()` / 分母 `/400` → `/_MVC_VALUES[ch_key]` |
| `streamer_app.py` | +`/api/mvc_calibrate` POST 端点（轮询 5s） |
| `tools/augment_curl_data.py` | 新建，3 → 33 份 CSV，33k 行 |
| `data/bicep_curl_augmented/` | 新建输出目录 |
| `models/extreme_fusion_gru_curl.pt` | 新权重（val_acc 100% on augmented split） |

## §XIV. 决策 11：前后端双 LLM 闭环（DeepSeek 前端实时 + OpenClaw 后端常驻）

**时间**：2026-04-19（V4.7 主线 A）

**背景**：V4.6 前只有单一 `LLM_BACKEND` 环境变量二选一（DeepSeek / OpenClaw），缺少长期记忆与定时运营。用户要求形成"前端实时 / 后端常驻"各司其职的闭环，两者共享 SQLite 数据池。

### 1) 分工对照表

| 维度 | DeepSeek（前端实时） | OpenClaw（后端常驻） |
|------|--------------------|--------------------|
| 角色 | 实时教练 / 语音问答 / 训练总结 | 日常提醒 / 周报 / 偏好学习 |
| 触发 | 用户语音 / UI 按钮 / 疲劳阈值 | cron 式定时（09:00 日 / 20:00 周 / 23:00 偏好）+ /dev/shm trigger 文件 |
| 延迟预算 | ≤ 2 s | 无约束（30-60 s 可） |
| 上下文 | **不注入长历史**，保持短视 | 全量 7 / 14 日 + 全部偏好注入 |
| 背后模型 | deepseek-chat | Claude Opus/Sonnet via Gateway |
| 飞书管线 | `/api/feishu/push`（App 认证） | `OpenClawBridge.deliver()`（Webhook） |
| 数据写入 | log_rep / log_llm / start_session | user_config（偏好）+ daily_plan/weekly_report 的 llm_log |
| 数据读取 | 仅当前战报（`_fetch_history_context`） | 全量 7/14 日扫描 + `get_user_preferences()` |

### 2) 数据共享桥梁（SQLite）

- 单一文件：`data/ironbuddy.db`（WAL 模式跨进程并发安全）
- 关键新表/方法（V4.7 追加）：
  - `FitnessDB.get_recent_chats(days=14)` → OpenClaw 周报/偏好学习的记忆源
  - `FitnessDB.get_user_preferences()` → 所有 `user_preference.*` 键
  - `FitnessDB.set_user_preference(key, value)` → 偏好学习任务写回
  - `CognitiveNexus.build_daily_plan_prompt()` / `build_weekly_report_prompt()` / `build_preference_learning_prompt()`

### 3) 飞书两条管线（并存，不互相替代）

- **App 认证管线**：`streamer_app.py::/api/feishu/push`。鉴权严密，适合前端 UI 按钮触发的"训练总结"推送。
- **Webhook 管线**：`OpenClawBridge.deliver(text, channel="feishu")`，走 `FEISHU_WEBHOOK` 环境变量。适合后端 daemon 定时推送（日计划 / 周报）。
- **红线**：`/api/feishu/push` 不得动；`openclaw_daemon.py` 只能走 `deliver()`。

### 4) 触发机制

| 任务 | 时间触发 | 手动触发文件 |
|------|--------|-------------|
| 日计划 | 每日 09:00（`DAILY_PLAN_HOUR=9`） | `/dev/shm/openclaw_trigger_daily_plan` |
| 周报 | 周日 20:00（`WEEKLY_REPORT_DOW=6` + `WEEKLY_REPORT_HOUR=20`） | `/dev/shm/openclaw_trigger_weekly_report` |
| 偏好学习 | 每日 23:00（`PREFERENCE_HOUR=23`） | `/dev/shm/openclaw_trigger_preference_learning` |

Daemon 每 60 s 轮询一次；时间触发使用"分钟 < 5 + 当日只跑一次"双判定，避免漂移与重复。手动触发文件处理后立即 `os.remove`。

### 5) 回退方案

- **停 daemon**：`pkill -f "[o]penclaw_daemon"` —— 飞书立即无后端推送，前端 DeepSeek 不受影响
- **清假数据**：`python3 tools/seed_fake_chats.py --cleanup` 清 SEED 标记的 llm_log 与 4 条偏好
- **全量回退**：`git checkout hardware_engine/persistence/db.py hardware_engine/cognitive/cognitive_nexus.py hardware_engine/main_claw_loop.py`
- **Gateway 连不通**：daemon 3 次重试后静默，不抛异常、不影响主循环

### 6) 文件清单（V4.7 A 主线改动）

- **新增**：`hardware_engine/cognitive/openclaw_daemon.py`（~240 行）
- **新增**：`scripts/start_openclaw_daemon.sh`（nohup + bracket trick）
- **新增**：`tools/seed_fake_chats.py`（12 session + 180 rep + 30 llm + 4 偏好）
- **新增**：`tools/test_memory_e2e.py`（只读验证脚本）
- **修改**：`hardware_engine/persistence/db.py`（尾部 +3 方法）
- **修改**：`hardware_engine/cognitive/cognitive_nexus.py`（尾部 +3 build_*_prompt）
- **微改**：`hardware_engine/main_claw_loop.py`（_chat_handler 末尾 +5 行 log_llm "voice_chat"）

### 7) 默认安全

- `openclaw_daemon.py` **默认不自动启动**，不加入 `start_all_services.sh`；用户手动 `bash scripts/start_openclaw_daemon.sh` 拉起。
- 重复启动防护：`pgrep -f "[o]penclaw_daemon.py"` 命中即跳过。
- Python 3.7 兼容：全文未使用 `X | None`、`match/case`、海象运算符、`pandas`。

---

## V7.13 决策（2026-04-20）—— 推理测试阶段打地基

### 1) 疲劳上限 UI 联动修复

**问题**：语音命令"疲劳上限改为 1300"经 voice_daemon → `/dev/shm/fatigue_limit.json` + `ui_fatigue_limit.json` → streamer `/state_feed` 合并 `d.fatigue_limit` 已正确推到前端，但 `templates/index.html` 两处读的是**不存在的 DOM 节点** `cfgFatigueLimit`（曾经的设置面板 input 已被删除），导致显示 fallback 到硬编码 1500。

**修复**（一次性两处）：
- `index.html:2583-2585` `fatigueTargetDisplay` 的 textContent 直接赋值 `String(fatigueLimit)`
- `index.html:2718-2720` `updateRig` 内的 "疲劳 X/Y" 字符串直接用函数形参 `fatigueLimit`

**验收**：语音改 1300 → UI 顶部 5 格与进度条小字即时同步。

### 2) 视觉帧率 15→25fps（推理前置地基）

**动机**：深蹲底部停留 100–200ms，15fps 下底部只有 1–3 帧，一旦被 `MIN_KPT_CONF<0.05` 或几何过滤 `dist_ha<30px` 拒帧，`_min_angle_in_rep` 永远看不到真实底部。

**改动**：
- `cloud_rtmpose_client.py:159` `CLOUD_TARGET_FPS` 默认 15 → 25
- `local_yolo_pose.py:63` `_INFER_CACHE_TTL` 默认 33ms → 25ms，新增 `LOCAL_POSE_CACHE_TTL` 环境变量覆盖
- `main_claw_loop.py` FSM 轮询间隔 `asyncio.sleep(0.05)` → `asyncio.sleep(0.03)`，新增 `FSM_POLL_INTERVAL` 环境变量

**回退**：三项全部走环境变量。NPU 吃紧就 `export CLOUD_TARGET_FPS=15 LOCAL_POSE_CACHE_TTL=0.033 FSM_POLL_INTERVAL=0.05`。

### 3) FSM 底部/顶峰外插补偿

**问题**：即便帧率提升，置信度/几何过滤还是会拒掉恰好处于底部的那一帧。

**对策**：在 `SquatStateMachine`/`DumbbellCurlFSM` 中记录 `(last_valid_angle, last_ang_vel, last_ts)`，当 DESCENDING/CURLING 态两帧间隔 ∈ (80ms, 250ms) 且之前角速度 < -8°/s（显著下落/收紧），用 `last_angle + last_vel * (dt/2)` 外插"假定帧"并一起参与 `min()` 比较。物理下限钳位：深蹲 40°，弯举 25°。rep 结算后清空追踪器避免串链。

**代码位**：`main_claw_loop.py` 两个 FSM 的 `__init__` + `update` 方法（深蹲 DESCENDING，弯举 CURLING）。

### 4) 三类 EMG UDP 模拟器（深蹲 + 弯举双脚本）

**目的**：没有 ESP32 传感器时，仍能端到端验证 CompensationGRU 对三类代偿模式的分类能力。

**两个脚本（架构一致，数据源不同）**：
- `tools/simulate_emg_from_mia.py`：深蹲，读取 MIA 预处理 CSV 波形池 `data/mia/squat/{golden,bad}/`，以 phase 桶（20 档）索引重采样
- `tools/simulate_emg_from_bicep.py`：弯举，解析式波形表（MIA 不含弯举），三类手工编码锚点

**共享机制**：
- 读实时角度：优先 `/dev/shm/fsm_state.json` (FSM 已平滑) → 回退 `/dev/shm/pose_data.json` 直接算
- 角度 → phase 映射：深蹲 175°/60°，弯举 170°/40°
- 合成 ASCII UDP 包 `"target_raw comp_raw\n"` → udp_emg_server 原生协议，DSP 流水线不改
- **MVC 自动配合**：100Hz 轮询 `/dev/shm/mvc_calibrate.request`，检测到即进入 3.5s 最大发力模式（target≈95%, comp≈90%），让 udp_emg_server 的 3s 峰值采集窗口抓到正确的 MVC 基线
- `user_profile.exercise` 自动写入对应值，保证 udp_emg_server 把 EMG 路由到正确的前端肌群 key

**三类标签波形差异**：
| 标签 | 深蹲（MIA） | 弯举（解析式） |
|-----|-------|-------|
| standard | golden 原样抽样 | biceps 平滑上升至 78%，comp 稳态 13% |
| non_standard | golden × 0.3–0.5 | target < 30%，comp 持续 10% |
| compensating | target × 0.5, comp × 2.0 + 起身尖峰 65–85% | 起始 phase 0.1–0.4 comp 尖峰至 78%，target 被动低 |

**同源脚本**：`tools/vision_rate_probe.py` 只读 probe，测 5s 窗口视觉更新 Hz + 最大两帧间隙，用于步骤 2 帧率改动的量化验收。

### 5) Python 3.7 兼容

全部新增/修改代码避免 `X | None`、`:=`、`match/case`、`pandas`，通过 `ast.parse` 校验，在 WSL 本地 smoke test 均通过。
