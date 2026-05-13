"""Contracts for FSM pose-frame de-duplication and stale-frame gating."""
import ast
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_LOOP = os.path.join(PROJECT_ROOT, "hardware_engine", "main_claw_loop.py")


def _src():
    with open(MAIN_LOOP, "r", encoding="utf-8") as f:
        return f.read()


def _function_body(src, name):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError("%s missing" % name)


def test_pose_frame_key_prefers_frame_idx_and_timestamp():
    body = _function_body(_src(), "_pose_frame_key")
    assert 'pose_data.get("frame_idx")' in body
    assert 'pose_data.get("timestamp")' in body
    assert '"idx"' in body
    assert '"ts"' in body
    assert '"mtime"' in body


def test_pose_frame_is_fresh_has_tight_stale_guard():
    src = _src()
    body = _function_body(src, "_pose_frame_is_fresh")
    assert "POSE_FRAME_MAX_AGE_S = 0.75" in src
    assert "now - ts" in body
    assert "<= float(max_age_s)" in body


def test_main_loop_skips_duplicate_and_stale_pose_frames():
    src = _src()
    assert "class _PoseFrameSkipped(Exception):" in src
    assert "_last_pose_key = [None]" in src
    assert "_pose_frame_is_fresh(pose_data, _pose_mtime)" in src
    assert "_pose_key == _last_pose_key[0]" in src
    assert "_last_pose_key[0] = _pose_key" in src
    assert "raise _PoseFrameSkipped()" in src
    assert "except _PoseFrameSkipped:" in src
