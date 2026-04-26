"""V7.30 voice router — Tier A regex fallback + Tier B DeepSeek tool calling.

Replaces the 414-line if-chain in voice_daemon._try_voice_command with a
two-tier dispatch:

    Tier A (INSTANT_FALLBACK)  — millisecond regex match for offline-safe
                                  intents (mute / cancel / stop). Network
                                  outage doesn't break critical commands.

    Tier B (DeepSeek tool call) — for everything else. The model picks
                                  one of 8 tools or returns a chat reply.
                                  Implicit ack: dispatch and speak the
                                  ack template; no triple-confirm.

The router itself is pure (no Flask / no /dev/shm). Side effects are
delegated to the `Action` returned to the caller, who decides how to
realise them (write shm, speak, etc).

Python 3.7 compatible.
"""
import json
import logging
from collections import namedtuple

from hardware_engine.voice.tools import TOOLS, TOOL_ACK, DISPLAY_NAMES


# Action returned by handle_user_text:
#   kind="speak"     → just speak `text`, nothing else
#   kind="tool"      → execute tool with `args`, also speak ack `text`
#   kind="silent"    → no spoken response (e.g. report_status fills it later)
Action = namedtuple("Action", ["kind", "text", "tool_name", "args"])


def speak_action(text):
    return Action(kind="speak", text=text, tool_name="", args={})


def tool_action(name, args, ack_text):
    return Action(kind="tool", text=ack_text, tool_name=name, args=args)


def silent_action():
    return Action(kind="silent", text="", tool_name="", args={})


# ---- Tier A handlers ---------------------------------------------------------
def _handle_mute():
    return tool_action("set_mute", {"muted": True}, u"好，静音")


def _handle_unmute():
    return tool_action("set_mute", {"muted": False}, u"好，开始")


def _handle_stop():
    return tool_action("stop_speaking", {}, u"")


def _handle_cancel():
    return speak_action(u"好，取消")


INSTANT_FALLBACK = [
    # NOTE: order matters — longer / more specific patterns first to avoid
    # substring shadowing ("静音" would otherwise eat "解除静音").
    (u"解除静音", _handle_unmute),
    (u"可以说话", _handle_unmute),
    (u"静音", _handle_mute),
    (u"闭嘴", _handle_mute),
    (u"停", _handle_stop),
    (u"取消", _handle_cancel),
    (u"不对", _handle_cancel),
]


def _tier_a(text):
    for keyword, fn in INSTANT_FALLBACK:
        if keyword in text:
            logging.info(u"[ROUTER] Tier A match: %s", keyword)
            return fn()
    return None


# ---- Tier B: DeepSeek tool dispatch -----------------------------------------
def _format_ack(tool_name, args):
    template = TOOL_ACK.get(tool_name)
    if template is None:
        return u""
    if tool_name in ("switch_exercise", "switch_vision_mode", "switch_inference_backend"):
        key = (
            args.get("action")
            or args.get("mode")
            or args.get("backend")
            or ""
        )
        display = DISPLAY_NAMES.get(key, key)
        return template.format(display_name=display)
    if tool_name == "set_fatigue_limit":
        return template.format(value=args.get("value", "?"))
    return template


def _dispatch_tool_call(tc):
    """Convert a raw DeepSeek tool_call dict to an Action."""
    fn_block = tc.get("function") or {}
    name = fn_block.get("name", "")
    raw_args = fn_block.get("arguments") or "{}"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
    except ValueError:
        logging.warning(u"[ROUTER] tool_call args parse failed: %s", raw_args)
        args = {}
    if not name:
        return None
    ack = _format_ack(name, args)
    return tool_action(name, args, ack)


# ---- Public entry point ------------------------------------------------------
NEUTRAL_PROMPT = (
    u"你是 IronBuddy 健身教练。"
    u"回答简短自然：3 句话以内，80 字以内，不用 markdown。"
    u"当前用户的训练实况会作为上下文给你（动作类型/达标数/违规数/疲劳值），可参考但不强求引用。"
    u"当用户问健身建议时，给专业、具体的建议，不预设用户偏好。"
    u"你不能执行系统命令；如果用户表达类似指令意图，回复" \
    u"\"这条指令请直接对系统说，例如 切到深蹲\"。"
)


def handle_user_text(text, deepseek_client, system_prompt=None):
    """Route user text to Tier A or Tier B; return one Action.

    deepseek_client must expose chat_with_tools(system, user, tools).
    Pass None to disable Tier B (offline mode); router falls back to a
    static "听不懂" reply.
    """
    if not text or not text.strip():
        return silent_action()

    a = _tier_a(text)
    if a is not None:
        return a

    if deepseek_client is None:
        return speak_action(u"听不懂，再说一次")

    sys_prompt = system_prompt or NEUTRAL_PROMPT
    resp = deepseek_client.chat_with_tools(sys_prompt, text, TOOLS)
    if resp.has_tool_call:
        # Take the first tool call only — multi-call sequencing not needed
        # for the demo flow and complicates ack ordering.
        action = _dispatch_tool_call(resp.tool_calls[0])
        if action is not None:
            return action
    if resp.content:
        return speak_action(resp.content)
    return speak_action(u"听不懂，再说一次")
