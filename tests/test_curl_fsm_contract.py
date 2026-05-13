"""Dumbbell curl FSM contract tests.

The full main loop imports board-only runtime dependencies, so these tests load
only the curl FSM class body with tiny stubs and drive it with synthetic elbow
angles.
"""
import json
import logging
import math
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_LOOP = os.path.join(PROJECT_ROOT, "hardware_engine", "main_claw_loop.py")


class _FakeTime(object):
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


def _read_main_loop():
    with open(MAIN_LOOP, "r", encoding="utf-8") as f:
        return f.read()


def _load_curl_fsm():
    src = _read_main_loop()
    start = src.find("class DumbbellCurlFSM:")
    assert start >= 0, "DumbbellCurlFSM missing"
    end = src.find("\n\nasync def _deepseek", start)
    assert end > start, "DumbbellCurlFSM end marker missing"
    fake_time = _FakeTime()
    ns = {
        "json": json,
        "logging": logging,
        "math": math,
        "os": os,
        "time": fake_time,
        "_GRU_MODEL": None,
        "_pose_fps": lambda pose_data: 0.0,
        "_write_angle_debug": lambda *args, **kwargs: None,
        "_round_point": lambda pt: [round(float(pt[0]), 1), round(float(pt[1]), 1)],
    }
    ns["_safe_float"] = lambda value, default=0.0: float(value) if value is not None else float(default)
    ns["_build_rep_event"] = lambda exercise, rep_index, angle_metric, min_angle, rom, visual_result, finalize_reason, started_ts, ended_ts: {
        "exercise": exercise,
        "rep_index": int(rep_index or 0),
        "angle_metric": angle_metric,
        "min_angle": float(min_angle),
        "rom": float(rom),
        "visual_result": visual_result,
        "final_result": visual_result,
        "finalize_reason": finalize_reason,
        "started_ts": float(started_ts),
        "ended_ts": float(ended_ts),
    }
    exec(compile(src[start:end], "<curl_fsm>", "exec"), ns)
    return ns["DumbbellCurlFSM"], fake_time


def _pose_with_elbow_angle(angle_deg):
    kpts = [[0.0, 0.0, 0.01] for _ in range(17)]
    elbow = (100.0, 100.0)
    shoulder = (100.0, 0.0)
    length = 100.0
    theta = math.radians(float(angle_deg))
    wrist = (
        elbow[0] + math.sin(theta) * length,
        elbow[1] - math.cos(theta) * length,
    )
    kpts[5] = [shoulder[0], shoulder[1], 0.95]
    kpts[7] = [elbow[0], elbow[1], 0.95]
    kpts[9] = [wrist[0], wrist[1], 0.95]
    return {"objects": [{"score": 0.99, "kpts": kpts}], "source": "test"}


def _arm_points(angle_deg, x_offset=0.0):
    elbow = (100.0 + float(x_offset), 100.0)
    shoulder = (100.0 + float(x_offset), 0.0)
    length = 100.0
    theta = math.radians(float(angle_deg))
    wrist = (
        elbow[0] + math.sin(theta) * length,
        elbow[1] - math.cos(theta) * length,
    )
    return shoulder, elbow, wrist


def _pose_with_dual_arm_angles(left_angle, right_angle, left_conf=0.95, right_conf=0.55):
    kpts = [[0.0, 0.0, 0.01] for _ in range(17)]
    l_shoulder, l_elbow, l_wrist = _arm_points(left_angle, x_offset=-80.0)
    r_shoulder, r_elbow, r_wrist = _arm_points(right_angle, x_offset=80.0)
    kpts[5] = [l_shoulder[0], l_shoulder[1], left_conf]
    kpts[7] = [l_elbow[0], l_elbow[1], left_conf]
    kpts[9] = [l_wrist[0], l_wrist[1], left_conf]
    kpts[6] = [r_shoulder[0], r_shoulder[1], right_conf]
    kpts[8] = [r_elbow[0], r_elbow[1], right_conf]
    kpts[10] = [r_wrist[0], r_wrist[1], right_conf]
    return {"objects": [{"score": 0.99, "kpts": kpts}], "source": "test"}


def _drive_angles(fsm, fake_time, angles):
    for angle in angles:
        fake_time.advance(0.12)
        fsm.update(_pose_with_elbow_angle(angle))


def test_standard_curl_emits_unified_rep_event():
    DumbbellCurlFSM, fake_time = _load_curl_fsm()
    fsm = DumbbellCurlFSM()
    fsm.sync_to_frontend = lambda current_angle=180.0, nn_result=None: None

    _drive_angles(
        fsm,
        fake_time,
        [160, 154, 146, 136, 118, 96, 72, 48, 45, 50, 62, 80, 100, 120],
    )

    assert fsm._total_reps_count == 1
    event = fsm._last_rep_event
    assert event["exercise"] == "bicep_curl"
    assert event["rep_index"] == 1
    assert event["angle_metric"] == "elbow_angle"
    assert event["min_angle"] <= 65.0
    assert event["rom"] >= 35.0
    assert event["visual_result"] == "standard"
    assert event["final_result"] == "standard"
    assert event["finalize_reason"] == "normal_recovery"


def test_short_range_curl_is_non_standard_but_still_one_rep():
    DumbbellCurlFSM, fake_time = _load_curl_fsm()
    fsm = DumbbellCurlFSM()
    fsm.sync_to_frontend = lambda current_angle=180.0, nn_result=None: None

    _drive_angles(
        fsm,
        fake_time,
        [160, 154, 148, 138, 126, 112, 96, 84, 86, 96, 110, 122, 134, 146],
    )

    assert fsm._total_reps_count == 1
    assert fsm._last_rep_event["visual_result"] == "non_standard"
    assert fsm._last_rep_event["final_result"] == "non_standard"
    assert fsm._last_rep_event["min_angle"] > 65.0


def test_low_bottom_angle_is_standard_even_with_short_rom():
    DumbbellCurlFSM, fake_time = _load_curl_fsm()
    fsm = DumbbellCurlFSM()
    fsm.sync_to_frontend = lambda current_angle=180.0, nn_result=None: None

    fake_time.advance(0.12)
    fsm._begin_curl_rep(100.0)
    fsm._min_angle_in_rep = 78.0
    fsm._rep_last_valid_angle = 100.0
    fake_time.advance(0.12)
    assert fsm._finalize_curl_rep("unit_short_rom", current_angle=100.0)

    event = fsm._last_rep_event
    assert event["min_angle"] <= fsm.ANGLE_STANDARD
    assert event["rom"] < fsm.CURL_MIN_ROM
    assert event["visual_result"] == "standard"
    assert event["final_result"] == "standard"
    assert fsm.good_squats == 1
    assert fsm.failed_squats == 0


def test_curl_jitter_after_finalize_does_not_double_count():
    DumbbellCurlFSM, fake_time = _load_curl_fsm()
    fsm = DumbbellCurlFSM()
    fsm.sync_to_frontend = lambda current_angle=180.0, nn_result=None: None

    _drive_angles(
        fsm,
        fake_time,
        [160, 154, 146, 136, 118, 96, 72, 48, 45, 50, 62, 80, 100, 120],
    )
    _drive_angles(fsm, fake_time, [107, 109, 108, 110, 109, 111])

    assert fsm._total_reps_count == 1


def test_curl_no_person_resets_pre_entry_motion_history():
    DumbbellCurlFSM, fake_time = _load_curl_fsm()
    fsm = DumbbellCurlFSM()
    fsm.sync_to_frontend = lambda current_angle=180.0, nn_result=None: None

    fsm.state = "STAND"
    fsm._angle_history = [170, 160, 150, 140, 130, 120]
    fsm._closing_frames = 3
    fsm._last_valid_angle_cu = 120.0
    fsm._last_ang_vel_cu = -80.0
    fsm._active_side = "left"

    fake_time.advance(0.12)
    fsm.update({"objects": []})
    fake_time.advance(0.12)
    fsm.update(_pose_with_elbow_angle(70))

    assert fsm.state == "STAND"
    assert not fsm._rep_in_progress
    assert fsm._total_reps_count == 0


def test_curl_no_person_cancels_partial_rep_state():
    DumbbellCurlFSM, fake_time = _load_curl_fsm()
    fsm = DumbbellCurlFSM()
    fsm.sync_to_frontend = lambda current_angle=180.0, nn_result=None: None

    fsm.state = "CURLING"
    fsm._rep_in_progress = True
    fsm._rep_started_ts = fake_time.time()
    fsm._rep_start_angle = 130.0
    fsm._min_angle_in_rep = 72.0
    fsm._active_side = "left"
    fsm._opening_frames = 1

    fake_time.advance(0.12)
    fsm.update({"objects": []})

    assert fsm.state == "NO_PERSON"
    assert not fsm._rep_in_progress
    assert fsm._rep_start_angle is None
    assert fsm._min_angle_in_rep == 999
    assert fsm._active_side is None
    assert fsm._total_reps_count == 0


def test_curl_side_switch_resets_pre_entry_motion_history():
    DumbbellCurlFSM, fake_time = _load_curl_fsm()
    fsm = DumbbellCurlFSM()
    fsm.sync_to_frontend = lambda current_angle=180.0, nn_result=None: None

    fsm.state = "STAND"
    fsm._angle_history = [170, 160, 150, 140, 130, 120]
    fsm._closing_frames = 3
    fsm._side_angle_history["left"] = [170, 160, 150, 140, 130, 120]
    fsm._side_closing_frames["left"] = 3

    fake_time.advance(0.12)
    fsm.update(_pose_with_dual_arm_angles(170, 70, left_conf=0.95, right_conf=0.75))

    assert fsm.state == "STAND"
    assert not fsm._rep_in_progress
    assert fsm._total_reps_count == 0


def test_curl_same_side_continuous_motion_can_enter_after_noise_guard():
    DumbbellCurlFSM, fake_time = _load_curl_fsm()
    fsm = DumbbellCurlFSM()
    fsm.sync_to_frontend = lambda current_angle=180.0, nn_result=None: None

    for angle in [160, 150, 138, 124, 108, 90, 80]:
        fake_time.advance(0.12)
        fsm.update(_pose_with_dual_arm_angles(angle, 170, left_conf=0.95, right_conf=0.55))

    assert fsm.state == "CURLING"
    assert fsm._rep_in_progress
    assert fsm._active_side == "left"


def test_curl_selects_lower_elbow_angle_over_higher_confidence_other_arm():
    DumbbellCurlFSM, fake_time = _load_curl_fsm()
    fsm = DumbbellCurlFSM()
    fsm.sync_to_frontend = lambda current_angle=180.0, nn_result=None: None

    fake_time.advance(0.12)
    angle = fsm.update(
        _pose_with_dual_arm_angles(
            left_angle=155,
            right_angle=45,
            left_conf=0.95,
            right_conf=0.55,
        )
    )

    assert angle < 60.0


def test_curl_angle_debug_contains_side_diagnostics():
    calls = []

    def _capture_debug(*args, **kwargs):
        calls.append((args, kwargs))

    src = _read_main_loop()
    start = src.find("class DumbbellCurlFSM:")
    end = src.find("\n\nasync def _deepseek", start)
    fake_time = _FakeTime()
    ns = {
        "json": json,
        "logging": logging,
        "math": math,
        "os": os,
        "time": fake_time,
        "_GRU_MODEL": None,
        "_pose_fps": lambda pose_data: 0.0,
        "_write_angle_debug": _capture_debug,
        "_round_point": lambda pt: [round(float(pt[0]), 1), round(float(pt[1]), 1)],
    }
    ns["_safe_float"] = lambda value, default=0.0: float(value) if value is not None else float(default)
    ns["_build_rep_event"] = lambda exercise, rep_index, angle_metric, min_angle, rom, visual_result, finalize_reason, started_ts, ended_ts: {
        "exercise": exercise,
        "rep_index": int(rep_index or 0),
        "angle_metric": angle_metric,
        "min_angle": float(min_angle),
        "rom": float(rom),
        "visual_result": visual_result,
        "final_result": visual_result,
        "finalize_reason": finalize_reason,
        "started_ts": float(started_ts),
        "ended_ts": float(ended_ts),
    }
    exec(compile(src[start:end], "<curl_fsm>", "exec"), ns)
    fsm = ns["DumbbellCurlFSM"]()
    fsm.sync_to_frontend = lambda current_angle=180.0, nn_result=None: None

    fake_time.advance(0.12)
    fsm.update(_pose_with_dual_arm_angles(155, 45, left_conf=0.95, right_conf=0.55))

    assert calls
    extra = calls[-1][0][-1]
    assert extra["selected_side"] == "right"
    assert extra["selection_reason"] == "lower_elbow_angle_right"
    assert extra["left_angle"] > 140.0
    assert extra["right_angle"] < 60.0
    assert extra["right_elbow"]
    assert extra["right_wrist"]
