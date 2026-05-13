import csv
import json
from pathlib import Path

import tools.ironbuddy_lane_b_data_audit as audit


def _write_csv(path, label, rows=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "Timestamp",
                "Ang_Vel",
                "Angle",
                "Ang_Accel",
                "Target_RMS",
                "Comp_RMS",
                "Symmetry_Score",
                "Phase_Progress",
                "pose_score",
                "label",
            ],
        )
        writer.writeheader()
        for i in range(rows):
            writer.writerow({
                "Timestamp": i,
                "Ang_Vel": 0.0,
                "Angle": 120.0,
                "Ang_Accel": 0.0,
                "Target_RMS": 25.0,
                "Comp_RMS": 15.0,
                "Symmetry_Score": 1.0,
                "Phase_Progress": 0.5,
                "pose_score": 0.8,
                "label": label,
            })


def _sample(i, target=45.0, comp=25.0):
    vals = [0.0, 120.0, 0.0, target, comp, 1.0, 0.5]
    return {
        "ts": 1000.0 + i * 0.02,
        "values": vals,
        "features": {
            "Ang_Vel": vals[0],
            "Angle": vals[1],
            "Ang_Accel": vals[2],
            "Target_RMS": vals[3],
            "Comp_RMS": vals[4],
            "Symmetry_Score": vals[5],
            "Phase_Progress": vals[6],
        },
        "exercise": "bicep_curl",
        "inference_mode": "vision_sensor",
    }


def _stream_row(i, rms0=180.0, rms1=80.0, pct0=95.0, pct1=45.0):
    return [1000.0 + i * 0.02, 1800 + i, 1700 + i, 10.0, -8.0, rms0, rms1, pct0, pct1, i]


def _write_group(path, label, rep_count=5):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ok": True,
        "group_id": path.stem,
        "exercise": "bicep_curl",
        "label": label,
        "board_mode_at_start": {"exercise": "bicep_curl", "inference_mode": "vision_sensor"},
        "board_mode_at_end": {"exercise": "bicep_curl", "inference_mode": "vision_sensor"},
        "start_gate": {"transport_ok": True, "valid_for_gru": True},
        "end_gate": {"transport_ok": True, "valid_for_gru": True},
        "gru_7d_samples": [_sample(i) for i in range(40)],
        "stream_samples": [_stream_row(i, pct0=100.0 if label == "standard" else 85.0) for i in range(40)],
        "rep_events": [{"classification_source": "gru"} for _ in range(rep_count)],
        "emg_mapping_summary": {
            "channels": [
                {
                    "channel": 0,
                    "current_pct_sat100_ratio": 0.774 if label == "standard" else 0.2,
                    "mvc_or_domain_saturation_suspected": True,
                },
                {
                    "channel": 1,
                    "current_pct_sat100_ratio": 0.021,
                    "mvc_or_domain_saturation_suspected": True,
                },
            ]
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_lane_b_data_audit_builds_index_and_preprocess_findings(tmp_path, monkeypatch):
    runs_root = tmp_path / "runs"
    operator_root = tmp_path / "operator"
    personal_root = tmp_path / "personal"
    datasets_root = personal_root / "datasets"
    training_root = personal_root / "training_runs"
    old_root = tmp_path / "data" / "bicep_curl"
    old_aug_root = tmp_path / "data" / "bicep_curl_augmented"
    v42_root = tmp_path / "data" / "v42"
    mia_root = tmp_path / "data" / "mia" / "squat"

    for label in ("golden", "bad", "lazy"):
        _write_csv(old_root / label / (label + ".csv"), label)
        _write_csv(old_aug_root / label / (label + "_aug.csv"), label)

    (v42_root / "user_01" / "curl" / "standard").mkdir(parents=True, exist_ok=True)
    (v42_root / "user_01" / "anthropometry.json").write_text(json.dumps({"generated": "mock"}), encoding="utf-8")
    (v42_root / "user_01" / "mvc_calibration.json").write_text(json.dumps({"source": "mock"}), encoding="utf-8")
    (mia_root / "_conversion_report.json").parent.mkdir(parents=True, exist_ok=True)
    (mia_root / "_conversion_report.json").write_text(
        json.dumps({"label_counts": {"golden": 10, "bad": 3}}),
        encoding="utf-8",
    )

    run = runs_root / "20260511-203305"
    session = {"board_ip": "10.62.98.224", "groups": [{"group_id": "x"}]}
    (run / "session_index.json").parent.mkdir(parents=True, exist_ok=True)
    (run / "session_index.json").write_text(json.dumps(session), encoding="utf-8")
    _write_group(run / "groups" / "001_standard.json", "standard")
    _write_group(run / "groups" / "002_compensating.json", "compensating")
    _write_group(run / "groups" / "003_non_standard.json", "non_standard")
    (operator_root / "lane_b_longrun_readiness_20260511-203305.json").parent.mkdir(parents=True, exist_ok=True)
    (operator_root / "lane_b_longrun_readiness_20260511-203305.json").write_text(
        json.dumps({
            "mvc": {
                "runtime_mvc_values_valid": False,
                "domain_method": "stretch",
            }
        }),
        encoding="utf-8",
    )

    dataset_dir = datasets_root / "20260512_101010_20260511-203305"
    dataset_dir.mkdir(parents=True, exist_ok=True)
    (dataset_dir / "personal_dataset_manifest.json").write_text(
        json.dumps({
            "training_ready": False,
            "source_run_id": "20260511-203305",
            "emg_view": "stable_remap_pct",
            "preprocess_version": "lane_b_v1_stable_remap_pct",
            "labels": {},
        }),
        encoding="utf-8",
    )
    train_dir = training_root / "20260512_111111"
    train_dir.mkdir(parents=True, exist_ok=True)
    (train_dir / "candidate_weight.pt").write_bytes(b"pt")
    (train_dir / "train_report.json").write_text(
        json.dumps({
            "passed_acceptance": False,
            "source_dataset": {"data_dir": "data/bicep_curl_personal/datasets/20260512_101010_20260511-203305"},
            "emg_view": "stable_remap_pct",
            "preprocess_version": "lane_b_v1_stable_remap_pct",
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(audit, "RUNS_ROOT", runs_root)
    monkeypatch.setattr(audit, "PERSONAL_ROOT", personal_root)
    monkeypatch.setattr(audit, "DATASETS_ROOT", datasets_root)
    monkeypatch.setattr(audit, "TRAINING_ROOT", training_root)
    monkeypatch.setattr(audit, "OLD_BICEP_ROOT", old_root)
    monkeypatch.setattr(audit, "OLD_BICEP_AUG_ROOT", old_aug_root)
    monkeypatch.setattr(audit, "V42_ROOT", v42_root)
    monkeypatch.setattr(audit, "MIA_SQUAT_ROOT", mia_root)
    monkeypatch.setattr(audit, "MIA_REPORT", mia_root / "_conversion_report.json")
    monkeypatch.setattr(audit, "OPERATOR_RUNS_ROOT", operator_root)
    monkeypatch.setattr(audit, "load_runtime_preprocess_meta", lambda: {
        "mvc_valid": True,
        "domain_method": "identity",
        "mvc_source": "schema_v2",
        "mvc_values": {"target": 500.0, "comp": 520.0},
        "domain_params": {
            "target": {"alpha": 1.0, "beta": 0.0},
            "comp": {"alpha": 1.0, "beta": 0.0},
        },
    })

    report = audit.build_report()

    assert set(report.keys()) >= {
        "summary",
        "historical_assets",
        "sensor_lab_runs",
        "personal_datasets",
        "training_runs",
        "mvc_runtime",
        "preprocess_findings",
    }
    assert report["summary"]["recommended_training_emg_view"] == "raw_rms_robust100"
    assert report["sensor_lab_runs"]["latest_three_label_run"] == "20260511-203305"
    assert report["personal_datasets"]["count"] == 1
    assert report["training_runs"]["count"] == 1
    assert report["preprocess_findings"]["saturation_detected"] is True
    assert report["preprocess_findings"]["runtime_mvc_missing"] is True
    assert report["preprocess_findings"]["key_run_domain_method"] == "stretch"
    assert report["preprocess_findings"]["verdict"] == "raw_rms_robust100_preferred_no_mvc"
