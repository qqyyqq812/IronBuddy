import csv
import json
from pathlib import Path

import tools.ironbuddy_lane_b_7d_compare as compare


ROOT = Path(__file__).resolve().parents[1]


def _write_training_csv(path, label, target, comp):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=compare.FEATURES_7D + ["label"],
        )
        writer.writeheader()
        for i in range(35):
            writer.writerow({
                "Ang_Vel": i % 5,
                "Angle": 120 + (i % 20),
                "Ang_Accel": (i % 3) - 1,
                "Target_RMS": target,
                "Comp_RMS": comp,
                "Symmetry_Score": 1.0,
                "Phase_Progress": (i % 10) / 10.0,
                "label": label,
            })


def _stream_sample(ts, pkt):
    return [
        ts,
        1800 + pkt,
        1700 + pkt,
        12.0,
        -8.0,
        160.0,
        80.0,
        90.0,
        40.0,
        pkt,
    ]


def _write_group(path, label, rep_prediction="standard", with_angle=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = [_stream_sample(1000.0 + i * 0.05, i) for i in range(60)]
    angles = []
    if with_angle:
        for i in range(35):
            angles.append({
                "ts": 1000.0 + i * 0.10,
                "decision_angle": 150 - (i % 20),
                "state": "CURLING" if i % 3 else "STAND",
                "selected_side": "left" if i % 2 else "right",
                "side_trend": "falling" if i % 4 else "rising",
                "opening_frames": i % 3,
                "side_closing_frames": i % 2,
                "rep_in_progress": bool(i % 5),
            })
    payload = {
        "ok": True,
        "group_id": path.stem,
        "label": label,
        "stream_samples": stream,
        "angle_debug_snapshots": angles,
        "rep_events": [
            {
                "id": 2,
                "classification_source": "gru",
                "model_class": rep_prediction,
                "visual_result": "standard",
                "angle_min": 60.0,
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_7d_compare_discards_invalid_group_and_maps_old_labels(tmp_path):
    old = tmp_path / "data" / "bicep_curl"
    _write_training_csv(old / "golden" / "golden.csv", "golden", 25, 40)
    _write_training_csv(old / "bad" / "bad.csv", "bad", 25, 6)
    _write_training_csv(old / "lazy" / "lazy.csv", "lazy", 42, 17)

    run = tmp_path / "run"
    _write_group(run / "groups" / "20260511_205143_001_standard.json", "standard")
    _write_group(run / "groups" / "20260511_205324_002_standard.json", "standard")
    _write_group(run / "groups" / "20260511_205420_003_compensating.json", "compensating")

    report = compare.analyze_run(
        run_dir=run,
        discard_globs=["*_001_standard.json"],
        old_data_dir=old,
        model_path=tmp_path / "missing.pt",
        do_replay=False,
    )

    assert report["discarded_groups"][0]["file"] == "20260511_205143_001_standard.json"
    assert report["used_group_files"] == [
        "20260511_205324_002_standard.json",
        "20260511_205420_003_compensating.json",
    ]
    assert report["old_training"]["by_label"]["standard"]["old_dir"] == "golden"
    assert report["old_training"]["by_label"]["compensating"]["old_dir"] == "bad"
    assert report["groups"][0]["emg_modes"]["current_pct"]["row_count"] > 0
    assert report["groups"][0]["emg_modes"]["old_pct400"]["stats"]["Target_RMS"]["mean"] == 40.0
    assert report["groups"][1]["missed_rep_report"]["logged_rep_count"] == 1


def test_7d_compare_handles_missing_angle_debug_without_crashing(tmp_path):
    old = tmp_path / "data" / "bicep_curl"
    _write_training_csv(old / "golden" / "golden.csv", "golden", 25, 40)
    _write_training_csv(old / "bad" / "bad.csv", "bad", 25, 6)
    _write_training_csv(old / "lazy" / "lazy.csv", "lazy", 42, 17)
    run = tmp_path / "run"
    _write_group(run / "groups" / "002_standard.json", "standard", with_angle=False)

    report = compare.analyze_run(
        run_dir=run,
        discard_globs=["*_001_standard.json"],
        old_data_dir=old,
        model_path=tmp_path / "missing.pt",
        do_replay=False,
    )

    group = report["groups"][0]
    assert group["emg_modes"]["current_pct"]["row_count"] == 0
    assert group["raw_rms_summary"]["ok"] is True
    assert group["exact_gru_7d_available"] is False


def test_main_loop_exports_exact_gru_7d_window_contract():
    src = (ROOT / "hardware_engine" / "main_claw_loop.py").read_text(encoding="utf-8")
    assert "/dev/shm/gru_7d_buffer.json" in src
    assert "/dev/shm/gru_last_window.json" in src
    assert "def _append_gru_7d_sample" in src
    assert "def _write_gru_7d_window" in src
    assert '"Ang_Vel", "Angle", "Ang_Accel", "Target_RMS", "Comp_RMS"' in src
    assert "_write_gru_7d_window(" in src
