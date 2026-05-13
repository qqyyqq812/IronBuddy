#!/usr/bin/env python3
"""Shared Lane B EMG preprocessing helpers."""

from __future__ import print_function

import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MVC_VALUES_PATH = ROOT / "hardware_engine" / "sensor" / "mvc_values.json"
DOMAIN_CALIB_PATH = ROOT / "hardware_engine" / "sensor" / "domain_calibration.json"

DEFAULT_MVC_VALUES = {"target": 400.0, "comp": 400.0}
DEFAULT_DOMAIN_PARAMS = {
    "target": {"alpha": 1.0, "beta": 0.0},
    "comp": {"alpha": 1.0, "beta": 0.0},
}
DEFAULT_EMG_VIEW = "raw_rms_robust100"
PREPROCESS_VERSION = "lane_b_v2_raw_rms_robust100"
RAW_RMS_ROBUST_FLOOR = 20.0
VALID_EMG_VIEWS = (
    "raw_rms_robust100",
    "stable_remap_pct",
    "current_pct",
    "old_pct400",
    "raw_rms",
)


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


def _read_json(path, default=None):
    try:
        with Path(path).open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def _clip_pct(value):
    value = _to_float(value, 0.0) or 0.0
    if value < 0.0:
        return 0.0
    if value > 100.0:
        return 100.0
    return float(value)


def _pick_float(value, fallback):
    parsed = _to_float(value)
    return fallback if parsed is None else parsed


def _value_stats(values):
    nums = [_to_float(value) for value in values if _to_float(value) is not None]
    if not nums:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "max": None, "min": None}
    nums = sorted(nums)
    n = len(nums)

    def pct(q):
        if n == 1:
            return nums[0]
        idx = (n - 1) * q
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return nums[lo]
        frac = idx - lo
        return nums[lo] + (nums[hi] - nums[lo]) * frac

    return {
        "n": n,
        "mean": round(sum(nums) / float(n), 3),
        "p50": round(pct(0.50), 3),
        "p95": round(pct(0.95), 3),
        "max": round(nums[-1], 3),
        "min": round(nums[0], 3),
    }


def _saturation_ratio(values, threshold=99.0):
    nums = [_to_float(value) for value in values if _to_float(value) is not None]
    if not nums:
        return None
    return round(sum(1 for value in nums if value >= threshold) / float(len(nums)), 4)


def _domain_params_from_payload(payload):
    params = {
        "target": dict(DEFAULT_DOMAIN_PARAMS["target"]),
        "comp": dict(DEFAULT_DOMAIN_PARAMS["comp"]),
    }
    payload = payload if isinstance(payload, dict) else {}
    calibration = payload.get("calibration") if isinstance(payload.get("calibration"), dict) else {}
    method = calibration.get("method_primary") or "identity"
    body = calibration.get(method) if isinstance(calibration.get(method), dict) else {}
    for key in ("target", "comp"):
        item = body.get(key) if isinstance(body.get(key), dict) else {}
        params[key] = {
            "alpha": _to_float(item.get("alpha"), 1.0) or 1.0,
            "beta": _to_float(item.get("beta"), 0.0) or 0.0,
        }
    return method, params


def load_runtime_preprocess_meta(mvc_path=MVC_VALUES_PATH, domain_path=DOMAIN_CALIB_PATH):
    mvc_payload = _read_json(mvc_path, {})
    mvc_schema_v2 = bool(int(mvc_payload.get("schema_version", 1) or 1) >= 2) if mvc_payload else False
    domain_payload = _read_json(domain_path, {})

    mvc_target = _to_float(
        mvc_payload.get("target")
        if "target" in mvc_payload else
        (mvc_payload.get("mvc_values") or {}).get("target"),
        DEFAULT_MVC_VALUES["target"],
    )
    mvc_comp = _to_float(
        mvc_payload.get("comp")
        if "comp" in mvc_payload else
        (mvc_payload.get("mvc_values") or {}).get("comp"),
        DEFAULT_MVC_VALUES["comp"],
    )
    mvc_valid = bool(
        mvc_target is not None and
        mvc_comp is not None and
        50.0 <= mvc_target <= 2000.0 and
        50.0 <= mvc_comp <= 2000.0
    )
    mvc_ready = bool(mvc_valid and mvc_schema_v2)
    if not mvc_ready:
        mvc_target = DEFAULT_MVC_VALUES["target"]
        mvc_comp = DEFAULT_MVC_VALUES["comp"]
    domain_method, domain_params = _domain_params_from_payload(domain_payload)
    return {
        "preprocess_version": PREPROCESS_VERSION,
        "default_training_view": DEFAULT_EMG_VIEW,
        "current_pct_path": "RMS / MVC -> domain stretch -> clip 0..100",
        "raw_rms_robust100_path": "RMS / per-export p95(raw RMS) -> clip 0..100; no MVC",
        "raw_rms_robust100": {
            "method": "runtime_placeholder",
            "quantile": 0.95,
            "floor": RAW_RMS_ROBUST_FLOOR,
            "target_ref": 100.0,
            "comp_ref": 100.0,
            "source": "default_until_export",
            "uses_mvc": False,
        },
        "mvc_source": "schema_v2" if mvc_ready else "default400",
        "mvc_values": {
            "target": float(mvc_target),
            "comp": float(mvc_comp),
        },
        "mvc_exists": Path(mvc_path).exists(),
        "mvc_schema_v2": mvc_schema_v2,
        "mvc_valid": mvc_ready,
        "domain_exists": Path(domain_path).exists(),
        "domain_method": domain_method,
        "domain_params": domain_params,
    }


def normalize_preprocess_meta(meta=None):
    runtime = load_runtime_preprocess_meta()
    meta = meta if isinstance(meta, dict) else {}
    out = dict(runtime)
    out.update({
        "preprocess_version": meta.get("preprocess_version") or runtime["preprocess_version"],
        "default_training_view": meta.get("default_training_view") or runtime["default_training_view"],
        "current_pct_path": meta.get("current_pct_path") or runtime["current_pct_path"],
        "raw_rms_robust100_path": meta.get("raw_rms_robust100_path") or runtime["raw_rms_robust100_path"],
        "mvc_source": meta.get("mvc_source") or runtime["mvc_source"],
        "mvc_exists": bool(meta.get("mvc_exists", runtime["mvc_exists"])),
        "mvc_schema_v2": bool(meta.get("mvc_schema_v2", runtime["mvc_schema_v2"])),
        "mvc_valid": bool(meta.get("mvc_valid", runtime["mvc_valid"])),
        "domain_exists": bool(meta.get("domain_exists", runtime["domain_exists"])),
        "domain_method": meta.get("domain_method") or runtime["domain_method"],
    })
    mvc_values = meta.get("mvc_values") if isinstance(meta.get("mvc_values"), dict) else {}
    out["mvc_values"] = {
        "target": _pick_float(mvc_values.get("target"), runtime["mvc_values"]["target"]),
        "comp": _pick_float(mvc_values.get("comp"), runtime["mvc_values"]["comp"]),
    }
    domain_params = meta.get("domain_params") if isinstance(meta.get("domain_params"), dict) else {}
    merged_params = {
        "target": dict(runtime["domain_params"]["target"]),
        "comp": dict(runtime["domain_params"]["comp"]),
    }
    for key in ("target", "comp"):
        if isinstance(domain_params.get(key), dict):
            merged_params[key] = {
                "alpha": _pick_float(domain_params[key].get("alpha"), merged_params[key]["alpha"]),
                "beta": _pick_float(domain_params[key].get("beta"), merged_params[key]["beta"]),
            }
    out["domain_params"] = merged_params
    robust = meta.get("raw_rms_robust100") if isinstance(meta.get("raw_rms_robust100"), dict) else {}
    runtime_robust = runtime.get("raw_rms_robust100") or {}
    normalized_robust = {
        "method": robust.get("method") or runtime_robust.get("method") or "runtime_placeholder",
        "quantile": _pick_float(robust.get("quantile"), runtime_robust.get("quantile", 0.95)),
        "floor": _pick_float(robust.get("floor"), runtime_robust.get("floor", RAW_RMS_ROBUST_FLOOR)),
        "target_ref": _pick_float(robust.get("target_ref"), runtime_robust.get("target_ref", 100.0)),
        "comp_ref": _pick_float(robust.get("comp_ref"), runtime_robust.get("comp_ref", 100.0)),
        "source": robust.get("source") or runtime_robust.get("source") or "default_until_export",
        "uses_mvc": bool(robust.get("uses_mvc", False)),
    }
    if "groups_pooled" in robust:
        try:
            normalized_robust["groups_pooled"] = int(robust["groups_pooled"])
        except Exception:
            pass
    out["raw_rms_robust100"] = normalized_robust
    return out


def stable_pct_from_rms(rms_value, channel_key, preprocess_meta=None):
    meta = normalize_preprocess_meta(preprocess_meta)
    rms_value = _to_float(rms_value, 0.0) or 0.0
    channel_key = "target" if channel_key == "target" else "comp"
    mvc_base = meta["mvc_values"][channel_key]
    params = meta["domain_params"][channel_key]
    base_pct = (rms_value / float(mvc_base)) * 100.0
    remapped = params["alpha"] * base_pct + params["beta"]
    return _clip_pct(remapped)


def old_pct400_from_rms(rms_value):
    rms_value = _to_float(rms_value, 0.0) or 0.0
    return _clip_pct((rms_value / 400.0) * 100.0)


def _percentile(values, q):
    values = sorted([_to_float(v) for v in values if _to_float(v) is not None])
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * float(q)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def raw_rms_robust_meta_from_stream(stream_samples, q=0.95, floor=RAW_RMS_ROBUST_FLOOR):
    """Build no-MVC RMS scaling from the recorded stream itself."""
    target = []
    comp = []
    for row in stream_samples or []:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        t = _to_float(row[5])
        c = _to_float(row[6])
        if t is not None:
            target.append(t)
        if c is not None:
            comp.append(c)
    target_ref = max(float(floor), _percentile(target, q) or float(floor))
    comp_ref = max(float(floor), _percentile(comp, q) or float(floor))
    return {
        "method": "per_export_stream_rms_p95",
        "quantile": float(q),
        "floor": float(floor),
        "target_ref": round(target_ref, 6),
        "comp_ref": round(comp_ref, 6),
        "source": "stream_samples.rms0/rms1",
        "uses_mvc": False,
    }


def pooled_raw_rms_robust_meta(groups, q=0.95, floor=RAW_RMS_ROBUST_FLOOR):
    """Pool stream_samples across all groups, compute one global p95 per channel.

    Returns dict matching the per-group raw_rms_robust meta format but with
    method=pooled_global_rms_p95 so the manifest carries the pooling provenance.
    Returns None if no usable rms data is present.
    """
    target = []
    comp = []
    group_count = 0
    for g in groups or []:
        samples = (g or {}).get("stream_samples") if isinstance(g, dict) else None
        added = False
        for row in samples or []:
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                continue
            t = _to_float(row[5])
            c = _to_float(row[6])
            if t is not None:
                target.append(t)
                added = True
            if c is not None:
                comp.append(c)
                added = True
        if added:
            group_count += 1
    if not target and not comp:
        return None
    target_ref = max(float(floor), _percentile(target, q) or float(floor))
    comp_ref = max(float(floor), _percentile(comp, q) or float(floor))
    return {
        "method": "pooled_global_rms_p95",
        "quantile": float(q),
        "floor": float(floor),
        "target_ref": round(target_ref, 6),
        "comp_ref": round(comp_ref, 6),
        "source": "stream_samples.rms0/rms1",
        "uses_mvc": False,
        "groups_pooled": int(group_count),
    }


def raw_rms_robust100_from_rms(rms_value, channel_key, preprocess_meta=None):
    meta = normalize_preprocess_meta(preprocess_meta)
    robust = meta.get("raw_rms_robust100") if isinstance(meta.get("raw_rms_robust100"), dict) else {}
    channel_key = "target" if channel_key == "target" else "comp"
    ref_key = "target_ref" if channel_key == "target" else "comp_ref"
    ref = _to_float(robust.get(ref_key), RAW_RMS_ROBUST_FLOOR) or RAW_RMS_ROBUST_FLOOR
    ref = max(float(RAW_RMS_ROBUST_FLOOR), ref)
    return _clip_pct((_to_float(rms_value, 0.0) or 0.0) / ref * 100.0)


def build_stream_view_rows(stream_samples, preprocess_meta=None):
    meta = normalize_preprocess_meta(preprocess_meta)
    rows = []
    for row in stream_samples or []:
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            continue
        ts = _to_float(row[0])
        rms0 = _to_float(row[5], 0.0) or 0.0
        rms1 = _to_float(row[6], 0.0) or 0.0
        current_target = _clip_pct(row[7])
        current_comp = _clip_pct(row[8])
        rows.append({
            "ts": ts,
            "raw_rms": {"target": rms0, "comp": rms1},
            "current_pct": {"target": current_target, "comp": current_comp},
            "old_pct400": {
                "target": old_pct400_from_rms(rms0),
                "comp": old_pct400_from_rms(rms1),
            },
            "stable_remap_pct": {
                "target": stable_pct_from_rms(rms0, "target", meta),
                "comp": stable_pct_from_rms(rms1, "comp", meta),
            },
            "raw_rms_robust100": {
                "target": raw_rms_robust100_from_rms(rms0, "target", meta),
                "comp": raw_rms_robust100_from_rms(rms1, "comp", meta),
            },
        })
    return rows


def summarize_stream_views(stream_samples, preprocess_meta=None):
    rows = build_stream_view_rows(stream_samples, preprocess_meta=preprocess_meta)
    summary = {
        "ok": bool(rows),
        "sample_count": len(rows),
        "views": {},
    }
    for view_name in VALID_EMG_VIEWS:
        target_vals = []
        comp_vals = []
        for row in rows:
            pair = row[view_name]
            target_vals.append(pair["target"])
            comp_vals.append(pair["comp"])
        summary["views"][view_name] = {
            "target": _value_stats(target_vals),
            "comp": _value_stats(comp_vals),
            "target_sat100_ratio": _saturation_ratio(target_vals),
            "comp_sat100_ratio": _saturation_ratio(comp_vals),
        }
    return summary


def apply_emg_view_to_exact_rows(exact_rows, stream_samples, emg_view=DEFAULT_EMG_VIEW,
                                 preprocess_meta=None, max_match_dt_s=0.35):
    if emg_view not in VALID_EMG_VIEWS:
        raise ValueError("unsupported emg_view: %s" % emg_view)
    mapped_rows = []
    rows = build_stream_view_rows(stream_samples, preprocess_meta=preprocess_meta)
    ts_rows = [row["ts"] for row in rows if row.get("ts") is not None]
    usable_rows = [row for row in rows if row.get("ts") is not None]
    matched = 0
    fallback_current = 0
    if usable_rows:
        from bisect import bisect_left
    else:
        bisect_left = None
    for exact in exact_rows or []:
        values = list(exact.get("values") or [])
        if len(values) < 7:
            continue
        picked = None
        if emg_view == "current_pct" and not usable_rows:
            picked = {
                "target": _clip_pct(values[3]),
                "comp": _clip_pct(values[4]),
            }
            fallback_current += 1
        elif usable_rows and bisect_left is not None:
            ts = _to_float(exact.get("ts"))
            pos = bisect_left(ts_rows, ts)
            candidates = []
            if pos < len(usable_rows):
                candidates.append(usable_rows[pos])
            if pos > 0:
                candidates.append(usable_rows[pos - 1])
            if candidates:
                best = min(
                    candidates,
                    key=lambda item: abs((_to_float(item.get("ts"), 0.0) or 0.0) - (ts or 0.0)),
                )
                delta = abs((_to_float(best.get("ts"), 0.0) or 0.0) - (ts or 0.0))
                if delta <= float(max_match_dt_s):
                    picked = best[emg_view]
                    matched += 1
        if picked is None:
            continue
        values[3] = float(picked["target"])
        values[4] = float(picked["comp"])
        sample = dict(exact)
        sample["values"] = values
        features = dict(sample.get("features") or {})
        features["Target_RMS"] = values[3]
        features["Comp_RMS"] = values[4]
        sample["features"] = features
        sample["emg_view"] = emg_view
        mapped_rows.append(sample)
    return {
        "rows": mapped_rows,
        "matched_count": matched,
        "fallback_current_count": fallback_current,
        "input_exact_rows": len(exact_rows or []),
        "output_rows": len(mapped_rows),
    }
