"""V7.30 _realize_action adapter test (mocks _write_signal + speak).

Cannot import voice_daemon directly (libasound + baidu deps), so we
extract the adapter via exec into a sandboxed namespace with stubs.
"""
import os
import sys
import time
from collections import namedtuple
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def adapter_ns():
    """Load _realize_action + _format_status_report from voice_daemon source.

    Skips the heavy imports (libasound / baidu) by extracting only the
    function definitions from the bottom of the file into a fresh module.
    """
    src_path = os.path.join(os.path.dirname(__file__), "..",
                              "hardware_engine", "voice_daemon.py")
    with open(src_path, "r", encoding="utf-8") as f:
        src = f.read()
    start = src.find("def _realize_action")
    end = src.find('if __name__ == "__main__":')
    snippet = src[start:end]

    # Provide the namespace stubs the adapter needs.
    ns = {
        "logging": __import__("logging"),
        "time": time,
        "json": __import__("json"),
        "_write_signal": mock.MagicMock(name="_write_signal"),
    }
    exec(snippet, ns)
    return ns


@pytest.fixture
def Action():
    return namedtuple("Action", ["kind", "text", "tool_name", "args"])


def test_silent_action_is_noop(adapter_ns, Action):
    speak = mock.MagicMock()
    out = adapter_ns["_realize_action"](
        Action("silent", "", "", {}), speak_fn=speak)
    assert out is False
    speak.assert_not_called()
    adapter_ns["_write_signal"].assert_not_called()


def test_speak_action_invokes_speak(adapter_ns, Action):
    speak = mock.MagicMock()
    adapter_ns["_realize_action"](Action("speak", u"你好", "", {}), speak_fn=speak)
    speak.assert_called_once_with(u"你好")


def test_set_mute_writes_mute_signal(adapter_ns, Action):
    adapter_ns["_realize_action"](
        Action("tool", u"好，静音", "set_mute", {"muted": True}),
        speak_fn=mock.MagicMock(),
    )
    args = adapter_ns["_write_signal"].call_args
    assert args[0][0] == "/dev/shm/mute_signal.json"
    assert args[0][1]["muted"] is True


def test_switch_exercise_squat_writes_two_signals(adapter_ns, Action):
    adapter_ns["_realize_action"](
        Action("tool", u"好，切到深蹲", "switch_exercise", {"action": "squat"}),
        speak_fn=mock.MagicMock(),
    )
    paths = [c[0][0] for c in adapter_ns["_write_signal"].call_args_list]
    assert "/dev/shm/exercise_mode.json" in paths
    assert "/dev/shm/user_profile.json" in paths


def test_switch_exercise_curl_maps_to_bicep_curl_profile(adapter_ns, Action):
    adapter_ns["_realize_action"](
        Action("tool", u"好，切到弯举", "switch_exercise", {"action": "curl"}),
        speak_fn=mock.MagicMock(),
    )
    profile_calls = [c for c in adapter_ns["_write_signal"].call_args_list
                     if c[0][0] == "/dev/shm/user_profile.json"]
    assert profile_calls
    assert profile_calls[0][0][1]["exercise"] == "bicep_curl"


def test_switch_vision_mode_writes_inference_mode(adapter_ns, Action):
    adapter_ns["_realize_action"](
        Action("tool", "", "switch_vision_mode", {"mode": "vision_sensor"}),
        speak_fn=mock.MagicMock(),
    )
    args = adapter_ns["_write_signal"].call_args
    assert args[0][0] == "/dev/shm/inference_mode.json"
    assert args[0][1]["mode"] == "vision_sensor"


def test_switch_inference_backend_writes_vision_mode(adapter_ns, Action):
    adapter_ns["_realize_action"](
        Action("tool", "", "switch_inference_backend", {"backend": "cloud_gpu"}),
        speak_fn=mock.MagicMock(),
    )
    args = adapter_ns["_write_signal"].call_args
    assert args[0][0] == "/dev/shm/vision_mode.json"
    assert args[0][1]["mode"] == "cloud_gpu"


def test_set_fatigue_limit_clamps_out_of_range(adapter_ns, Action):
    adapter_ns["_realize_action"](
        Action("tool", "", "set_fatigue_limit", {"value": 50}),
        speak_fn=mock.MagicMock(),
    )
    adapter_ns["_write_signal"].assert_not_called()


def test_set_fatigue_limit_writes_when_in_range(adapter_ns, Action):
    adapter_ns["_realize_action"](
        Action("tool", "", "set_fatigue_limit", {"value": 1500}),
        speak_fn=mock.MagicMock(),
    )
    args = adapter_ns["_write_signal"].call_args
    assert args[0][0] == "/dev/shm/fatigue_limit.json"
    assert args[0][1]["limit"] == 1500


def test_start_mvc_writes_auto_mvc(adapter_ns, Action):
    adapter_ns["_realize_action"](
        Action("tool", "", "start_mvc_calibrate", {}),
        speak_fn=mock.MagicMock(),
    )
    args = adapter_ns["_write_signal"].call_args
    assert args[0][0] == "/dev/shm/auto_mvc.json"


def test_push_feishu_writes_auto_trigger(adapter_ns, Action):
    adapter_ns["_realize_action"](
        Action("tool", "", "push_feishu_summary", {}),
        speak_fn=mock.MagicMock(),
    )
    args = adapter_ns["_write_signal"].call_args
    assert args[0][0] == "/dev/shm/auto_trigger.json"
    assert args[0][1]["reason"] == "feishu_summary"


def test_shutdown_writes_shutdown_signal(adapter_ns, Action):
    adapter_ns["_realize_action"](
        Action("tool", "", "shutdown", {}),
        speak_fn=mock.MagicMock(),
    )
    args = adapter_ns["_write_signal"].call_args
    assert args[0][0] == "/dev/shm/shutdown.json"


def test_unknown_tool_no_signal(adapter_ns, Action):
    adapter_ns["_realize_action"](
        Action("tool", "", "definitely_not_a_tool", {}),
        speak_fn=mock.MagicMock(),
    )
    adapter_ns["_write_signal"].assert_not_called()


def test_format_status_report_handles_missing_file(adapter_ns):
    with mock.patch("builtins.open", side_effect=OSError("no")):
        out = adapter_ns["_format_status_report"]()
    assert out == u"暂无训练数据"


def test_format_status_report_renders_data(adapter_ns):
    payload = '{"good": 10, "failed": 2, "comp": 1, "fatigue": 850}'
    with mock.patch("builtins.open", mock.mock_open(read_data=payload)):
        out = adapter_ns["_format_status_report"]()
    assert u"10" in out
    assert u"850" in out


def test_switch_exercise_curl_emits_mvc_followup_tip(adapter_ns, Action):
    """V7.30 Phase 3: switching to curl should suggest MVC calibration."""
    speak = mock.MagicMock()
    adapter_ns["_realize_action"](
        Action("tool", u"好，切到弯举", "switch_exercise", {"action": "curl"}),
        speak_fn=speak,
    )
    spoken_texts = [c.args[0] for c in speak.call_args_list]
    assert any(u"弯举" in t for t in spoken_texts)
    assert any(u"MVC" in t for t in spoken_texts)


def test_switch_exercise_squat_no_mvc_followup(adapter_ns, Action):
    """Switching to squat does NOT trigger the MVC suggestion."""
    speak = mock.MagicMock()
    adapter_ns["_realize_action"](
        Action("tool", u"好，切到深蹲", "switch_exercise", {"action": "squat"}),
        speak_fn=speak,
    )
    spoken_texts = [c.args[0] for c in speak.call_args_list]
    assert not any(u"MVC" in t for t in spoken_texts)
