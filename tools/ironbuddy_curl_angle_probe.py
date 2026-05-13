#!/usr/bin/env python3
"""Poll IronBuddy curl angle diagnostics during a short live test."""
import argparse
import json
import os
import time
import urllib.error
import urllib.request


DEFAULT_BOARD_IP = "10.29.10.224"
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _json_request(url, method="GET", payload=None, timeout=3.0):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _OPENER.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw)


def _ensure_parent(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent)


def _default_out_path():
    ts = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(
        "docs",
        "test_runs",
        "ironbuddy_operator",
        ts + "_curl_angle_probe.jsonl",
    )


def _fmt(value, default="-"):
    if value is None:
        return default
    try:
        return "%.1f" % float(value)
    except Exception:
        return str(value)


def _summarize(samples):
    valid = [s for s in samples if s.get("angle_diag", {}).get("exercise") == "bicep_curl"]
    if not valid:
        return {"samples": len(samples), "curl_samples": 0}
    diag_rows = [s.get("angle_diag", {}) for s in valid]
    raw_vals = [d.get("raw_angle") for d in diag_rows if isinstance(d.get("raw_angle"), (int, float))]
    smooth_vals = [d.get("smooth_angle") for d in diag_rows if isinstance(d.get("smooth_angle"), (int, float))]
    left_vals = [d.get("left_angle") for d in diag_rows if isinstance(d.get("left_angle"), (int, float))]
    right_vals = [d.get("right_angle") for d in diag_rows if isinstance(d.get("right_angle"), (int, float))]
    reasons = {}
    sides = {}
    for d in diag_rows:
        reason = d.get("selection_reason") or ""
        side = d.get("selected_side") or ""
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
        if side:
            sides[side] = sides.get(side, 0) + 1
    return {
        "samples": len(samples),
        "curl_samples": len(valid),
        "min_raw_angle": min(raw_vals) if raw_vals else None,
        "min_smooth_angle": min(smooth_vals) if smooth_vals else None,
        "min_left_angle": min(left_vals) if left_vals else None,
        "min_right_angle": min(right_vals) if right_vals else None,
        "selected_sides": sides,
        "selection_reasons": reasons,
        "last_rep_event": valid[-1].get("last_rep_event"),
        "last_rep_result": valid[-1].get("last_rep_result"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board-ip", default=DEFAULT_BOARD_IP)
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--interval", type=float, default=0.15)
    parser.add_argument("--out", default=_default_out_path())
    parser.add_argument("--switch-curl", action="store_true",
                        help="Switch board to bicep_curl + pure_vision before sampling.")
    args = parser.parse_args()

    base = "http://%s:5000" % args.board_ip
    if args.switch_curl:
        _json_request(base + "/api/exercise_mode", method="POST",
                      payload={"mode": "bicep_curl", "src": "curl_angle_probe"})
        _json_request(base + "/api/switch_inference_mode", method="POST",
                      payload={"mode": "pure_vision", "src": "curl_angle_probe"})

    _ensure_parent(args.out)
    samples = []
    deadline = time.time() + max(0.1, args.duration)
    next_print = 0.0
    with open(args.out, "w", encoding="utf-8") as f:
        while time.time() < deadline:
            now = time.time()
            try:
                state = _json_request(base + "/state_feed", timeout=2.0)
                state["_probe_ts"] = now
                samples.append(state)
                f.write(json.dumps(state, ensure_ascii=False) + "\n")
                f.flush()
                diag = state.get("angle_diag", {})
                if now >= next_print:
                    print(
                        "t=%4.1fs ex=%s state=%s angle=%s raw=%s L=%s R=%s side=%s reason=%s reps=%s last=%s"
                        % (
                            args.duration - max(0.0, deadline - now),
                            state.get("exercise"),
                            state.get("state"),
                            _fmt(state.get("angle")),
                            _fmt(diag.get("raw_angle")),
                            _fmt(diag.get("left_angle")),
                            _fmt(diag.get("right_angle")),
                            diag.get("selected_side", "-"),
                            diag.get("selection_reason", "-"),
                            state.get("total_reps"),
                            state.get("last_rep_result") or "-",
                        )
                    )
                    next_print = now + 0.5
            except (OSError, urllib.error.URLError, ValueError) as exc:
                print("probe_error=%s" % exc)
            time.sleep(max(0.03, args.interval))

    summary = _summarize(samples)
    print("out=%s" % args.out)
    print("summary=%s" % json.dumps(summary, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
