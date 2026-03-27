# 第 6 章：云端大模型对接与语音交互

> 本章记录 IronBuddy 如何通过 OpenClaw 网关对接 DeepSeek 大语言模型、实现语音唤醒对话、以及飞书群自动推送功能。所有内容基于 `openclaw_bridge.py`、`voice_daemon.py`、`main_claw_loop.py` 实际代码。

---

## 6.1 OpenClaw 网关架构

### 为什么需要网关？
板端无法直接调用 DeepSeek API——API Key 不宜暴露在开发板上，且板端网络受限。因此，系统采用「中转网关」架构：

```
板端 (127.0.0.1:18789)
  ↓ WebSocket（通过 SSH 反向隧道）
宿主机 OpenClaw Gateway (127.0.0.1:18789)
  ↓ HTTPS
DeepSeek API (云端)
```

### SSH 反向隧道
```bash
ssh -i C:\temp\id_rsa -R 18789:127.0.0.1:18789 toybrick@板端IP
```
`-R 18789:127.0.0.1:18789` 将板端的 18789 端口映射到宿主机上正在运行的 OpenClaw Gateway。板端代码写 `ws://127.0.0.1:18789`，数据通过 SSH 加密隧道自动中转到宿主机。

### OpenClaw Gateway
OpenClaw 是一个开源的 Node.js LLM 网关，运行在 WSL 宿主机上：
```bash
openclaw gateway --port 18789
```
它管理 DeepSeek API Key、处理 WebSocket 鉴权挑战、转发对话请求和响应。

---

## 6.2 OpenClaw Bridge 实现（`openclaw_bridge.py`）

### 连接与鉴权
```python
class OpenClawBridge:
    def __init__(self, gateway_url="ws://127.0.0.1:18789", token="..."):
        ...

    async def connect(self):
        # 1. WebSocket 连接
        # 2. Gateway 发来 connect.challenge（nonce）
        # 3. 回复 connect 请求（带 token、role=operator）
        # 4. 收到 res ok=True → 鉴权成功
```

### UUID 无记忆会话
```python
async def ask(self, text, timeout=60, generate_new_session=True):
    # 每次对话生成随机 8 位 UUID Session: "agent:main:a3f7b2c1"
    # 使 DeepSeek 完全处于"无记忆"状态，杜绝历史残留导致的假死
    current_session = f"agent:main:{uuid.uuid4().hex[:8]}"
```

**设计理由：** 健身教练点评不需要跨组记忆。每次对话只需发送当前组的统计数据（标准 N 次 + 违规 M 次），大模型即可生成针对性点评。使用一次性 Session 可以完全避免上下文爆炸和会话假死。

### 异步 Future 匹配
请求和响应通过 `req_id` → `runId` 映射关系匹配：
1. 发送 `chat.send` 请求，附带 `req_id`
2. Gateway 返回 `res`，包含 `runId`
3. 后续 `chat` 事件通过 `runId` 匹配，resolve 对应的 Future

这保证了主循环不被阻塞——`ask()` 返回一个 Future，60 秒超时后自动放弃。

---

## 6.3 Prompt 设计

发送给 DeepSeek 的 prompt **不包含任何原始坐标数据**，但会注入多维训练统计：

```python
# V2.2 富 Prompt 模板
prompt = f"""你是 IronBuddy 健身教练。本组训练数据：
- 标准深蹲: {good_count} 次，违规半蹲: {failed_count} 次（合格率 {rate_pct}%）
- 肌肉激活TOP3: {muscle_info}
最近3天训练记录:
{history_lines}
要求：先表扬进步，再指出不足，给一个具体改进建议。60字以内。"""
```

**V2.1 → V2.2 升级点：** 从仅发「标准 N + 违规 M」升级为注入合格率、肌肉激活数据、历史对比，使教练回复从泛泛鼓励变为个性化点评。

---

## 6.4 语音对话系统

### 完整流程
```
用户说"教练" → voice_daemon 检测唤醒词
  ↓
TTS 播报"我在，请说"
  ↓
用户说话 → arecord 录音（3秒 chunk）
  ↓
Vosk 离线 ASR 转文字 → 写入 /dev/shm/chat_input.txt
  ↓
main_claw_loop.py 轮询该文件 → 组装 prompt → OpenClaw 发送
  ↓
DeepSeek 回复 → 写入 /dev/shm/chat_reply.txt
  ↓
tts_daemon.sh 检测到新回复 → edge-tts 合成 → 音箱播放
```

### voice_daemon.py 关键设计（V2.2 更新）

| 特性 | 实现 |
|------|------|
| **ASR 引擎** | **Vosk 离线优先**（~50MB 中文模型），Google ASR 降级 fallback |
| 唤醒词 | 模糊匹配："教练"、"教"、"叫练"、"coach" 等 13 个变体 |
| 麦克风自动检测 | 遍历 `plughw:0,0`、`plughw:2,0`、`plughw:3,0` |
| 能量阈值 | 150（V2.1 调优值） |
| 识别延迟 | Vosk: **<1s**（流式离线） / Google: 3-8s（网络依赖） |
| 对话积攒 | 多个 3秒 chunk 累积拼接，静音后一次性发送 |
| 声音打断 | 唤醒词触发时，`killall` 杀掉正在播放的所有声音 |

### tts_daemon.sh

轮询 `/dev/shm/llm_reply.txt` 和 `/dev/shm/chat_reply.txt`：
```bash
# 声线: zh-CN-YunxiNeural
edge-tts --text "$text" --voice zh-CN-YunxiNeural --write-media /tmp/tts.mp3
# 关键: -r 16000 强制重采样，解决硬件 I2S 16kHz 锁定导致的声卡死锁
mpg123 -a plughw:0,0 -r 16000 -f 8000 -q /tmp/tts.mp3
```

---

## 6.5 飞书推送功能

### 触发条件
用户语音中包含「飞书」+「发/推送/安排」等关键词组合。

### 执行流程
1. **即时反馈**：TTS 播报"好的，正在帮你安排"
2. **异步等待 8 秒**：等待当前组 DeepSeek 点评完成
3. **组装训练计划 prompt**：注入用户本组数据 + 历史训练记录
4. **DeepSeek 生成进阶训练安排**
5. **飞书 WebHook 推送**：通过 `deliver()` 方法发送到飞书群

### deliver() 实现
```python
async def deliver(self, text, channel="feishu", timeout=30):
    # 使用 urllib 直连飞书 WebHook（不走 OpenClaw）
    webhook_url = os.environ.get("FEISHU_WEBHOOK", "")
    data = json.dumps({"msg_type": "text", "content": {"text": text}})
    urllib.request.urlopen(req, timeout=timeout, context=ctx)
```

> ⚠️ 飞书 WebHook URL 通过环境变量 `FEISHU_WEBHOOK` 配置，在 `start_validation.sh` 中取消注释对应行即可启用。

---

## 6.6 触发方式与用户控制权

系统**不会自动触发**大模型点评。只有在以下情况下才会向云端发送请求：
1. 用户通过语音说出唤醒词"教练"进行对话
2. 用户在前端网页点击"生成本组点评"按钮
3. 用户语音中包含飞书推送关键词

这确保了用户对训练节奏的**完全控制权**——系统不会在训练中途突然打断用户。

---

## 附：关键文件清单

| 文件 | 行数 | 描述 |
|------|------|------|
| `openclaw_bridge.py` | 218 | WebSocket 桥接（鉴权+UUID会话+飞书推送） |
| `voice_daemon.py` | ~310 | 唤醒式语音守护（麦克风检测+ASR+对话积攒） |
| `main_claw_loop.py` | ~510 | 主循环（轮询 chat_input + 组装 prompt） |
| `tts_daemon.sh` | — | TTS 播报守护（edge-tts + mpg123 重采样） |
| `peripheral_daemon.sh` | — | 外设旁路监听（蜂鸣器+大模型回复触发） |
