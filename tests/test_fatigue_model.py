import json

from hardware_engine import fatigue_model


def _dose_features(**extra):
    base = {
        "exercise": "squat",
        "rom": 90,
        "result": "standard",
        "phase": "UP",
        "target_rms": 200,
        "compensation_rms": 40,
        "target_mvc": 400,
        "comp_mvc": 400,
        "dt_s": 1.0,
        "d_target": 7.0,
        "target_fatigue": 1500,
        "emg_valid": True,
        "pose_valid": True,
        "is_training": True,
    }
    base.update(extra)
    return base


def test_dose_integral_standard_action_accumulates_d_eff():
    result = fatigue_model.compute_fatigue(_dose_features(), previous_score=0.0, now=1.0)

    assert result["fatigue_model_version"] == "dose_integral_v1"
    assert result["fatigue_increment"] > 0
    assert result["fatigue_score"] > 0
    assert result["d_eff"] > 0
    assert result["instant_load"] > 0
    assert result["fatigue_progress_pct"] > 0
    assert result["fatigue_components"]["dose"][0]["name"] == "a_target"


def test_quality_gate_orders_standard_compensating_and_non_standard():
    standard = fatigue_model.compute_fatigue(_dose_features(result="standard"), now=1.0)
    comp = fatigue_model.compute_fatigue(_dose_features(result="compensating"), now=1.0)
    bad = fatigue_model.compute_fatigue(_dose_features(result="non_standard"), now=1.0)

    assert standard["instant_load"] > comp["instant_load"]
    assert comp["instant_load"] > bad["instant_load"]
    assert standard["fatigue_components"]["visual"][2]["name"] == "q_class"


def test_compensation_penalty_reduces_effective_dose():
    low_comp = fatigue_model.compute_fatigue(
        _dose_features(compensation_rms=30),
        previous_score=0,
    )
    high_comp = fatigue_model.compute_fatigue(
        _dose_features(compensation_rms=360),
        previous_score=0,
    )

    assert low_comp["p_comp"] > high_comp["p_comp"]
    assert low_comp["instant_load"] > high_comp["instant_load"]


def test_rep_level_dt_is_not_clamped_to_frame_window():
    rep_level = fatigue_model.compute_fatigue(_dose_features(dt_s=1.0), previous_score=0)
    frame_level = fatigue_model.compute_fatigue(
        _dose_features(dt_s=1.0, integration_mode="frame"),
        previous_score=0,
    )

    assert rep_level["dt_s"] == 1.0
    assert frame_level["dt_s"] == 0.2
    assert rep_level["fatigue_increment"] > frame_level["fatigue_increment"]


def test_invalid_emg_marks_signal_gate_and_uses_conservative_visual_fallback():
    result = fatigue_model.compute_fatigue(
        _dose_features(
            target_rms=0,
            compensation_rms=0,
            target_mvc=0,
            emg_valid=False,
            signal_mode="floating_no_contact",
        ),
        previous_score=100,
    )

    assert result["fatigue_score"] > 100
    assert result["instant_load"] > 0
    assert result["v_signal"] < 1.0
    assert result["fatigue_components"]["signal"][0]["status"] == "missing"
    assert result["fatigue_components"]["emg"][0]["status"] == "missing"


def test_rest_phase_does_not_accumulate_dose():
    result = fatigue_model.compute_fatigue(
        _dose_features(phase="REST", is_training=False),
        previous_score=200,
    )

    assert result["fatigue_score"] == 200
    assert result["fatigue_increment"] == 0
    assert result["instant_load"] == 0


def test_feature_snapshot_jsonl_supports_later_export(tmp_path):
    path = tmp_path / "fatigue_snapshots.jsonl"
    snapshot = fatigue_model.compute_fatigue(_dose_features(), previous_score=200, now=123.0)

    assert fatigue_model.append_feature_snapshot(snapshot, path=str(path)) is True
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["fatigue_model_version"] == "dose_integral_v1"
    assert row["features"]["exercise"] == "squat"
    assert row["fatigue_score"] > 200
    assert row["d_eff"] > 0
