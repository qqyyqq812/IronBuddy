"""Admin start/stop source contract checks.

The live endpoints run on the embedded board and control real services, so these
tests guard the source-level contract without importing streamer_app.py.
"""
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREAMER = os.path.join(PROJECT_ROOT, "streamer_app.py")
INDEX = os.path.join(PROJECT_ROOT, "templates", "index.html")


def _read_streamer():
    with open(STREAMER, "r", encoding="utf-8") as f:
        return f.read()


def _function_body(src, name):
    marker = "def " + name + "("
    start = src.find(marker)
    assert start >= 0, name + " not found"
    end = src.find("\n@app.route", start + 1)
    if end < 0:
        end = len(src)
    return src[start:end]


def test_admin_stop_all_preserves_streamer_console():
    body = _function_body(_read_streamer(), "admin_stop")
    assert 'name == "streamer"' in body
    assert "continue" in body[body.find('name == "streamer"'):body.find("all_pids.extend")]
    assert "控制台保留运行" in body
    assert "_flush_runtime_db_buffers(finalize_open_sessions=True)" in body
    assert '"db_flush": flush_result' in body or "'db_flush': flush_result" in body


def test_admin_stop_rejects_explicit_streamer_shutdown():
    body = _function_body(_read_streamer(), "admin_stop")
    assert 'target == "streamer"' in body
    assert "stop_train_services_only" in body
    assert "refuse" in body.lower()


def test_admin_start_all_prepares_clean_take_baseline():
    body = _function_body(_read_streamer(), "admin_start")
    assert "_prepare_clean_take_baseline()" in body
    helper = _function_body(_read_streamer(), "_prepare_clean_take_baseline")
    for path in (
        "/dev/shm/fsm_reset_signal",
        "/dev/shm/fatigue_reset.request",
        "/dev/shm/chat_events.jsonl",
        "/dev/shm/ironbuddy_rag_delivery.json",
        "/dev/shm/ironbuddy_training_session.json",
        "/dev/shm/inference_mode.json",
        "/dev/shm/vision_mode.json",
    ):
        assert path in helper
    assert '"exercise":"squat"' in helper or '"exercise": "squat"' in helper
    assert '"mode":"pure_vision"' in helper or '"mode": "pure_vision"' in helper


def test_admin_start_does_not_try_to_launch_streamer_from_all():
    src = _read_streamer()
    launchers = src[src.find("_SERVICE_LAUNCHERS = {"):src.find("@app.route('/api/admin/start'")]
    assert '"vision"' in launchers
    assert '"fsm"' in launchers
    assert '"emg"' in launchers
    assert '"voice"' in launchers
    assert '"streamer"' not in launchers
    assert "IRONBUDDY_DB_DEFER_WRITES=1" in launchers


def test_admin_start_uses_verified_speaker_path_only():
    src = _read_streamer()
    body = _function_body(src, "admin_start")
    assert "Playback Path' 6" not in body
    assert "Playback Path 6" not in body
    assert "Playback Path' 2" in body or "Playback Path 2" in body


def test_restart_streamer_script_never_kills_all_python_processes():
    path = os.path.join(PROJECT_ROOT, "scripts", "restart_streamer.sh")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "pkill -9 -f python3" not in src
    assert "killall" not in src
    assert "/home/toybrick/streamer_v3" in src
    assert "streamer_app.py" in src


def test_recover_streamer_script_is_precise_wsl_entrypoint():
    path = os.path.join(PROJECT_ROOT, "scripts", "recover_streamer.sh")
    assert os.path.exists(path)
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "IRONBUDDY_BOARD_IP" in src
    assert "10.29.10.224" in src
    assert "ssh" in src
    assert "scripts/restart_streamer.sh" in src or "streamer_watchdog.py" in src
    assert "killall -9 python3" not in src
    assert "pkill -9 -f python3" not in src


def test_stop_validation_stops_training_services_without_killing_streamer():
    path = os.path.join(PROJECT_ROOT, "scripts", "stop_validation.sh")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "IRONBUDDY_BOARD_IP" in src
    assert "10.29.10.224" in src
    legacy_ip = ".".join(("10", "18", "76", "224"))
    assert legacy_ip not in src
    assert "killall -9 python3" not in src
    assert "streamer_app.py" not in src
    for sig in ("cloud_rtmpose_client.py", "main_claw_loop.py", "udp_emg_server.py", "voice_daemon.py"):
        assert sig in src


def test_streamer_watchdog_is_independent_and_precise():
    path = os.path.join(PROJECT_ROOT, "scripts", "streamer_watchdog.py")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "def _streamer_pids" in src
    assert "streamer_app.py" in src
    assert "pkill" not in src
    assert "killall" not in src
    assert "streamer.log" in src
    assert "--restart-unhealthy" in src
    assert "bool(args.restart_unhealthy)" in src


def test_streamer_watchdog_installer_has_reboot_fallback():
    path = os.path.join(PROJECT_ROOT, "scripts", "install_streamer_watchdog_user.sh")
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "systemctl --user enable ironbuddy-streamer-watchdog.service" in src
    assert "systemctl --user restart ironbuddy-streamer-watchdog.service" in src
    assert "@reboot" in src
    assert "streamer_watchdog.py --loop" in src
    assert "streamer_watchdog.py\" --once" not in src


def test_main_ui_caps_chat_bubbles_and_resets_recording_state():
    with open(INDEX, "r", encoding="utf-8") as f:
        src = f.read()
    assert "MAX_CHAT_BUBBLES" in src
    assert "trimChatBubbles" in src
    assert "resetRecordingFrontendState" in src
    assert "_repEventQueue.length = 0" in src
    assert "lastChatEventSeq = 0" in src
    assert "dsHistorySeen = {}" in src
