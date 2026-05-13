import csv
import importlib
import json
from pathlib import Path

import pytest
import tools.ironbuddy_export_personal_bicep_gru_dataset as export_tool


ROOT = Path(__file__).resolve().parents[1]


def _sample(i, target=45.0, comp=25.0):
    values = [
        float((i % 7) - 3),
        float(150 - (i % 40)),
        float((i % 5) - 2),
        float(target),
        float(comp),
        1.0,
        float((i % 30) / 30.0),
    ]
    return {
        "ts": 1000.0 + i * 0.03,
        "values": values,
        "features": dict(zip(export_tool.FEATURES_7D, values)),
        "exercise": "bicep_curl",
        "inference_mode": "vision_sensor",
    }


def _stream_sample(i, target=45.0, comp=25.0, current_target=None, current_comp=None):
    current_target = target if current_target is None else current_target
    current_comp = comp if current_comp is None else current_comp
    return [
        1000.0 + i * 0.03,
        1800.0 + i,
        1700.0 + i,
        10.0,
        -8.0,
        target * 4.0,
        comp * 4.0,
        float(current_target),
        float(current_comp),
        i,
    ]


def _write_group(path, label, rows=40, reps=5, target=45.0, comp=25.0, exact=True):
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
        "gru_7d_samples": [_sample(i, target=target, comp=comp) for i in range(rows)] if exact else [],
        "stream_samples": [_stream_sample(i, target=target, comp=comp) for i in range(max(rows, 1))],
        "emg_preprocess": {
            "preprocess_version": "lane_b_v2_raw_rms_robust100",
            "default_training_view": "raw_rms_robust100",
            "mvc_source": "default400",
            "mvc_values": {"target": 400.0, "comp": 400.0},
            "domain_method": "identity",
            "domain_params": {
                "target": {"alpha": 1.0, "beta": 0.0},
                "comp": {"alpha": 1.0, "beta": 0.0},
            },
        },
        "rep_events": [
            {"id": i + 1, "classification_source": "gru", "model_class": label}
            for i in range(reps)
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _args(**kwargs):
    class Args:
        emg_view = "raw_rms_robust100"
        seq_len = 30
        stride = 10
        min_groups_per_label = 5
        min_reps_per_group = 4
        min_reps_per_label = 25
        min_windows_per_label = 10
        saturation_threshold = 99.0
        max_saturation_ratio = 0.60
    args = Args()
    for key, value in kwargs.items():
        setattr(args, key, value)
    return args


def test_export_personal_dataset_accepts_ready_current_exact_groups(tmp_path):
    run = tmp_path / "run"
    label_specs = {
        "standard": (45.0, 25.0),
        "compensating": (55.0, 35.0),
        "non_standard": (35.0, 50.0),
    }
    for label, (target, comp) in label_specs.items():
        for idx in range(5):
            _write_group(
                run / "groups" / ("%03d_%s.json" % (idx, label)),
                label,
                rows=60,
                reps=5,
                target=target,
                comp=comp,
            )

    out = tmp_path / "personal_out"
    manifest = export_tool.export_dataset([run], out, _args())

    assert manifest["training_ready"] is True
    assert manifest["labels"]["standard"]["accepted_groups"] == 5
    assert manifest["labels"]["compensating"]["reps"] == 25
    assert (out / "golden").is_dir()
    assert (out / "bad").is_dir()
    assert (out / "lazy").is_dir()
    assert len(list((out / "golden").glob("*.csv"))) == 5
    assert (out / "personal_dataset_manifest.json").exists()
    assert (out / "audit_snapshot.json").exists()
    assert (out / "export_report.md").exists()
    assert (out / "README.md").exists()
    manifest_json = json.loads((out / "personal_dataset_manifest.json").read_text(encoding="utf-8"))
    assert manifest_json["emg_view"] == "raw_rms_robust100"
    assert manifest_json["raw_rms_robust100"]["uses_mvc"] is False
    assert manifest_json["source_run_id"] == "run"


def test_export_personal_dataset_rejects_missing_exact_saturated_and_rep_failed(tmp_path):
    run = tmp_path / "run"
    _write_group(run / "groups" / "001_missing.json", "standard", exact=False, reps=5)
    _write_group(run / "groups" / "002_saturated.json", "compensating", target=100.0, comp=100.0, reps=5)
    _write_group(run / "groups" / "003_rep_failed.json", "non_standard", reps=1)

    manifest = export_tool.export_dataset([run], tmp_path / "out", _args())
    reasons = {g["file"]: set(g.get("reasons") or []) for g in manifest["groups"]}

    assert manifest["training_ready"] is False
    assert "missing_exact_gru_7d" in reasons["001_missing.json"]
    assert "range_saturated" not in reasons["002_saturated.json"]
    assert "rep_boundary_failed" in reasons["003_rep_failed.json"]
    assert not list((tmp_path / "out" / "golden").glob("*.csv"))


def test_export_personal_dataset_rejects_mode_signal_and_non_gru_reps(tmp_path):
    run = tmp_path / "run"
    bad_mode = run / "groups" / "001_bad_mode.json"
    bad_signal = run / "groups" / "002_bad_signal.json"
    fallback_reps = run / "groups" / "003_fallback_reps.json"
    _write_group(bad_mode, "standard", reps=5)
    _write_group(bad_signal, "standard", reps=5)
    _write_group(fallback_reps, "standard", reps=5)

    mode_payload = json.loads(bad_mode.read_text(encoding="utf-8"))
    mode_payload["board_mode_at_start"] = {"exercise": "squat", "inference_mode": "pure_vision"}
    bad_mode.write_text(json.dumps(mode_payload), encoding="utf-8")

    signal_payload = json.loads(bad_signal.read_text(encoding="utf-8"))
    signal_payload["start_gate"] = {"transport_ok": True, "valid_for_gru": False}
    bad_signal.write_text(json.dumps(signal_payload), encoding="utf-8")

    fallback_payload = json.loads(fallback_reps.read_text(encoding="utf-8"))
    for rep in fallback_payload["rep_events"]:
        rep["classification_source"] = "visual_fallback_no_emg"
    fallback_reps.write_text(json.dumps(fallback_payload), encoding="utf-8")

    manifest = export_tool.export_dataset([run], tmp_path / "out", _args())
    reasons = {g["file"]: set(g.get("reasons") or []) for g in manifest["groups"]}

    assert "mode_not_vision_sensor_at_start" in reasons["001_bad_mode.json"]
    assert "invalid_start_signal_gate" in reasons["002_bad_signal.json"]
    assert "rep_boundary_failed" in reasons["003_fallback_reps.json"]
    assert manifest["groups"][2]["rep_gate_count"] == 0


def test_personal_training_script_uses_group_level_split_and_no_old_data_default():
    src = (ROOT / "tools" / "train_gru_three_class_bicep_personal.py").read_text(encoding="utf-8")
    assert "def split_groups" in src
    assert "manifest_training_ready" in src
    assert "dataset_not_training_ready" in src
    assert "manifest_required" in src
    assert "data/bicep_curl" not in src
    assert "training_runs" in src
    assert "candidate_weight.pt" in src
    assert "min_recall" in src
    assert "unexpected_emg_view" in src
    assert "raw_rms_robust100" in src


def test_export_personal_dataset_uses_raw_rms_robust100_by_default(tmp_path):
    run = tmp_path / "run"
    group = run / "groups" / "001_standard.json"
    _write_group(group, "standard", rows=40, reps=5, target=45.0, comp=25.0)
    payload = json.loads(group.read_text(encoding="utf-8"))
    payload["gru_7d_samples"] = [_sample(i, target=99.0, comp=88.0) for i in range(40)]
    payload["stream_samples"] = [_stream_sample(i, target=45.0, comp=25.0, current_target=99.0, current_comp=88.0) for i in range(40)]
    group.write_text(json.dumps(payload), encoding="utf-8")

    manifest = export_tool.export_dataset([run], tmp_path / "out", _args())
    assert manifest["groups"][0]["emg_view"] == "raw_rms_robust100"
    assert manifest["groups"][0]["raw_rms_robust100"]["uses_mvc"] is False
    csv_path = next((tmp_path / "out" / "golden").glob("*.csv"))
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        first = next(csv.DictReader(f))
    assert 90.0 <= float(first["Target_RMS"]) <= 100.0
    assert 90.0 <= float(first["Comp_RMS"]) <= 100.0


def test_export_personal_squat_dataset_accepts_visual_boundary_groups_and_writes_distribution(tmp_path):
    run = tmp_path / "run"
    for label, (target, comp) in {
        "standard": (55.0, 18.0),
        "compensating": (35.0, 70.0),
        "non_standard": (18.0, 22.0),
    }.items():
        for idx in range(2):
            path = run / "groups" / ("%03d_%s.json" % (idx, label))
            _write_group(path, label, rows=50, reps=4, target=target, comp=comp)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["exercise"] = "squat"
            payload["board_mode_at_start"] = {"exercise": "squat", "inference_mode": "vision_sensor"}
            payload["board_mode_at_end"] = {"exercise": "squat", "inference_mode": "vision_sensor"}
            for rep in payload["rep_events"]:
                rep["classification_source"] = "visual_fallback_no_model"
                rep["visual_result"] = label
                rep["model_class"] = None
            path.write_text(json.dumps(payload), encoding="utf-8")

    out = tmp_path / "squat_out"
    manifest = export_tool.export_dataset([run], out, _args(
        exercise="squat",
        emg_view="stable_remap_pct",
        allow_non_gru_reps=True,
        min_groups_per_label=2,
        min_reps_per_label=8,
        min_windows_per_label=4,
    ))

    assert manifest["exercise"] == "squat"
    assert manifest["training_ready"] is True
    assert manifest["labels"]["standard"]["accepted_groups"] == 2
    assert manifest["labels"]["compensating"]["reps"] == 8
    assert (out / "feature_distribution.json").exists()
    assert (out / "feature_distribution.html").exists()
    dist = json.loads((out / "feature_distribution.json").read_text(encoding="utf-8"))
    assert len(dist["points"]) == 6
    assert dist["labels"]["compensating"]["features"]["Comp_RMS"]["mean"] > dist["labels"]["standard"]["features"]["Comp_RMS"]["mean"]


def test_personal_training_requires_manifest(tmp_path):
    pytest.importorskip("torch")
    train_tool = importlib.import_module("tools.train_gru_three_class_bicep_personal")

    class Args:
        data_dir = str(tmp_path / "missing_dataset")
        out = str(tmp_path / "training_runs" / "candidate_weight.pt")
        report = None
        epochs = 1
        batch_size = 4
        lr = 1e-3
        seed = 42
        seq_len = 30
        stride = 10
        val_ratio = 0.2
        min_recall = 0.8
        allow_not_ready = False
        allow_non_default_emg_view = False
        save_failed = False

    code, result = train_tool.train_personal(Args())

    assert code == 2
    assert result["error"] == "manifest_required"


def test_personal_training_has_augmentation_controls_and_squat_wrapper():
    src = (ROOT / "tools" / "train_gru_three_class_bicep_personal.py").read_text(encoding="utf-8")
    squat_src = (ROOT / "tools" / "train_gru_three_class_squat_personal.py").read_text(encoding="utf-8")
    export_src = (ROOT / "tools" / "ironbuddy_export_personal_squat_gru_dataset.py").read_text(encoding="utf-8")

    assert "augment_window" in src
    assert "augment_rounds" in src
    assert "augment_amp_jitter" in src
    assert "augment_time_warp" in src
    assert "data/squat_personal" in squat_src
    assert "candidate_extreme_fusion_gru.pt" in squat_src
    assert '"--exercise", "squat"' in export_src
