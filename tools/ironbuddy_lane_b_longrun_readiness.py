#!/usr/bin/env python3
"""Long-run readiness audit for Lane B personal GRU, MVC, and squat/MIA.

The script is local-first and read-only. It summarizes what is ready for the
next live acceptance window and writes a compact JSON report when requested.
"""

from __future__ import print_function

import argparse
import json
import os
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = ROOT / "docs" / "test_runs" / "ironbuddy_sensor_lab"
MIA_REPORT = ROOT / "data" / "mia" / "squat" / "_conversion_report.json"
MVC_VALUES = ROOT / "hardware_engine" / "sensor" / "mvc_values.json"
MVC_HELPER = ROOT / "hardware_engine" / "sensor" / "mvc_calibration.py"
UDP_EMG_SERVER = ROOT / "hardware_engine" / "sensor" / "udp_emg_server.py"
DOMAIN_CALIB = ROOT / "hardware_engine" / "sensor" / "domain_calibration.json"
BICEP_CANONICAL = ROOT / "hardware_engine" / "extreme_fusion_gru_bicep.pt"
SQUAT_CANONICAL = ROOT / "hardware_engine" / "extreme_fusion_gru.pt"
V42_SQUAT_METRICS = ROOT / "hardware_engine" / "cognitive" / "weights" / "v42_fusion_head_squat_metrics.json"
V42_CURL_METRICS = ROOT / "hardware_engine" / "cognitive" / "weights" / "v42_fusion_head_curl_metrics.json"

LABELS = ("standard", "compensating", "non_standard")


def _rel(path):
    path = Path(path)
    try:
        return str(path.relative_to(ROOT))
    except Exception:
        return str(path)


def _read_json(path, default=None):
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def _safe_float(value, default=None):
    try:
        value = float(value)
        if value != value:
            return default
        return value
    except Exception:
        return default


def _latest_run():
    runs = sorted([p for p in RUNS_ROOT.glob("20*") if p.is_dir()])
    return runs[-1] if runs else None


def _group_files(run_dir):
    if not run_dir:
        return []
    return sorted((Path(run_dir) / "groups").glob("*.json"))


def _mode_ok(group):
    if group.get("exercise") not in ("bicep_curl", "curl"):
        return False
    for key in ("board_mode_at_start", "board_mode_at_end"):
        mode = group.get(key) if isinstance(group.get(key), dict) else {}
        exercise = mode.get("exercise")
        if exercise == "curl":
            exercise = "bicep_curl"
        if exercise != "bicep_curl" or mode.get("inference_mode") != "vision_sensor":
            return False
    return True


def _signal_ok(group):
    for key in ("start_gate", "end_gate"):
        gate = group.get(key) if isinstance(group.get(key), dict) else {}
        if gate.get("transport_ok") is not True or gate.get("valid_for_gru") is not True:
            return False
    return True


def _count_group_windows(group, seq_len=30, stride=10):
    rows = group.get("gru_7d_samples") or []
    if not rows:
        for window in group.get("gru_last_windows") or []:
            raw = window.get("raw_window") if isinstance(window, dict) else None
            if isinstance(raw, list):
                rows.extend(raw)
    n = len(rows)
    if n < seq_len:
        return 0, n
    return int((n - seq_len) // stride) + 1, n


def _group_sat_ratio(group, threshold=99.0):
    samples = group.get("gru_7d_samples") or []
    vals = []
    for sample in samples:
        values = sample.get("values") if isinstance(sample, dict) else None
        if isinstance(values, list) and len(values) >= 5:
            vals.append((_safe_float(values[3], 0.0), _safe_float(values[4], 0.0)))
    if not vals:
        return None
    hits = sum(1 for a, b in vals if a >= threshold or b >= threshold)
    return hits / float(len(vals))


def audit_personal_bicep(run_dir):
    groups = []
    summary = dict((label, {
        "groups": 0,
        "accepted_groups": 0,
        "gru_reps": 0,
        "windows": 0,
        "rows": 0,
        "rejected": [],
    }) for label in LABELS)
    for path in _group_files(run_dir):
        group = _read_json(path, {})
        label = group.get("label")
        if label not in LABELS:
            continue
        windows, rows = _count_group_windows(group)
        reps = [r for r in group.get("rep_events") or [] if isinstance(r, dict)]
        gru_reps = [r for r in reps if r.get("classification_source") == "gru"]
        sat = _group_sat_ratio(group)
        reasons = []
        if not _mode_ok(group):
            reasons.append("mode_not_bicep_vision_sensor")
        if not _signal_ok(group):
            reasons.append("invalid_signal_gate")
        if rows < 30:
            reasons.append("missing_exact_gru_7d")
        if len(gru_reps) < 4:
            reasons.append("rep_boundary_failed")
        if sat is not None and sat > 0.60:
            reasons.append("range_saturated")
        accepted = not reasons
        item = {
            "file": path.name,
            "label": label,
            "accepted": accepted,
            "reasons": reasons,
            "gru_reps": len(gru_reps),
            "logged_reps": len(reps),
            "windows": windows,
            "rows": rows,
            "sat_ratio": None if sat is None else round(sat, 4),
        }
        groups.append(item)
        s = summary[label]
        s["groups"] += 1
        if accepted:
            s["accepted_groups"] += 1
            s["gru_reps"] += len(gru_reps)
            s["windows"] += windows
            s["rows"] += rows
        else:
            s["rejected"].append({"file": path.name, "reasons": reasons})
    for label in LABELS:
        s = summary[label]
        s["ready"] = bool(s["accepted_groups"] >= 5 and (s["gru_reps"] >= 25 or s["windows"] >= 120))
    return {
        "run_dir": _rel(run_dir) if run_dir else None,
        "groups": groups,
        "labels": summary,
        "training_ready": all(summary[label]["ready"] for label in LABELS),
        "canonical_weight_exists": BICEP_CANONICAL.exists(),
        "personal_candidates": [_rel(p) for p in sorted((ROOT / "hardware_engine").glob("extreme_fusion_gru_bicep_personal_*.pt"))],
    }


def audit_mvc():
    mvc = _read_json(MVC_VALUES, {})
    domain = _read_json(DOMAIN_CALIB, {})
    legacy_v42 = sorted((ROOT / "data" / "v42").glob("user_*/mvc_calibration.json"))
    udp_src = ""
    try:
        udp_src = UDP_EMG_SERVER.read_text(encoding="utf-8", errors="replace")
    except Exception:
        udp_src = ""
    schema_v2 = int(mvc.get("schema_version", 1) or 1) >= 2 if mvc else False
    valid_numbers = False
    if mvc:
        target = _safe_float(mvc.get("target") or (mvc.get("mvc_values") or {}).get("target"))
        comp = _safe_float(mvc.get("comp") or (mvc.get("mvc_values") or {}).get("comp"))
        valid_numbers = bool(target is not None and comp is not None and 50 <= target <= 2000 and 50 <= comp <= 2000)
    return {
        "mvc_helper_exists": MVC_HELPER.exists(),
        "udp_schema_v2_support": "mvc_calibration" in udp_src and "_build_mvc_payload" in udp_src,
        "runtime_mvc_values_exists": MVC_VALUES.exists(),
        "runtime_mvc_schema_v2": schema_v2,
        "runtime_mvc_values_valid": valid_numbers,
        "legacy_v42_mvc_files": [_rel(p) for p in legacy_v42],
        "domain_calibration_exists": DOMAIN_CALIB.exists(),
        "domain_method": ((domain.get("calibration") or {}).get("method_primary") if isinstance(domain, dict) else None),
        "unified_mvc_ready": bool(MVC_VALUES.exists() and schema_v2 and valid_numbers),
        "gap": "runtime/api/lab/voice schemas are not unified" if not schema_v2 else "",
    }


def _metrics_summary(path):
    data = _read_json(path, {})
    return {
        "path": _rel(path),
        "exists": Path(path).exists(),
        "avg_val_f1": data.get("avg_val_f1"),
        "n_folds": data.get("n_folds"),
        "label_names": data.get("label_names"),
    }


def audit_squat_mia():
    mia = _read_json(MIA_REPORT, {})
    label_counts = mia.get("label_counts") or {}
    return {
        "mia_report": _rel(MIA_REPORT),
        "mia_ready": bool(label_counts.get("golden") and label_counts.get("bad")),
        "mia_label_counts": label_counts,
        "squat_canonical_weight_exists": SQUAT_CANONICAL.exists(),
        "v42_squat_fusion_metrics": _metrics_summary(V42_SQUAT_METRICS),
        "v42_curl_fusion_metrics": _metrics_summary(V42_CURL_METRICS),
        "recommended_runtime": "7d_compensation_gru",
        "do_not_use_as_mainline": "v42_dual_branch_fusion_head_until_metrics_improve",
    }


def build_report(run_dir):
    return {
        "ok": True,
        "created_ts": time.time(),
        "run_dir": _rel(run_dir) if run_dir else None,
        "personal_bicep": audit_personal_bicep(run_dir),
        "mvc": audit_mvc(),
        "squat_mia": audit_squat_mia(),
    }


def print_human(report):
    b = report["personal_bicep"]
    print("Lane B 长线预检")
    print("=" * 18)
    print("run_dir: %s" % b.get("run_dir"))
    print("个人弯举训练就绪: %s" % b.get("training_ready"))
    for label in LABELS:
        s = b["labels"][label]
        print("- %s: accepted=%s reps=%s windows=%s ready=%s" % (
            label, s["accepted_groups"], s["gru_reps"], s["windows"], s["ready"]))
        for item in s["rejected"][:5]:
            print("  reject %s: %s" % (item["file"], ",".join(item["reasons"])))
    m = report["mvc"]
    print("MVC统一就绪: %s (helper=%s runtime_values=%s schema_v2=%s domain=%s)" % (
        m["unified_mvc_ready"],
        m["mvc_helper_exists"],
        m["runtime_mvc_values_exists"],
        m["runtime_mvc_schema_v2"],
        m["domain_calibration_exists"],
    ))
    s = report["squat_mia"]
    print("深蹲/MIA资产: ready=%s labels=%s squat_weight=%s" % (
        s["mia_ready"], s["mia_label_counts"], s["squat_canonical_weight_exists"]))
    print("V4.2 fusion 不作为主线: squat_f1=%s curl_f1=%s" % (
        s["v42_squat_fusion_metrics"].get("avg_val_f1"),
        s["v42_curl_fusion_metrics"].get("avg_val_f1"),
    ))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=None,
                        help="Sensor Lab run dir. Defaults to latest run.")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir) if args.run_dir else _latest_run()
    report = build_report(run_dir)
    print_human(report)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        print("json_out: %s" % _rel(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
