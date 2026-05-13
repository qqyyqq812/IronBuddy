#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe a personal bicep-curl GRU candidate (.pt) for collapsed/inverted boundary.

Usage:
    python3 tools/probe_model_boundary.py path/to/model.pt
    python3 tools/probe_model_boundary.py path/to/model.pt --json

Exit code:
    0 — model passes (no collapsed predictor, no boundary inversion)
    1 — model fails (one or more probes flagged)
    2 — could not load model (missing file, bad state_dict, etc.)

This script is the manual pre-deployment gate. It runs the 6 canonical probes
from `train_gru_three_class_bicep_personal._sanity_probe_model` plus 5 extra
synthetic windows (noise / ramp / pulse / saturation / typical standard) that
exercise corner cases that the canonical probes miss.

Python 3.7 compatible (no `X | None`, no walrus, no match/case).
"""

from __future__ import print_function

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hardware_engine.cognitive.fusion_model import CompensationGRU, CLASS_NAMES  # noqa: E402


# ---------------------------------------------------------------------------
# Canonical 6 probes (kept consistent with train script `_sanity_probe_model`)
# ---------------------------------------------------------------------------
CANONICAL_PROBES = [
    ("zero_window",  0.0,  0.0),
    ("low_both",     10.0, 10.0),
    ("mid_both",     30.0, 30.0),
    ("high_both",    80.0, 80.0),
    ("high_T_low_C", 80.0, 10.0),
    ("low_T_high_C", 10.0, 80.0),
]


def _make_constant_window(target_pct, comp_pct, seq_len=30):
    """7D window with constant Target_RMS / Comp_RMS (pre-normalized to [0,1])."""
    win = np.zeros((1, seq_len, 7), dtype=np.float32)
    win[:, :, 3] = float(target_pct) / 100.0
    win[:, :, 4] = float(comp_pct) / 100.0
    return win


def _make_noise_window(seq_len=30, seed=42):
    """Random uniform noise per frame, both EMG columns. No coherent signal."""
    rng = np.random.default_rng(seed)
    win = np.zeros((1, seq_len, 7), dtype=np.float32)
    win[:, :, 3] = rng.uniform(0.0, 1.0, size=(seq_len,)).astype(np.float32)
    win[:, :, 4] = rng.uniform(0.0, 1.0, size=(seq_len,)).astype(np.float32)
    return win


def _make_ramp_window(seq_len=30):
    """Target / Comp ramp linearly 0 → 100 together (typical fatigue-free build)."""
    win = np.zeros((1, seq_len, 7), dtype=np.float32)
    ramp = np.linspace(0.0, 1.0, seq_len, dtype=np.float32)
    win[0, :, 3] = ramp
    win[0, :, 4] = ramp
    return win


def _make_pulse_window(seq_len=30):
    """First half high EMG, second half low. Exercises temporal sensitivity."""
    win = np.zeros((1, seq_len, 7), dtype=np.float32)
    half = seq_len // 2
    win[0, :half, 3] = 0.80
    win[0, :half, 4] = 0.80
    win[0, half:, 3] = 0.05
    win[0, half:, 4] = 0.05
    return win


def _make_saturation_window(seq_len=30):
    """Both EMG at 1.0 (saturated). Edge case: clip values shouldn't fool the model."""
    win = np.ones((1, seq_len, 7), dtype=np.float32)
    # Only EMG columns saturated; zero out kinematic columns.
    win[:, :, 0] = 0.0  # Ang_Vel
    win[:, :, 1] = 0.0  # Angle
    win[:, :, 2] = 0.0  # Ang_Accel
    win[:, :, 5] = 0.0  # Symmetry
    win[:, :, 6] = 0.0  # Phase
    return win


def _make_typical_standard_rep(seq_len=30, seed=7):
    """Target oscillates ~0.50±0.05, Comp ~0.20±0.05 — looks like a good rep."""
    rng = np.random.default_rng(seed)
    win = np.zeros((1, seq_len, 7), dtype=np.float32)
    target_noise = rng.normal(0.50, 0.05, size=(seq_len,)).astype(np.float32)
    comp_noise = rng.normal(0.20, 0.05, size=(seq_len,)).astype(np.float32)
    win[0, :, 3] = np.clip(target_noise, 0.0, 1.0)
    win[0, :, 4] = np.clip(comp_noise, 0.0, 1.0)
    return win


def _eval_window(model, win, name, target_pct, comp_pct):
    with torch.no_grad():
        sim, logits, _phase = model(torch.from_numpy(win))
        probs = torch.softmax(logits, dim=1)[0].cpu().numpy().tolist()
        cls = int(np.argmax(probs))
        label = CLASS_NAMES[cls]
    return {
        "name": name,
        "target_rms": target_pct,
        "comp_rms": comp_pct,
        "label": label,
        "probs": [round(float(p), 3) for p in probs],
        "sim": round(float(sim.item()), 3),
    }


def run_probes(model):
    """Run canonical + extended probes; return a (results, health) tuple."""
    model.eval()
    results = []
    # Canonical 6.
    for name, t, c in CANONICAL_PROBES:
        results.append(_eval_window(model, _make_constant_window(t, c), name, t, c))
    # Extended 5.
    results.append(_eval_window(model, _make_noise_window(),       "noise",            "rand", "rand"))
    results.append(_eval_window(model, _make_ramp_window(),        "ramp_0_to_100",    "0->100", "0->100"))
    results.append(_eval_window(model, _make_pulse_window(),       "pulse_hi_then_lo", "80/5",  "80/5"))
    results.append(_eval_window(model, _make_saturation_window(),  "saturation",       100.0,  100.0))
    results.append(_eval_window(model, _make_typical_standard_rep(),
                                "typical_standard_rep",                                 50.0,   20.0))

    # ---- Detectors ----
    # Collapse: two complementary signals over canonical probes only (extended
    # probes add diagnostic value but aren't used for the pass/fail vote).
    #   1. Compensating-prob spread < 0.10 — model emits constant comp prob.
    #   2. Max class prob over all canonical probes < 0.50 — model never
    #      commits to any class even on extreme inputs.
    # Detector #2 catches the May-13 model which has spread = 0.114 (just over
    # the spread threshold) but max prob = 0.394 across all 6 canonicals.
    canon_names = set(n for n, _, _ in CANONICAL_PROBES)
    canon_results = [r for r in results if r["name"] in canon_names]
    comp_probs = [r["probs"][1] for r in canon_results]
    comp_spread = float(max(comp_probs) - min(comp_probs)) if comp_probs else 0.0
    max_class_prob = max(max(r["probs"]) for r in canon_results) if canon_results else 0.0
    is_collapsed = (comp_spread < 0.10) or (max_class_prob < 0.50)

    # Inversion: high_T_low_C should NOT be compensating.
    high_T_low_C = next((r for r in results if r["name"] == "high_T_low_C"), None)
    is_inverted = bool(high_T_low_C and high_T_low_C["label"] == "compensating")

    # Extra diagnostic: typical_standard_rep should NOT classify as compensating
    # (warning only — does not drive the pass/fail vote).
    typical = next((r for r in results if r["name"] == "typical_standard_rep"), None)
    typical_misclassifies = bool(typical and typical["label"] == "compensating")

    health = {
        "ok": (not is_collapsed) and (not is_inverted),
        "is_collapsed_predictor": bool(is_collapsed),
        "is_boundary_inverted": bool(is_inverted),
        "typical_standard_misclassified_as_comp": typical_misclassifies,
        "comp_prob_spread": round(comp_spread, 3),
        "max_class_prob": round(max_class_prob, 3),
        "probes": results,
    }
    return health


def _format_table(probes):
    headers = ["probe", "T_RMS", "C_RMS", "label", "p[std]", "p[comp]", "p[non]", "sim"]
    rows = [headers]
    for p in probes:
        rows.append([
            p["name"],
            str(p["target_rms"]),
            str(p["comp_rms"]),
            p["label"],
            "%.3f" % p["probs"][0],
            "%.3f" % p["probs"][1],
            "%.3f" % p["probs"][2],
            "%.3f" % p["sim"],
        ])
    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    out = []
    for r_idx, row in enumerate(rows):
        line = "  ".join(row[i].ljust(widths[i]) for i in range(len(headers)))
        out.append(line)
        if r_idx == 0:
            out.append("  ".join("-" * widths[i] for i in range(len(headers))))
    return "\n".join(out)


def load_model(model_path, input_size=7, hidden_size=16):
    if not Path(model_path).exists():
        raise FileNotFoundError(model_path)
    model = CompensationGRU(input_size=input_size, hidden_size=hidden_size)
    state = torch.load(model_path, map_location="cpu")
    model.load_state_dict(state)
    return model


def main(argv=None):
    parser = argparse.ArgumentParser(description="Probe a GRU .pt for collapsed/inverted boundary.")
    parser.add_argument("model_path", help="Path to candidate .pt file")
    parser.add_argument("--hidden-size", type=int, default=16,
                        help="GRU hidden_size (default: 16, matches train script)")
    parser.add_argument("--input-size", type=int, default=7,
                        help="Input feature count (default: 7)")
    parser.add_argument("--json", action="store_true",
                        help="Print health dict as JSON instead of table")
    args = parser.parse_args(argv)

    try:
        model = load_model(args.model_path, input_size=args.input_size, hidden_size=args.hidden_size)
    except Exception as exc:
        print("[ERROR] Could not load model: %s" % exc, file=sys.stderr)
        return 2

    health = run_probes(model)

    if args.json:
        print(json.dumps(health, ensure_ascii=False, indent=2))
    else:
        print("Probe results for %s" % args.model_path)
        print("")
        print(_format_table(health["probes"]))
        print("")
        print("comp_prob_spread (canonical) = %.3f (collapse if < 0.10)" % health["comp_prob_spread"])
        print("max_class_prob   (canonical) = %.3f (collapse if < 0.50)" % health["max_class_prob"])
        if health["ok"]:
            print("HEALTH: PASS")
        else:
            flags = []
            if health["is_collapsed_predictor"]:
                if health["comp_prob_spread"] < 0.10:
                    flags.append("collapsed_predictor (comp_prob_spread=%.3f < 0.10)" % health["comp_prob_spread"])
                if health["max_class_prob"] < 0.50:
                    flags.append("collapsed_predictor (max_class_prob=%.3f < 0.50)" % health["max_class_prob"])
            if health["is_boundary_inverted"]:
                flags.append("boundary_inverted (high_T_low_C -> compensating)")
            print("HEALTH: FAIL: " + " | ".join(flags))
        if health["typical_standard_misclassified_as_comp"]:
            print("WARN: typical_standard_rep classified as compensating (model is hostile to standard reps)")
    return 0 if health["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
