"""V7.30 R3 race fix: streamer dual-writes canonical + intent files."""
import os
import re

STREAMER = os.path.join(os.path.dirname(__file__), "..", "streamer_app.py")


def _src():
    with open(STREAMER, "r", encoding="utf-8") as f:
        return f.read()


def _block(name):
    src = _src()
    idx = src.find("def %s(" % name)
    end = src.find("\n@app.route", idx + 1)
    if end == -1:
        end = src.find("\ndef ", idx + 1)
    return src[idx:end]


def test_atomic_write_helper_exists():
    src = _src()
    assert "def _atomic_write_json(" in src


def test_exercise_mode_writes_canonical_and_intent():
    body = _block("api_exercise_mode")
    assert "/dev/shm/exercise_mode.json" in body
    assert "/dev/shm/intent_exercise_mode.json" in body


def test_inference_mode_writes_canonical_and_intent():
    body = _block("api_switch_inference_mode")
    assert "/dev/shm/inference_mode.json" in body
    assert "/dev/shm/intent_inference_mode.json" in body


def test_fatigue_limit_writes_canonical_and_intent_and_ui():
    body = _block("api_fatigue_limit")
    assert "/dev/shm/fatigue_limit.json" in body
    assert "/dev/shm/intent_fatigue_limit.json" in body
    assert "/dev/shm/ui_fatigue_limit.json" in body


def test_intent_writes_carry_src_field():
    """Source-of-truth tagging: intent writes carry src='ui' so a future
    FSM watcher can distinguish ui-originated requests from voice-originated."""
    src = _src()
    # Three sites must include `"src": "ui"` literal
    count = src.count('"src": "ui"')
    assert count >= 3
