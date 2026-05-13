import json

from hardware_engine import training_plan
from hardware_engine import training_report


def _sample_report():
    plan = training_plan.create_default_plan(exercise="bicep_curl", now=1.0)
    session = {
        "exercise": "bicep_curl",
        "duration_s": 480,
        "sets": [
            {"set_index": 1, "good": 8, "failed": 0, "comp": 0, "fatigue": 90},
            {"set_index": 2, "good": 7, "failed": 1, "comp": 0, "fatigue": 130},
            {"set_index": 3, "good": 6, "failed": 1, "comp": 1, "fatigue": 190},
        ],
    }
    return training_report.build_training_report(
        fsm_state={},
        session_state=session,
        plan_state=plan,
        now=2.0,
    )


def test_feishu_session_report_message_is_interactive_card_payload():
    report = _sample_report()
    message = training_report.build_feishu_session_report_message(report)
    assert message["msg_type"] == "interactive"
    assert message["card"]["config"]["wide_screen_mode"] is True
    assert message["card"]["header"]["title"]["content"].startswith("IronBuddy训练报告")
    assert json.dumps(message, ensure_ascii=False)


def test_feishu_session_report_card_contains_required_training_sections():
    report = _sample_report()
    card = training_report.build_feishu_session_report_card(report)
    text = json.dumps(card, ensure_ascii=False)
    assert "训练概览" in text
    assert "每组情况" in text
    assert "恢复建议" in text
    assert "总reps" in text
    assert "合格率" in text
    assert "疲劳趋势" in text
    assert "估算卡路里" in text
    assert "主要肌群" in text
    assert "明日建议" in text
    assert "估算" in text
    assert "MET 3.5" in text
    assert "70.0kg" in text
    assert "肱二头肌" in text


def test_feishu_markdown_matches_structured_report_totals():
    report = _sample_report()
    markdown = training_report.format_feishu_session_report_markdown(report)
    assert "**总reps**：24" in markdown
    assert "**合格率**：87.5%" in markdown
    assert "第1组" in markdown
    assert "第2组" in markdown
    assert "第3组" in markdown


def test_session_report_keeps_completed_set_after_next_set_reset():
    plan = training_plan.create_default_plan(exercise="squat", now=1.0)
    plan["current_set"] = 2
    session = {
        "exercise": "squat",
        "plan_active": True,
        "current_set": 2,
        "sets": [
            {"set_index": 1, "good": 5, "failed": 1, "comp": 0, "fatigue": 130},
        ],
    }
    fsm_after_next_set = {
        "exercise": "squat",
        "current_set": 2,
        "good": 0,
        "failed": 0,
        "comp": 0,
        "total_reps": 0,
    }
    report = training_report.build_training_report(
        fsm_state=fsm_after_next_set,
        session_state=session,
        plan_state=plan,
        now=3.0,
    )
    card_text = json.dumps(training_report.build_feishu_session_report_card(report), ensure_ascii=False)
    assert report["total_reps"] == 6
    assert report["sets"][0]["total_reps"] == 6
    assert report["sets"][1]["total_reps"] == 0
    assert "总reps：6" in card_text
