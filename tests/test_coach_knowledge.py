"""Smoke tests for the local coach knowledge fast path."""

from hardware_engine.cognitive import coach_knowledge


def test_capability_questions_hit_fixed_reply():
    assert coach_knowledge.is_capability_question("介绍一下你的功能")
    body = coach_knowledge.format_capability_reply()
    assert "视觉" in body
    assert "传感" in body
    assert "飞书" in body
    assert "拍摄" not in body
    assert "演示" not in body


def test_manual_reply_is_local_and_short():
    body = coach_knowledge.format_manual_reply("怎么用")
    assert isinstance(body, str)
    assert len(body) < 500
