"""TDD RED: DumbbellCurlFSM rep-event fields needed for correct DB write.

Current state (FAILING):
  - _last_rep_min_angle not set before _min_angle_in_rep is reset to 999
  - _pending_gru_angle_result not initialised in __init__
  - No _finalize_curl_rep() helper (inline logic, not symmetric with SquatStateMachine)

After implementation these tests must PASS (GREEN).
All checks are source-text level to avoid importing board-only deps (cv2, torch).
"""
import ast
import os

MAIN_LOOP = os.path.join(
    os.path.dirname(__file__), "..", "hardware_engine", "main_claw_loop.py"
)


def _src():
    with open(MAIN_LOOP, "r", encoding="utf-8") as f:
        return f.read()


def _class_body(src, class_name):
    """Return source text from 'class <name>:' to the next top-level 'class '."""
    marker = "class %s" % class_name
    start = src.find(marker)
    assert start >= 0, "class %s not found" % class_name
    # Find next top-level class definition
    end = src.find("\nclass ", start + 1)
    return src[start:end if end >= 0 else len(src)]


def _method_body(src, class_name, method_name):
    """Return source text of one method inside a class."""
    class_src = _class_body(src, class_name)
    marker = "    def %s" % method_name
    start = class_src.find(marker)
    assert start >= 0, "%s.%s not found" % (class_name, method_name)
    # Next method at same indent level
    end = class_src.find("\n    def ", start + 1)
    return class_src[start:end if end >= 0 else len(class_src)]


# ---------------------------------------------------------------------------
# 1. __init__ fields
# ---------------------------------------------------------------------------

def test_curl_fsm_init_has_last_rep_min_angle():
    """_last_rep_min_angle must be initialised to 999 (mirrors SquatStateMachine)."""
    body = _method_body(_src(), "DumbbellCurlFSM", "__init__")
    assert "_last_rep_min_angle" in body, (
        "DumbbellCurlFSM.__init__ missing self._last_rep_min_angle; "
        "DB write reads _min_angle_in_rep=999 (already reset) — needs saved value"
    )


def test_curl_fsm_init_has_pending_gru_angle_result():
    """_pending_gru_angle_result must be initialised (mirrors SquatStateMachine)."""
    body = _method_body(_src(), "DumbbellCurlFSM", "__init__")
    assert "_pending_gru_angle_result" in body, (
        "DumbbellCurlFSM.__init__ missing self._pending_gru_angle_result; "
        "GRU trigger loop uses getattr(fsm,'_pending_gru_angle_result',None) — "
        "silently returns None for curl"
    )


def test_curl_fsm_init_has_rep_in_progress():
    """_rep_in_progress flag required for unified rep event contract."""
    body = _method_body(_src(), "DumbbellCurlFSM", "__init__")
    assert "_rep_in_progress" in body, (
        "DumbbellCurlFSM.__init__ missing self._rep_in_progress"
    )


# ---------------------------------------------------------------------------
# 2. _finalize_curl_rep method exists (symmetric with _finalize_pending_rep)
# ---------------------------------------------------------------------------

def test_curl_fsm_has_finalize_curl_rep_method():
    """DumbbellCurlFSM must have a _finalize_curl_rep() method, not inline logic."""
    s = _src()
    assert "def _finalize_curl_rep" in s, (
        "_finalize_curl_rep() method missing from DumbbellCurlFSM; "
        "inline finalization makes it impossible to save _last_rep_min_angle "
        "before clearing _min_angle_in_rep=999"
    )


# ---------------------------------------------------------------------------
# 3. Correct ordering: save before reset
# ---------------------------------------------------------------------------

def test_curl_fsm_saves_last_rep_min_angle_before_reset():
    """_last_rep_min_angle = self._min_angle_in_rep MUST appear before
    self._min_angle_in_rep = 999 inside DumbbellCurlFSM (either in
    _finalize_curl_rep or inline)."""
    s = _src()
    class_src = _method_body(s, "DumbbellCurlFSM", "_finalize_curl_rep")

    save_idx = class_src.find("_last_rep_min_angle = self._min_angle_in_rep")
    reset_idx = class_src.find("self._min_angle_in_rep = 999")

    assert save_idx >= 0, (
        "_last_rep_min_angle = self._min_angle_in_rep assignment not found "
        "in DumbbellCurlFSM"
    )
    assert reset_idx >= 0, "self._min_angle_in_rep = 999 reset not found in DumbbellCurlFSM"
    assert save_idx < reset_idx, (
        "_last_rep_min_angle must be saved BEFORE _min_angle_in_rep is reset to 999; "
        "got save_idx=%d reset_idx=%d" % (save_idx, reset_idx)
    )


# ---------------------------------------------------------------------------
# 4. SquatStateMachine symmetric check (must still pass after refactor)
# ---------------------------------------------------------------------------

def test_squat_fsm_still_has_last_rep_min_angle():
    """SquatStateMachine already has _last_rep_min_angle — ensure refactor keeps it."""
    body = _method_body(_src(), "SquatStateMachine", "__init__")
    assert "_last_rep_min_angle" in body, (
        "SquatStateMachine.__init__ should retain _last_rep_min_angle"
    )


def test_source_parses():
    ast.parse(_src())
