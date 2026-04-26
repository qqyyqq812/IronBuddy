"""V7.30 voice_daemon integration test.

Verifies that the voice/state, voice/recorder, voice/turn modules
have been wired into voice_daemon.py without breaking the import chain.

Cannot import voice_daemon.py itself (depends on baidu-aip + libasound),
so we use AST + textual checks.
"""
import ast
import os

VOICE_DAEMON = os.path.join(
    os.path.dirname(__file__), "..", "hardware_engine", "voice_daemon.py"
)


def _src():
    with open(VOICE_DAEMON, "r", encoding="utf-8") as f:
        return f.read()


def test_voice_daemon_parses():
    ast.parse(_src())


def test_imports_state_recorder_turn():
    s = _src()
    assert "from hardware_engine.voice.state import" in s
    assert "from hardware_engine.voice.recorder import" in s
    assert "from hardware_engine.voice.turn import" in s


def test_singletons_defined():
    s = _src()
    assert "_voice_sm = VoiceStateMachine()" in s
    assert "_arecord_gate = ArecordGate()" in s
    assert "_turn_writer = TurnWriter()" in s


def test_dialog_enter_drives_state_machine():
    s = _src()
    enter_idx = s.find("def _dialog_enter():")
    enter_end = s.find("def _dialog_exit():")
    enter_body = s[enter_idx:enter_end]
    assert "_voice_sm.transition(VoiceState.DIALOG" in enter_body


def test_dialog_exit_drives_state_machine_and_closes_turn():
    s = _src()
    exit_idx = s.find("def _dialog_exit():")
    next_def_idx = s.find("def ", exit_idx + 1)
    exit_body = s[exit_idx:next_def_idx]
    assert "_close_turn()" in exit_body
    assert "_voice_sm.transition(VoiceState.LISTEN" in exit_body


def test_wake_detection_starts_turn():
    s = _src()
    # before _dialog_enter() in main loop, _start_turn(stage="wake") must fire
    enter_call_idx = s.find("        _dialog_enter()", s.find("if not is_wake"))
    pre = s[max(0, enter_call_idx - 200) : enter_call_idx]
    assert '_start_turn(stage="wake")' in pre


def test_publish_chat_input_emits_user_input():
    s = _src()
    pubrange_start = s.find("def _publish_chat_input_raw")
    pubrange_end = s.find("def ", pubrange_start + 1)
    body = s[pubrange_start:pubrange_end]
    assert '_emit_turn_stage("user_input"' in body


def test_publish_chat_reply_emits_assistant_reply():
    s = _src()
    pubrange_start = s.find("def _publish_chat_reply")
    pubrange_end = s.find("def ", pubrange_start + 1)
    body = s[pubrange_start:pubrange_end]
    assert '_emit_turn_stage("assistant_reply"' in body


def test_active_speech_cap_break_in_record_with_vad():
    s = _src()
    rec_start = s.find("def record_with_vad")
    rec_end = s.find("\ndef ", rec_start + 1)
    body = s[rec_start:rec_end]
    assert "ACTIVE_SPEECH_CAP" in body
    assert "speech_start" in body


def test_main_applies_vad_config():
    s = _src()
    main_start = s.find("def main():")
    init_baidu_idx = s.find("client = _init_baidu()")
    init_block = s[main_start:init_baidu_idx]
    assert "VADConfig()" in init_block
    assert "apply_to_voice_daemon" in init_block
