import ast
import os


STREAMER = os.path.join(os.path.dirname(__file__), "..", "streamer_app.py")
FEISHU = os.path.join(
    os.path.dirname(__file__),
    "..",
    "hardware_engine",
    "integrations",
    "feishu_client.py",
)
OPENCLOUD = os.path.join(
    os.path.dirname(__file__),
    "..",
    "scripts",
    "opencloud_reminder_daemon.py",
)
OFFLINE_ACCEPTANCE = os.path.join(
    os.path.dirname(__file__),
    "..",
    "tools",
    "ironbuddy_offline_acceptance.py",
)


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_streamer_new_routes_parse_and_exist():
    s = _read(STREAMER)
    ast.parse(s)
    assert "@app.route('/api/coach/capabilities'" in s
    assert "@app.route('/api/coach/rag_query'" in s
    assert "@app.route('/api/feishu/card_push'" in s
    assert "@app.route('/api/opencloud/status'" in s
    assert "@app.route('/api/openclaw/status'" in s
    assert "@app.route('/api/tts_volume'" in s
    assert "manual_reply" in s
    assert "manual_intent" in s


def test_demo_recording_routes_exist():
    s = _read(STREAMER)
    assert "@app.route('/api/demo/rag_status'" in s
    assert "@app.route('/api/demo/opencloud_records'" in s
    assert "@app.route('/api/demo/debug_workbench'" in s
    assert "@app.route('/api/demo/code_graph'" in s
    assert "def demo_rag_status" in s
    assert "def demo_opencloud_records" in s
    assert "def demo_debug_workbench" in s
    assert "def demo_code_graph" in s


def test_streamer_feishu_uses_interactive_card_not_text_for_smart_push():
    s = _read(STREAMER)
    assert "def _build_feishu_training_card" in s
    assert '"msg_type": "interactive"' in s
    assert '"msg_type": "text"' in s  # ping remains text-only selftest
    smart_idx = s.find("def _feishu_smart_push_impl")
    smart_body = s[smart_idx:s.find("# ===== V2", smart_idx)]
    assert '"msg_type": "interactive"' in smart_body
    assert '"content": json.dumps(card' in smart_body


def test_opencloud_status_masks_secrets_by_shape():
    s = _read(STREAMER)
    idx = s.find("def opencloud_status")
    body = s[idx:s.find("\n\n# ===== Probe", idx + 1)]
    # V7.37 renamed (user clarified: OpenClaw is "后台" reminder, not "云端")
    assert ("OpenClaw 后台提醒" in body) or ("OpenClaw 云端提醒" in body)
    assert "configured" in body
    assert "bool(_pick_config" in body
    assert "FEISHU_APP_SECRET" not in body


def test_tts_volume_api_writes_shm_and_clamps():
    s = _read(STREAMER)
    idx = s.find("def api_tts_volume")
    body = s[idx:s.find("\n\ndef _atomic_write_json", idx)]
    assert '"/dev/shm/tts_volume.json"' in body
    assert "max(1, min(15" in body
    assert '"src": "ui"' in body
    assert "_apply_tts_mixer" in body
    assert "amixer" in s[s.find("def _apply_tts_mixer"):s.find("# ===== JPEG", s.find("def _apply_tts_mixer"))]
    assert "mixer" in body


def test_mute_api_returns_mixer_and_recovers_current_volume():
    s = _read(STREAMER)
    idx = s.find("def api_mute")
    body = s[idx:s.find("\n\n@app.route('/api/fatigue_limit'", idx)]
    assert '"/dev/shm/mute_signal.json"' in body
    assert '"/dev/shm/voice_interrupt"' in body
    assert '"/dev/shm/replay_last_tts.json"' in body
    assert "killall" in body
    assert "_apply_tts_mixer(muted=muted)" in body
    assert '"mixer"' in body
    assert '"replay_requested"' in body


def test_voice_diag_exposes_volume_controls():
    s = _read(STREAMER)
    idx = s.find("def admin_voice_diag")
    body = s[idx:s.find("\n\n@app.route('/api/admin/voice_test'", idx)]
    assert "alsa_volume_controls" in body
    assert "_read_tts_volume" in body
    assert "voice_boot_status" in body
    assert "wake_log_markers" in body


def test_fsm_state_exposes_angle_diagnostics():
    s = _read(STREAMER)
    assert "angle_diag" in s
    assert "/dev/shm/angle_debug.json" in s


def test_feishu_training_card_builder_exists():
    s = _read(FEISHU)
    ast.parse(s)
    idx = s.find("def build_training_card")
    body = s[idx:]
    assert "def build_training_card" in body
    assert 'FeishuClient._md("**训练状态**' in body
    assert 'FeishuClient._md("**教练建议**' in body
    assert '"msg_type"' not in body
    assert "chat_id_masked" in s


def test_opencloud_reminder_daemon_dry_run_and_snapshot_fallback():
    s = _read(OPENCLOUD)
    ast.parse(s)
    assert "def load_snapshot" in s
    assert '"_snapshot_source": "default"' in s
    assert "dry_run = bool(args.dry_run or not args.send)" in s
    assert "FeishuClient.build_training_card" in s


def test_offline_acceptance_script_covers_main_surfaces():
    s = _read(OFFLINE_ACCEPTANCE)
    ast.parse(s)
    assert "/api/coach/capabilities" in s
    assert "/api/coach/rag_query" in s
    assert "/api/feishu/card_push" in s
    assert "/api/opencloud/status" in s
    assert "/api/openclaw/status" in s
    assert "opencloud_reminder_daemon.py" in s
