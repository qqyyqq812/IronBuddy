"""Source checks for the local Lane B Sensor Lab."""

import os
import importlib.util
import sys
import types


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAB = os.path.join(ROOT, "tools", "ironbuddy_sensor_lab.py")
FREQ = os.path.join(ROOT, "docs", "hardware_ref", "freq.py")


def _read():
    with open(LAB, "r", encoding="utf-8") as f:
        return f.read()


def _read_freq():
    with open(FREQ, "r", encoding="utf-8") as f:
        return f.read()


def _load_lab():
    if "flask" not in sys.modules:
        flask_stub = types.ModuleType("flask")
        flask_stub.Flask = lambda *args, **kwargs: None
        flask_stub.Response = lambda value=None, *args, **kwargs: value
        flask_stub.jsonify = lambda value=None, *args, **kwargs: value
        flask_stub.request = None
        sys.modules["flask"] = flask_stub
    spec = importlib.util.spec_from_file_location("ironbuddy_sensor_lab_under_test", LAB)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sensor_lab_has_fast_emg_endpoint():
    src = _read()
    assert '@app.route("/api/emg_fast")' in src
    assert "emg_raw_waveform.json" in src
    assert "emg_stream_buffer.json" in src
    assert "age_s" in src
    assert "signal_mode" in src
    assert "valid_for_gru" in src


def test_board_streamer_and_udp_server_expose_stream_buffer():
    streamer_path = os.path.join(ROOT, "streamer_app.py")
    udp_path = os.path.join(ROOT, "hardware_engine", "sensor", "udp_emg_server.py")
    with open(streamer_path, "r", encoding="utf-8") as f:
        streamer_src = f.read()
    with open(udp_path, "r", encoding="utf-8") as f:
        udp_src = f.read()
    assert "@app.route('/api/emg_stream')" in streamer_src
    assert "text/event-stream" in streamer_src
    assert "emg_stream_buffer.json" in streamer_src
    assert "STREAM_RING = deque(maxlen=EMG_STREAM_RING_LIMIT)" in udp_src
    assert "EMG_STREAM_RING_LIMIT = 1000" in udp_src
    assert '"filtered0"' in udp_src


def test_sensor_lab_has_filtered_history_endpoint():
    src = _read()
    assert '@app.route("/api/emg_stream")' in src
    assert 'new EventSource' in src
    assert "requestAnimationFrame(frame)" in src
    assert "stream_samples = deque" in src
    assert "emg_stream_buffer.json" in src
    assert '"filtered0"' in src or "filtered0" in src


def test_sensor_lab_uses_raw_and_processed_acceptance_scopes():
    src = _read()
    freq_src = _read_freq()
    assert 'id="rawCh0Chart"' in src
    assert 'id="rawCh1Chart"' in src
    assert 'id="filteredCh0Chart"' in src
    assert 'id="filteredCh1Chart"' in src
    assert 'id="filteredChart"' not in src
    assert 'id="rawChart"' not in src
    assert "toggleRawScale" not in src
    assert "WINDOW_S = 1.0" in src
    assert "RAW_ADC_MIN = 0" in src
    assert "RAW_ADC_MAX = 4095" in src
    assert "RAW_WINDOW = 1000" in src
    assert "rawScopeRows()" in src
    assert "sample ${rows.length}/${RAW_WINDOW}" in src
    assert "drawRawChannel('rawCh0Chart', rawRows, 1" in src
    assert "drawRawChannel('rawCh1Chart', rawRows, 2" in src
    assert "rawCh0StatsText" in src
    assert "rawCh1StatsText" in src
    assert "ch0StatsText" in src
    assert "ch1StatsText" in src
    assert "rawGateText" in src
    assert "原始ADC 串口示波器" in freq_src
    assert "max-lines-per-frame" in freq_src
    assert "reset_input_buffer" in freq_src
    assert "rail" in freq_src
    assert "jump" in freq_src


def test_sensor_lab_filtered_scope_uses_split_synced_canvases():
    src = _read()
    assert "displayEndTs" in src
    assert "DISPLAY_LAG_S = 0.16" in src
    assert "FILTERED_Y_OPTIONS = [200, 500, 1000, 4096]" in src
    assert "let filteredYRange = 1000" in src
    assert "function setFilteredRange(range)" in src
    assert "button[data-range]" in src
    assert "function updateDisplayClock()" in src
    assert "function rangeFor(rows, idx)" not in src
    assert "drawChannel('filteredCh0Chart', rows, 3" in src
    assert "drawChannel('filteredCh1Chart', rows, 4" in src
    assert "lineJoin = 'round'" in src
    assert "lineCap = 'round'" in src
    assert "悬空态，突刺不代表可用肌电" in src


def test_sensor_lab_has_reference_end_of_group_comparison():
    src = _read()
    assert '@app.route("/api/reference_waveforms")' in src
    assert "data/v42/user_03/curl/*/rep_001.csv" in src
    assert "compare_filtered_to_reference" in src
    assert "target_peak_phase_delta" in src
    assert "selectReference" not in src


def test_sensor_lab_chinese_acceptance_console_fields():
    src = _read()
    for text in (
        "Lane B 弯举一体化工作台",
        "传感器状态",
        "当前模式",
        "当前组",
        "保存位置",
        "下一步",
        "开始记录",
        "结束记录",
        "已保存组列表",
        "采集资格",
        "raw ADC 输入质量",
        "processed 信号",
    ):
        assert text in src
    assert 'id="sensorStateVal"' in src
    assert 'id="modeVal"' in src
    assert 'id="currentGroupVal"' in src
    assert 'id="savePathVal"' in src
    assert 'id="summaryPill"' in src
    assert 'id="captureEligibilityVal"' in src
    assert "classification_source" in src
    assert "captureEligibility(h, rec)" in src


def test_sensor_lab_recording_api_saves_group_and_queries_rep_events():
    src = _read()
    assert '@app.route("/api/recording/start", methods=["POST"])' in src
    assert '@app.route("/api/recording/stop", methods=["POST"])' in src
    assert '@app.route("/api/recording/groups")' in src
    assert "def start_recording(" in src
    assert "def stop_recording(" in src
    assert "session_index.json" in src
    assert "status_snapshots" in src
    assert "fsm_snapshots" in src
    assert "rep_events" in src
    assert "stream_samples" in src
    assert "baseline_rep_id" in src
    assert "query_rep_events" in src
    assert "/api/db/query/rep_events" in src
    assert "save_group_result" in src
    assert "last_group_save_path" in src
    assert "CUSTOM_ACTION_ROOT" in src
    assert "custom_action_save_path_rel" in src
    assert 'data" / "custom_actions"' in src
    assert "eligible_for_gru_accuracy" in src


def test_sensor_lab_supports_personal_squat_dataset_training_flow():
    src = _read()
    assert 'data-exercise="squat"' in src
    assert 'data-exercise="bicep_curl"' in src
    assert "selectExercise('squat')" in src
    assert "/api/personal_dataset/export" in src
    assert "/api/personal_dataset/train" in src
    assert "ironbuddy_export_personal_squat_gru_dataset.py" in src
    assert "train_gru_three_class_squat_personal.py" in src
    assert "data/squat_personal" in src
    assert "feature_distribution.html" in src
    assert "feature_points" in src
    assert "drawFeatureChart" in src
    assert "allow-non-gru-reps" in src


def test_sensor_lab_records_emg_and_vision_evidence_package():
    src = _read()
    for text in (
        "vision_pose_samples",
        "angle_debug_snapshots",
        "vision_summary",
        "emg_raw_summary",
        "emg_mapping_summary",
        "emg_preprocess",
        "stable_remap_pct",
        "default_training_view",
        "training_compare",
        "gru_7d_samples",
        "gru_last_windows",
        "gru_7d_summary",
        "gru_7d_evidence",
        "/dev/shm/gru_7d_buffer.json",
        "/dev/shm/gru_last_window.json",
        "vision_evidence",
        "/dev/shm/pose_data.json",
        "/dev/shm/angle_debug.json",
        "raw_adc_not_available_in_old_training_csv",
        "old_pct_400",
        "VISION_CAPTURE_INTERVAL_S",
        "def vision_loop(",
        "视觉证据",
        "训练口径",
        'id="visionVal"',
        'id="mappingVal"',
    ):
        assert text in src


def test_sensor_lab_recording_is_local_only_and_does_not_switch_board_mode():
    src = _read()
    assert "local recording only; board mode is not changed by Sensor Lab" in src
    assert "lab_local_recording" in src
    assert "start_recording(" in src
    assert "/api/recording/start" in src
    assert "/api/recording/stop" in src
    assert "/api/user_profile" not in src
    assert '@app.route("/api/board_mode/switch", methods=["POST"])' in src
    assert "/api/exercise_mode" in src
    assert "/api/switch_inference_mode" in src
    assert "function switchBoardMode" in src
    assert "startRecording()" in src
    assert "/api/test_capture/start" not in src
    assert "/api/mvc_calibrate" not in src
    assert "sensor_not_ready" not in src
    assert "current_lock_owner" in src


def test_sensor_lab_has_live_reps_and_explicit_deploy_flow():
    src = _read()
    assert '@app.route("/api/live_reps")' in src
    assert '@app.route("/api/personal_dataset/deploy", methods=["POST"])' in src
    assert "def live_reps" in src
    assert "def deploy_personal_gru" in src
    assert "lane_lock_not_owned" in src
    assert "extreme_fusion_gru_bicep.pt" in src
    assert "lane_b_runtime_preprocess.json" in src
    assert "部署并测试" in src
    assert "liveRepsRows" in src


def test_sensor_lab_ui_allows_recording_without_sensor_gate_and_custom_actions():
    src = _read()
    assert 'id="customExerciseName"' in src
    assert "function createCustomExercise()" in src
    assert "可记录：无传感" in src
    assert "可记录，本轮不计EMG" in src
    assert "不可采" not in src
    assert "只复盘：" not in src
    assert "Access-Control-Allow-Origin" in src


def test_sensor_lab_labels_three_classes():
    src = _read()
    for label in ("standard", "compensating", "non_standard"):
        assert label in src


def test_floating_signal_gate_blocks_gru():
    lab = _load_lab()
    samples = []
    for i in range(120):
        samples.append([float(i) / 100.0, 0 if i % 3 == 0 else 3200, 3800 if i % 2 == 0 else 0])
    stats = lab.raw_wave_stats(samples)
    gate = lab.signal_gate(True, {"pct": [100, 100]}, stats, simulated=False)
    assert gate["signal_mode"] == "floating_no_contact"
    assert gate["valid_for_gru"] is False
    assert gate["transport_ok"] is True


def test_saturated_signal_without_raw_stats_blocks_gru():
    lab = _load_lab()
    gate = lab.signal_gate(True, {"pct": [100, 100]}, {}, simulated=False)
    assert gate["signal_mode"] == "floating_no_contact"
    assert gate["valid_for_gru"] is False
    assert gate["reason"] == "pct_saturated_without_raw_stats"


def test_contact_rest_signal_gate_can_pass_gru_gate():
    lab = _load_lab()
    samples = [[float(i) / 100.0, 1800 + (i % 4), 1810 + (i % 3)] for i in range(120)]
    stats = lab.raw_wave_stats(samples)
    gate = lab.signal_gate(True, {"pct": [4, 3]}, stats, simulated=False)
    assert gate["signal_mode"] == "contact_rest_candidate"
    assert gate["valid_for_gru"] is True


def test_gru_only_summary_ignores_visual_fallback_for_accuracy():
    lab = _load_lab()
    reps = [
        {
            "label": "standard",
            "prediction": "standard",
            "classification_source": "gru",
        },
        {
            "label": "standard",
            "prediction": "non_standard",
            "classification_source": "visual_fallback_no_emg",
        },
    ]
    summary = lab.summarize_group("standard", reps, {"ok": True, "similarity": 0.7})
    assert summary["rep_count"] == 2
    assert summary["gru_rep_count"] == 1
    assert summary["correct"] == 1
    assert summary["accuracy"] == 1.0
    assert summary["fallback_reasons"]["visual_fallback_no_emg"] == 1


def test_stream_wave_stats_preserves_raw_rate_and_filtered_span():
    lab = _load_lab()
    samples = []
    for i in range(1000):
        samples.append([
            i / 1000.0,
            1800 + (i % 40),
            1900 - (i % 30),
            -20.0 + (i % 20),
            10.0 - (i % 10),
            15.0,
            12.0,
            4.0,
            3.0,
            i + 1,
        ])
    stats = lab.stream_wave_stats(samples)
    assert stats["time"]["rate_hz"] >= 990
    assert stats["filtered_channels"][0]["span"] > 0


def test_sensor_lab_stream_payload_keeps_filtered_rows_and_full_stream_rows():
    lab = _load_lab()
    samples = [
        [1.0, 1800, 1900, -11.0, 7.0, 13.0, 9.0, 3.0, 2.0, 101],
        [1.01, 1810, 1888, -12.0, 8.0, 14.0, 10.0, 4.0, 3.0, 102],
    ]
    rows = lab.filtered_display_rows(samples)
    assert rows == [
        [1.0, -11.0, 7.0, 13.0, 9.0, 3.0, 2.0, 101.0],
        [1.01, -12.0, 8.0, 14.0, 10.0, 4.0, 3.0, 102.0],
    ]
    src = _read()
    assert '"stream_samples"' in src
    assert '"stream_columns"' in src
    assert "body.stream_samples || body.samples || []" in src
    assert '"raw0", "raw1", "filtered0", "filtered1"' in src


def test_sensor_lab_snapshot_fallback_preserves_raw_for_gate_but_displays_filtered_debug():
    lab = _load_lab()
    raw_samples = [
        [1.0, 12.0, 4095.0],
        [1.01, 14.0, 4000.0],
    ]
    debug = {
        "filtered": [-5.0, 6.0],
        "rms": [11.0, 22.0],
        "pct": [3, 8],
        "packet_count": 40,
    }
    rows = lab.fallback_stream_rows(raw_samples, debug)
    assert rows[0] == [1.0, 12.0, 4095.0, -5.0, 6.0, 11.0, 22.0, 3.0, 8.0, 40.0]
    assert lab.filtered_display_rows(rows)[0] == [1.0, -5.0, 6.0, 11.0, 22.0, 3.0, 8.0, 40.0]


def test_pose_sample_preserves_full_17_keypoints():
    lab = _load_lab()
    kpts = [[i, i + 1, 0.5] for i in range(17)]
    sample = lab.sanitize_pose_sample(
        {"timestamp": 10.0, "frame_idx": 7, "objects": [{"score": 0.9, "kpts": kpts}]},
        capture_ts=11.0,
        remote_now=11.0,
    )
    assert sample["valid_person"] is True
    assert sample["frame_idx"] == 7
    assert sample["objects"][0]["kpt_count"] == 17
    assert len(sample["objects"][0]["kpts"]) == 17


def test_emg_mapping_summary_keeps_old_pct_and_current_pct():
    lab = _load_lab()
    rows = [[i / 100.0, 1700, 1710, 1, 2, 200, 160, 95, 100, i] for i in range(10)]
    summary = lab.emg_mapping_summary(rows)
    assert summary["ok"] is True
    assert "old_pct_400" in summary["channels"][0]
    assert "current_pct" in summary["channels"][0]
    assert summary["channels"][0]["old_pct_400"]["mean"] == 50.0
    assert summary["channels"][0]["current_pct"]["mean"] == 95.0


def test_old_training_compare_marks_raw_adc_missing():
    lab = _load_lab()
    compare = lab.old_bicep_training_compare()
    assert compare["raw_adc_status"] == "raw_adc_not_available_in_old_training_csv"
    assert "Target_RMS" in compare["labels"]["standard"]["base"]


def test_stop_recording_writes_complete_evidence_package(tmp_path):
    lab = _load_lab()
    session = lab.SensorLabSession("127.0.0.1", tmp_path)
    session.validation.update({
        "active": True,
        "phase": "recording",
        "exercise": "bicep_curl",
        "label": "standard",
        "group_id": "test_standard",
        "started_ts": 1.0,
        "baseline_rep_id": 1,
        "capture": {"mode": "lab_local_recording"},
        "start_gate": {"signal_mode": "contact_rest_candidate", "valid_for_gru": True},
        "board_mode_at_start": {"exercise": "bicep_curl", "inference_mode": "vision_sensor"},
    })
    session.recording_pose_samples = [
        lab.sanitize_pose_sample(
            {
                "timestamp": 1.1,
                "frame_idx": 10,
                "objects": [{"score": 0.9, "kpts": [[i, i + 1, 0.8] for i in range(17)]}],
            },
            capture_ts=1.1,
            remote_now=1.1,
        )
    ]
    session.recording_angle_debug_snapshots = [
        {"ts": 1.1, "raw_angle": 66.0, "smooth_angle": 65.0, "selected_side": "right"}
    ]
    session.recording_status_snapshots = [{"ts": 1.1, "gate": {"signal_mode": "contact_rest_candidate"}}]
    session.recording_fsm_snapshots = [{"ts": 1.1, "fsm": {"last_drop_reason": None}}]
    session.ingest_stream_samples([
        [1.1, 1700, 1710, 1, 2, 200, 160, 95, 100, 11],
        [1.2, 1710, 1720, 2, 3, 220, 170, 98, 100, 12],
    ])

    def fake_read_board_snapshot():
        session.latest = {
            "health": {
                "transport_ok": True,
                "valid_for_gru": True,
                "signal_mode": "contact_rest_candidate",
                "fsm_exercise": "bicep_curl",
                "inference_mode": "vision_sensor",
                "raw_stats": {},
                "emg_debug": {"pct": [10, 8]},
            }
        }
        return session.latest

    session.read_board_snapshot = fake_read_board_snapshot
    session.query_rep_events = lambda exercise="bicep_curl", limit=200: ([
        {
            "id": 2,
            "classification_source": "gru",
            "model_class": "standard",
            "model_confidence": 0.91,
            "model_similarity": 0.82,
            "emg_ok": 1,
        }
    ], {"ok_http": True})

    result = session.stop_recording()
    group = result["group"]
    assert group["vision_pose_samples"][0]["objects"][0]["kpt_count"] == 17
    assert group["angle_debug_snapshots"][0]["smooth_angle"] == 65.0
    assert group["vision_summary"]["pose_sample_count"] == 1
    assert group["emg_raw_summary"]["samples"] == 2
    assert group["emg_mapping_summary"]["channels"][0]["old_pct_400"]["mean"] == 52.5
    assert group["training_compare"]["old_bicep_training_compare"]["raw_adc_status"] == "raw_adc_not_available_in_old_training_csv"
    assert os.path.exists(group["save_path"])
    assert group["custom_action_save_path_rel"].startswith("data/custom_actions/bicep_curl/standard/")
    assert os.path.exists(group["custom_action_save_path"])


def test_reference_distance_metrics_report_peak_area_and_similarity():
    lab = _load_lab()
    refs = {
        "labels": {
            "standard": {
                "rows": [
                    [0.0, 0.1, 0.2, 0.0, 0.0],
                    [0.5, 0.8, 0.4, 0.0, 0.0],
                    [1.0, 0.3, 0.1, 0.0, 0.0],
                ]
            }
        }
    }
    samples = [
        [1.0, 10, 20, 100, 200, 0, 0],
        [1.1, 80, 40, 100, 200, 0, 0],
        [1.2, 30, 10, 100, 200, 0, 0],
    ]
    metrics = lab.compare_filtered_to_reference("standard", samples, refs)
    assert metrics["ok"] is True
    assert metrics["sample_count"] == 3
    assert metrics["target_peak_delta"] == 0.0
    assert metrics["comp_peak_delta"] == 0.0
    assert metrics["similarity"] == 1.0
