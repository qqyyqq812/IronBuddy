import asyncio
import json
import os
import uuid
import logging
import websockets

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class OpenClawBridge:
    MAX_RECONNECT_RETRIES = 5
    RECONNECT_INTERVAL = 10  # seconds between reconnection attempts

    def __init__(self, gateway_url="ws://127.0.0.1:18789", token="67d0442e3a5e7aa3f4d5519cee1ac1a7d413b7062c7dc4c6"):
        self.gateway_url = gateway_url
        self.token = token
        self.ws = None
        # 移除写死的 session_key = "agent:main:main"
        self._response_futures = {}
        self._connected_event = asyncio.Event()
        self._reconnect_task = None

    async def connect(self):
        """建立 WebSocket 连接并响应鉴权挑战"""
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            self.ws = await asyncio.wait_for(
                websockets.connect(self.gateway_url, extra_headers=headers),
                timeout=10
            )
            asyncio.create_task(self._listen_loop())
            await asyncio.wait_for(self._connected_event.wait(), timeout=10)
            logging.info("🌟 OpenClaw Bridge 已连接且鉴权成功")
            return True
        except Exception as e:
            logging.error(f"❌ WebSocket 连接失败: {e}")
            self.ws = None
            return False

    async def _listen_loop(self):
        """后台驻留心跳与消息事件轮询线程"""
        try:
            async for msg_raw in self.ws:
                msg = json.loads(msg_raw)
                
                # 调试日志：记录 Gateway 返回的所有消息
                msg_type = msg.get("type", "?")
                msg_event = msg.get("event", "")
                msg_id = msg.get("id", "")
                msg_ok = msg.get("ok", "")
                logging.info(f"📨 收到GW消息: type={msg_type}, event={msg_event}, id={msg_id}, ok={msg_ok}")
                
                # Check for challenge
                if msg.get("type") == "event" and msg.get("event") == "connect.challenge":
                    nonce = msg["payload"].get("nonce")
                    response_payload = {
                        "type": "req",
                        "id": "connect-1",
                        "method": "connect",
                        "params": {
                            "minProtocol": 3,
                            "maxProtocol": 3,
                            "client": {"id": "cli", "version": "1.0.0", "platform": "linux", "mode": "cli"},
                            "role": "operator",
                            "scopes": ["operator", "operator.read", "operator.write"],
                            "caps": [],
                            "auth": {"token": self.token}
                        }
                    }
                    await self.ws.send(json.dumps(response_payload))
                
                # Check for auth success res
                elif msg.get("type") == "res" and msg.get("id") == "connect-1":
                    if msg.get("ok"):
                        self._connected_event.set()
                    else:
                        logging.error(f"鉴权彻底失败: {msg}")

                elif msg.get("event") in ["chat", "agent"]:
                    payload = msg.get("payload", {})
                    state = payload.get("state") # "ok" or "error"
                    state = payload.get("state")
                    message_data = payload.get("message", {})
                    run_id = payload.get("runId")
                    logging.info(f"📨 [CHAT] state={state}, runId={run_id}, pending={list(self._response_futures.keys())}")

                    # 定位对应的 future：优先用 runId 匹配，找不到就用最老的 pending future
                    target_future = None
                    target_key = None
                    if run_id in self._response_futures:
                        target_future = self._response_futures[run_id]
                        target_key = run_id
                    elif self._response_futures:
                        # Gateway 可能不返回 chat.send 的 res 确认，导致 runId 映射缺失
                        # 此时 resolve 最早注册的 future（FIFO 语义）
                        target_key = next(iter(self._response_futures))
                        target_future = self._response_futures[target_key]
                        logging.info(f"📨 [CHAT] runId 无精确匹配，回退到最老的 future: {target_key}")

                    if target_future and not target_future.done():
                        if state == "final":
                            final_text = ""
                            content_arr = message_data.get("content", [])
                            if isinstance(content_arr, list):
                                for block in content_arr:
                                    if block.get("type") == "text":
                                        final_text += block.get("text", "")
                            
                            if "</think>" in final_text:
                                final_text = final_text.split("</think>")[-1].strip()

                            target_future.set_result(final_text)
                            self._response_futures.pop(target_key, None)
                            logging.info(f"✅ [CHAT] 收到最终回复: {final_text[:80]}")

                        elif state == "error":
                            logging.error(f"❌ [CHAT] 错误: {payload}")
                            target_future.set_exception(Exception("Chat Error: " + str(payload)))
                            self._response_futures.pop(target_key, None)
                    elif not target_future:
                        logging.info(f"⚠️ [CHAT] state={state}, 但无 pending future 可 resolve")
                            
                # Check for Initial Request Acknowledgment
                elif msg.get("type") == "res" and isinstance(msg.get("payload"), dict):
                    req_id = msg.get("id")
                    logging.info(f"📨 [RES] req_id={req_id}, ok={msg.get('ok')}, payload_keys={list(msg.get('payload', {}).keys())}")
                    if req_id in self._response_futures:
                        if msg.get("ok"):
                            run_id = msg["payload"].get("runId")
                            logging.info(f"📨 [RES] 映射 future: req_id={req_id} → runId={run_id}")
                            if run_id:
                                # 移交 Future 拥有权给 runId
                                future = self._response_futures.pop(req_id)
                                self._response_futures[run_id] = future
                        else:
                            # chat.send 被拒绝 — 立即返回错误
                            error_info = msg.get("error", msg.get("payload", {}))
                            logging.error(f"❌ chat.send 被拒绝: {error_info}")
                            if not self._response_futures[req_id].done():
                                self._response_futures[req_id].set_result(f"Gateway rejected: {error_info}")
                        
        except websockets.exceptions.ConnectionClosed:
            logging.warning("⚠️ OpenClaw Bridge 远端连接已被挂断")
        except Exception as e:
            logging.error(f"❌ 侦听线程崩溃: {e}")
        finally:
            self._connected_event.clear()
            self.ws = None
            # Schedule reconnection attempt in the background
            if self._reconnect_task is None or self._reconnect_task.done():
                self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        """Attempt to reconnect every RECONNECT_INTERVAL seconds, up to MAX_RECONNECT_RETRIES times."""
        for attempt in range(1, self.MAX_RECONNECT_RETRIES + 1):
            logging.info(f"🔄 [重连] 第 {attempt}/{self.MAX_RECONNECT_RETRIES} 次尝试重连 OpenClaw Gateway...")
            await asyncio.sleep(self.RECONNECT_INTERVAL)
            success = await self.connect()
            if success:
                logging.info("✅ [重连] OpenClaw Bridge 重连成功")
                return
        logging.error(f"❌ [重连] {self.MAX_RECONNECT_RETRIES} 次重连均失败，放弃。")

    async def health_check(self):
        """Return a dict with connection status, readable by the web dashboard."""
        return {
            "connected": self._connected_event.is_set(),
            "gateway_url": self.gateway_url,
            "pending_futures": len(self._response_futures)
        }

    async def ask(self, text, timeout=60, generate_new_session=True):
        """高度封装的对外大模型询问 API (自带 Context 斩断)"""
        if not self._connected_event.is_set():
            success = await self.connect()
            if not success:
                return "Failed to connect to gateway"

        req_id = "chat-" + str(uuid.uuid4())
        # 【架构级斩断死锁】每次调用随机生成一个仅存活一轮的 8 位 UUID Session
        # 使得大模型完全处于“无记忆”、“快准狠”的状态，彻底杜绝历史残留导致的假死
        current_session = f"agent:main:{str(uuid.uuid4().hex[:8])}" if generate_new_session else "agent:main:persist"
        
        future = asyncio.get_event_loop().create_future()
        self._response_futures[req_id] = future
        
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
        await self.ws.send(json.dumps(rpc_probe))
        
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._response_futures.pop(req_id, None)
            return "Timeout error while waiting for OpenClaw Gateway"

    async def deliver(self, text, channel="feishu", timeout=30):
        """用 urllib 回退至纯正的 Webhook 直连机制，因为 WSL 侧的部分通道 Auth 已过期"""
        webhook_url = os.environ.get("FEISHU_WEBHOOK", "")
        if channel == "feishu" and webhook_url:
            import urllib.request
            import ssl
            data = json.dumps({
                "msg_type": "text",
                "content": {"text": text}
            }).encode("utf-8")
            req = urllib.request.Request(webhook_url, data=data, headers={'Content-Type': 'application/json'})
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                await asyncio.get_event_loop().run_in_executor(
                    None, 
                    lambda: urllib.request.urlopen(req, timeout=timeout, context=ctx)
                )
                logging.info(f"📤 消息已成功投递到 Webhook 飞书群!")
                return True
            except Exception as e:
                logging.error(f"❌ Webhook投递失败: {e}")
                return False
        else:
            logging.warning("⚠️ 未配置局域网 FEISHU_WEBHOOK，或者走其他的channel，跳过推送。")
            return False

    async def ask_with_memory(self, text, memory_context="", timeout=60):
        """有状态对话：注入 SOUL.md 人格 + 训练记忆上下文，使用固定 Session 保持连贯性"""
        full_prompt = memory_context + "\n\n" + text if memory_context else text
        # 日报模式使用固定 session，保持对话连贯性
        return await self.ask(full_prompt, timeout=timeout, generate_new_session=False)


if __name__ == "__main__":
    async def run_demo():
        bridge = OpenClawBridge()
        print("🧠 初始化 OpenClaw 桥接器...")
        reply = await bridge.ask("我的深蹲腿分得不够开，这对膝盖有损伤吗？请用精练的语言指出风险。")
        print(f"教练回复: {reply}")
    asyncio.run(run_demo())
