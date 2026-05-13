# -*- coding: utf-8 -*-

from hardware_engine.cognitive import coach_answer


def _rag_result():
    return {
        "ok": True,
        "source_mode": "vector",
        "hits": [
            {
                "id": "pubmed:35872038",
                "evidence_id": "pubmed:35872038",
                "title": "Resistance training and knee pain guidance",
                "source": "PubMed",
                "venue": "Sports Medicine",
                "year": 2022,
                "url": "https://pubmed.ncbi.nlm.nih.gov/35872038/",
                "doi": "10.1000/knee-safe",
                "pmid": "35872038",
                "abstract_or_snippet": "Pain during exercise should be monitored conservatively.",
            }
        ],
        "context": "向量 RAG 证据命中",
    }


def test_knee_pain_answer_has_required_user_sections():
    answer = coach_answer.build_coach_answer(
        "深蹲时膝盖疼还能继续练吗？",
        rag_result=_rag_result(),
    )

    assert answer["ok"] is True
    assert answer["answer_type"] == "risk"
    for key in ("conclusion", "recommendation", "reason", "risk", "next_step"):
        assert key in answer
        assert answer[key]
        assert key in answer["user_text"]


def test_sources_keep_metadata_out_of_user_text_opening():
    answer = coach_answer.build_coach_answer(
        "膝盖酸痛怎么办？",
        rag_result=_rag_result(),
    )

    opening = answer["user_text"][:80]
    assert "10.1000/knee-safe" not in opening
    assert "35872038" not in opening
    assert "pubmed:35872038" not in opening
    assert answer["sources"][0]["doi"] == "10.1000/knee-safe"
    assert answer["sources"][0]["pmid"] == "35872038"
    assert answer["debug"]["evidence_ids"] == ["pubmed:35872038"]


def test_training_query_produces_actionable_answer():
    answer = coach_answer.build_coach_answer(
        "今天深蹲训练计划怎么安排？",
        training_context={"exercise_label": "深蹲", "sets": [{"target_reps": 8}]},
    )

    assert answer["ok"] is True
    assert answer["answer_type"] == "plan"
    assert "先" in answer["recommendation"] or "做" in answer["recommendation"]
    assert "下一步" in answer["user_text"]


def test_pain_query_triggers_conservative_risk_reminder():
    answer = coach_answer.build_coach_answer("弯举时肘部刺痛，还要加重量吗？")

    assert answer["answer_type"] == "risk"
    assert "停止" in answer["risk"] or "暂停" in answer["risk"]
    assert "医生" in answer["risk"] or "专业" in answer["risk"]
