#!/usr/bin/env python3
"""Train a personal squat GRU from current Sensor Lab exports only."""

from __future__ import print_function

import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import train_gru_three_class_bicep_personal as trainer  # noqa: E402

DEFAULT_RUN_ROOT = ROOT / "data" / "squat_personal" / "training_runs"
DEFAULT_RUN_ROOT_LABEL = "data/squat_personal/training_runs"


def default_out_path():
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return DEFAULT_RUN_ROOT / stamp / "candidate_extreme_fusion_gru.pt"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--out" not in argv:
        argv.extend(["--out", str(default_out_path())])
    if "--exercise" not in argv:
        argv.extend(["--exercise", "squat"])
    return trainer.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
