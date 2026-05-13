#!/usr/bin/env python3
"""Validate and promote a personal bicep-curl GRU candidate locally.

This tool does not deploy to the board and does not restart services. By
default it only checks the candidate. Use --apply to replace the local canonical
bicep weight after creating a timestamped backup.
"""

from __future__ import print_function

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "hardware_engine" / "extreme_fusion_gru_bicep.pt"
BACKUP_DIR = ROOT / "hardware_engine" / "model_backups"
PERSONAL_GLOB = "extreme_fusion_gru_bicep_personal_*.pt"


def _rel(path):
    path = Path(path)
    try:
        return str(path.relative_to(ROOT))
    except Exception:
        return str(path)


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def latest_candidate():
    candidates = sorted((ROOT / "hardware_engine").glob(PERSONAL_GLOB))
    return candidates[-1] if candidates else None


def validate_candidate(candidate):
    candidate = Path(candidate)
    if not candidate.exists():
        return False, "candidate_missing"
    try:
        import torch
        sys.path.insert(0, str(ROOT))
        from hardware_engine.cognitive.fusion_model import CompensationGRU

        state = torch.load(str(candidate), map_location="cpu")
        model = CompensationGRU(input_size=7, hidden_size=16)
        model.load_state_dict(state)
        model.eval()
        return True, "loadable"
    except Exception as exc:
        return False, "load_failed:%s" % exc


def promote(candidate, apply=False):
    candidate = Path(candidate)
    ok, detail = validate_candidate(candidate)
    report = {
        "ok": bool(ok),
        "detail": detail,
        "candidate": _rel(candidate),
        "canonical": _rel(CANONICAL),
        "applied": False,
        "backup": None,
        "ts": time.time(),
    }
    if not ok:
        return 2, report
    if not apply:
        return 0, report

    if not CANONICAL.exists():
        return 2, dict(report, ok=False, detail="canonical_missing")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / ("extreme_fusion_gru_bicep.%s.pt" % stamp)
    shutil.copy2(str(CANONICAL), str(backup))

    tmp = CANONICAL.with_suffix(CANONICAL.suffix + ".tmp")
    shutil.copy2(str(candidate), str(tmp))
    os.replace(str(tmp), str(CANONICAL))

    report.update({
        "applied": True,
        "backup": _rel(backup),
        "detail": "promoted",
    })
    _atomic_write_json(BACKUP_DIR / ("promotion.%s.json" % stamp), report)
    return 0, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", default=None,
                        help="Candidate .pt path. Defaults to latest hardware_engine/%s." % PERSONAL_GLOB)
    parser.add_argument("--apply", action="store_true",
                        help="Actually replace hardware_engine/extreme_fusion_gru_bicep.pt after backup.")
    args = parser.parse_args(argv)

    candidate = Path(args.candidate) if args.candidate else latest_candidate()
    if candidate is None:
        print("[FATAL] no personal bicep candidate found")
        return 2
    code, report = promote(candidate, apply=args.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if code == 0 and not args.apply:
        print("dry_run=true; add --apply after you accept this candidate")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
