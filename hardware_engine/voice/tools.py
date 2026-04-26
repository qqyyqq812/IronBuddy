"""V7.30 voice tool definitions for DeepSeek tool-calling.

8 tools (design doc §4.2). Schema is OpenAI tool-calling compatible
(DeepSeek uses the same shape).
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "switch_exercise",
            "description": "切换当前训练动作到深蹲或弯举",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["squat", "curl"],
                        "description": "目标动作：squat=深蹲，curl=弯举",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_vision_mode",
            "description": "切换视觉判定模式：纯视觉 vs 视觉+肌电融合",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["pure_vision", "vision_sensor"],
                    },
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_inference_backend",
            "description": "切换推理后端：本地 NPU 或云端 GPU",
            "parameters": {
                "type": "object",
                "properties": {
                    "backend": {
                        "type": "string",
                        "enum": ["local_npu", "cloud_gpu"],
                    },
                },
                "required": ["backend"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_fatigue_limit",
            "description": "设置自动总结的疲劳上限阈值",
            "parameters": {
                "type": "object",
                "properties": {
                    "value": {
                        "type": "integer",
                        "minimum": 100,
                        "maximum": 5000,
                    },
                },
                "required": ["value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_mvc_calibrate",
            "description": "启动 MVC（最大自主收缩）肌电校准流程",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "push_feishu_summary",
            "description": "把当前训练总结推送到飞书",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shutdown",
            "description": "关闭整个 IronBuddy 系统",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "report_status",
            "description": "口头报告当前训练状态（动作/达标数/违规数/疲劳值）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# Implicit-ack templates: spoken back to user *immediately* on tool dispatch.
# Keep them short — design north star is "拍摄成功 + 拿奖", no triple confirm.
TOOL_ACK = {
    "switch_exercise":
        u"好，切到{display_name}",
    "switch_vision_mode":
        u"好，切到{display_name}模式",
    "switch_inference_backend":
        u"好，推理切到{display_name}",
    "set_fatigue_limit":
        u"好，疲劳上限设为{value}",
    "start_mvc_calibrate":
        u"好，开始 MVC 校准",
    "push_feishu_summary":
        u"好，已推送到飞书",
    "shutdown":
        u"好，系统将关闭",
    "report_status":
        u"",  # report_status 直接读出状态，无需 ack
}


# Display name resolution for ack templates.
DISPLAY_NAMES = {
    "squat": u"深蹲",
    "curl": u"弯举",
    "pure_vision": u"纯视觉",
    "vision_sensor": u"视觉肌电融合",
    "local_npu": u"本地 NPU",
    "cloud_gpu": u"云端 GPU",
}
