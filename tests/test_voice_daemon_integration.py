"""Voice daemon integration checks using AST/source text.

The live module depends on board audio and baidu-aip, so tests avoid importing
it directly.
"""
import ast
import os


VOICE_DAEMON = os.path.join(
    os.path.dirname(__file__), "..", "hardware_engine", "voice_daemon.py"
)


def _src():
    with open(VOICE_DAEMON, "r", encoding="utf-8") as f:
        return f.read()


def _function_body(src, name):
    marker = "def %s" % name
    start = src.find(marker)
    assert start >= 0, "missing %s" % name
    end = src.find("\ndef ", start + 1)
    return src[start:end if end >= 0 else len(src)]


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
    body = _function_body(_src(), "_dialog_enter():")
    assert "_voice_sm.transition(VoiceState.DIALOG" in body


def test_dialog_exit_drives_state_machine_and_closes_turn():
    body = _function_body(_src(), "_dialog_exit():")
    assert "_close_turn()" in body
    assert "_voice_sm.transition(VoiceState.LISTEN" in body


def test_wake_detection_starts_turn():
    s = _src()
    wake_idx = s.find("[\\u5524\\u9192] \\u547d\\u4e2d")
    assert wake_idx >= 0
    block = s[wake_idx:wake_idx + 500]
    assert '_start_turn(stage="wake")' in block
    assert "_dialog_enter()" in block


def test_publish_chat_input_emits_user_input():
    body = _function_body(_src(), "_publish_chat_input_raw")
    assert '_emit_turn_stage("user_input"' in body


def test_publish_chat_reply_emits_assistant_reply():
    body = _function_body(_src(), "_publish_chat_reply")
    assert '_emit_turn_stage("assistant_reply"' in body


def test_active_speech_cap_break_in_record_with_vad():
    body = _function_body(_src(), "record_with_vad")
    assert "ACTIVE_SPEECH_CAP" in body
    assert "speech_start" in body


def test_main_applies_vad_config():
    s = _src()
    main_start = s.find("def main():")
    init_baidu_idx = s.find("client = _init_baidu()")
    init_block = s[main_start:init_baidu_idx]
    assert "VADConfig()" in init_block
    assert "apply_to_voice_daemon" in init_block


def test_wake_second_listen_uses_primary_vad_slot_and_quiet_idle():
    s = _src()
    wake_idx = s.find("[WakeOnly] second_record_start")
    wake_end = s.find('_kill_active_recording("two_step_done")', wake_idx)
    wake_block = s[wake_idx:wake_end]
    assert '_primary_record_with_vad(' in wake_block
    assert '"wake_second"' in wake_block
    assert '_finish_quiet_turn("second_no_speech_idle")' in wake_block
    assert '_finish_unclear_turn("second_no_speech_idle")' not in wake_block


def test_barge_watchers_skip_dialog_and_primary_listen():
    s = _src()
    wake_body = _function_body(s, "_barge_wake_watcher")
    mute_body = _function_body(s, "_barge_mute_watcher")
    for body in (wake_body, mute_body):
        assert "_dialog_active_safe()" in body
        assert "_primary_listen_active.is_set()" in body
        assert "_barge_record_with_vad(timeout=2)" in body
        assert "record_with_vad(timeout=2, fast_start=True)" not in body


def test_primary_vad_slot_serializes_against_barge_vad():
    s = _src()
    assert "_primary_listen_active = threading.Event()" in s
    assert "_vad_call_lock = threading.Lock()" in s
    assert "def _barge_record_with_vad" in s
    assert "def _primary_record_with_vad" in s
    assert "_vad_call_lock.acquire(False)" in s
    assert "_vad_call_lock.acquire()" in s


def test_tts_uses_wav_synthesis_and_explicit_16k_wav_playback():
    s = _src()
    assert "'aue': 6" in s
    assert "'aue': 4" not in s

    for fn_name in ("_launch_aplay", "play_audio"):
        body = _function_body(s, fn_name)
        assert '"-t", "wav"' in body
        assert '"-r", "16000"' in body
        assert '"-f", "S16_LE"' in body
        assert '"-c", "1"' in body


def test_barge_mute_watcher_disabled_by_default_for_audio_stability():
    s = _src()
    assert (
        'ENABLE_BARGE_MUTE_WATCHER = os.environ.get("VOICE_ENABLE_BARGE_MUTE", "0")'
        in s
    )
    main_body = _function_body(s, "main")
    assert "if ENABLE_BARGE_MUTE_WATCHER:" in main_body
    assert "threading.Thread(target=_barge_mute_watcher" in main_body


def test_daily_plan_voice_intent_is_product_layer_only():
    s = _src()
    assert "def _try_daily_plan_voice_command" in s
    assert "/api/training_plan/daily" in s
    assert "/api/training_plan/daily/accept" in s
    assert "_try_daily_plan_voice_command(text)" in s

    body = _function_body(s, "_try_daily_plan_voice_command")
    assert "_post_streamer_json" in body
    assert "daily_plan_generate" in body
    assert "daily_plan_accept" in body
    # Guard the fragile playback path: planning intent must not edit ALSA/aplay.
    assert "aplay" not in body
    assert "amixer" not in body
    assert "Playback Path" not in body


def test_mvc_is_removed_from_voice_layer():
    s = _src()
    assert "def _is_mvc_start_intent" not in s
    assert "_mcv_wait_until" not in s
    assert "/dev/shm/auto_mvc.json" not in s
    assert "/dev/shm/mvc_calibrate.request" not in s
    assert "开始 MVC 测试" not in s
    assert "start_mvc_calibrate" not in s


def test_manual_voice_fallback_uses_raw_recording_and_route_path():
    s = _src()
    assert "MANUAL_VOICE_RECORD_PATH = \"/dev/shm/manual_voice_record.request\"" in s
    assert "MANUAL_VOICE_STOP_PATH = \"/dev/shm/manual_voice_stop.request\"" in s
    assert "MANUAL_VOICE_STATUS_PATH = \"/dev/shm/manual_voice_status.json\"" in s
    assert "_primary_listen_reason = [None]" in s
    assert "_manual_voice_active = threading.Event()" in s
    assert "def _make_manual_voice_stop_check(request_ts, min_record_s=0.8)" in s
    assert "def _manual_voice_watcher" in s
    assert "def record_manual_until_stop" in s
    assert "def _primary_manual_record_until_stop" in s
    body = _function_body(s, "_manual_voice_watcher")
    assert "_primary_manual_record_until_stop(" in body
    assert "_primary_record_with_vad(" not in body
    assert 'active_reason != "wake_idle"' in body
    assert '_kill_active_recording("manual_voice_start")' in body
    assert "_manual_voice_active.set()" in body
    assert "_manual_voice_active.clear()" in body
    assert "stop_check=lambda" not in body
    assert "stop_check=_make_manual_voice_stop_check(req_ts)" in body
    assert "sound2text(client)" in body
    assert "route_fn(text, display_text=text)" in body
    raw_body = _function_body(s, "record_manual_until_stop")
    assert "record_with_vad(" not in raw_body
    assert "if stop_check is not None and stop_check()" in raw_body
    assert "wf.writeframes(raw_bytes)" in raw_body
    main_body = _function_body(s, "main")
    assert "threading.Thread(target=_manual_voice_watcher" in main_body
    assert "args=(client, _route_text)" in main_body
    assert "if _manual_voice_active.is_set():" in main_body
    assert "skip wake_asr_empty kill during manual recording" in main_body
    assert "skip boot_prompt_echo kill during manual recording" in main_body
    assert '_primary_record_with_vad("wake_idle", timeout=WAKE_TIMEOUT' in main_body
    assert "status = record_with_vad(timeout=WAKE_TIMEOUT)" not in main_body


def test_vad_accepts_optional_stop_check_without_changing_default_callers():
    body = _function_body(_src(), "record_with_vad")
    assert "stop_check=None" in body
    assert "stop_check is not None and stop_check()" in body


def test_voice_deepseek_chat_uses_adp_before_deepseek_fallback():
    s = _src()
    assert "from hardware_engine.cognitive import adp_knowledge" in s
    assert "search_adp_knowledge(text, limit=3" in s
    assert "ADP 专业知识库内容" in s
    assert "from hardware_engine.cognitive import vector_knowledge" not in s
    assert "search_vector_knowledge" not in s
    assert "from hardware_engine.cognitive import online_knowledge" in s
    assert "search_online_knowledge(text, limit=3" in s
    assert "优先参考以下本地知识库事实" not in s


def test_stats_query_no_longer_matches_bare_duoshao():
    s = _src()
    body = _function_body(s, "_try_voice_command")
    query_tuple = body[body.find("_QUERY_WORDS = ("):body.find("# \\u67e5\\u8be2\\u6761\\u4ef6", body.find("_QUERY_WORDS = ("))]
    assert 'u"\\u591a\\u5c11",' not in query_tuple
    assert 'u"\\u591a\\u5c11\\u4e2a"' in query_tuple
