"""Static contract for visual rep events and GRU trigger ownership."""
import ast
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_LOOP = os.path.join(PROJECT_ROOT, "hardware_engine", "main_claw_loop.py")


REP_EVENT_FIELDS = (
    "exercise",
    "rep_index",
    "angle_metric",
    "min_angle",
    "rom",
    "visual_result",
    "final_result",
    "finalize_reason",
    "started_ts",
    "ended_ts",
)


def _src():
    with open(MAIN_LOOP, "r", encoding="utf-8") as f:
        return f.read()


def _class_body(src, name, next_name=None):
    marker = "class " + name + ":"
    start = src.find(marker)
    assert start >= 0, name + " missing"
    if next_name is None:
        end = src.find("\n\nasync def ", start)
    else:
        end = src.find("class " + next_name + ":", start + 1)
    assert end > start
    return src[start:end]


def test_main_loop_still_parses():
    ast.parse(_src())


def test_unified_rep_event_builder_has_required_fields():
    src = _src()
    assert "def _build_rep_event(" in src
    for field in REP_EVENT_FIELDS:
        assert '"' + field + '"' in src


def test_squat_and_curl_store_last_rep_event():
    src = _src()
    squat = _class_body(src, "SquatStateMachine", "DumbbellCurlFSM")
    curl = _class_body(src, "DumbbellCurlFSM")
    assert "self._last_rep_event = None" in squat
    assert "self._last_rep_event = _build_rep_event(" in squat
    assert "self._last_rep_event = None" in curl
    assert "self._last_rep_event = _build_rep_event(" in curl


def test_curl_visual_threshold_and_frontend_fields():
    curl = _class_body(_src(), "DumbbellCurlFSM")
    assert "ANGLE_STANDARD = 80" in curl
    assert "CURL_MIN_ROM" in curl
    assert 'cached.get("confidence"' in curl
    for field in (
        '"rep_in_progress"',
        '"last_rep_result"',
        '"last_rep_min_angle"',
        '"total_reps"',
        '"last_rep_event"',
    ):
        assert field in curl


def test_gru_uses_rep_event_and_emg_health_gate():
    src = _src()
    assert "_last_rep_event" in src
    assert "def _emg_signal_ok(" in src
    assert 'classification_source = "visual_fallback_no_emg"' in src
    assert 'classification_source = "gru"' in src
    assert "_d.log_rep(" in src
    assert "update_session_counts" in src
    assert "getattr(fsm,'_min_angle_in_rep'" not in src


def test_rep_event_preserves_final_classification_for_ui():
    src = _src()
    assert '"final_result": visual_result' in src
    assert '_rep_event["final_result"] = final_class' in src
    assert '_rep_event["classification_source"] = classification_source' in src
