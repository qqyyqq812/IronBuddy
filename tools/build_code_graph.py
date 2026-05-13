#!/usr/bin/env python3
"""Generate data/code_graph/graph.json for IronBuddy 3d-force-graph viewer.

Python 3.7 compatible. stdlib only.

Usage:
    python3 tools/build_code_graph.py --refresh
    python3 tools/build_code_graph.py --out custom/path.json
"""
from __future__ import absolute_import, print_function

import argparse
import ast
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path


INCLUDE_DIRS = (
    "hardware_engine",
    "tools",
    "scripts",
)
INCLUDE_FILES = (
    "streamer_app.py",
)
FRONTEND_FILE = "templates/index.html"

EXCLUDE_PREFIXES = (
    "tests/",
    ".archive/",
    "tools/rknn-toolkit_source",
    "hardware_engine/__pycache__",
    "tools/__pycache__",
    "scripts/__pycache__",
)

# Order matters: longer / more specific prefix first.
KIND_BY_PATH = (
    ("hardware_engine/main_claw_loop.py", "fsm"),
    ("hardware_engine/voice_daemon.py", "voice"),
    ("hardware_engine/voice", "voice"),
    ("hardware_engine/cognitive", "cognitive"),
    ("hardware_engine/sensor", "sensor"),
    ("hardware_engine/ai_sensory", "vision"),
    ("hardware_engine/integrations", "cloud"),
    ("streamer_app.py", "api"),
    ("templates/", "frontend"),
    ("tools/ironbuddy_operator_console.py", "debug"),
    ("tools/ironbuddy_sensor_lab.py", "debug"),
    ("tools/", "shared"),
    ("scripts/", "shared"),
)


def kind_for_path(rel_path):
    for prefix, kind in KIND_BY_PATH:
        if rel_path.startswith(prefix):
            return kind
    return "shared"


def is_excluded(rel_path):
    return any(rel_path.startswith(p) for p in EXCLUDE_PREFIXES)


def collect_files(repo_root):
    files = []
    repo_root = Path(repo_root)
    for inc in INCLUDE_DIRS:
        base = repo_root / inc
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            rel = str(p.relative_to(repo_root)).replace(os.sep, "/")
            if is_excluded(rel):
                continue
            files.append((rel, p))
    for inc in INCLUDE_FILES:
        p = repo_root / inc
        if p.exists():
            rel = inc
            if not is_excluded(rel):
                files.append((rel, p))
    fp = repo_root / FRONTEND_FILE
    if fp.exists():
        files.append((FRONTEND_FILE, fp))
    return files


def parse_imports(file_path):
    """Return list of dotted module names imported. Ignores syntax errors and
    non-Python files.
    """
    if not str(file_path).endswith(".py"):
        return []
    try:
        with open(str(file_path), "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return []
    try:
        tree = ast.parse(text)
    except Exception:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                out.append((mod + "." + alias.name).strip("."))
    return [m for m in out if m]


def module_to_relpath(module_name, all_paths_set):
    """Map a dotted module name to one of our nodes if possible."""
    candidates = [
        module_name.replace(".", "/") + ".py",
        module_name.replace(".", "/") + "/__init__.py",
    ]
    for c in candidates:
        if c in all_paths_set:
            return c
    parts = module_name.split(".")
    # Try shortening from the right (e.g. import-symbol → module file)
    while parts:
        c = "/".join(parts) + ".py"
        if c in all_paths_set:
            return c
        c = "/".join(parts) + "/__init__.py"
        if c in all_paths_set:
            return c
        parts = parts[:-1]
    return None


def loc_count(file_path):
    try:
        with open(str(file_path), "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def git_age_days(rel_path, repo_root):
    """Return -1 if file is dirty (uncommitted), else days since last commit.
    Returns 999 on git error.
    """
    try:
        dirty = subprocess.check_output(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--", rel_path],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8").strip()
        if dirty:
            return -1
        ts_str = subprocess.check_output(
            ["git", "-C", str(repo_root), "log", "-1", "--format=%ct", "--", rel_path],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8").strip()
        if not ts_str:
            return 999
        ts = int(ts_str)
        return int((time.time() - ts) / 86400)
    except Exception:
        return 999


def head_short_sha(repo_root):
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8").strip()
    except Exception:
        return ""


def build_graph(repo_root=None):
    repo_root = Path(repo_root or ".").resolve()
    files = collect_files(repo_root)

    # Build node id mapping. Use basename when unique, else full rel path.
    paths_set = {rel for rel, _ in files}
    label_to_count = {}
    for rel, _ in files:
        b = os.path.basename(rel)
        label_to_count[b] = label_to_count.get(b, 0) + 1

    rel_to_id = {}
    nodes = []
    for rel, p in files:
        b = os.path.basename(rel)
        node_id = b if label_to_count[b] == 1 else rel
        rel_to_id[rel] = node_id
        nodes.append({
            "id": node_id,
            "label": os.path.splitext(b)[0],
            "kind": kind_for_path(rel),
            "loc": loc_count(p),
            "git_age_days": git_age_days(rel, repo_root),
            "path": rel,
        })

    edges = []
    seen_pairs = set()
    for rel, p in files:
        if not rel.endswith(".py"):
            continue
        src_id = rel_to_id[rel]
        for mod in parse_imports(p):
            target_rel = module_to_relpath(mod, paths_set)
            if not target_rel or target_rel == rel:
                continue
            target_id = rel_to_id.get(target_rel)
            if not target_id:
                continue
            pair = (src_id, target_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            edges.append({"source": src_id, "target": target_id, "kind": "import"})

    return {
        "nodes": nodes,
        "edges": edges,
        "generated_at": datetime.datetime.now().isoformat(),
        "commit": head_short_sha(repo_root),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true",
                        help="rebuild even if graph.json already exists")
    parser.add_argument("--out", default="data/code_graph/graph.json")
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()

    out = Path(args.repo_root).resolve() / args.out
    if not args.refresh and out.exists():
        print("graph exists at {} (pass --refresh to rebuild)".format(out))
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    g = build_graph(args.repo_root)
    out.write_text(json.dumps(g, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote {} nodes, {} edges to {}".format(
        len(g["nodes"]), len(g["edges"]), out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
