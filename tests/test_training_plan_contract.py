import json
import os

from hardware_engine import training_plan
from hardware_engine import training_report


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREAMER = os.path.join(PROJECT_ROOT, "streamer_app.py")
MAIN_LOOP = os.path.join(PROJECT_ROOT, "hardware_engine", "main_claw_loop.py")


def _read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _extract_function_source(src, name):
    marker = "def " + name + "("
    start = src.find(marker)
    assert start >= 0, name + " not found"
    end = len(src)
    for off, line in enumerate(src[start:].splitlines(keepends=True)):
        if off == 0:
            continue
        if line and (line.startswith("def ") or line.startswith("class ")
                     or line.startswith("@app.route(")):
            end = start + sum(len(l) for l in src[start:].splitlines(keepends=True)[:off])
            break
    return src[start:end]


def _load_daily_plan_helpers():
    src = _read(STREAMER)
    snippet = (
        "import json\nimport re\nimport time\n"
        "PLAN_MIN_FATIGUE_TARGET = 300\n"
        "PLAN_MAX_FATIGUE_TARGET = 1500\n"
        "PLAN_DEFAULT_FATIGUE_TARGET = 600\n"
        "def _exercise_label_for_ui(exercise):\n    return '深蹲'\n\n"
        + _extract_function_source(src, "_extract_json_object")
        + "\n"
        + _extract_function_source(src, "_normalize_deepseek_daily_plan")
    )
    mod = type(os)("_daily_plan_helpers")
    exec(compile(snippet, "<daily_plan_helpers>", "exec"), mod.__dict__)
    return mod


def test_default_plan_is_current_exercise_three_sets_of_eight():
    plan = training_plan.create_default_plan(exercise="bicep_curl", now=123.0)
    assert plan["exercise"] == "bicep_curl"
    assert plan["exercise_label"] == "哑铃弯举"
    assert plan["current_set"] == 1
    assert [s["target_reps"] for s in plan["sets"]] == [8, 8, 8]
    assert [s["target_fatigue"] for s in plan["sets"]] == [600, 700, 800]
    assert plan["weight_kg"] == 70.0


def test_plan_set_targets_are_editable_and_normalized():
    plan = training_plan.create_default_plan(exercise="squat", now=1.0)
    edited = training_plan.set_set_target(plan, 2, 10, now=2.0)
    edited = training_plan.set_current_set(edited, 2, now=3.0)
    assert [s["target_reps"] for s in edited["sets"]] == [8, 10, 8]
    assert edited["current_set"] == 2
    assert edited["updated_ts"] == 3.0


def test_runtime_plan_json_roundtrip_is_atomic(tmp_path):
    path = os.path.join(str(tmp_path), "training_plan.json")
    plan = training_plan.create_default_plan(exercise="curl", now=10.0)
    written = training_plan.write_plan_state(plan, path=path)
    loaded = training_plan.read_plan_state(path=path)
    assert written["exercise"] == "bicep_curl"
    assert loaded["sets"][0]["target_reps"] == 8
    assert json.loads(open(path, "r", encoding="utf-8").read())["schema_version"] == 1
    assert not os.path.exists(path + ".tmp.%s" % os.getpid())


def test_update_plan_state_supports_bulk_edits(tmp_path):
    path = os.path.join(str(tmp_path), "plan.json")
    plan = training_plan.update_plan_state(
        path=path,
        exercise="squat",
        set_count=4,
        reps_per_set=6,
        current_set=3,
        set_targets={3: 12},
        weight_kg=72.5,
        now=22.0,
    )
    assert len(plan["sets"]) == 4
    assert [s["target_reps"] for s in plan["sets"]] == [6, 6, 12, 6]
    assert plan["current_set"] == 3
    assert plan["weight_kg"] == 72.5
    assert training_plan.read_plan_state(path=path)["sets"][2]["target_reps"] == 12


def test_training_plan_fatigue_targets_use_lane_e_dose_scale(tmp_path):
    path = os.path.join(str(tmp_path), "plan.json")
    plan = training_plan.update_plan_state(
        path=path,
        exercise="squat",
        set_count=3,
        reps_per_set=8,
        fatigue_targets={1: 450, 2: 600, 3: 2000},
        now=11.0,
    )
    assert training_plan.DEFAULT_FATIGUE_TARGET == 600
    assert training_plan.MAX_FATIGUE_TARGET == 1500
    assert [s["target_fatigue"] for s in plan["sets"]] == [450, 600, 1500]


def test_training_report_aggregates_plan_session_and_fsm_state():
    plan = training_plan.create_default_plan(exercise="squat", now=1.0)
    session = {
        "exercise": "squat",
        "duration_s": 600,
        "sets": [
            {"set_index": 1, "good": 8, "failed": 0, "comp": 0, "fatigue": 120},
            {"set_index": 2, "good": 6, "failed": 1, "comp": 1, "fatigue": 180},
        ],
    }
    fsm = {"exercise": "squat", "current_set": 3, "good": 5, "failed": 2, "comp": 0, "fatigue": 240}
    report = training_report.build_training_report(fsm, session, plan, now=99.0)
    assert report["exercise"] == "squat"
    assert len(report["sets"]) == 3
    assert report["sets"][2]["set_index"] == 3
    assert report["total_reps"] == 23
    assert report["total_good"] == 19
    assert report["qualified_rate_pct"] == 82.6
    assert report["fatigue_trend"]["label"] == "上升"
    assert report["calorie_estimate"]["is_estimate"] is True
    assert report["calorie_estimate"]["met"] == 5.0
    assert "股四头肌" in report["main_muscle_groups"]
    assert "明天" in report["tomorrow_advice"]


def test_training_report_default_curl_met_and_text_fields():
    plan = training_plan.create_default_plan(exercise="curl", now=1.0)
    fsm = {"exercise": "bicep_curl", "good": 7, "failed": 1, "comp": 0, "fatigue": 80}
    report = training_report.build_training_report(fsm_state=fsm, plan_state=plan, now=2.0)
    assert report["calorie_estimate"]["weight_kg"] == 70.0
    assert report["calorie_estimate"]["met"] == 3.5
    assert "估算" in report["text"]
    assert "每组情况" in report["text"]
    assert "总reps" in report["text"]
    assert "合格率" in report["text"]
    assert "疲劳趋势" in report["text"]
    assert "主要肌群" in report["text"]
    assert "明日建议" in report["text"]


def test_training_report_active_plan_exercise_wins_over_stale_fsm_exercise():
    plan = training_plan.create_default_plan(exercise="bicep_curl", now=1.0)
    session = {"exercise": "bicep_curl", "plan_active": True}
    stale_fsm = {"exercise": "squat", "current_set": 1, "good": 0, "failed": 0, "comp": 0}
    report = training_report.build_training_report(
        fsm_state=stale_fsm,
        session_state=session,
        plan_state=plan,
        now=2.0,
    )
    assert report["exercise"] == "bicep_curl"
    assert report["exercise_label"] == "哑铃弯举"
    assert report["calorie_estimate"]["met"] == 3.5
    assert "肱二头肌" in report["main_muscle_groups"]


def test_streamer_next_set_request_carries_completed_and_next_set():
    src = _read(STREAMER)
    body = src[src.find("def api_training_plan_next_set"):src.find("\n\n@app.route('/api/training_report'", src.find("def api_training_plan_next_set"))]
    assert '"completed_set": current' in body
    assert '"next_set": next_set' in body
    assert '"src": "training_plan_api"' in body
    assert "TRAINING_SESSION_PATH" in src
    assert "@app.route('/api/training_session/evidence')" in src


def test_daily_plan_deepseek_contract_is_bounded_and_falls_back():
    src = _read(STREAMER)
    assert "def _call_deepseek_daily_plan" in src
    assert "https://api.deepseek.com/v1/chat/completions" in src
    assert '"model": "deepseek-chat"' in src
    assert "timeout_s=8.0" in src
    assert "def _normalize_deepseek_daily_plan" in src
    normalizer = src[src.find("def _normalize_deepseek_daily_plan"):src.find("def _call_deepseek_daily_plan")]
    assert 'exercise = "squat"' in normalizer
    assert "set_count != 3" in normalizer
    assert "fatigue_targets" in normalizer
    assert "evidence_ids" in normalizer
    assert "v < PLAN_MIN_FATIGUE_TARGET or v > PLAN_MAX_FATIGUE_TARGET" in normalizer
    assert "300 到 5000" not in src
    assert "PLAN_DEFAULT_FATIGUE_TARGET" in src
    for label in ("恢复", "稳态", "进阶"):
        assert label in normalizer
    daily_api = src[src.find("def api_daily_training_plan"):src.find("def api_daily_training_plan_accept")]
    assert "_call_deepseek_daily_plan" in daily_api
    assert 'plan["source"] = "rule_fallback"' in daily_api
    assert 'plan["fallback_reason"]' in daily_api
    caller = src[src.find("def _call_deepseek_daily_plan"):src.find("def _read_daily_plan_state")]
    assert "_search_lane_a_professional_knowledge(query, limit=3)" in caller
    assert "search_vector_knowledge(query, limit=3)" not in caller


def test_daily_plan_deepseek_json_normalizer_accepts_bounded_plan():
    mod = _load_daily_plan_helpers()
    raw = mod._extract_json_object('```json\n{"exercise":"squat","set_count":3,"fatigue_targets":[600,700,800],"estimated_rep_range":[6,10],"evidence_ids":["pubmed:12345"],"intensity":"稳态","summary":"今日深蹲","reason":"基于在线证据 pubmed:12345 和历史疲劳","coach_line":"开始训练"}\n```')
    plan, reason = mod._normalize_deepseek_daily_plan(raw, {
        "recent_total_reps": 12,
        "rag_evidence": {"hits": [{"id": "pubmed:12345"}]},
    })
    assert reason == ""
    assert plan["source"] == "deepseek"
    assert plan["exercise"] == "squat"
    assert plan["target_type"] == "fatigue"
    assert plan["fatigue_targets"] == [600, 700, 800]
    assert plan["set_targets"] == [600, 700, 800]
    assert plan["estimated_rep_range"] == [6, 10]
    assert plan["evidence_ids"] == ["pubmed:12345"]
    assert plan["intensity"] == "稳态"


def test_daily_plan_deepseek_json_normalizer_accepts_only_returned_vector_ids():
    mod = _load_daily_plan_helpers()
    raw = {
        "exercise": "squat",
        "set_count": 3,
        "fatigue_targets": [500, 600, 700],
        "estimated_rep_range": [5, 9],
        "evidence_ids": ["vector:emg-1"],
        "intensity": "稳态",
        "summary": "今日深蹲",
        "reason": "基于向量 RAG 证据 vector:emg-1",
    }
    plan, reason = mod._normalize_deepseek_daily_plan(
        raw,
        {"rag_evidence": {"source_mode": "vector", "hits": [{"id": "vector:emg-1"}]}},
    )
    assert reason == ""
    assert plan["evidence_ids"] == ["vector:emg-1"]

    raw["evidence_ids"] = ["vector:missing"]
    plan, reason = mod._normalize_deepseek_daily_plan(
        raw,
        {"rag_evidence": {"source_mode": "vector", "hits": [{"id": "vector:emg-1"}]}},
    )
    assert plan is None
    assert reason == "missing_online_evidence"


def test_daily_plan_deepseek_json_normalizer_rejects_unsafe_plan():
    mod = _load_daily_plan_helpers()
    too_many = {
        "exercise": "squat",
        "set_count": 3,
        "fatigue_targets": [2000, 2000, 2000],
        "evidence_ids": ["pubmed:12345"],
        "intensity": "进阶",
    }
    plan, reason = mod._normalize_deepseek_daily_plan(
        too_many,
        {"rag_evidence": {"hits": [{"id": "pubmed:12345"}]}},
    )
    assert plan is None
    assert reason == "fatigue_target_out_of_range"

    wrong_exercise = dict(too_many)
    wrong_exercise["exercise"] = "bicep_curl"
    wrong_exercise["fatigue_targets"] = [600, 700, 800]
    plan, reason = mod._normalize_deepseek_daily_plan(wrong_exercise, {})
    assert plan is None
    assert reason == "exercise_not_allowed"

    no_evidence = dict(wrong_exercise)
    no_evidence["exercise"] = "squat"
    no_evidence["evidence_ids"] = []
    plan, reason = mod._normalize_deepseek_daily_plan(no_evidence, {"rag_evidence": {"hits": []}})
    assert plan is None
    assert reason == "missing_online_evidence"

    fake_evidence = dict(no_evidence)
    fake_evidence["evidence_ids"] = ["madeup:1"]
    plan, reason = mod._normalize_deepseek_daily_plan(fake_evidence, {"rag_evidence": {"hits": []}})
    assert plan is None
    assert reason == "missing_online_evidence"

    wrong_id = dict(no_evidence)
    wrong_id["evidence_ids"] = ["madeup:1"]
    plan, reason = mod._normalize_deepseek_daily_plan(
        wrong_id,
        {"rag_evidence": {"hits": [{"id": "pubmed:12345"}]}},
    )
    assert plan is None
    assert reason == "missing_online_evidence"


def test_main_loop_records_completed_set_before_next_set_reset():
    src = _read(MAIN_LOOP)
    assert "TRAINING_SESSION_FILE = \"/dev/shm/ironbuddy_training_session.json\"" in src
    assert "TRAINING_PLAN_FILE = \"/dev/shm/ironbuddy_training_plan.json\"" in src
    assert "def _record_completed_training_set" in src
    assert "def _request_auto_next_training_set" in src
    assert '"src": "fatigue_auto_next_set"' in src
    block = src[src.find("if os.path.exists(\"/dev/shm/next_set.request\")"):src.find("# 前端重置信号")]
    assert "_record_completed_training_set(fsm, _next_req, _next_set)" in block
    assert "fsm = SquatStateMachine()" in block


def test_main_loop_passes_integral_dose_features_without_curl_fixed_increment():
    src = _read(MAIN_LOOP)
    helper = src[src.find("def _fatigue_features_from_rep"):src.find("\n\ndef _apply_fatigue_model_to_fsm")]
    for field in ("dt_s", "integration_mode", "target_mvc", "comp_mvc", "phase", "target_fatigue"):
        assert '"' + field + '"' in helper
    assert "def _read_muscle_activation_meta" in src
    assert "from hardware_engine.fatigue_model import compute_fatigue, append_feature_snapshot" in src
    assert "from fatigue_model import compute_fatigue, append_feature_snapshot" in src
    assert "def _read_emg_debug_meta" in src
    assert "def _read_emg_stream_meta" in src
    assert '"/dev/shm/emg_debug_snapshot.json"' in src
    assert '"/dev/shm/emg_stream_buffer.json"' in src
    assert '"real_udp_debug"' in src
    assert '"real_udp_stream"' in src
    assert 'bool(data.get("simulated") or data.get("sensor_simulated"))' in src
    assert "data.update(fallback)" in src
    assert 'emg_meta=_read_muscle_activation_meta("squat")' in src
    assert 'emg_meta=_read_muscle_activation_meta("bicep_curl")' in src
    curl_body = src[src.find("def _finalize_curl_rep"):src.find("        # vision_sensor 模式下", src.find("def _finalize_curl_rep"))]
    assert "1500.0 / 7.0" not in curl_body
    assert "self.total_fatigue_volume += volume" not in curl_body


def test_main_loop_default_runtime_fatigue_limit_matches_lane_e_plan_scale():
    src = _read(MAIN_LOOP)
    assert "DEFAULT_RUNTIME_FATIGUE_LIMIT = 600" in src
    assert "_fatigue_limit = [DEFAULT_RUNTIME_FATIGUE_LIMIT]" in src
    assert 'fl_data.get("limit", DEFAULT_RUNTIME_FATIGUE_LIMIT)' in src


def test_udp_emg_exports_formal_dose_fields_without_removing_activations():
    src = _read(os.path.join(PROJECT_ROOT, "hardware_engine", "sensor", "udp_emg_server.py"))
    out_start = src.find("out = {")
    out_block = src[out_start:src.find("debug_out = dict(RAW_DEBUG)", out_start)]
    assert '"activations": acts' in out_block
    for field in (
        '"target_rms"',
        '"comp_rms"',
        '"target_mvc"',
        '"comp_mvc"',
        '"target_pct"',
        '"comp_pct"',
        '"signal_mode"',
        '"valid"',
    ):
        assert field in out_block


def test_auto_rag_bridge_filters_control_and_time_questions():
    src = _read(STREAMER)
    assert 'os.environ.get("IRONBUDDY_RAG_AUTO_SEND", "1")' in src
    assert "if dry_run and _rag_auto_send_enabled()" in src
    assert "已自动发送飞书详报" in src
    assert "飞书详报草稿已生成，尚未发送" not in src
    assert "仅生成详报草稿" not in src
    assert "def _is_auto_rag_candidate" in src
    helper = src[src.find("def _is_auto_rag_candidate"):src.find("\n\ndef _prepare_and_maybe_send_rag_delivery")]
    for phrase in ("静音", "下一组", "上限", "现在几点", "当前时间"):
        assert phrase in helper
    assert "knowledge_hints" not in helper
    assert "len(compact) < 4" in helper
    bridge = src[src.find("def _maybe_rag_delivery_from_latest_chat"):src.find("\n\n@app.route('/api/rag/delivery'")]
    assert "not_knowledge_question" in bridge
    manual = src[src.find("def api_rag_delivery"):src.find("\n\ndef _read_voice_turn", src.find("def api_rag_delivery"))]
    assert "not _is_auto_rag_candidate(query)" in manual
    assert "not_knowledge_question" in manual


def test_user_visible_rag_routes_are_adp_first_without_vector_default():
    src = _read(STREAMER)
    assert "def _search_lane_a_professional_knowledge" in src
    assert 'IRONBUDDY_ENABLE_VECTOR_FALLBACK", "0"' in src
    coach = src[src.find("def coach_rag_query"):src.find("\n\ndef _rag_auto_send_enabled")]
    demo = src[src.find("def demo_rag_status"):src.find("\n\n@app.route('/api/demo/opencloud_records'")]
    for body in (coach, demo):
        assert "_search_lane_a_professional_knowledge(query, limit=limit)" in body
        assert "search_vector_knowledge(query, limit=limit)" not in body
        assert '"source_mode": online.get("source_mode") or "adp"' in body


def test_auto_rag_notice_emits_answer_before_hit_status():
    src = _read(STREAMER)
    body = src[src.find("def _prepare_and_maybe_send_rag_delivery"):src.find("\n\ndef _latest_user_chat_event")]
    assert 'kind="rag_answer"' in body
    assert 'kind="rag_hit_notice"' in body
    assert body.find('kind="rag_answer"') < body.find('kind="rag_hit_notice"')
    assert 'title = "ADP" if result.get("source_mode") == "adp"' in body
