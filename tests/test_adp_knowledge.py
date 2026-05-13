import json

from hardware_engine.cognitive import adp_knowledge


class StubResponse(object):
    def __init__(self, lines):
        self.lines = lines

    def __iter__(self):
        for line in self.lines:
            yield line.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_search_adp_knowledge_parses_sse_text_delta(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=0):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return StubResponse([
            "event: text.delta\n",
            'data: {"Type":"text.delta","Text":"先停止当前组，"}\n',
            "event: text.delta\n",
            'data: {"Type":"text.delta","Text":"检查膝盖方向。"}\n',
            "data: [DONE]\n",
        ])

    monkeypatch.setattr(adp_knowledge.urllib.request, "urlopen", fake_urlopen)
    result = adp_knowledge.search_adp_knowledge(
        "深蹲膝盖不舒服怎么办",
        app_key="secret-app-key",
        endpoint="https://adp.example/chat",
    )

    assert result["ok"] is True
    assert result["source_mode"] == "adp"
    assert result["reason"] == "rag_hit"
    assert result["hits"][0]["source"] == "Tencent ADP Knowledge App"
    assert "先停止当前组" in result["context"]
    assert seen["url"] == "https://adp.example/chat"
    assert seen["body"]["AppKey"] == "secret-app-key"
    assert seen["body"]["SearchNetwork"] == "disable"


def test_search_adp_knowledge_reports_missing_key(monkeypatch, tmp_path):
    monkeypatch.setattr(adp_knowledge, "API_CONFIG_PATH", str(tmp_path / "missing.json"))
    monkeypatch.delenv("ADP_APP_KEY", raising=False)

    result = adp_knowledge.search_adp_knowledge("深蹲膝盖不舒服怎么办")

    assert result["ok"] is False
    assert result["source_mode"] == "adp"
    assert result["reason"] == "missing_app_key"
    assert result["hits"] == []
