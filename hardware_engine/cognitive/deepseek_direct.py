"""
DeepSeek Direct Connection — 绕过 WebSocket Gateway，直连 DeepSeek API
使用 raw requests + SSE 流式解析，兼容 Python 3.7+（板端无 openai SDK）
提供与 OpenClawBridge 相同的 ask() 接口，支持 conversation history
"""
import os
import time
import json
import logging
import collections
import asyncio

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


class DeepSeekDirect(object):
    """
    直连 DeepSeek API 的轻量级桥接器。
    接口与 OpenClawBridge 对齐：ask(text, timeout) -> str
    """

    def __init__(self, api_key=None, base_url="https://api.deepseek.com",
                 model="deepseek-chat", soul_text=""):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.soul_text = soul_text
        # 对话历史：最近 6 条消息（3 轮对话）
        self.history = collections.deque(maxlen=6)
        self._connected_event_flag = bool(self.api_key)

        if not self.api_key:
            logging.warning("DEEPSEEK_API_KEY 未配置，DeepSeekDirect 将不可用")

    # ---- 兼容 OpenClawBridge 的 health_check 接口 ----
    async def health_check(self):
        return {
            "connected": self._connected_event_flag,
            "gateway_url": self.base_url,
            "pending_futures": 0,
            "backend": "deepseek_direct"
        }

    # ---- 兼容 OpenClawBridge 的 connect 接口 ----
    async def connect(self):
        if not self.api_key:
            logging.error("DEEPSEEK_API_KEY 未设置，无法连接")
            return False
        # 直连模式无需 WebSocket 握手，直接标记就绪
        self._connected_event_flag = True
        logging.info("DeepSeek Direct 已就绪 (API Key: ...%s)", self.api_key[-6:])
        return True

    # ---- 核心：ask 接口（与 OpenClawBridge.ask 签名一致） ----
    async def ask(self, text, timeout=20, generate_new_session=True):
        if not self.api_key:
            return "DeepSeek API Key 未配置"
        if not HAS_REQUESTS:
            return "requests 库未安装，无法使用直连模式"

        # 构建消息列表
        messages = []
        if self.soul_text:
            messages.append({"role": "system", "content": self.soul_text})

        # generate_new_session=False 时保留历史（对话模式）
        if not generate_new_session:
            messages.extend(list(self.history))

        messages.append({"role": "user", "content": text})

        # 在线程池中执行同步 HTTP 请求，避免阻塞 asyncio 事件循环
        loop = asyncio.get_event_loop()
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._sync_chat, messages),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            return "DeepSeek Direct: 请求超时"
        except Exception as e:
            logging.error("DeepSeek Direct 异常: %s", e)
            return "DeepSeek Direct: 请求异常 (%s)" % str(e)

        # 更新对话历史
        if not generate_new_session and result:
            self.history.append({"role": "user", "content": text})
            self.history.append({"role": "assistant", "content": result})

        return result

    # ---- 兼容 ask_with_memory ----
    async def ask_with_memory(self, text, memory_context="", timeout=60):
        full_prompt = (memory_context + "\n\n" + text) if memory_context else text
        return await self.ask(full_prompt, timeout=timeout, generate_new_session=False)

    # ---- 飞书推送 (自建应用 API，非 webhook) ----
    async def deliver(self, text, channel="feishu", timeout=30):
        feishu_app_id = os.environ.get("FEISHU_APP_ID", "")
        feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", "")
        feishu_chat_id = os.environ.get("FEISHU_CHAT_ID", "")

        if channel == "feishu" and feishu_app_id and feishu_app_secret and feishu_chat_id:
            import urllib.request
            import ssl
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                loop = asyncio.get_event_loop()

                # Step 1: Get tenant_access_token
                token_data = json.dumps({
                    "app_id": feishu_app_id,
                    "app_secret": feishu_app_secret,
                }).encode("utf-8")
                token_req = urllib.request.Request(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    data=token_data,
                    headers={"Content-Type": "application/json"},
                )
                token_resp = await loop.run_in_executor(
                    None,
                    lambda: json.loads(urllib.request.urlopen(token_req, timeout=10, context=ctx).read()),
                )
                access_token = token_resp.get("tenant_access_token", "")
                if not access_token:
                    logging.error("飞书 token 获取失败: %s", token_resp)
                    return False

                # Step 2: Send message
                msg_data = json.dumps({
                    "receive_id": feishu_chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}),
                }).encode("utf-8")
                msg_req = urllib.request.Request(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    data=msg_data,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer " + access_token,
                    },
                )
                msg_resp = await loop.run_in_executor(
                    None,
                    lambda: json.loads(urllib.request.urlopen(msg_req, timeout=timeout, context=ctx).read()),
                )
                if msg_resp.get("code") == 0:
                    logging.info("飞书推送成功: message_id=%s", msg_resp.get("data", {}).get("message_id", ""))
                    return True
                else:
                    logging.error("飞书推送失败: %s", msg_resp)
                    return False
            except Exception as e:
                logging.error("飞书推送异常: %s", e)
                return False
        else:
            missing = []
            if not feishu_app_id:
                missing.append("FEISHU_APP_ID")
            if not feishu_app_secret:
                missing.append("FEISHU_APP_SECRET")
            if not feishu_chat_id:
                missing.append("FEISHU_CHAT_ID")
            logging.warning("飞书配置不完整 (缺少 %s)，跳过推送", ", ".join(missing))
            return False

    # ---- 同步 HTTP 请求（流式 SSE） ----
    def _sync_chat(self, messages, retries=2):
        """同步调用 DeepSeek chat/completions，使用流式 SSE 获取更快的首 token。
        加入超时/断线重试：指数退避 (2^attempt 秒)，避免瞬时网络抖动导致 _ds_lock 卡死。
        """
        url = self.base_url + "/chat/completions"
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 80,  # V7.10 限 80 tokens (~30 汉字), 提速+杜绝长篇
        }

        last_exc = None
        for attempt in range(retries):
            try:
                resp = requests.post(url, headers=headers, json=payload,
                                     stream=True, timeout=15)
                resp.raise_for_status()

                full_text = []
                for line in resp.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    # SSE 格式: "data: {...}" 或 "data: [DONE]"
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                full_text.append(content)
                        except (json.JSONDecodeError, IndexError, KeyError):
                            continue

                result = "".join(full_text)

                # 去除 <think>...</think> 推理块
                if "</think>" in result:
                    result = result.split("</think>")[-1].strip()

                return result
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_exc = exc
                if attempt < retries - 1:
                    wait = 2 ** attempt
                    logging.warning("[DeepSeek] 超时/断线, %ss 后重试 (%d/%d): %s",
                                    wait, attempt + 1, retries, exc)
                    time.sleep(wait)
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        return ""


if __name__ == "__main__":
    async def _demo():
        bridge = DeepSeekDirect()
        ok = await bridge.connect()
        if ok:
            reply = await bridge.ask("你好，简单介绍一下自己，20字以内。")
            print("回复:", reply)

    asyncio.run(_demo())
