"""V7.30 Phase 3 auto fatigue trigger chain tests (FSM write + voice watcher).

AST + source-text checks: live behavior depends on FSM running with simulator
and voice_daemon running on a board, both deferred to manual session.
"""
import ast
import os

MAIN_LOOP = os.path.join(os.path.dirname(__file__), "..",
                           "hardware_engine", "main_claw_loop.py")
VOICE_DAEMON = os.path.join(os.path.dirname(__file__), "..",
                              "hardware_engine", "voice_daemon.py")


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def test_main_claw_loop_writes_auto_trigger():
    src = _read(MAIN_LOOP)
    assert "/dev/shm/auto_trigger.json" in src
    assert "auto_trigger.json.tmp" in src


def test_auto_trigger_payload_includes_reason_and_stats():
    src = _read(MAIN_LOOP)
    idx = src.find("_auto_trigger_payload")
    assert idx >= 0
    block = src[idx:idx + 600]
    for field in ('"reason"', '"good"', '"failed"', '"comp"', '"fatigue"'):
        assert field in block, "expected %s in auto_trigger payload" % field


def test_auto_trigger_uses_fatigue_max_label():
    src = _read(MAIN_LOOP)
    assert '"fatigue_max"' in src or "'fatigue_max'" in src


def test_voice_daemon_has_auto_trigger_watcher():
    src = _read(VOICE_DAEMON)
    assert "def _auto_trigger_watcher():" in src


def test_auto_trigger_watcher_thread_started():
    src = _read(VOICE_DAEMON)
    assert "target=_auto_trigger_watcher" in src


def test_auto_trigger_watcher_transitions_to_busy():
    src = _read(VOICE_DAEMON)
    idx = src.find("def _auto_trigger_watcher():")
    end = src.find("\ndef ", idx + 1)
    body = src[idx:end]
    assert "VoiceState.BUSY" in body
    assert "_arecord_gate.suspend()" in body
    assert '_start_turn(stage="auto"' in body


def test_auto_trigger_watcher_consumes_file():
    """Watcher must rm /dev/shm/auto_trigger.json after handling
    (else next loop sees the same mtime and re-fires)."""
    src = _read(VOICE_DAEMON)
    idx = src.find("def _auto_trigger_watcher():")
    end = src.find("\ndef ", idx + 1)
    body = src[idx:end]
    assert "os.remove(path)" in body


def test_modules_still_parse():
    ast.parse(_read(MAIN_LOOP))
    ast.parse(_read(VOICE_DAEMON))
