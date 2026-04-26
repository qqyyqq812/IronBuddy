"""Tests for hardware_engine.voice.router — Tier A regex + Tier B tool dispatch."""
import json
import os
import sys
from collections import namedtuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hardware_engine.voice.router import (
    handle_user_text, INSTANT_FALLBACK, _format_ack, _dispatch_tool_call,
    speak_action, tool_action, silent_action,
)
from hardware_engine.voice.tools import TOOLS, TOOL_ACK, DISPLAY_NAMES


class _FakeToolResponse(object):
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.has_tool_call = bool(self.tool_calls)


class _FakeClient(object):
    def __init__(self, response):
        self.response = response
        self.last_call = None

    def chat_with_tools(self, system, user, tools):
        self.last_call = (system, user, tools)
        return self.response


def test_empty_input_returns_silent():
    a = handle_user_text("", _FakeClient(_FakeToolResponse()))
    assert a.kind == "silent"


def test_whitespace_only_returns_silent():
    a = handle_user_text("   \n\t  ", _FakeClient(_FakeToolResponse()))
    assert a.kind == "silent"


def test_tier_a_mute_keyword_matches_before_deepseek():
    client = _FakeClient(_FakeToolResponse())
    a = handle_user_text(u"静音", client)
    assert a.kind == "tool"
    assert a.tool_name == "set_mute"
    assert a.args == {"muted": True}
    assert client.last_call is None


def test_tier_a_unmute():
    a = handle_user_text(u"解除静音", _FakeClient(_FakeToolResponse()))
    assert a.tool_name == "set_mute"
    assert a.args == {"muted": False}


def test_tier_a_cancel_speaks():
    a = handle_user_text(u"取消", _FakeClient(_FakeToolResponse()))
    assert a.kind == "speak"


def test_tier_a_stop_silent_ack():
    a = handle_user_text(u"停", _FakeClient(_FakeToolResponse()))
    assert a.tool_name == "stop_speaking"
    assert a.text == u""


def test_tier_b_tool_call_dispatched():
    tc = {
        "function": {
            "name": "switch_exercise",
            "arguments": json.dumps({"action": "squat"}),
        }
    }
    client = _FakeClient(_FakeToolResponse(tool_calls=[tc]))
    a = handle_user_text(u"切到深蹲", client)
    assert a.kind == "tool"
    assert a.tool_name == "switch_exercise"
    assert a.args == {"action": "squat"}
    assert u"深蹲" in a.text


def test_tier_b_tool_call_with_args_dict():
    tc = {
        "function": {
            "name": "set_fatigue_limit",
            "arguments": {"value": 800},
        }
    }
    client = _FakeClient(_FakeToolResponse(tool_calls=[tc]))
    a = handle_user_text(u"疲劳上限设为800", client)
    assert a.tool_name == "set_fatigue_limit"
    assert a.args == {"value": 800}
    assert u"800" in a.text


def test_tier_b_no_tool_falls_back_to_chat_content():
    client = _FakeClient(_FakeToolResponse(content=u"今天好好训练吧"))
    a = handle_user_text(u"今天天气怎样", client)
    assert a.kind == "speak"
    assert u"训练" in a.text


def test_tier_b_empty_response_returns_default():
    client = _FakeClient(_FakeToolResponse())
    a = handle_user_text(u"今天天气怎样", client)
    assert a.kind == "speak"
    assert a.text == u"听不懂，再说一次"


def test_no_client_returns_static_speak():
    a = handle_user_text(u"今天天气怎样", None)
    assert a.kind == "speak"
    assert a.text == u"听不懂，再说一次"


def test_format_ack_uses_display_name_for_squat():
    out = _format_ack("switch_exercise", {"action": "squat"})
    assert u"深蹲" in out


def test_format_ack_uses_display_name_for_pure_vision():
    out = _format_ack("switch_vision_mode", {"mode": "pure_vision"})
    assert u"纯视觉" in out


def test_format_ack_set_fatigue():
    out = _format_ack("set_fatigue_limit", {"value": 1500})
    assert u"1500" in out


def test_format_ack_unknown_tool_returns_empty():
    assert _format_ack("totally_made_up", {}) == u""


def test_format_ack_report_status_returns_empty():
    assert _format_ack("report_status", {}) == u""


def test_dispatch_tool_call_handles_malformed_args():
    tc = {"function": {"name": "switch_exercise", "arguments": "not-json"}}
    a = _dispatch_tool_call(tc)
    assert a.tool_name == "switch_exercise"
    assert a.args == {}


def test_dispatch_tool_call_drops_nameless():
    tc = {"function": {"arguments": "{}"}}
    a = _dispatch_tool_call(tc)
    assert a is None


def test_tools_spec_has_8_tools():
    assert len(TOOLS) == 8


def test_tool_ack_covers_all_tools():
    names = [t["function"]["name"] for t in TOOLS]
    for n in names:
        assert n in TOOL_ACK


def test_display_names_include_all_enums():
    for key in ["squat", "curl", "pure_vision", "vision_sensor", "local_npu", "cloud_gpu"]:
        assert key in DISPLAY_NAMES
