#!/usr/bin/env python3
"""Export current Sensor Lab groups into a personal bicep-curl GRU dataset.

The exporter intentionally uses only current Lab evidence. Old
data/bicep_curl/* files are never read here.

Input modes (mutually exclusive, must pass exactly one):
  --run-dir <path>      Scan <path>/groups/*.json. Can be repeated to merge
                        multiple Sensor Lab runs.
  --group-files a,b,c   Comma-separated explicit group json paths. Use this
                        to merge a hand-picked subset across sessions (e.g.
                        first session 3 groups + second session 3 groups for
                        a unified retraining set).
"""

from __future__ import print_function

import argparse
import csv
import json
import math
import re
import time
from collections import defaultdict
from pathlib import Path

try:
    from tools.ironbuddy_lane_b_emg_preprocess import (
        DEFAULT_EMG_VIEW,
        PREPROCESS_VERSION,
        VALID_EMG_VIEWS,
        apply_emg_view_to_exact_rows,
        build_stream_view_rows,
        load_runtime_preprocess_meta,
        normalize_preprocess_meta,
        pooled_raw_rms_robust_meta,
        raw_rms_robust_meta_from_stream,
        summarize_stream_views,
    )
except Exception:
    from ironbuddy_lane_b_emg_preprocess import (  # type: ignore
        DEFAULT_EMG_VIEW,
        PREPROCESS_VERSION,
        VALID_EMG_VIEWS,
        apply_emg_view_to_exact_rows,
        build_stream_view_rows,
        load_runtime_preprocess_meta,
        normalize_preprocess_meta,
        pooled_raw_rms_robust_meta,
        raw_rms_robust_meta_from_stream,
        summarize_stream_views,
    )


ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = ROOT / "docs" / "test_runs" / "ironbuddy_sensor_lab"
PERSONAL_ROOT = ROOT / "data" / "bicep_curl_personal"
DEFAULT_OUT_ROOT = PERSONAL_ROOT / "datasets"
DEFAULT_EXERCISE = "bicep_curl"
PERSONAL_ROOT_BY_EXERCISE = {
    "bicep_curl": ROOT / "data" / "bicep_curl_personal",
    "squat": ROOT / "data" / "squat_personal",
}
PERSONAL_ROOT_LABEL_BY_EXERCISE = {
    "bicep_curl": "data/bicep_curl_personal",
    "squat": "data/squat_personal",
}

FEATURES_7D = [
    "Ang_Vel",
    "Angle",
    "Ang_Accel",
    "Target_RMS",
    "Comp_RMS",
    "Symmetry_Score",
    "Phase_Progress",
]
CSV_FIELDS = [
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
    "source_group_id",
    "source_run",
]
LABEL_TO_USER_LABEL = {
    "standard": "golden",
    "compensating": "bad",
    "non_standard": "lazy",
}
USER_LABEL_TO_LABEL = dict((v, k) for k, v in LABEL_TO_USER_LABEL.items())
LABEL_CN = {
    "standard": "标准",
    "compensating": "代偿",
    "non_standard": "不标准",
}


def _canonical_exercise(value):
    if value in ("curl", "bicep_curl"):
        return "bicep_curl"
    if value == "squat":
        return "squat"
    return value or ""


def _exercise_slug(value):
    value = _canonical_exercise(value)
    return value if value in ("bicep_curl", "squat") else DEFAULT_EXERCISE


def _to_float(value, default=None):
    try:
        if value is None:
            return default
        value = float(value)
        if math.isnan(value) or math.isinf(value):
            return default
        return value
    except Exception:
        return default


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _round(value, ndigits=4):
    value = _to_float(value)
    return None if value is None else round(value, ndigits)


def _safe_name(value):
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "unknown"))


def _read_json(path):
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _rel(path):
    path = Path(path)
    try:
        return str(path.relative_to(ROOT))
    except Exception:
        return str(path)


def _run_tag(run_dirs):
    names = sorted([Path(path).name for path in (run_dirs or []) if path])
    if not names:
        return "no_run"
    if len(names) == 1:
        return names[0]
    return "%s_plus_%s" % (names[-1], len(names) - 1)


def _group_preprocess_meta(group):
    meta = group.get("emg_preprocess") if isinstance(group.get("emg_preprocess"), dict) else {}
    return normalize_preprocess_meta(meta or load_runtime_preprocess_meta())


def _export_preprocess_meta(group, args, pooled_robust=None):
    meta = _group_preprocess_meta(group)
    if getattr(args, "emg_view", DEFAULT_EMG_VIEW) == "raw_rms_robust100":
        meta = dict(meta)
        meta["preprocess_version"] = PREPROCESS_VERSION
        meta["default_training_view"] = "raw_rms_robust100"
        if pooled_robust:
            meta["raw_rms_robust100"] = dict(pooled_robust)
        else:
            meta["raw_rms_robust100"] = raw_rms_robust_meta_from_stream(group.get("stream_samples") or [])
    return normalize_preprocess_meta(meta)


def _materialize_rows_for_view(group, emg_view, args, pooled_robust=None):
    exact_rows = extract_exact_rows(group)
    if emg_view == "current_pct":
        return {
            "rows": exact_rows,
            "matched_count": len(exact_rows),
            "fallback_current_count": 0,
            "input_exact_rows": len(exact_rows),
            "output_rows": len(exact_rows),
        }
    return apply_emg_view_to_exact_rows(
        exact_rows,
        group.get("stream_samples") or [],
        emg_view=emg_view,
        preprocess_meta=_export_preprocess_meta(group, args, pooled_robust=pooled_robust),
    )


def latest_run_dir():
    runs = [p for p in RUNS_ROOT.iterdir() if p.is_dir()] if RUNS_ROOT.is_dir() else []
    if not runs:
        return None
    return sorted(runs)[-1]


def _sample_values(sample):
    sample = sample if isinstance(sample, dict) else {}
    values = sample.get("values")
    if isinstance(values, list) and len(values) >= len(FEATURES_7D):
        return [_to_float(v) for v in values[:len(FEATURES_7D)]]
    features = sample.get("features") if isinstance(sample.get("features"), dict) else {}
    values = [_to_float(features.get(col)) for col in FEATURES_7D]
    return values if any(v is not None for v in values) else []


def extract_exact_rows(group):
    """Return exact runtime 7D rows from gru_7d_samples or gru_last_windows."""
    rows = []
    for sample in group.get("gru_7d_samples") or []:
        values = _sample_values(sample)
        if len(values) < len(FEATURES_7D) or any(v is None for v in values):
            continue
        rows.append({
            "ts": _to_float(sample.get("ts"), 0.0) or 0.0,
            "values": [float(v) for v in values],
            "pose_score": _to_float(sample.get("pose_score"), 0.0) or 0.0,
            "source": "gru_7d_samples",
        })
    if rows:
        return sorted(rows, key=lambda r: r["ts"])

    # Fallback for older/newer groups that only captured exact rep windows.
    synthetic_ts = 0.0
    for window in group.get("gru_last_windows") or []:
        raw_window = window.get("raw_window") if isinstance(window, dict) else None
        if not isinstance(raw_window, list):
            continue
        base_ts = _to_float(window.get("ts"), synthetic_ts) or synthetic_ts
        for idx, values in enumerate(raw_window):
            if not isinstance(values, list) or len(values) < len(FEATURES_7D):
                continue
            clean = [_to_float(v) for v in values[:len(FEATURES_7D)]]
            if any(v is None for v in clean):
                continue
            rows.append({
                "ts": base_ts + idx * 0.02,
                "values": [float(v) for v in clean],
                "pose_score": 0.0,
                "source": "gru_last_windows.raw_window",
            })
        synthetic_ts = max(synthetic_ts + 10.0, base_ts + 10.0)
    return sorted(rows, key=lambda r: r["ts"])


def count_reps(group):
    reps = [r for r in (group.get("rep_events") or []) if isinstance(r, dict)]
    gru_reps = [r for r in reps if (r.get("classification_source") or "") == "gru"]
    return len(reps), len(gru_reps)


def _mode_snapshot_ok(snapshot, exercise=DEFAULT_EXERCISE):
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    actual_exercise = _canonical_exercise(snapshot.get("exercise"))
    mode = snapshot.get("inference_mode")
    return actual_exercise == _exercise_slug(exercise) and mode == "vision_sensor"


def _signal_gate_ok(gate):
    gate = gate if isinstance(gate, dict) else {}
    return gate.get("transport_ok") is True and gate.get("valid_for_gru") is True


def saturation_ratio(rows, feature_index, threshold):
    if not rows:
        return None
    vals = [row["values"][feature_index] for row in rows]
    return sum(1 for v in vals if v >= threshold) / float(len(vals))


def estimated_windows(row_count, seq_len, stride):
    if row_count < seq_len:
        return 0
    return int((row_count - seq_len) // stride) + 1


def evaluate_group(path, group, args, pooled_robust=None):
    label = group.get("label")
    user_label = LABEL_TO_USER_LABEL.get(label)
    group_id = group.get("group_id") or Path(path).stem
    exercise = _exercise_slug(getattr(args, "exercise", DEFAULT_EXERCISE))
    row_materialized = _materialize_rows_for_view(group, args.emg_view, args, pooled_robust=pooled_robust)
    rows = row_materialized.get("rows") or []
    rep_count, gru_rep_count = count_reps(group)
    target_sat = saturation_ratio(rows, FEATURES_7D.index("Target_RMS"), args.saturation_threshold)
    comp_sat = saturation_ratio(rows, FEATURES_7D.index("Comp_RMS"), args.saturation_threshold)
    max_sat = max([v for v in (target_sat, comp_sat) if v is not None], default=None)
    windows = estimated_windows(len(rows), args.seq_len, args.stride)
    reasons = []
    allow_mode_mismatch = bool(getattr(args, "allow_mode_mismatch", False))
    allow_invalid_signal_gate = bool(getattr(args, "allow_invalid_signal_gate", False))
    allow_non_gru_reps = bool(getattr(args, "allow_non_gru_reps", False))
    if not user_label:
        reasons.append("invalid_label")
    if not allow_mode_mismatch:
        if _canonical_exercise(group.get("exercise")) != exercise:
            reasons.append("wrong_exercise")
        if not _mode_snapshot_ok(group.get("board_mode_at_start"), exercise=exercise):
            reasons.append("mode_not_vision_sensor_at_start")
        if not _mode_snapshot_ok(group.get("board_mode_at_end"), exercise=exercise):
            reasons.append("mode_not_vision_sensor_at_end")
    if not allow_invalid_signal_gate:
        if not _signal_gate_ok(group.get("start_gate")):
            reasons.append("invalid_start_signal_gate")
        if not _signal_gate_ok(group.get("end_gate")):
            reasons.append("invalid_end_signal_gate")
    if not rows:
        reasons.append("missing_exact_gru_7d")
    elif len(rows) < args.seq_len:
        reasons.append("too_few_exact_rows")
    if args.emg_view != "current_pct" and len(group.get("stream_samples") or []) == 0:
        reasons.append("missing_stream_samples_for_emg_view")
    if args.emg_view != "raw_rms_robust100" and max_sat is not None and max_sat > args.max_saturation_ratio:
        reasons.append("range_saturated")
    rep_gate_count = rep_count if allow_non_gru_reps else gru_rep_count
    if rep_gate_count < args.min_reps_per_group:
        reasons.append("rep_boundary_failed")
    accepted = not reasons
    preprocess_meta = _export_preprocess_meta(group, args, pooled_robust=pooled_robust)
    per_group_robust = None
    if getattr(args, "emg_view", DEFAULT_EMG_VIEW) == "raw_rms_robust100":
        per_group_robust = raw_rms_robust_meta_from_stream(group.get("stream_samples") or [])
    return {
        "accepted": accepted,
        "reasons": reasons,
        "file": Path(path).name,
        "path": _rel(path),
        "run_dir": _rel(Path(path).parents[1]),
        "group_id": group_id,
        "label": label,
        "label_cn": LABEL_CN.get(label, label),
        "user_label": user_label,
        "exercise": exercise,
        "exact_rows": len(rows),
        "estimated_windows": windows,
        "rep_count": rep_count,
        "gru_rep_count": gru_rep_count,
        "rep_gate_count": rep_gate_count,
        "emg_view": args.emg_view,
        "target_sat_ratio": _round(target_sat),
        "comp_sat_ratio": _round(comp_sat),
        "max_sat_ratio": _round(max_sat),
        "row_source": rows[0]["source"] if rows else "",
        "stream_view_row_count": len(group.get("stream_samples") or []),
        "matched_stream_rows": row_materialized.get("matched_count", 0),
        "fallback_current_count": row_materialized.get("fallback_current_count", 0),
        "preprocess_version": preprocess_meta.get("preprocess_version"),
        "mvc_source": preprocess_meta.get("mvc_source"),
        "mvc_values": preprocess_meta.get("mvc_values"),
        "domain_method": preprocess_meta.get("domain_method"),
        "domain_params": preprocess_meta.get("domain_params"),
        "raw_rms_robust100": preprocess_meta.get("raw_rms_robust100"),
        "per_group_raw_rms_robust100": per_group_robust,
        "mode_gate": {
            "exercise": _canonical_exercise(group.get("exercise")),
            "expected_exercise": exercise,
            "board_mode_at_start_ok": _mode_snapshot_ok(group.get("board_mode_at_start"), exercise=exercise),
            "board_mode_at_end_ok": _mode_snapshot_ok(group.get("board_mode_at_end"), exercise=exercise),
            "start_signal_gate_ok": _signal_gate_ok(group.get("start_gate")),
            "end_signal_gate_ok": _signal_gate_ok(group.get("end_gate")),
        },
        "rows": rows,
    }


def iter_group_files(run_dirs):
    seen = set()
    for run_dir in run_dirs:
        groups_dir = Path(run_dir) / "groups"
        for path in sorted(groups_dir.glob("*.json")):
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            yield path


def _collect_group_files(args):
    """Return (group_paths, run_dirs) honoring --group-files vs --run-dir.

    --group-files takes precedence when present. run_dirs in that case is the
    list of unique parents-of-parents (i.e. the Sensor Lab run dirs) of every
    valid group path, preserving discovery order.
    """
    explicit = getattr(args, "group_files", None)
    if explicit:
        raw_paths = [chunk.strip() for chunk in explicit.split(",") if chunk.strip()]
        group_paths = []
        run_dirs = []
        seen_groups = set()
        seen_runs = set()
        for raw in raw_paths:
            path = Path(raw).resolve()
            if not path.is_file():
                print("[WARN] skip missing group file: %s" % raw)
                continue
            key = str(path)
            if key in seen_groups:
                continue
            seen_groups.add(key)
            group_paths.append(path)
            try:
                run_dir = path.parents[1]
            except IndexError:
                continue
            run_key = str(run_dir.resolve())
            if run_key not in seen_runs:
                seen_runs.add(run_key)
                run_dirs.append(run_dir)
        return group_paths, run_dirs

    run_dirs = [Path(p) for p in args.run_dir] if args.run_dir else [latest_run_dir()]
    run_dirs = [p for p in run_dirs if p is not None]
    group_paths = list(iter_group_files(run_dirs))
    return group_paths, run_dirs


def write_group_csv(out_dir, group_info):
    user_label = group_info["user_label"]
    label = group_info["label"]
    group_id = _safe_name(group_info["group_id"])
    source_run = _safe_name(Path(group_info["run_dir"]).name)
    path = Path(out_dir) / user_label / ("%s__%s.csv" % (source_run, group_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for idx, row in enumerate(group_info["rows"]):
            values = row["values"]
            writer.writerow({
                "Timestamp": "%.6f" % (row.get("ts") or idx),
                "Ang_Vel": "%.6f" % values[0],
                "Angle": "%.6f" % values[1],
                "Ang_Accel": "%.6f" % values[2],
                "Target_RMS": "%.6f" % values[3],
                "Comp_RMS": "%.6f" % values[4],
                "Symmetry_Score": "%.6f" % values[5],
                "Phase_Progress": "%.6f" % values[6],
                "pose_score": "%.6f" % (row.get("pose_score") or 0.0),
                "label": user_label,
                "source_group_id": group_id,
                "source_run": source_run,
            })
    return path


def summarize_label(accepted_groups, args):
    reps = sum(g["rep_gate_count"] for g in accepted_groups)
    logged_reps = sum(g["rep_count"] for g in accepted_groups)
    gru_reps = sum(g["gru_rep_count"] for g in accepted_groups)
    windows = sum(g["estimated_windows"] for g in accepted_groups)
    rows = sum(g["exact_rows"] for g in accepted_groups)
    return {
        "accepted_groups": len(accepted_groups),
        "reps": reps,
        "logged_reps": logged_reps,
        "gru_reps": gru_reps,
        "estimated_windows": windows,
        "exact_rows": rows,
        "ready": bool(
            len(accepted_groups) >= args.min_groups_per_label and
            (reps >= args.min_reps_per_label or windows >= args.min_windows_per_label)
        ),
    }


def _stats(values):
    values = [_to_float(v) for v in values if _to_float(v) is not None]
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "max": None, "min": None}
    values = sorted(values)
    n = len(values)

    def pct(q):
        if n == 1:
            return values[0]
        pos = (n - 1) * q
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return values[lo]
        return values[lo] + (values[hi] - values[lo]) * (pos - lo)

    return {
        "n": n,
        "mean": _round(sum(values) / float(n), 4),
        "p50": _round(pct(0.50), 4),
        "p95": _round(pct(0.95), 4),
        "max": _round(values[-1], 4),
        "min": _round(values[0], 4),
    }


def feature_distribution(accepted_by_label):
    labels = {}
    points = []
    for label in ("standard", "compensating", "non_standard"):
        groups = accepted_by_label.get(label, [])
        by_feature = dict((name, []) for name in FEATURES_7D)
        label_points = []
        for group in groups:
            rows = group.get("rows") or []
            if not rows:
                continue
            values_by_idx = [[] for _ in FEATURES_7D]
            for row in rows:
                values = row.get("values") or []
                if len(values) < len(FEATURES_7D):
                    continue
                for idx, value in enumerate(values[:len(FEATURES_7D)]):
                    values_by_idx[idx].append(value)
                    by_feature[FEATURES_7D[idx]].append(value)
            target_mean = _stats(values_by_idx[FEATURES_7D.index("Target_RMS")]).get("mean")
            comp_mean = _stats(values_by_idx[FEATURES_7D.index("Comp_RMS")]).get("mean")
            angle_min = _stats(values_by_idx[FEATURES_7D.index("Angle")]).get("min")
            point = {
                "label": label,
                "label_cn": LABEL_CN.get(label, label),
                "group_id": group.get("group_id"),
                "x_target_mean": target_mean,
                "y_comp_mean": comp_mean,
                "angle_min": angle_min,
                "rows": len(rows),
                "rep_gate_count": group.get("rep_gate_count"),
            }
            points.append(point)
            label_points.append(point)
        labels[label] = {
            "group_count": len(groups),
            "features": dict((name, _stats(values)) for name, values in by_feature.items()),
            "points": label_points,
        }
    return {
        "features": list(FEATURES_7D),
        "labels": labels,
        "points": points,
    }


def build_feature_distribution_html(distribution):
    payload = json.dumps(distribution, ensure_ascii=False)
    template = """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>Lane B GRU 特征分布</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:24px;background:#f7f8f8;color:#10201c}
canvas{width:100%;max-width:960px;height:520px;background:#fff;border:1px solid #d7dfdc;border-radius:8px}
.legend span{display:inline-flex;align-items:center;margin-right:16px;font-weight:700}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:6px}
table{border-collapse:collapse;margin-top:18px;background:#fff}
td,th{border:1px solid #d7dfdc;padding:8px 10px;text-align:left}
</style>
<h1>Lane B GRU 特征分布</h1>
<p>横轴是 Target_RMS 均值，纵轴是 Comp_RMS 均值。每个点是一组 Sensor Lab 采集。</p>
<div class="legend">
<span><i class="dot" style="background:#1f77b4"></i>标准</span>
<span><i class="dot" style="background:#d62728"></i>代偿</span>
<span><i class="dot" style="background:#2ca02c"></i>不标准</span>
</div>
<canvas id="chart" width="960" height="520"></canvas>
<table id="summary"></table>
<script>
const data = __DISTRIBUTION_JSON__;
const colors = {standard:'#1f77b4', compensating:'#d62728', non_standard:'#2ca02c'};
const cn = {standard:'标准', compensating:'代偿', non_standard:'不标准'};
const c = document.getElementById('chart'), ctx = c.getContext('2d');
const pts = (data.points || []).filter(p => p.x_target_mean !== null && p.y_comp_mean !== null);
const maxX = Math.max(100, ...pts.map(p => Number(p.x_target_mean) || 0)) * 1.1;
const maxY = Math.max(100, ...pts.map(p => Number(p.y_comp_mean) || 0)) * 1.1;
function sx(x){ return 64 + (Number(x)||0) / maxX * (c.width - 104); }
function sy(y){ return c.height - 48 - (Number(y)||0) / maxY * (c.height - 88); }
ctx.fillStyle = '#fff'; ctx.fillRect(0,0,c.width,c.height);
ctx.strokeStyle = '#d7dfdc'; ctx.lineWidth = 1;
for(let i=0;i<=5;i++){
  const x=64+i*(c.width-104)/5, y=40+i*(c.height-88)/5;
  ctx.beginPath(); ctx.moveTo(x,40); ctx.lineTo(x,c.height-48); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(64,y); ctx.lineTo(c.width-40,y); ctx.stroke();
}
ctx.fillStyle='#4f625c'; ctx.font='14px monospace';
ctx.fillText('Target_RMS mean', c.width/2-60, c.height-14);
ctx.save(); ctx.translate(18,c.height/2+50); ctx.rotate(-Math.PI/2); ctx.fillText('Comp_RMS mean',0,0); ctx.restore();
pts.forEach(p => {
  ctx.fillStyle = colors[p.label] || '#555';
  ctx.beginPath(); ctx.arc(sx(p.x_target_mean), sy(p.y_comp_mean), 7, 0, Math.PI*2); ctx.fill();
  ctx.fillStyle='#263832'; ctx.font='11px monospace';
  ctx.fillText(String(p.group_id || '').slice(-14), sx(p.x_target_mean)+9, sy(p.y_comp_mean)-8);
});
document.getElementById('summary').innerHTML = '<tr><th>类别</th><th>组数</th><th>Target均值</th><th>Comp均值</th><th>Angle最小值</th></tr>' +
  Object.keys(cn).map(k => {
    const item = (data.labels || {})[k] || {};
    const f = item.features || {};
    return `<tr><td>${cn[k]}</td><td>${item.group_count || 0}</td><td>${(f.Target_RMS||{}).mean ?? '--'}</td><td>${(f.Comp_RMS||{}).mean ?? '--'}</td><td>${(f.Angle||{}).min ?? '--'}</td></tr>`;
  }).join('');
</script>
</html>
"""
    return template.replace("__DISTRIBUTION_JSON__", payload)


def build_markdown(manifest):
    lines = []
    lines.append("# Lane B 当前个人 GRU 数据集导出报告")
    lines.append("")
    if manifest.get("is_merged_source"):
        lines.append("## 数据来源")
        lines.append("")
        lines.append("- 合并自 %d 个 Sensor Lab session" % len(manifest.get("run_dirs") or []))
        lines.append("- source_run_id: `%s`" % manifest.get("source_run_id"))
        per_run = manifest.get("per_run_groups") or {}
        for run_name in sorted(per_run.keys()):
            group_ids = per_run.get(run_name) or []
            lines.append("- `%s` (%d 组): %s" % (
                run_name,
                len(group_ids),
                ", ".join("`%s`" % gid for gid in group_ids),
            ))
        lines.append("")
    lines.append("- 输出目录: `%s`" % manifest.get("out_dir"))
    lines.append("- 训练就绪: `%s`" % manifest.get("training_ready"))
    lines.append("- EMG 口径: `%s`" % manifest.get("emg_view"))
    lines.append("- 预处理版本: `%s`" % manifest.get("preprocess_version"))
    if manifest.get("raw_rms_robust100"):
        r = manifest.get("raw_rms_robust100") or {}
        lines.append("- 裸数据缩放: `target_ref=%s, comp_ref=%s, no_mvc=%s, method=%s` (pooled_groups=%s)" % (
            r.get("target_ref"),
            r.get("comp_ref"),
            not bool(r.get("uses_mvc")),
            r.get("method"),
            r.get("groups_pooled"),
        ))
    lines.append("- 动作: `%s`" % manifest.get("exercise"))
    lines.append("- 数据原则: 只使用当前 Lab exact 7D，不读取旧历史训练数据。")
    lines.append("")
    lines.append("## 按类别汇总")
    lines.append("| 类别 | accepted组 | GRU rep | 记录rep | 训练窗口估计 | exact行 | ready |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for label in ("standard", "compensating", "non_standard"):
        s = manifest["labels"].get(label, {})
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            LABEL_CN.get(label, label),
            s.get("accepted_groups", 0),
            s.get("gru_reps", 0),
            s.get("logged_reps", 0),
            s.get("estimated_windows", 0),
            s.get("exact_rows", 0),
            s.get("ready", False),
        ))
    lines.append("")
    lines.append("## 被拒绝的组")
    rejected = [g for g in manifest.get("groups", []) if not g.get("accepted")]
    if not rejected:
        lines.append("- 无")
    else:
        for g in rejected:
            lines.append("- `%s`: `%s` rows=%s reps=%s sat=%s" % (
                g.get("file"),
                ",".join(g.get("reasons") or []),
                g.get("exact_rows"),
                g.get("rep_count"),
                g.get("max_sat_ratio"),
            ))
    lines.append("")
    lines.append("## 下一步")
    if manifest.get("training_ready"):
        lines.append("- 可以运行个人 GRU 训练脚本生成候选权重。")
    else:
        lines.append("- 暂不训练；继续用 Lab 采集，直到三类都 ready。")
    return "\n".join(lines)


def export_dataset(run_dirs, out_dir, args, group_paths=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    accepted_by_label = defaultdict(list)
    groups = []
    exercise = _exercise_slug(getattr(args, "exercise", DEFAULT_EXERCISE))
    runtime_preprocess_meta = load_runtime_preprocess_meta()
    run_dir_names = [Path(p).name for p in (run_dirs or []) if p]
    is_merged = len(run_dir_names) > 1
    if is_merged:
        source_run_id = "merged_%d_sessions_%s" % (
            len(run_dir_names),
            time.strftime("%Y%m%d_%H%M%S"),
        )
    else:
        source_run_id = _run_tag(run_dirs)
    if group_paths is None:
        group_paths = list(iter_group_files(run_dirs))

    use_pooled = getattr(args, "emg_view", DEFAULT_EMG_VIEW) == "raw_rms_robust100"

    # Pass 1: read every group json and pre-evaluate with per-group meta to
    # decide acceptance. We discard the per-group normalized rows; the final
    # CSV is materialized in pass 2 using the pooled ref.
    loaded = []
    for path in group_paths:
        try:
            group = _read_json(path)
        except Exception as exc:
            groups.append({
                "accepted": False,
                "file": Path(path).name,
                "path": _rel(path),
                "reasons": ["json_read_failed:%s" % exc],
            })
            continue
        info = evaluate_group(path, group, args)
        loaded.append((path, group, info))

    # Compute pooled ref from accepted groups only so a rejected group's noisy
    # stream cannot contaminate the global p95.
    pooled_robust = None
    if use_pooled:
        accepted_groups_for_pool = [grp for (_p, grp, info) in loaded if info.get("accepted")]
        pooled_robust = pooled_raw_rms_robust_meta(accepted_groups_for_pool)

    # Pass 2: rematerialize rows for accepted groups using pooled ref, build
    # the final info dicts and CSVs.
    for path, group, _initial_info in loaded:
        info = evaluate_group(path, group, args, pooled_robust=pooled_robust)
        rows = info.pop("rows")
        if info.get("accepted"):
            info["csv_path"] = _rel(write_group_csv(out_dir, dict(info, rows=rows)))
            accepted_by_label[info["label"]].append(dict(info, rows=rows))
        groups.append(info)

    label_summary = {}
    for label in ("standard", "compensating", "non_standard"):
        label_summary[label] = summarize_label(accepted_by_label.get(label, []), args)
    training_ready = all(label_summary[label]["ready"] for label in ("standard", "compensating", "non_standard"))
    manifest_meta = runtime_preprocess_meta
    if pooled_robust is not None:
        # Pooled global ref is the source of truth; pick a representative group
        # for the non-raw_rms fields (mvc/domain) which stay per-group.
        rep_group = next((g for g in groups if g.get("preprocess_version")), None)
        if rep_group is not None:
            manifest_meta = {
                "preprocess_version": rep_group.get("preprocess_version"),
                "mvc_source": rep_group.get("mvc_source"),
                "mvc_values": rep_group.get("mvc_values"),
                "domain_method": rep_group.get("domain_method"),
                "domain_params": rep_group.get("domain_params"),
                "raw_rms_robust100": dict(pooled_robust),
            }
        else:
            manifest_meta = {
                "preprocess_version": PREPROCESS_VERSION,
                "raw_rms_robust100": dict(pooled_robust),
            }
    else:
        for group in groups:
            if group.get("preprocess_version"):
                manifest_meta = {
                    "preprocess_version": group.get("preprocess_version"),
                    "mvc_source": group.get("mvc_source"),
                    "mvc_values": group.get("mvc_values"),
                    "domain_method": group.get("domain_method"),
                    "domain_params": group.get("domain_params"),
                    "raw_rms_robust100": group.get("raw_rms_robust100"),
                }
                break
    per_group_raw_rms_robust100 = {}
    for g in groups:
        per_group = g.get("per_group_raw_rms_robust100")
        if per_group:
            gid = g.get("group_id") or g.get("file") or "?"
            per_group_raw_rms_robust100[gid] = per_group
    manifest = {
        "ok": True,
        "schema_version": 1,
        "created_ts": time.time(),
        "out_dir": _rel(out_dir),
        "source_run_id": source_run_id,
        "run_dirs": [_rel(p) for p in run_dirs],
        "is_merged_source": is_merged,
        "exercise": exercise,
        "features": list(FEATURES_7D),
        "csv_fields": list(CSV_FIELDS),
        "label_map": dict(LABEL_TO_USER_LABEL),
        "emg_view": args.emg_view,
        "preprocess_version": manifest_meta.get("preprocess_version") or PREPROCESS_VERSION,
        "mvc_source": manifest_meta.get("mvc_source"),
        "mvc_values": manifest_meta.get("mvc_values"),
        "domain_method": manifest_meta.get("domain_method"),
        "domain_params": manifest_meta.get("domain_params"),
        "raw_rms_robust100": manifest_meta.get("raw_rms_robust100"),
        "per_group_raw_rms_robust100": per_group_raw_rms_robust100,
        "quality_gates": {
            "seq_len": args.seq_len,
            "stride": args.stride,
            "min_groups_per_label": args.min_groups_per_label,
            "min_reps_per_group": args.min_reps_per_group,
            "min_reps_per_label": args.min_reps_per_label,
            "min_windows_per_label": args.min_windows_per_label,
            "saturation_threshold": args.saturation_threshold,
            "max_saturation_ratio": args.max_saturation_ratio,
            "require_exercise_vision_sensor": exercise if not bool(getattr(args, "allow_mode_mismatch", False)) else None,
            "require_valid_signal_gate": not bool(getattr(args, "allow_invalid_signal_gate", False)),
            "require_gru_reps": not bool(getattr(args, "allow_non_gru_reps", False)),
        },
        "training_ready": training_ready,
        "labels": label_summary,
        "groups": groups,
    }
    if is_merged:
        per_run_groups = defaultdict(list)
        for g in groups:
            run_name = Path(g.get("run_dir") or "").name or "unknown_run"
            per_run_groups[run_name].append(g.get("group_id") or g.get("file") or "?")
        manifest["per_run_groups"] = {k: sorted(v) for k, v in per_run_groups.items()}
    distribution = feature_distribution(accepted_by_label)
    manifest["feature_distribution"] = distribution
    _atomic_write_json(out_dir / "personal_dataset_manifest.json", manifest)
    audit_snapshot = {
        "source_run_id": source_run_id,
        "run_dirs": [_rel(p) for p in run_dirs],
        "is_merged_source": is_merged,
        "exercise": exercise,
        "emg_view": args.emg_view,
        "preprocess_version": manifest.get("preprocess_version"),
        "mvc_source": manifest.get("mvc_source"),
        "mvc_values": manifest.get("mvc_values"),
        "domain_method": manifest.get("domain_method"),
        "domain_params": manifest.get("domain_params"),
        "raw_rms_robust100": manifest.get("raw_rms_robust100"),
        "per_group_raw_rms_robust100": per_group_raw_rms_robust100,
        "training_ready": training_ready,
        "labels": label_summary,
        "accepted_group_count": sum(len(v) for v in accepted_by_label.values()),
        "rejected_groups": [g for g in groups if not g.get("accepted")],
        "feature_distribution_path": _rel(out_dir / "feature_distribution.json"),
    }
    _atomic_write_json(out_dir / "audit_snapshot.json", audit_snapshot)
    _atomic_write_json(out_dir / "feature_distribution.json", distribution)
    report_md = build_markdown(manifest)
    (out_dir / "export_report.md").write_text(report_md, encoding="utf-8")
    (out_dir / "README.md").write_text(report_md, encoding="utf-8")
    (out_dir / "feature_distribution.html").write_text(
        build_feature_distribution_html(distribution),
        encoding="utf-8",
    )
    return manifest


def default_out_dir(run_dirs=None, exercise=DEFAULT_EXERCISE):
    stamp = time.strftime("%Y%m%d_%H%M%S")
    root = PERSONAL_ROOT_BY_EXERCISE.get(_exercise_slug(exercise), PERSONAL_ROOT) / "datasets"
    return root / ("%s_%s" % (stamp, _run_tag(run_dirs)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", action="append", default=None,
                        help="Sensor Lab run dir. Can be repeated. Mutually exclusive with --group-files. Defaults to latest run if neither is given.")
    parser.add_argument("--group-files", default=None,
                        help="Comma-separated list of group json files to merge into one dataset. "
                             "Mutually exclusive with --run-dir. Use this to merge a subset of "
                             "groups across multiple Sensor Lab sessions.")
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--emg-view", choices=VALID_EMG_VIEWS, default=DEFAULT_EMG_VIEW)
    parser.add_argument("--exercise", choices=("bicep_curl", "squat"), default=DEFAULT_EXERCISE)
    parser.add_argument("--seq-len", type=int, default=30)
    parser.add_argument("--stride", type=int, default=10)
    parser.add_argument("--min-groups-per-label", type=int, default=5)
    parser.add_argument("--min-reps-per-group", type=int, default=4)
    parser.add_argument("--min-reps-per-label", type=int, default=25)
    parser.add_argument("--min-windows-per-label", type=int, default=120)
    parser.add_argument("--saturation-threshold", type=float, default=99.0)
    parser.add_argument("--max-saturation-ratio", type=float, default=0.60)
    parser.add_argument("--allow-mode-mismatch", action="store_true",
                        help="Diagnostic only: do not reject groups outside bicep_curl/vision_sensor.")
    parser.add_argument("--allow-invalid-signal-gate", action="store_true",
                        help="Diagnostic only: do not reject groups whose start/end gate was invalid.")
    parser.add_argument("--allow-non-gru-reps", action="store_true",
                        help="Diagnostic only: count visual/fallback reps toward the rep gate.")
    parser.add_argument("--require-ready", action="store_true",
                        help="Exit 2 if the exported dataset is not train-ready.")
    args = parser.parse_args(argv)

    if args.run_dir and args.group_files:
        print("[FATAL] --run-dir and --group-files are mutually exclusive; pass only one")
        return 2

    group_paths, run_dirs = _collect_group_files(args)
    if args.group_files and not group_paths:
        print("[FATAL] --group-files did not resolve to any existing files")
        return 2
    if not run_dirs:
        print("[FATAL] no Sensor Lab run dir found")
        return 2
    out_dir = Path(args.out_dir) if args.out_dir else default_out_dir(run_dirs, exercise=args.exercise)
    manifest = export_dataset(run_dirs, out_dir, args, group_paths=group_paths)
    print("ok: wrote %s" % (out_dir / "personal_dataset_manifest.json"))
    print("ok: wrote %s" % (out_dir / "audit_snapshot.json"))
    print("ok: wrote %s" % (out_dir / "export_report.md"))
    print("training_ready=%s" % manifest.get("training_ready"))
    for label, summary in manifest.get("labels", {}).items():
        print("%s groups=%s reps=%s windows=%s ready=%s" % (
            label,
            summary.get("accepted_groups"),
            summary.get("reps"),
            summary.get("estimated_windows"),
            summary.get("ready"),
        ))
    if args.require_ready and not manifest.get("training_ready"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
