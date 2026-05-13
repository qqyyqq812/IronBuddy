import json
import os

from hardware_engine.cognitive import vector_knowledge


class StubResponse(object):
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_vector_search_returns_cited_evidence(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout=0):
        calls.append((request.full_url, request.data))
        if request.full_url == "http://embed/v1/embeddings":
            return StubResponse({"embedding": [0.1, 0.2, 0.3]})
        assert request.full_url == "http://qdrant/collections/ironbuddy_evidence/points/search"
        return StubResponse({
            "result": [
                {
                    "id": "pubmed:12345",
                    "score": 0.82,
                    "payload": {
                        "evidence_id": "pubmed:12345",
                        "source": "PubMed",
                        "title": "Surface EMG fatigue evidence",
                        "venue": "Journal of EMG",
                        "year": 2021,
                        "url": "https://pubmed.ncbi.nlm.nih.gov/12345/",
                        "doi": "10.1000/emg",
                        "pmid": "12345",
                        "abstract_or_snippet": "RMS and MDF change during fatigue.",
                        "embedding_model": "bge-m3",
                        "embedding_created_at": 1000.0,
                    },
                }
            ]
        })

    monkeypatch.setattr(vector_knowledge.urllib.request, "urlopen", fake_urlopen)
    result = vector_knowledge.search_vector_knowledge(
        "肌电疲劳怎么判断",
        limit=1,
        allow_online_bootstrap=False,
        vector_url="http://qdrant",
        embedding_url="http://embed/v1/embeddings",
    )

    assert result["ok"] is True
    assert result["source_mode"] == "vector"
    assert result["reason"] == "rag_hit"
    assert result["hits"][0]["id"] == "pubmed:12345"
    assert result["hits"][0]["embedding_model"] == "bge-m3"
    assert "pubmed:12345" in result["context"]
    assert any(b"bge-m3" in (data or b"") for _url, data in calls)


def test_vector_unavailable_is_explicit_without_local_fallback(monkeypatch):
    def boom(request, timeout=0):
        raise vector_knowledge.urllib.error.URLError("offline")

    monkeypatch.setattr(vector_knowledge.urllib.request, "urlopen", boom)
    result = vector_knowledge.search_vector_knowledge(
        "膝盖酸痛怎么办",
        allow_online_bootstrap=False,
        vector_url="http://qdrant",
        embedding_url="http://embed/v1/embeddings",
    )

    assert result["ok"] is False
    assert result["source_mode"] == "vector"
    assert result["reason"] == "vector_unavailable"
    assert result["hits"] == []
    assert result["errors"]


def test_vector_bootstraps_from_online_sources_when_requested(monkeypatch):
    def boom(request, timeout=0):
        raise vector_knowledge.urllib.error.URLError("offline")

    def fake_online(query, limit=3, **kwargs):
        return {
            "ok": True,
            "source_mode": "online",
            "reason": "rag_hit",
            "hits": [{"id": "openalex:W1", "title": "Velocity loss", "source": "OpenAlex"}],
            "context": "在线专业知识库命中",
            "errors": [],
        }

    monkeypatch.setattr(vector_knowledge.urllib.request, "urlopen", boom)
    monkeypatch.setattr(vector_knowledge.online_knowledge, "search_online_knowledge", fake_online)
    result = vector_knowledge.search_vector_knowledge(
        "训练疲劳 velocity loss",
        allow_online_bootstrap=True,
        vector_url="http://qdrant",
        embedding_url="http://embed/v1/embeddings",
    )

    assert result["ok"] is True
    assert result["source_mode"] == "online_pending_vector_ingest"
    assert result["reason"] == "vector_unavailable"
    assert result["hits"][0]["id"] == "openalex:W1"
    assert result["vector"]["ingest_pending"] is True


def test_ingest_evidence_upserts_metadata_without_secret_values(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=0):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["auth"] = request.headers.get("Authorization")
        return StubResponse({"result": {"operation_id": 1}})

    monkeypatch.setattr(vector_knowledge.urllib.request, "urlopen", fake_urlopen)
    result = vector_knowledge.ingest_evidence(
        {
            "id": "pubmed:999",
            "source": "PubMed",
            "title": "MVC normalized RMS",
            "abstract_or_snippet": "RMS normalized by MVC.",
            "pmid": "999",
        },
        vector=[0.1, 0.2],
        vector_url="http://qdrant",
        api_key="secret-for-header-only",
    )

    assert result["ok"] is True
    assert seen["url"] == "http://qdrant/collections/ironbuddy_evidence/points?wait=true"
    point = seen["body"]["points"][0]
    assert point["id"] == "pubmed:999"
    assert point["payload"]["embedding_model"] == "bge-m3"
    assert point["payload"]["pmid"] == "999"
    assert seen["auth"] == "Bearer secret-for-header-only"


def test_api_config_fallback_supplies_vector_and_embedding_urls(monkeypatch, tmp_path):
    cfg_path = tmp_path / ".api_config.json"
    cfg_path.write_text(json.dumps({
        "RAG_VECTOR_URL": "http://qdrant-from-config",
        "RAG_EMBEDDING_URL": "http://embed-from-config",
        "RAG_EMBEDDING_MODEL": "bge-m3",
    }), encoding="utf-8")
    monkeypatch.setattr(vector_knowledge, "API_CONFIG_PATH", str(cfg_path))
    monkeypatch.delenv("RAG_VECTOR_URL", raising=False)
    monkeypatch.delenv("RAG_EMBEDDING_URL", raising=False)

    calls = []

    def fake_urlopen(request, timeout=0):
        calls.append(request.full_url)
        if request.full_url == "http://embed-from-config":
            return StubResponse({"embedding": [0.1, 0.2, 0.3]})
        return StubResponse({"result": []})

    monkeypatch.setattr(vector_knowledge.urllib.request, "urlopen", fake_urlopen)
    result = vector_knowledge.search_vector_knowledge(
        "肌电疲劳怎么判断",
        allow_online_bootstrap=False,
    )

    assert result["ok"] is False
    assert calls[0] == "http://embed-from-config"
    assert calls[1] == "http://qdrant-from-config/collections/ironbuddy_evidence/points/search"


def test_embedding_status_snapshot_checks_health_and_dimension(monkeypatch):
    def fake_urlopen(request, timeout=0):
        if request.full_url == "http://embed/health":
            return StubResponse({
                "ok": True,
                "model": "bge-m3",
                "backend": "sentence-transformers",
                "model_loaded": True,
            })
        assert request.full_url == "http://embed"
        return StubResponse({"embedding": [0.1, 0.2, 0.3, 0.4]})

    monkeypatch.setattr(vector_knowledge.urllib.request, "urlopen", fake_urlopen)
    status = vector_knowledge.embedding_status_snapshot(embedding_url="http://embed")

    assert status["configured"] is True
    assert status["online"] is True
    assert status["model"] == "bge-m3"
    assert status["backend"] == "sentence-transformers"
    assert status["model_loaded"] is True
    assert status["dim"] == 4
