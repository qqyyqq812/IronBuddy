#!/usr/bin/env python3
"""Export current Sensor Lab squat groups into a personal GRU dataset."""

from __future__ import print_function

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import ironbuddy_export_personal_bicep_gru_dataset as exporter  # noqa: E402


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--exercise" not in argv:
        argv = ["--exercise", "squat"] + argv
    return exporter.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
