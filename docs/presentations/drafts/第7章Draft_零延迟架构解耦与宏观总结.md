# 第 7 章 Draft：端云架构解耦与 OpenClaw 桥接层技术详解

> 本文档基于 `hardware_engine/cognitive/openclaw_bridge.py`（218 行）和 `main_claw_loop.py` 中的异步调度代码编写。

---

## 一、架构分层总览

IronBuddy 系统按物理位置分为三层，各层职责严格隔离：

| 层级 | 物理位置 | 核心进程 | 职责 |
|------|---------|---------|------|
| 边缘感知层 | RK3399ProX 板端 | C++ NPU 引擎 (`main`)、`pose_subscriber.py`、`main_claw_loop.py`、`voice_daemon.py`、`tts_daemon.sh`、`peripheral_daemon.sh` | 视觉推理、角度计算、动作判定、蜂鸣警报、语音录入/播放 |
| 指挥控制层 | 宿主机 (Windows/WSL) | `openclaw gateway`（18789 端口） | 大模型网关中转、SSH 隧道维护、代码开发与同步 |
| 云端智能层 | DeepSeek API (远端) | — | 接收训练战报、生成教练点评、飞书推送内容 |

**关键设计**：板端与云端**不直接通信**。所有大模型请求经由宿主机上的 OpenClaw 网关中转，板端通过 SSH 反向隧道 (`-R 18789:127.0.0.1:18789`) 访问网关的本地端口。

---

## 二、OpenClawBridge 类详解

### 2.1 初始化与鉴权

`OpenClawBridge` 通过 WebSocket 连接 OpenClaw 网关（默认 `ws://127.0.0.1:18789`），使用 Token 进行鉴权：

```python
class OpenClawBridge:
    def __init__(self, gateway_url="ws://127.0.0.1:18789",
                 token="67d0442e..."):
        self.ws = None
        self._response_futures = {}      # {req_id/run_id: asyncio.Future}
        self._connected_event = asyncio.Event()
```

鉴权流程为 Challenge-Response 模式：
1. 网关发送 `connect.challenge` 事件（含 nonce）
2. 客户端回复 `connect` 请求（含 Token、角色、权限范围）
3. 网关返回 `res`（`id="connect-1"`），`ok=True` 表示鉴权成功

### 2.2 消息监听循环 `_listen_loop()`

后台常驻协程，轮询 WebSocket 消息流。处理三类消息：

| 消息类型 | 判定条件 | 处理方式 |
|---------|---------|---------|
| 鉴权挑战 | `event == "connect.challenge"` | 发送鉴权响应 |
| 对话事件 | `event in ["chat", "agent"]` | 匹配 Future 并填充结果 |
| 请求确认 | `type == "res"` + `payload.runId` | 将 Future 从 `req_id` 重映射到 `runId` |

### 2.3 Future 匹配机制

这是桥接层最关键的设计，解决了异步请求与响应的关联问题：

```
发送请求时：
  req_id = "chat-<uuid>"
  _response_futures[req_id] = Future

收到 res 确认时（含 runId）：
  future = _response_futures.pop(req_id)
  _response_futures[runId] = future    # 重映射

收到 chat/agent 事件（state="final"）时：
  优先用 runId 匹配 → 找不到则用最老的 pending Future（FIFO 回退）
  提取 message.content[].text → future.set_result(text)
```

**FIFO 回退策略**：由于 OpenClaw 网关有时不返回 `res` 确认，导致 `runId` 映射缺失。此时取最早注册的 Future 进行 resolve，确保不会因为协议不完整而永久挂起。

---

## 三、UUID 无记忆会话机制

### 3.1 问题背景

早期版本使用固定的 `sessionKey = "agent:main:main"`，导致所有对话共享同一上下文窗口。大量历史消息累积后，大模型的 Context 窗口溢出，请求被拒绝或超时。

### 3.2 解决方案

`ask()` 方法每次调用时生成随机 8 位 UUID 作为 `sessionKey`：

```python
async def ask(self, text, timeout=60, generate_new_session=True):
    req_id = "chat-" + str(uuid.uuid4())
    current_session = f"agent:main:{uuid.uuid4().hex[:8]}" if generate_new_session else "agent:main:persist"
    
    rpc_probe = {
        "type": "req",
        "id": req_id,
        "method": "chat.send",
        "params": {
            "sessionKey": current_session,
            "message": text,
            "idempotencyKey": str(uuid.uuid4())
        }
    }
```

**效果**：每次对话都是全新的上下文，大模型只需处理当前这一条精简 prompt，响应时间从数十秒降至 2-3 秒。

### 3.3 超时保护

使用 `asyncio.wait_for(future, timeout=60)` 限制等待时间。超时后自动清理 pending Future，防止内存泄漏。

---

## 四、飞书推送实现

### 4.1 WebHook 直连

`deliver()` 方法绕过 OpenClaw 通道，直接使用 `urllib` 向飞书 WebHook 发送消息：

```python
async def deliver(self, text, channel="feishu", timeout=30):
    webhook_url = os.environ.get("FEISHU_WEBHOOK", "")
    data = json.dumps({
        "msg_type": "text",
        "content": {"text": text}
    }).encode("utf-8")
    # 通过 run_in_executor 在线程池中执行同步 HTTP 请求
    await asyncio.get_event_loop().run_in_executor(
        None, lambda: urllib.request.urlopen(req, timeout=timeout, context=ctx)
    )
```

### 4.2 Wizard of Oz 剧本拦截

在 `main_claw_loop.py` 的 `_chat_handler()` 中，系统对用户语音进行关键词检测：

```python
feishu_kw = ["飞书", "飞出", "飞叔", "非书", "废书"]
action_kw = ["发", "推送", "计划", "安排", "生成"]
if any(k in user_text for k in feishu_kw) and any(a in user_text for a in action_kw):
    # 1. 立即回复用户确认消息
    reply = "没问题！我已经为你量身定制好了明天的进阶计划..."
    # 2. 后台启动延迟协程
    async def delayed_feishu_push():
        await asyncio.sleep(8)
        plan_text = await bridge.ask(plan_prompt, generate_new_session=True)
        await bridge.deliver(plan_text, channel="feishu")
    asyncio.create_task(delayed_feishu_push())
```

这种设计实现了"即时反馈 + 后台异步执行"的用户体验，用户不需要等待大模型响应即可获得确认。

---

## 五、SSH 反向隧道

板端无法直接访问 WSL 宿主机上的 OpenClaw 网关。解决方案是在 SSH 连接时建立反向端口转发：

```bash
ssh -R 18789:127.0.0.1:18789 toybrick@板端IP
```

这使得板端的 `ws://127.0.0.1:18789` 实际指向宿主机上运行的 OpenClaw 网关进程。该隧道由 `start_validation.sh` 自动建立和维护。

---

## 六、进程间通信（IPC）汇总

系统内各进程通过共享内存文件（`/dev/shm/`）进行通信：

| 文件 | 写入方 | 读取方 | 内容 |
|------|-------|-------|------|
| `pose_data.json` | C++ NPU 引擎 | `main_claw_loop.py` | 17 个关键点坐标 |
| `result.jpg` | C++ NPU 引擎 | `streamer_app.py` | 骨骼叠加画面 |
| `fsm_state.json` | `main_claw_loop.py` | `streamer_app.py`（前端） | 状态、计数、角度 |
| `llm_reply.txt` | `main_claw_loop.py` | `tts_daemon.sh` | 大模型训练点评 |
| `chat_input.txt` | `voice_daemon.py` | `main_claw_loop.py` | 用户语音转文字 |
| `chat_reply.txt` | `main_claw_loop.py` | `tts_daemon.sh` | 大模型对话回复 |
| `trigger_deepseek` | `streamer_app.py` | `main_claw_loop.py` | 前端按钮触发信号 |
| `fsm_reset_signal` | `streamer_app.py` | `main_claw_loop.py` | 前端重置信号 |

所有文件写入采用"写临时文件 + `os.rename()` 原子替换"策略，避免读写竞态导致的数据损坏。
