#!/usr/bin/env python3
"""Lane B bicep-curl 7D training-vs-live comparison.

This is an offline analysis tool. It reads the original bicep-curl 7D CSVs and
Sensor Lab group JSON files, then reports how the live features differ from the
training distribution. It never changes board state.
"""

from __future__ import print_function

import argparse
import bisect
import csv
import fnmatch
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = ROOT / "docs" / "test_runs" / "ironbuddy_sensor_lab"
DEFAULT_RUN_DIR = RUNS_ROOT / "20260511-203305"
DEFAULT_MODEL = ROOT / "hardware_engine" / "extreme_fusion_gru_bicep.pt"

FEATURES_7D = [
    "Ang_Vel",
    "Angle",
    "Ang_Accel",
    "Target_RMS",
    "Comp_RMS",
    "Symmetry_Score",
    "Phase_Progress",
]

LABELS = ("standard", "compensating", "non_standard")
LABEL_TO_OLD_DIR = {
    "standard": "golden",
    "compensating": "bad",
    "non_standard": "lazy",
}
LABEL_CN = {
    "standard": "标准",
    "compensating": "代偿",
    "non_standard": "不标准",
    "unknown": "未知",
}

STREAM_COLUMNS = [
    "ts",
    "raw0",
    "raw1",
    "filtered0",
    "filtered1",
    "rms0",
    "rms1",
    "pct0",
    "pct1",
    "packet_count",
]


def _round(value, ndigits=4):
    try:
        if value is None:
            return None
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return None
        return round(value, ndigits)
    except Exception:
        return None


def _to_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_div(num, den):
    try:
        den = float(den)
        if den == 0.0:
            return None
        return float(num) / den
    except Exception:
        return None


def _percentile(sorted_values, q):
    if not sorted_values:
        return None
    idx = int(round((len(sorted_values) - 1) * q))
    idx = max(0, min(len(sorted_values) - 1, idx))
    return sorted_values[idx]


def value_stats(values):
    vals = [_to_float(v) for v in values]
    vals = [v for v in vals if v is not None]
    if not vals:
        return {
            "n": 0,
            "mean": None,
            "std": None,
            "min": None,
            "p50": None,
            "p95": None,
            "max": None,
        }
    ordered = sorted(vals)
    mean = sum(vals) / float(len(vals))
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / float(len(vals)))
    return {
        "n": len(vals),
        "mean": _round(mean),
        "std": _round(std),
        "min": _round(min(vals)),
        "p50": _round(_percentile(ordered, 0.50)),
        "p95": _round(_percentile(ordered, 0.95)),
        "max": _round(max(vals)),
    }


def summarize_feature_rows(rows):
    return {feature: value_stats([row.get(feature) for row in rows]) for feature in FEATURES_7D}


def read_json(path):
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def safe_display_path(path):
    path = Path(path)
    try:
        return str(path.relative_to(ROOT))
    except Exception:
        return str(path)


def load_old_training(root):
    root = Path(root)
    by_label = {}
    all_rows = []
    for label in LABELS:
        old_dir_name = LABEL_TO_OLD_DIR[label]
        label_dir = root / old_dir_name
        csv_paths = sorted(label_dir.glob("*.csv")) if label_dir.is_dir() else []
        rows = []
        for path in csv_paths:
            try:
                with path.open("r", encoding="utf-8", newline="") as f:
                    reader = csv.DictReader(f)
                    for raw in reader:
                        row = {}
                        ok = True
                        for feature in FEATURES_7D:
                            value = _to_float(raw.get(feature))
                            if value is None:
                                ok = False
                                break
                            row[feature] = value
                        if ok:
                            rows.append(row)
                            all_rows.append(dict(row, label=label))
            except Exception:
                continue
        by_label[label] = {
            "label": label,
            "label_cn": LABEL_CN.get(label, label),
            "old_dir": old_dir_name,
            "path": safe_display_path(label_dir) if label_dir.exists() else str(label_dir),
            "files": len(csv_paths),
            "rows": len(rows),
            "stats": summarize_feature_rows(rows),
        }
    return {
        "source": "data/bicep_curl/{golden,bad,lazy}",
        "raw_adc_status": "raw_adc_not_available_in_old_training_csv",
        "features": FEATURES_7D,
        "by_label": by_label,
        "pooled_stats": summarize_feature_rows(all_rows),
        "row_count": len(all_rows),
    }


def load_group_files(run_dir, discard_globs):
    groups_dir = Path(run_dir) / "groups"
    paths = sorted(groups_dir.glob("*.json"))
    kept = []
    discarded = []
    for path in paths:
        discard_reason = None
        for pattern in discard_globs:
            if fnmatch.fnmatch(path.name, pattern):
                discard_reason = pattern
                break
        if discard_reason:
            discarded.append({"path": str(path), "file": path.name, "reason": discard_reason})
        else:
            kept.append(path)
    return kept, discarded


def _angle_value(snapshot):
    for key in ("decision_angle", "smooth_angle", "angle", "raw_angle", "side_angle"):
        value = _to_float(snapshot.get(key))
        if value is not None:
            return value
    return None


def extract_angle_rows(group):
    snapshots = []
    for item in group.get("angle_debug_snapshots") or []:
        if not isinstance(item, dict):
            continue
        ts = _to_float(item.get("ts"), _to_float(item.get("capture_ts")))
        angle = _angle_value(item)
        if ts is None or angle is None:
            continue
        snapshots.append((ts, angle, item))
    snapshots.sort(key=lambda x: x[0])
    rows = []
    prev_angle = None
    prev_vel = 0.0
    history = []
    for ts, angle, raw in snapshots:
        if prev_angle is None:
            ang_vel = 0.0
        else:
            # Board runtime uses per-frame angle delta, not deg/s, for GRU input.
            ang_vel = angle - prev_angle
        ang_accel = ang_vel - prev_vel
        prev_angle = angle
        prev_vel = ang_vel
        history.append(angle)
        history = history[-16:]
        a_min = min(history)
        a_max = max(history)
        phase = 1.0 - (angle - a_min) / max(a_max - a_min, 1.0)
        rows.append({
            "ts": ts,
            "Angle": angle,
            "Ang_Vel": ang_vel,
            "Ang_Accel": ang_accel,
            "Symmetry_Score": 1.0,
            "Phase_Progress": max(0.0, min(1.0, phase)),
            "angle_debug": raw,
        })
    return rows


def clean_stream_rows(group):
    rows = []
    for raw in group.get("stream_samples") or []:
        if not isinstance(raw, (list, tuple)) or len(raw) < 9:
            continue
        row = []
        ok = True
        for idx in range(min(len(raw), len(STREAM_COLUMNS))):
            value = _to_float(raw[idx])
            if idx == 0 and value is None:
                ok = False
                break
            row.append(value if value is not None else 0.0)
        while len(row) < len(STREAM_COLUMNS):
            row.append(0.0)
        if ok:
            rows.append(row)
    rows.sort(key=lambda r: r[0])
    return rows


def nearest_stream(stream_rows, ts, max_skew_s=0.75):
    if not stream_rows:
        return None, None
    times = [row[0] for row in stream_rows]
    idx = bisect.bisect_left(times, ts)
    candidates = []
    if idx < len(stream_rows):
        candidates.append(stream_rows[idx])
    if idx > 0:
        candidates.append(stream_rows[idx - 1])
    if not candidates:
        return None, None
    best = min(candidates, key=lambda row: abs(row[0] - ts))
    skew = abs(best[0] - ts)
    if skew > max_skew_s:
        return None, skew
    return best, skew


def _clip_pct(value):
    value = _to_float(value, 0.0) or 0.0
    return max(0.0, min(100.0, value))


def build_current_7d_rows(group, emg_mode="current_pct", remap=None):
    angle_rows = extract_angle_rows(group)
    stream_rows = clean_stream_rows(group)
    out = []
    skews = []
    for angle_row in angle_rows:
        stream, skew = nearest_stream(stream_rows, angle_row["ts"])
        if stream is None:
            continue
        if skew is not None:
            skews.append(skew)
        if emg_mode == "current_pct":
            target = _clip_pct(stream[7])
            comp = _clip_pct(stream[8])
        elif emg_mode == "old_pct400":
            target = _clip_pct((stream[5] / 400.0) * 100.0)
            comp = _clip_pct((stream[6] / 400.0) * 100.0)
        elif emg_mode == "remap_pct":
            target = _clip_pct(stream[7])
            comp = _clip_pct(stream[8])
            if remap:
                remap_features = remap.get("features") if isinstance(remap.get("features"), dict) else remap
                target = remap_value(target, remap_features.get("Target_RMS"))
                comp = remap_value(comp, remap_features.get("Comp_RMS"))
        else:
            raise ValueError("unknown emg_mode: %s" % emg_mode)
        out.append({
            "ts": angle_row["ts"],
            "Ang_Vel": angle_row["Ang_Vel"],
            "Angle": angle_row["Angle"],
            "Ang_Accel": angle_row["Ang_Accel"],
            "Target_RMS": target,
            "Comp_RMS": comp,
            "Symmetry_Score": angle_row["Symmetry_Score"],
            "Phase_Progress": angle_row["Phase_Progress"],
            "stream_packet_count": stream[9],
            "stream_skew_s": skew,
            "source": "angle_debug+stream_samples",
        })
    return out, {
        "angle_rows": len(angle_rows),
        "stream_rows": len(stream_rows),
        "matched_rows": len(out),
        "skew_s": value_stats(skews),
        "note": "approximate; exact runtime 7D capture is available only after gru_7d_buffer deployment",
    }


def raw_rms_summary(group):
    rows = clean_stream_rows(group)
    if not rows:
        return {"ok": False, "samples": 0}
    by_ch = []
    for idx, name in ((1, "raw0"), (2, "raw1")):
        vals = [row[idx] for row in rows]
        mean = sum(vals) / float(len(vals)) if vals else 0.0
        centered = [v - mean for v in vals]
        rms = math.sqrt(sum(v * v for v in centered) / float(len(centered))) if centered else None
        jumps = [abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))]
        by_ch.append({
            "channel": name,
            "raw_adc": value_stats(vals),
            "centered_rms": _round(rms),
            "centered_rms_norm_2048": _round((rms or 0.0) / 2048.0),
            "mean_abs_jump": _round(sum(jumps) / float(len(jumps))) if jumps else None,
        })
    return {"ok": True, "samples": len(rows), "channels": by_ch}


def exact_gru_7d_rows(group):
    rows = []
    for sample in group.get("gru_7d_samples") or []:
        if not isinstance(sample, dict):
            continue
        values = sample.get("values")
        if not isinstance(values, list) or len(values) < len(FEATURES_7D):
            features = sample.get("features") if isinstance(sample.get("features"), dict) else {}
            values = [features.get(col) for col in FEATURES_7D]
        row = {}
        ok = True
        for idx, feature in enumerate(FEATURES_7D):
            value = _to_float(values[idx] if idx < len(values) else None)
            if value is None:
                ok = False
                break
            row[feature] = value
        if ok:
            row["ts"] = _to_float(sample.get("ts"))
            row["source"] = "gru_7d_samples"
            rows.append(row)
    return rows


def remap_value(value, cfg):
    if not cfg:
        return _clip_pct(value)
    src_mean = cfg.get("src_mean")
    src_std = cfg.get("src_std")
    dst_mean = cfg.get("dst_mean")
    dst_std = cfg.get("dst_std")
    if src_mean is None or src_std in (None, 0) or dst_mean is None or dst_std is None:
        return _clip_pct(value)
    mapped = float(dst_mean) + ((float(value) - float(src_mean)) / float(src_std)) * float(dst_std)
    return _clip_pct(mapped)


def build_global_remap(old_training, group_rows_by_mode):
    current_rows = []
    for rows in group_rows_by_mode.get("current_pct", {}).values():
        current_rows.extend(rows)
    cfg = {}
    for feature in ("Target_RMS", "Comp_RMS"):
        src = value_stats([row.get(feature) for row in current_rows])
        dst = old_training.get("pooled_stats", {}).get(feature, {})
        cfg[feature] = {
            "src_mean": src.get("mean"),
            "src_std": src.get("std"),
            "dst_mean": dst.get("mean"),
            "dst_std": dst.get("std"),
        }
    return {
        "method": "pooled z-score map: live current_pct -> original training pooled Target/Comp distribution",
        "leaks_label": False,
        "features": cfg,
    }


def diff_vs_old(row_stats, old_stats):
    out = {}
    for feature in FEATURES_7D:
        cur = row_stats.get(feature, {})
        old = old_stats.get(feature, {})
        cur_mean = cur.get("mean")
        old_mean = old.get("mean")
        old_std = old.get("std")
        delta = None
        z = None
        if cur_mean is not None and old_mean is not None:
            delta = cur_mean - old_mean
            if old_std not in (None, 0):
                z = delta / old_std
        out[feature] = {
            "current_mean": cur_mean,
            "old_mean": old_mean,
            "delta": _round(delta),
            "z_vs_old": _round(z),
        }
    return out


def summarize_reps(group, label):
    rep_events = group.get("rep_events") or []
    source_counts = Counter()
    pred_counts = Counter()
    confusion = defaultdict(Counter)
    eligible = 0
    correct = 0
    angle_mins = []
    for rep in rep_events:
        if not isinstance(rep, dict):
            continue
        source = rep.get("classification_source") or "unknown"
        pred = rep.get("model_class") or rep.get("visual_result") or "unknown"
        source_counts[source] += 1
        pred_counts[pred] += 1
        angle = _to_float(rep.get("angle_min"))
        if angle is not None:
            angle_mins.append(angle)
        if source == "gru":
            eligible += 1
            if pred == label:
                correct += 1
            confusion[label][pred] += 1
    return {
        "rep_count": len(rep_events),
        "gru_rep_count": eligible,
        "correct": correct,
        "accuracy_gru_only": _round(_safe_div(correct, eligible)),
        "source_counts": dict(source_counts),
        "prediction_counts": dict(pred_counts),
        "confusion_gru_only": {k: dict(v) for k, v in confusion.items()},
        "angle_min": value_stats(angle_mins),
    }


def count_side_switches(values):
    prev = None
    count = 0
    for value in values:
        if not value:
            continue
        if prev is not None and value != prev:
            count += 1
        prev = value
    return count


def estimate_angle_valleys(angle_rows):
    valleys = []
    last_ts = -1e9
    for i in range(1, len(angle_rows) - 1):
        prev = angle_rows[i - 1]
        cur = angle_rows[i]
        nxt = angle_rows[i + 1]
        if cur["Angle"] <= prev["Angle"] and cur["Angle"] <= nxt["Angle"]:
            local_amp = max(prev["Angle"], nxt["Angle"]) - cur["Angle"]
            if cur["Angle"] <= 135.0 and local_amp >= 12.0 and cur["ts"] - last_ts >= 0.35:
                valleys.append({"ts": cur["ts"], "angle": _round(cur["Angle"]), "local_amp": _round(local_amp)})
                last_ts = cur["ts"]
    return valleys


def missed_rep_report(group, rep_summary):
    angle_rows = extract_angle_rows(group)
    snaps = [row.get("angle_debug") or {} for row in angle_rows]
    state_counts = Counter(s.get("state") or "unknown" for s in snaps)
    selected_sides = [s.get("selected_side") for s in snaps]
    active_sides = [s.get("active_side") for s in snaps]
    side_trends = Counter(s.get("side_trend") or "unknown" for s in snaps)
    trends = Counter(s.get("trend") or "unknown" for s in snaps)
    opening = [_to_float(s.get("opening_frames")) for s in snaps]
    side_closing = [_to_float(s.get("side_closing_frames")) for s in snaps]
    rep_in_progress = Counter(str(bool(s.get("rep_in_progress"))) for s in snaps)
    valleys = estimate_angle_valleys(angle_rows)
    duration = None
    if angle_rows:
        duration = angle_rows[-1]["ts"] - angle_rows[0]["ts"]
    possible = []
    if count_side_switches(selected_sides) >= max(3, len(angle_rows) // 15):
        possible.append("selected_side_switching_high")
    if rep_summary.get("rep_count", 0) < len(valleys):
        possible.append("angle_valleys_exceed_logged_reps")
    if (value_stats(opening).get("max") or 0) < 2:
        possible.append("opening_frames_gate_may_not_be_met")
    if (value_stats(side_closing).get("max") or 0) < 2:
        possible.append("closing_frames_gate_may_not_be_met")
    if state_counts.get("CURLING", 0) and rep_summary.get("rep_count", 0) <= 1:
        possible.append("curling_state_seen_but_finalize_gate_sparse")
    return {
        "ok": bool(angle_rows),
        "snapshot_count": len(angle_rows),
        "duration_s": _round(duration),
        "logged_rep_count": rep_summary.get("rep_count", 0),
        "estimated_angle_valleys": len(valleys),
        "valley_samples": valleys[:12],
        "state_counts": dict(state_counts),
        "selected_side_counts": dict(Counter(v or "" for v in selected_sides)),
        "selected_side_switches": count_side_switches(selected_sides),
        "active_side_counts": dict(Counter(v or "" for v in active_sides)),
        "side_trend_counts": dict(side_trends),
        "trend_counts": dict(trends),
        "rep_in_progress_counts": dict(rep_in_progress),
        "opening_frames": value_stats(opening),
        "side_closing_frames": value_stats(side_closing),
        "angle": value_stats([row["Angle"] for row in angle_rows]),
        "possible_miss_reasons": possible,
        "note": "diagnostic only; this report does not change FSM thresholds",
    }


def normalize_window(rows):
    out = []
    for row in rows:
        feat = [float(row.get(feature, 0.0) or 0.0) for feature in FEATURES_7D]
        feat[0] = max(-3.0, min(3.0, feat[0] / 30.0))
        feat[1] = feat[1] / 180.0
        feat[2] = max(-1.0, min(1.0, feat[2] / 10.0))
        feat[3] = _clip_pct(feat[3]) / 100.0
        feat[4] = _clip_pct(feat[4]) / 100.0
        out.append(feat)
    return out


def replay_rows(rows_by_group, model_path, seq_len=30, stride=10):
    if not Path(model_path).exists():
        return {"ok": False, "reason": "model_not_found", "model_path": str(model_path)}
    try:
        import numpy as np
        sys.path.insert(0, str(ROOT / "hardware_engine"))
        from cognitive.fusion_model import load_model
        model = load_model(str(model_path), input_size=7)
    except Exception as exc:
        return {"ok": False, "reason": "model_load_failed", "error": str(exc), "model_path": str(model_path)}

    by_group = {}
    for group_id, rows in rows_by_group.items():
        preds = []
        if len(rows) >= seq_len:
            for start in range(0, len(rows) - seq_len + 1, stride):
                window = normalize_window(rows[start:start + seq_len])
                try:
                    result = model.infer(np.array(window, dtype=np.float32))
                    preds.append(result)
                except Exception as exc:
                    preds.append({"error": str(exc)})
        pred_counts = Counter(p.get("classification", "error") for p in preds)
        by_group[group_id] = {
            "windows": len(preds),
            "prediction_counts": dict(pred_counts),
            "confidence": value_stats([p.get("confidence") for p in preds if isinstance(p, dict)]),
            "similarity": value_stats([p.get("similarity") for p in preds if isinstance(p, dict)]),
            "first_predictions": preds[:5],
        }
    return {
        "ok": True,
        "model_path": str(model_path),
        "seq_len": seq_len,
        "stride": stride,
        "by_group": by_group,
    }


def analyze_run(run_dir, discard_globs, old_data_dir, model_path, do_replay=True):
    old = load_old_training(old_data_dir)
    group_paths, discarded = load_group_files(run_dir, discard_globs)
    groups = []
    rows_by_mode = {"current_pct": {}, "old_pct400": {}, "remap_pct": {}, "exact_runtime": {}}

    loaded_groups = []
    for path in group_paths:
        data = read_json(path)
        label = data.get("label") or "unknown"
        group_id = data.get("group_id") or path.stem
        loaded_groups.append((path, data, label, group_id))
        rows, _meta = build_current_7d_rows(data, emg_mode="current_pct")
        rows_by_mode["current_pct"][group_id] = rows
        rows_by_mode["exact_runtime"][group_id] = exact_gru_7d_rows(data)

    remap = build_global_remap(old, rows_by_mode)

    for path, data, label, group_id in loaded_groups:
        mode_blocks = {}
        for mode in ("current_pct", "old_pct400", "remap_pct"):
            rows, meta = build_current_7d_rows(data, emg_mode=mode, remap=remap)
            rows_by_mode[mode][group_id] = rows
            stats = summarize_feature_rows(rows)
            old_label_stats = old.get("by_label", {}).get(label, {}).get("stats", {})
            mode_blocks[mode] = {
                "row_count": len(rows),
                "alignment": meta,
                "stats": stats,
                "diff_vs_old_label": diff_vs_old(stats, old_label_stats),
            }
        exact_rows = exact_gru_7d_rows(data)
        exact_stats = summarize_feature_rows(exact_rows)
        old_label_stats = old.get("by_label", {}).get(label, {}).get("stats", {})
        rep_summary = summarize_reps(data, label)
        group_report = {
            "file": path.name,
            "path": str(path),
            "group_id": group_id,
            "label": label,
            "label_cn": LABEL_CN.get(label, label),
            "started_ts": data.get("started_ts"),
            "completed_ts": data.get("completed_ts"),
            "rep_summary": rep_summary,
            "emg_modes": mode_blocks,
            "raw_rms_summary": raw_rms_summary(data),
            "exact_gru_7d_available": bool(exact_rows or data.get("gru_last_windows")),
            "exact_gru_7d": {
                "row_count": len(exact_rows),
                "stats": exact_stats,
                "diff_vs_old_label": diff_vs_old(exact_stats, old_label_stats),
                "last_window_count": len(data.get("gru_last_windows") or []),
            },
        }
        if label == "compensating":
            group_report["missed_rep_report"] = missed_rep_report(data, rep_summary)
        groups.append(group_report)

    replay = {}
    if do_replay:
        for mode in ("current_pct", "old_pct400", "remap_pct"):
            replay[mode] = replay_rows(rows_by_mode[mode], model_path)
        if any(rows_by_mode.get("exact_runtime", {}).values()):
            replay["exact_runtime"] = replay_rows(rows_by_mode["exact_runtime"], model_path)
    else:
        replay["skipped"] = True

    return {
        "ok": True,
        "run_dir": str(run_dir),
        "discarded_groups": discarded,
        "used_group_files": [g["file"] for g in groups],
        "features": FEATURES_7D,
        "old_training": old,
        "global_remap": remap,
        "groups": groups,
        "replay": replay,
        "lineage": {
            "old_data_dir": str(old_data_dir),
            "model_path": str(model_path),
            "tool": "tools/ironbuddy_lane_b_7d_compare.py",
        },
    }


def fmt_num(value, ndigits=2):
    value = _round(value, ndigits)
    return "-" if value is None else ("%.*f" % (ndigits, value))


def markdown_table(headers, rows):
    lines = []
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        lines.append("| " + " | ".join(str(x) for x in row) + " |")
    return "\n".join(lines)


def build_markdown(report):
    lines = []
    lines.append("# Lane B 7D 训练口径对照报告")
    lines.append("")
    lines.append("- 数据源: `%s`" % report.get("run_dir"))
    lines.append("- 已排除: `%s`" % ", ".join(d.get("file", "") for d in report.get("discarded_groups", [])))
    lines.append("- 使用组: `%s`" % ", ".join(report.get("used_group_files", [])))
    lines.append("- 旧训练源: `data/bicep_curl/{golden,bad,lazy}`")
    lines.append("")

    lines.append("## 旧训练 7D 分布")
    old_rows = []
    old = report.get("old_training", {}).get("by_label", {})
    for label in LABELS:
        stats = old.get(label, {}).get("stats", {})
        old_rows.append([
            LABEL_CN[label],
            old.get(label, {}).get("rows", 0),
            fmt_num(stats.get("Angle", {}).get("mean")),
            fmt_num(stats.get("Target_RMS", {}).get("mean")),
            fmt_num(stats.get("Comp_RMS", {}).get("mean")),
            fmt_num(stats.get("Ang_Vel", {}).get("p95")),
        ])
    lines.append(markdown_table(["类别", "样本", "Angle均值", "Target_RMS均值", "Comp_RMS均值", "Ang_Vel p95"], old_rows))
    lines.append("")

    lines.append("## 当前组结果")
    group_rows = []
    for group in report.get("groups", []):
        rep = group.get("rep_summary", {})
        cur = group.get("emg_modes", {}).get("current_pct", {}).get("stats", {})
        oldpct = group.get("emg_modes", {}).get("old_pct400", {}).get("stats", {})
        group_rows.append([
            group.get("group_id"),
            group.get("label_cn"),
            rep.get("rep_count"),
            rep.get("gru_rep_count"),
            rep.get("prediction_counts", {}),
            fmt_num(cur.get("Target_RMS", {}).get("mean")),
            fmt_num(cur.get("Comp_RMS", {}).get("mean")),
            fmt_num(oldpct.get("Target_RMS", {}).get("mean")),
            fmt_num(oldpct.get("Comp_RMS", {}).get("mean")),
        ])
    lines.append(markdown_table([
        "组", "真值", "rep", "GRU rep", "预测", "current Target", "current Comp", "old/400 Target", "old/400 Comp"
    ], group_rows))
    lines.append("")

    lines.append("## 与旧训练同标签差异")
    diff_rows = []
    for group in report.get("groups", []):
        for mode in ("current_pct", "old_pct400", "remap_pct"):
            diff = group.get("emg_modes", {}).get(mode, {}).get("diff_vs_old_label", {})
            diff_rows.append([
                group.get("label_cn"),
                mode,
                fmt_num(diff.get("Target_RMS", {}).get("delta")),
                fmt_num(diff.get("Target_RMS", {}).get("z_vs_old")),
                fmt_num(diff.get("Comp_RMS", {}).get("delta")),
                fmt_num(diff.get("Comp_RMS", {}).get("z_vs_old")),
                fmt_num(diff.get("Angle", {}).get("delta")),
            ])
    lines.append(markdown_table([
        "真值", "口径", "Target差值", "Target z", "Comp差值", "Comp z", "Angle差值"
    ], diff_rows))
    lines.append("")

    lines.append("## Offline replay")
    lines.append("")
    lines.append("> 注意：这里的 replay 使用 Lab 的 `angle_debug + stream_samples` 近似 7D；真实线上结果仍以 DB `rep_events` 为准。后续 exact GRU 7D buffer 部署后，再用 exact window 复算。")
    lines.append("")
    replay_rows_md = []
    for mode, block in (report.get("replay") or {}).items():
        if not isinstance(block, dict) or not block.get("ok"):
            replay_rows_md.append([mode, "not_ok", block.get("reason") if isinstance(block, dict) else "skipped", "-", "-"])
            continue
        for group_id, result in block.get("by_group", {}).items():
            replay_rows_md.append([
                mode,
                group_id,
                result.get("windows"),
                result.get("prediction_counts"),
                fmt_num(result.get("confidence", {}).get("mean"), 4),
            ])
    lines.append(markdown_table(["口径", "组", "窗口", "预测分布", "confidence均值"], replay_rows_md))
    lines.append("")

    comp = [g for g in report.get("groups", []) if g.get("label") == "compensating"]
    if comp:
        lines.append("## 代偿漏 rep 诊断")
        m = comp[0].get("missed_rep_report", {})
        lines.append("- 已记录 rep: `%s`" % m.get("logged_rep_count"))
        lines.append("- 角度谷值估计: `%s`" % m.get("estimated_angle_valleys"))
        lines.append("- 侧别切换: `%s`" % m.get("selected_side_switches"))
        lines.append("- 可能原因: `%s`" % ", ".join(m.get("possible_miss_reasons", [])))
        lines.append("- state_counts: `%s`" % m.get("state_counts"))
        lines.append("")

    lines.append("## 初步结论")
    lines.append("- `classification_source=gru` 的 rep 才计入 GRU；本次有效组里 GRU 确实参与，但输出几乎全是 standard。")
    lines.append("- 当前 `current_pct` 的 Target/Comp 分布明显高于旧 7D 训练分布，属于输入口径偏移，不应先调阈值。")
    lines.append("- `old_pct400` 是更接近旧训练口径的候选，但还需要看 replay 和后续 exact GRU 7D 窗口确认。")
    lines.append("- 旧训练语义要如实保留：旧 `bad -> compensating` 的 Comp_RMS 反而偏低，不能按直觉假设代偿通道一定更高。")
    lines.append("")
    return "\n".join(lines)


def latest_run_dir():
    runs = [p for p in RUNS_ROOT.iterdir() if p.is_dir()] if RUNS_ROOT.is_dir() else []
    return sorted(runs)[-1] if runs else DEFAULT_RUN_DIR


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR if DEFAULT_RUN_DIR.exists() else latest_run_dir()))
    parser.add_argument("--old-data-dir", default=str(ROOT / "data" / "bicep_curl"))
    parser.add_argument("--model-path", default=str(DEFAULT_MODEL))
    parser.add_argument("--discard", action="append", default=["*_001_standard.json"],
                        help="glob pattern relative to group file name; can be repeated")
    parser.add_argument("--out-json", default=None)
    parser.add_argument("--out-md", default=None)
    parser.add_argument("--no-replay", action="store_true")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    if args.out_json is None:
        args.out_json = str(run_dir / "lane_b_7d_compare_report.json")
    if args.out_md is None:
        args.out_md = str(run_dir / "lane_b_7d_compare_report.md")

    report = analyze_run(
        run_dir=run_dir,
        discard_globs=args.discard,
        old_data_dir=Path(args.old_data_dir),
        model_path=Path(args.model_path),
        do_replay=not args.no_replay,
    )
    atomic_write(args.out_json, report)
    Path(args.out_md).write_text(build_markdown(report), encoding="utf-8")

    print("ok: wrote %s" % args.out_json)
    print("ok: wrote %s" % args.out_md)
    for group in report.get("groups", []):
        rep = group.get("rep_summary", {})
        cur = group.get("emg_modes", {}).get("current_pct", {}).get("stats", {})
        print("%s label=%s reps=%s gru=%s preds=%s target_mean=%s comp_mean=%s" % (
            group.get("group_id"),
            group.get("label"),
            rep.get("rep_count"),
            rep.get("gru_rep_count"),
            rep.get("prediction_counts"),
            fmt_num(cur.get("Target_RMS", {}).get("mean")),
            fmt_num(cur.get("Comp_RMS", {}).get("mean")),
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
