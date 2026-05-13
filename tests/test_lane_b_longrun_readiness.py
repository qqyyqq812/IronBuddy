import json
from pathlib import Path

import tools.ironbuddy_lane_b_longrun_readiness as readiness


def _sample(i):
    return {
        "ts": 1000.0 + i * 0.02,
        "values": [1.0, 120.0, 0.1, 45.0, 20.0, 1.0, 0.5],
    }


def _write_group(path, label, bad_mode=False, bad_signal=False, source="gru"):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "exercise": "bicep_curl",
        "label": label,
        "board_mode_at_start": {"exercise": "bicep_curl", "inference_mode": "vision_sensor"},
        "board_mode_at_end": {"exercise": "bicep_curl", "inference_mode": "vision_sensor"},
        "start_gate": {"transport_ok": True, "valid_for_gru": True},
        "end_gate": {"transport_ok": True, "valid_for_gru": True},
        "gru_7d_samples": [_sample(i) for i in range(60)],
        "rep_events": [{"classification_source": source} for _ in range(5)],
    }
    if bad_mode:
        payload["board_mode_at_start"] = {"exercise": "squat", "inference_mode": "pure_vision"}
    if bad_signal:
        payload["end_gate"] = {"transport_ok": True, "valid_for_gru": False}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_longrun_personal_bicep_audit_counts_ready_groups(tmp_path):
    run = tmp_path / "run"
    for label in readiness.LABELS:
        for idx in range(5):
            _write_group(run / "groups" / ("%s_%d.json" % (label, idx)), label)

    report = readiness.audit_personal_bicep(run)

    assert report["training_ready"] is True
    assert report["labels"]["standard"]["accepted_groups"] == 5
    assert report["labels"]["compensating"]["gru_reps"] == 25


def test_longrun_personal_bicep_audit_reports_rejections(tmp_path):
    run = tmp_path / "run"
    _write_group(run / "groups" / "bad_mode.json", "standard", bad_mode=True)
    _write_group(run / "groups" / "bad_signal.json", "compensating", bad_signal=True)
    _write_group(run / "groups" / "fallback.json", "non_standard", source="visual_fallback_no_emg")

    report = readiness.audit_personal_bicep(run)
    reasons = {
        item["file"]: set(item["reasons"])
        for group in report["labels"].values()
        for item in group["rejected"]
    }

    assert "mode_not_bicep_vision_sensor" in reasons["bad_mode.json"]
    assert "invalid_signal_gate" in reasons["bad_signal.json"]
    assert "rep_boundary_failed" in reasons["fallback.json"]


def test_longrun_script_mentions_mvc_and_squat_assets():
    src = Path(readiness.__file__).read_text(encoding="utf-8")

    assert "mvc_values.json" in src
    assert "_conversion_report.json" in src
    assert "v42_dual_branch_fusion_head_until_metrics_improve" in src
