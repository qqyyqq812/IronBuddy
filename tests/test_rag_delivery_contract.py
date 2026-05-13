import json

import pytest

from hardware_engine import rag_delivery


@pytest.fixture(autouse=True)
def _disable_adp_by_default(monkeypatch):
    monkeypatch.setattr(rag_delivery, "adp_knowledge", None)


def _stub_online_result(query, limit=3, **kwargs):
    hits = [
        {
            "id": "pubmed:12345",
            "title": "Resistance training knee pain guidance",
            "source": "PubMed",
            "year": 2022,
            "url": "https://pubmed.ncbi.nlm.nih.gov/12345/",
            "doi": "10.1000/knee",
            "pmid": "12345",
            "abstract_or_snippet": "Evidence about knee pain and resistance training.",
            "retrieved_at": 1000.0,
        }
    ][:limit]
    return {
        "ok": True,
        "source_mode": "online",
        "query": query,
        "hits": hits,
        "context": "在线专业知识库命中：\n- [PubMed] Resistance training knee pain guidance",
        "errors": [],
    }


def _stub_adp_result(query, limit=3, **kwargs):
    hits = [
        {
            "id": "adp:answer:12345",
            "title": "ADP 教练回答",
            "source": "Tencent ADP Knowledge App",
            "abstract_or_snippet": "先给出可执行训练建议，再说明风险边界。",
            "retrieved_at": 1000.0,
        }
    ][:limit]
    return {
        "ok": True,
        "source_mode": "adp",
        "query": query,
        "hits": hits,
        "context": "ADP 专业教练知识命中：先给出可执行训练建议。",
        "errors": [],
    }


class AdpStub(object):
    @staticmethod
    def search_adp_knowledge(query, limit=3):
        return _stub_adp_result(query, limit=limit)

    @staticmethod
    def build_adp_context(query, hits, max_chars=480):
        return "ADP 专业教练知识命中"


def test_rag_hit_payload_contains_last_hit_and_feishu_card_input(monkeypatch, tmp_path):
    monkeypatch.setattr(rag_delivery, "adp_knowledge", AdpStub)
    monkeypatch.setattr(rag_delivery, "vector_knowledge", None)
    monkeypatch.setattr(rag_delivery.online_knowledge, "search_online_knowledge", _stub_online_result)

    runtime_path = tmp_path / "rag_delivery.json"
    result = rag_delivery.prepare_rag_delivery(
        "膝盖酸痛怎么办",
        turn_id="turn-1",
        runtime_path=str(runtime_path),
        now=1000.0,
    )

    assert result["ok"] is True
    assert result["should_deliver"] is True
    assert result["reason"] == "rag_hit"
    assert result["last_hit"]["query"] == "膝盖酸痛怎么办"
    assert result["last_hit"]["source_mode"] == "adp"
    assert result["last_hit"]["top_hit"]["id"] == "adp:answer:12345"
    assert result["last_hit"]["top_hit"]["source"] == "Tencent ADP Knowledge App"
    assert result["last_hit"]["hit_count"] == 1
    assert result["feishu"]["msg_type"] == "interactive"
    assert result["feishu"]["card_input"]["type"] == "rag_detail"
    assert "ADP" in result["feishu"]["body_text"]
    assert "可执行训练建议" in result["feishu"]["body_text"]
    assert result["feishu"]["card"]["header"]["title"]["content"].startswith("IronBuddy 在线知识库命中")

    saved = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert saved["last_hit"]["query_key"] == result["query_key"]
    assert saved["queries"][result["query_key"]]["last_turn_id"] == "turn-1"


def test_same_query_and_turn_is_deduped_without_refreshing_last_hit(monkeypatch, tmp_path):
    monkeypatch.setattr(rag_delivery, "adp_knowledge", AdpStub)
    monkeypatch.setattr(rag_delivery, "vector_knowledge", None)
    monkeypatch.setattr(rag_delivery.online_knowledge, "search_online_knowledge", _stub_online_result)
    runtime_path = tmp_path / "rag_delivery.json"

    first = rag_delivery.prepare_rag_delivery(
        "怎么推送训练总结到飞书",
        turn_id="turn-1",
        runtime_path=str(runtime_path),
        now=1000.0,
    )
    second = rag_delivery.prepare_rag_delivery(
        "怎么推送训练总结到飞书",
        turn_id="turn-1",
        runtime_path=str(runtime_path),
        now=1005.0,
    )

    assert first["should_deliver"] is True
    assert second["should_deliver"] is False
    assert second["reason"] == "duplicate_query_turn"
    saved = json.loads(runtime_path.read_text(encoding="utf-8"))
    assert saved["last_hit"]["ts"] == 1000.0


def test_same_query_in_new_turn_obeys_sixty_second_cooldown(monkeypatch, tmp_path):
    monkeypatch.setattr(rag_delivery, "adp_knowledge", AdpStub)
    monkeypatch.setattr(rag_delivery, "vector_knowledge", None)
    monkeypatch.setattr(rag_delivery.online_knowledge, "search_online_knowledge", _stub_online_result)
    runtime_path = tmp_path / "rag_delivery.json"

    rag_delivery.prepare_rag_delivery(
        "怎么推送训练总结到飞书",
        turn_id="turn-1",
        runtime_path=str(runtime_path),
        now=1000.0,
    )
    cooled = rag_delivery.prepare_rag_delivery(
        "怎么推送训练总结到飞书",
        turn_id="turn-2",
        runtime_path=str(runtime_path),
        now=1030.0,
    )
    after = rag_delivery.prepare_rag_delivery(
        "怎么推送训练总结到飞书",
        turn_id="turn-3",
        runtime_path=str(runtime_path),
        now=1061.0,
    )

    assert cooled["should_deliver"] is False
    assert cooled["reason"] == "cooldown"
    assert cooled["cooldown_remaining_s"] == 30
    assert after["should_deliver"] is True
    assert after["reason"] == "rag_hit"


def test_zero_cooldown_allows_explicit_manual_resend(monkeypatch, tmp_path):
    monkeypatch.setattr(rag_delivery, "adp_knowledge", AdpStub)
    monkeypatch.setattr(rag_delivery, "vector_knowledge", None)
    monkeypatch.setattr(rag_delivery.online_knowledge, "search_online_knowledge", _stub_online_result)
    runtime_path = tmp_path / "rag_delivery.json"

    first = rag_delivery.prepare_rag_delivery(
        "怎么推送训练总结到飞书",
        turn_id="turn-1",
        runtime_path=str(runtime_path),
        now=1000.0,
    )
    manual = rag_delivery.prepare_rag_delivery(
        "怎么推送训练总结到飞书",
        turn_id="manual-send",
        runtime_path=str(runtime_path),
        now=1001.0,
        cooldown_s=0,
    )

    assert first["should_deliver"] is True
    assert manual["should_deliver"] is True
    assert manual["reason"] == "rag_hit"


def test_empty_or_no_hit_queries_do_not_write_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(rag_delivery, "vector_knowledge", None)
    class EmptyAdpStub(object):
        @staticmethod
        def search_adp_knowledge(query, limit=3):
            return {
                "ok": True,
                "source_mode": "adp",
                "query": query,
                "hits": [],
                "context": "",
            }
    monkeypatch.setattr(rag_delivery, "adp_knowledge", EmptyAdpStub)
    runtime_path = tmp_path / "rag_delivery.json"

    empty = rag_delivery.prepare_rag_delivery("", runtime_path=str(runtime_path), now=1000.0)
    no_hit = rag_delivery.prepare_rag_delivery("完全不相关", runtime_path=str(runtime_path), now=1001.0)

    assert empty["ok"] is False
    assert empty["reason"] == "empty_query"
    assert no_hit["ok"] is True
    assert no_hit["should_deliver"] is False
    assert no_hit["reason"] == "no_hit"
    assert not runtime_path.exists()


def test_adp_unavailable_is_reported_without_local_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(rag_delivery, "vector_knowledge", None)
    class DownAdpStub(object):
        @staticmethod
        def search_adp_knowledge(query, limit=3):
            return {
                "ok": False,
                "source_mode": "adp",
                "query": query,
                "hits": [],
                "context": "",
                "reason": "adp_unavailable",
                "errors": [{"provider": "adp", "error": "timeout"}],
            }
    monkeypatch.setattr(rag_delivery, "adp_knowledge", DownAdpStub)

    result = rag_delivery.prepare_rag_delivery(
        "肌电疲劳怎么判断",
        runtime_path=str(tmp_path / "rag_delivery.json"),
        now=1000.0,
    )

    assert result["ok"] is False
    assert result["should_deliver"] is False
    assert result["reason"] == "adp_unavailable"
    assert result["source_mode"] == "adp"


def test_vector_delivery_path_is_disabled_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(rag_delivery, "adp_knowledge", None)
    monkeypatch.delenv("IRONBUDDY_ENABLE_VECTOR_FALLBACK", raising=False)

    class VectorStub(object):
        @staticmethod
        def search_vector_knowledge(query, limit=3):
            raise AssertionError("vector fallback should be disabled by default")

    monkeypatch.setattr(rag_delivery, "vector_knowledge", VectorStub)
    result = rag_delivery.prepare_rag_delivery(
        "肌电疲劳怎么判断",
        runtime_path=str(tmp_path / "rag_delivery.json"),
        now=1000.0,
    )

    assert result["ok"] is False
    assert result["should_deliver"] is False
    assert result["source_mode"] == "adp"
    assert result["reason"] == "adp_provider_unavailable"


def test_vector_delivery_path_preserves_vector_source_mode_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setattr(rag_delivery, "adp_knowledge", None)
    monkeypatch.setenv("IRONBUDDY_ENABLE_VECTOR_FALLBACK", "1")

    class VectorStub(object):
        @staticmethod
        def search_vector_knowledge(query, limit=3):
            return {
                "ok": True,
                "source_mode": "vector",
                "query": query,
                "hits": [
                    {
                        "id": "vector:emg-1",
                        "title": "BGE-M3 indexed EMG fatigue evidence",
                        "source": "PubMed",
                        "abstract_or_snippet": "Vector hit.",
                    }
                ],
                "context": "向量 RAG 证据命中",
                "errors": [],
            }

        @staticmethod
        def build_vector_context(query, hits, max_chars=480):
            return "向量 RAG 证据命中"

    monkeypatch.setattr(rag_delivery, "vector_knowledge", VectorStub)
    result = rag_delivery.prepare_rag_delivery(
        "肌电疲劳怎么判断",
        runtime_path=str(tmp_path / "rag_delivery.json"),
        now=1000.0,
    )

    assert result["ok"] is True
    assert result["source_mode"] == "vector"
    assert result["last_hit"]["source_mode"] == "vector"
    assert result["last_hit"]["top_hit"]["id"] == "vector:emg-1"


def test_adp_delivery_path_takes_priority(monkeypatch, tmp_path):
    class AdpStub(object):
        @staticmethod
        def search_adp_knowledge(query, limit=3):
            return {
                "ok": True,
                "source_mode": "adp",
                "query": query,
                "hits": [
                    {
                        "id": "adp:answer:1",
                        "title": "ADP 教练回答",
                        "source": "Tencent ADP Knowledge App",
                        "abstract_or_snippet": "先降低强度并检查膝盖方向。",
                    }
                ],
                "context": "ADP 专业教练知识命中",
                "errors": [],
            }

        @staticmethod
        def build_adp_context(query, hits, max_chars=480):
            return "ADP 专业教练知识命中"

    monkeypatch.setattr(rag_delivery, "adp_knowledge", AdpStub)
    monkeypatch.setenv("IRONBUDDY_ENABLE_VECTOR_FALLBACK", "1")

    class VectorStub(object):
        @staticmethod
        def search_vector_knowledge(query, limit=3):
            raise AssertionError("ADP hit must not call vector fallback")

    monkeypatch.setattr(rag_delivery, "vector_knowledge", VectorStub)
    result = rag_delivery.prepare_rag_delivery(
        "深蹲膝盖不舒服怎么办",
        runtime_path=str(tmp_path / "rag_delivery.json"),
        now=1000.0,
    )

    assert result["ok"] is True
    assert result["source_mode"] == "adp"
    assert result["last_hit"]["source_mode"] == "adp"
    assert result["last_hit"]["top_hit"]["id"] == "adp:answer:1"
