"""Static checks for squat angle accounting safety.

main_claw_loop.py imports board-only runtime dependencies, so this file keeps
coverage at source/AST level.
"""
import ast
import os


MAIN_LOOP = os.path.join(
    os.path.dirname(__file__), "..", "hardware_engine", "main_claw_loop.py"
)


def _src():
    with open(MAIN_LOOP, "r", encoding="utf-8") as f:
        return f.read()


def _function_body(src, name):
    marker = "def %s" % name
    start = src.find(marker)
    assert start >= 0, "missing %s" % name
    end = src.find("\ndef ", start + 1)
    return src[start:end if end >= 0 else len(src)]


def test_main_claw_loop_parses():
    ast.parse(_src())


def test_squat_rep_accounting_exposes_last_result_fields():
    s = _src()
    for field in (
        '"rep_in_progress"',
        '"rep_min_angle"',
        '"last_rep_result"',
        '"last_finalize_reason"',
        '"last_drop_reason"',
        '"last_rep_min_angle"',
        '"last_rep_mode"',
        '"total_reps"',
    ):
        assert field in s


def test_pending_rep_finalizes_as_binary_result_without_changing_threshold():
    body = _function_body(_src(), "_finalize_pending_rep")
    assert 'result = "standard" if bottom < self.ANGLE_STANDARD else "non_standard"' in body
    assert "self.good_squats += 1" in body
    assert "self.failed_squats += 1" in body
    assert "self._total_reps_count += 1" in body
    assert "ANGLE_STANDARD = 90" in _src()


def test_bad_frames_finalize_in_progress_rep_after_grace_period():
    body = _function_body(_src(), "_handle_pending_bad_frame")
    assert "self._REP_LOSS_GRACE_S" in body
    assert 'self._finalize_pending_rep(reason, current_angle=current_angle)' in body


def test_uncertain_depth_frames_are_rejected_before_smoothing_and_min_update():
    s = _src()
    helper = _function_body(s, "_squat_depth_frame_is_credible")
    assert "raw >= 90.0" in helper
    assert "dist < 55.0" in helper
    assert "raw < 75.0" in helper
    assert "conf < 0.20" in helper
    update_idx = s.find("depth_frame_credible = _squat_depth_frame_is_credible")
    append_idx = s.find("self._angle_history.append(raw_angle)", update_idx)
    assert update_idx >= 0 and append_idx > update_idx
    guard_block = s[update_idx:append_idx]
    assert '"drop_reason": "uncertain_depth_frame"' in guard_block
    assert "return None" in guard_block
    assert "self._handle_pending_bad_frame(\"uncertain_depth_frame\"" in guard_block


def test_min_angle_updates_only_from_filtered_smooth_or_conservative_virtual():
    s = _src()
    assert 'self._begin_pending_rep(angle, "smooth_frame")' in s
    assert 'self._update_pending_min(angle, "smooth_frame")' in s
    assert 'self._update_pending_min(virtual_bottom, "virtual")' in s
    assert "self._update_pending_min(raw_angle" not in s
