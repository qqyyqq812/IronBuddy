"""Tests for tools/build_code_graph.py — IronBuddy code graph generator.

Python 3.7 compatible. stdlib only.
"""
import json
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.build_code_graph import (
    build_graph,
    parse_imports,
    kind_for_path,
    is_excluded,
    module_to_relpath,
    loc_count,
)


# ---------- parse_imports ----------

def test_parse_imports_simple(tmp_path):
    f = tmp_path / "demo.py"
    f.write_text(
        "import os\n"
        "import hardware_engine.voice_daemon\n"
        "from hardware_engine.cognitive import coach_knowledge\n"
    )
    imports = parse_imports(f)
    assert "os" in imports
    assert "hardware_engine.voice_daemon" in imports
    assert "hardware_engine.cognitive.coach_knowledge" in imports


def test_parse_imports_handles_syntax_error(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def x(:\n    pass\n")
    imports = parse_imports(f)
    assert imports == []


def test_parse_imports_nonpy_returns_empty(tmp_path):
    f = tmp_path / "page.html"
    f.write_text("<html>not python</html>")
    assert parse_imports(f) == []


# ---------- kind_for_path ----------

def test_kind_for_path_voice():
    assert kind_for_path("hardware_engine/voice/recorder.py") == "voice"
    assert kind_for_path("hardware_engine/voice_daemon.py") == "voice"


def test_kind_for_path_cognitive():
    assert kind_for_path("hardware_engine/cognitive/coach_knowledge.py") == "cognitive"


def test_kind_for_path_fsm():
    assert kind_for_path("hardware_engine/main_claw_loop.py") == "fsm"


def test_kind_for_path_api():
    assert kind_for_path("streamer_app.py") == "api"


def test_kind_for_path_frontend():
    assert kind_for_path("templates/index.html") == "frontend"


def test_kind_for_path_debug():
    assert kind_for_path("tools/ironbuddy_operator_console.py") == "debug"
    assert kind_for_path("tools/ironbuddy_sensor_lab.py") == "debug"


# ---------- is_excluded ----------

def test_is_excluded_tests():
    assert is_excluded("tests/test_foo.py")


def test_is_excluded_archive():
    assert is_excluded(".archive/old.py")


def test_not_excluded_main():
    assert not is_excluded("streamer_app.py")
    assert not is_excluded("hardware_engine/voice_daemon.py")


# ---------- module_to_relpath ----------

def test_module_to_relpath_direct():
    paths = {"hardware_engine/voice_daemon.py", "streamer_app.py"}
    assert module_to_relpath("hardware_engine.voice_daemon", paths) == \
        "hardware_engine/voice_daemon.py"


def test_module_to_relpath_partial():
    """When importing a symbol from a module, fall back to the module file."""
    paths = {"hardware_engine/cognitive/coach_knowledge.py"}
    # `from hardware_engine.cognitive.coach_knowledge import lookup`
    got = module_to_relpath("hardware_engine.cognitive.coach_knowledge.lookup",
                             paths)
    assert got == "hardware_engine/cognitive/coach_knowledge.py"


def test_module_to_relpath_unknown():
    assert module_to_relpath("requests", {"streamer_app.py"}) is None


# ---------- loc_count ----------

def test_loc_count(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("a\nb\nc\n")
    assert loc_count(f) == 3


# ---------- build_graph ----------

def test_build_graph_returns_nodes_and_edges():
    g = build_graph(repo_root=PROJECT_ROOT)
    assert "nodes" in g and "edges" in g
    assert isinstance(g["nodes"], list)
    assert isinstance(g["edges"], list)
    assert len(g["nodes"]) > 0


def test_build_graph_includes_core_files():
    g = build_graph(repo_root=PROJECT_ROOT)
    paths = {n["path"] for n in g["nodes"]}
    assert "streamer_app.py" in paths
    assert "hardware_engine/voice_daemon.py" in paths
    assert "hardware_engine/main_claw_loop.py" in paths


def test_build_graph_node_schema():
    g = build_graph(repo_root=PROJECT_ROOT)
    for n in g["nodes"]:
        assert "id" in n
        assert "label" in n
        assert "kind" in n
        assert "loc" in n and isinstance(n["loc"], int)
        assert "git_age_days" in n and isinstance(n["git_age_days"], int)
        assert "path" in n


def test_build_graph_excludes_tests_and_archive():
    g = build_graph(repo_root=PROJECT_ROOT)
    paths = [n["path"] for n in g["nodes"]]
    assert not any(p.startswith("tests/") for p in paths)
    assert not any(p.startswith(".archive/") for p in paths)


def test_build_graph_has_metadata():
    g = build_graph(repo_root=PROJECT_ROOT)
    assert "generated_at" in g
    assert "commit" in g


def test_build_graph_edges_reference_existing_nodes():
    g = build_graph(repo_root=PROJECT_ROOT)
    node_ids = {n["id"] for n in g["nodes"]}
    for e in g["edges"]:
        assert e["source"] in node_ids
        assert e["target"] in node_ids
