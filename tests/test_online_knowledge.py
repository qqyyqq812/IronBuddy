import json
import urllib.parse

from hardware_engine.cognitive import online_knowledge


class StubResponse(object):
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_pubmed_provider_normalizes_external_evidence(monkeypatch, tmp_path):
    calls = []

    def fake_urlopen(request, timeout=0):
        url = request.full_url
        calls.append(url)
        if "esearch.fcgi" in url:
            return StubResponse({"esearchresult": {"idlist": ["12345"]}})
        assert "esummary.fcgi" in url
        return StubResponse({
            "result": {
                "12345": {
                    "uid": "12345",
                    "title": "Surface EMG muscle fatigue review",
                    "source": "PubMed Journal",
                    "pubdate": "2022 Jan",
                    "elocationid": "doi: 10.1000/test",
                    "articleids": [
                        {"idtype": "doi", "value": "10.1000/test"},
                        {"idtype": "pubmed", "value": "12345"},
                    ],
                }
            }
        })

    monkeypatch.setattr(online_knowledge.urllib.request, "urlopen", fake_urlopen)
    result = online_knowledge.search_online_knowledge(
        "肌电疲劳怎么判断",
        limit=1,
        cache_path=str(tmp_path / "cache.json"),
        providers=("pubmed",),
    )

    assert result["ok"] is True
    assert result["source_mode"] == "online"
    assert result["hits"][0]["source"] == "PubMed"
    assert result["hits"][0]["pmid"] == "12345"
    assert result["hits"][0]["doi"] == "10.1000/test"
    assert result["hits"][0]["url"].endswith("/12345/")
    assert any("surface EMG" in urllib.parse.unquote_plus(url) for url in calls)


def test_openalex_provider_uses_external_title_and_url(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=0):
        return StubResponse({
            "results": [
                {
                    "id": "https://openalex.org/W1",
                    "display_name": "Velocity loss and fatigue",
                    "publication_year": 2011,
                    "doi": "https://doi.org/10.2000/vel",
                    "primary_location": {
                        "source": {"display_name": "Sports Medicine"},
                        "landing_page_url": "https://example.org/paper",
                    },
                    "abstract_inverted_index": {
                        "fatigue": [0],
                        "velocity": [1],
                    },
                }
            ]
        })

    monkeypatch.setattr(online_knowledge.urllib.request, "urlopen", fake_urlopen)
    result = online_knowledge.search_online_knowledge(
        "velocity loss resistance training fatigue",
        limit=1,
        cache_path=str(tmp_path / "cache.json"),
        providers=("openalex",),
    )

    hit = result["hits"][0]
    assert hit["source"] == "OpenAlex"
    assert hit["title"] == "Velocity loss and fatigue"
    assert hit["year"] == 2011
    assert hit["doi"] == "10.2000/vel"
    assert hit["url"] == "https://example.org/paper"


def test_crossref_provider_normalizes_doi_metadata(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=0):
        assert "api.crossref.org/works" in request.full_url
        return StubResponse({
            "message": {
                "items": [
                    {
                        "DOI": "10.3000/crossref",
                        "title": ["ACSM resistance training position stand"],
                        "container-title": ["Medicine and Science in Sports and Exercise"],
                        "published-print": {"date-parts": [[2009, 3, 1]]},
                        "URL": "https://doi.org/10.3000/crossref",
                        "abstract": "<jats:p>Resistance training guidance.</jats:p>",
                    }
                ]
            }
        })

    monkeypatch.setattr(online_knowledge.urllib.request, "urlopen", fake_urlopen)
    result = online_knowledge.search_online_knowledge(
        "ACSM resistance training guideline",
        limit=1,
        cache_path=str(tmp_path / "cache.json"),
        providers=("crossref",),
    )

    hit = result["hits"][0]
    assert hit["source"] == "Crossref"
    assert hit["title"] == "ACSM resistance training position stand"
    assert hit["venue"] == "Medicine and Science in Sports and Exercise"
    assert hit["year"] == 2009
    assert hit["doi"] == "10.3000/crossref"
    assert "Resistance training guidance" in hit["abstract_or_snippet"]


def test_semantic_scholar_provider_normalizes_external_ids(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=0):
        assert "api.semanticscholar.org" in request.full_url
        return StubResponse({
            "data": [
                {
                    "paperId": "S2-PAPER",
                    "title": "Median frequency decline during muscle fatigue",
                    "year": 2018,
                    "venue": "Journal of Electromyography",
                    "url": "https://www.semanticscholar.org/paper/S2-PAPER",
                    "abstract": "MDF and MNF are frequency-domain fatigue indicators.",
                    "externalIds": {"DOI": "10.4000/s2", "PubMed": "45678"},
                }
            ]
        })

    monkeypatch.setattr(online_knowledge.urllib.request, "urlopen", fake_urlopen)
    result = online_knowledge.search_online_knowledge(
        "surface EMG MDF MNF muscle fatigue",
        limit=1,
        cache_path=str(tmp_path / "cache.json"),
        providers=("semantic_scholar",),
    )

    hit = result["hits"][0]
    assert hit["source"] == "Semantic Scholar"
    assert hit["id"] == "semantic:S2-PAPER"
    assert hit["doi"] == "10.4000/s2"
    assert hit["pmid"] == "45678"
    assert "MDF and MNF" in hit["abstract_or_snippet"]


def test_online_failure_is_explicit_not_local_fallback(monkeypatch, tmp_path):
    def boom(request, timeout=0):
        raise online_knowledge.urllib.error.URLError("offline")

    monkeypatch.setattr(online_knowledge.urllib.request, "urlopen", boom)
    result = online_knowledge.search_online_knowledge(
        "膝盖酸痛怎么办",
        limit=2,
        cache_path=str(tmp_path / "cache.json"),
        providers=("pubmed",),
    )

    assert result["ok"] is False
    assert result["source_mode"] == "online"
    assert result["hits"] == []
    assert result["reason"] == "online_unavailable"
