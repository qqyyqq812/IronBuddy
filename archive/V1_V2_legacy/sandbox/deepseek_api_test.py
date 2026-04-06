import asyncio
import json
import uuid
import websockets

GATEWAY_URL = "ws://127.0.0.1:18789"
TOKEN = "67d0442e3a5e7aa3f4d5519cee1ac1a7d413b7062c7dc4c6"

async def test_websocket():
    headers = {"Authorization": f"Bearer {TOKEN}"}
    
    print(f"🚀 尝试连接 OpenClaw WebSocket 网关: {GATEWAY_URL}")
    try:
        async with websockets.connect(GATEWAY_URL, extra_headers=headers) as websocket:
            print("✅ 物理连接建立成功！等待握手 Challenge...")
            
            # Step 1: 接收 Challenge
            challenge_msg_raw = await websocket.recv()
            challenge_msg = json.loads(challenge_msg_raw)
            print(f"📥 收到入站事件: {json.dumps(challenge_msg, indent=2)}")
            
            if challenge_msg.get("event") == "connect.challenge":
                nonce = challenge_msg["payload"]["nonce"]
                # 按照 OpenClaw Chrome Extension 的标准，响应 challenge
                response_payload = {
                    "type": "req",
                    "id": "connect-1",
                    "method": "connect",
                    "params": {
                        "minProtocol": 3,
                        "maxProtocol": 3,
                        "client": {
                            "id": "cli",
                            "version": "1.0.0",
                            "platform": "linux",
                            "mode": "cli"
                        },
                        "role": "operator",
                        "scopes": ["operator", "operator.read", "operator.write"],
                        "caps": [],
                        "auth": {"token": TOKEN}
                    }
                }
                print("📤 发送挑战应答...")
                await websocket.send(json.dumps(response_payload))
                
                # Step 2: 验证应答是否通过（可能会收到 connect.ready 或错误）
                ready_msg_raw = await websocket.recv()
                print(f"📥 收到鉴权结果: {ready_msg_raw}")
                
            # Step 3: 发起测试级大模型 RPC 呼唤 (chat.send)
            rpc_probe = {
                "type": "req",
                "id": "probe-2",
                "method": "chat.send",
                "params": {
                    "sessionKey": "agent:main:main",
                    "message": "我的深蹲做的标准吗？请用15个字以内严厉回答。",
                    "idempotencyKey": str(uuid.uuid4())
                }
            }
            print(f"📤 发起 RPC 探测: {rpc_probe['method']}")
            await websocket.send(json.dumps(rpc_probe))
            
            # Step 4: 等待聊天回复结束
            while True:
                final_resp_raw = await websocket.recv()
                final_resp = json.loads(final_resp_raw)
                if final_resp.get("event") == "chat":
                    payload = final_resp.get("payload", {})
                    state = payload.get("state")
                    msg = payload.get("message", {})
                    
                    if state == "delta":
                        # 增量
                        if "text" in msg:
                            print(msg["text"], end="", flush=True)
                    elif state == "final":
                        if "text" in msg:
                            print(msg["text"])
                        print("\n🎉 对话结束！")
                        break
                    elif state == "error":
                        print(f"\n❌ 对话错误: {payload}")
                        break
                elif final_resp.get("type") == "res" and final_resp.get("id") == "probe-2":
                    print(f"✓ 收到 RPC 请求回执: {final_resp}")
                else:
                    # ignore heartbeat
                    pass
            
    except Exception as e:
        print(f"❌ 发生致命错误: {e}")

if __name__ == "__main__":
    asyncio.run(test_websocket())
