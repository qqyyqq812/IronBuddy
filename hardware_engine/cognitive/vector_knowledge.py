# -*- coding: utf-8 -*-
"""Vector-backed professional knowledge retrieval for IronBuddy.

The board keeps this module stdlib-only. Embeddings and vector storage live on
the cloud connection server and are configured through environment variables.
"""

from __future__ import absolute_import

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from hardware_engine.cognitive import online_knowledge


DEFAULT_LIMIT = 3
DEFAULT_TIMEOUT_S = 4.0
DEFAULT_COLLECTION = "ironbuddy_evidence"
DEFAULT_EMBEDDING_MODEL = "bge-m3"
USER_AGENT = "IronBuddy/1.0 (vector-rag; contact=local)"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API_CONFIG_PATH = os.path.join(PROJECT_ROOT, ".api_config.json")


def _safe_text(value):
    if value is None:
        return u""
    try:
        return str(value)
    except Exception:
        return u""


def _clean_url(value):
    return _safe_text(value).strip().rstrip("/")


def _coerce_limit(limit):
    try:
        limit = int(limit)
    except Exception:
        limit = DEFAULT_LIMIT
    return max(1, min(8, limit))


def _api_config_value(name):
    try:
        if os.path.exists(API_CONFIG_PATH):
            with open(API_CONFIG_PATH, "r") as fh:
                cfg = json.load(fh)
            value = cfg.get(name)
            if value is None:
                value = cfg.get(name.lower())
            if value is not None:
                value = str(value).strip()
                if value:
                    return value
    except Exception:
        return ""
    return ""


def _env(name, default=""):
    value = os.environ.get(name, "")
    if value is None:
        value = ""
    value = str(value).strip()
    if value:
        return value
    value = _api_config_value(name)
    return value if value else default


def _json_request(url, payload=None, timeout_s=DEFAULT_TIMEOUT_S, headers=None, method=None):
    req_headers = {"User-Agent": USER_AGENT}
    if isinstance(headers, dict):
        req_headers.update(headers)
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers)
    if method:
        req.get_method = lambda: method
    with urllib.request.urlopen(req, timeout=float(timeout_s or DEFAULT_TIMEOUT_S)) as resp:
        raw = resp.read().decode("utf-8", "replace")
    if not raw:
        return {}
    return json.loads(raw)


def _auth_headers(api_key):
    if not api_key:
        return {}
    return {"api-key": api_key, "Authorization": "Bearer " + api_key}


def _normalise_embedding_response(data):
    if isinstance(data, dict):
        emb = data.get("embedding") or data.get("vector")
        if emb is None and isinstance(data.get("data"), list) and data.get("data"):
            first = data.get("data")[0]
            if isinstance(first, dict):
                emb = first.get("embedding") or first.get("vector")
        if emb is None and isinstance(data.get("result"), dict):
            emb = data.get("result", {}).get("embedding") or data.get("result", {}).get("vector")
    else:
        emb = data
    if not isinstance(emb, list):
        return []
    out = []
    for item in emb:
        try:
            out.append(float(item))
        except Exception:
            pass
    return out


def embed_query(query, embedding_url=None, embedding_model=None,
                timeout_s=DEFAULT_TIMEOUT_S):
    """Return an embedding vector from the configured BGE-M3 service."""
    url = _clean_url(embedding_url or _env("RAG_EMBEDDING_URL"))
    model = embedding_model or _env("RAG_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    if not url:
        raise RuntimeError("embedding_unavailable:RAG_EMBEDDING_URL not configured")
    payload = {"input": _safe_text(query), "text": _safe_text(query), "model": model}
    data = _json_request(url, payload=payload, timeout_s=timeout_s)
    vector = _normalise_embedding_response(data)
    if not vector:
        raise RuntimeError("embedding_unavailable:empty vector")
    return vector


def _qdrant_search(vector, limit, vector_url=None, collection=None, api_key=None,
                   timeout_s=DEFAULT_TIMEOUT_S):
    base = _clean_url(vector_url or _env("RAG_VECTOR_URL"))
    coll = collection or _env("RAG_VECTOR_COLLECTION", DEFAULT_COLLECTION)
    if not base:
        raise RuntimeError("vector_unavailable:RAG_VECTOR_URL not configured")
    url = "%s/collections/%s/points/search" % (base, urllib.parse.quote(coll))
    payload = {
        "vector": vector,
        "limit": _coerce_limit(limit),
        "with_payload": True,
        "with_vector": False,
    }
    return _json_request(
        url,
        payload=payload,
        timeout_s=timeout_s,
        headers=_auth_headers(api_key or _env("RAG_VECTOR_API_KEY")),
    )


def _hit_from_payload(item):
    if not isinstance(item, dict):
        return None
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else item
    if not isinstance(payload, dict):
        return None
    evidence_id = (
        payload.get("evidence_id") or payload.get("id") or payload.get("point_id") or item.get("id")
    )
    title = payload.get("title")
    if not evidence_id or not title:
        return None
    try:
        score = float(item.get("score"))
    except Exception:
        score = None
    hit = {
        "id": _safe_text(evidence_id),
        "evidence_id": _safe_text(evidence_id),
        "title": _safe_text(title).strip(),
        "source": _safe_text(payload.get("source") or "Vector RAG").strip(),
        "venue": _safe_text(payload.get("venue")).strip(),
        "year": payload.get("year"),
        "url": _safe_text(payload.get("url")).strip(),
        "doi": _safe_text(payload.get("doi")).strip(),
        "pmid": _safe_text(payload.get("pmid")).strip(),
        "abstract_or_snippet": _safe_text(payload.get("abstract_or_snippet")
                                         or payload.get("snippet")).strip()[:700],
        "retrieved_at": payload.get("retrieved_at"),
        "embedding_model": _safe_text(payload.get("embedding_model")).strip(),
        "embedding_created_at": payload.get("embedding_created_at"),
    }
    if score is not None:
        hit["score"] = score
    return hit


def _parse_qdrant_hits(data, limit):
    result = data.get("result") if isinstance(data, dict) else []
    if isinstance(result, dict):
        result = result.get("points") or result.get("hits") or []
    hits = []
    for item in result or []:
        hit = _hit_from_payload(item)
        if hit is not None:
            hits.append(hit)
        if len(hits) >= _coerce_limit(limit):
            break
    return hits


def build_vector_context(query, hits, max_chars=900):
    lines = []
    total = 0
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        source = _safe_text(hit.get("source") or "Vector")
        title = _safe_text(hit.get("title") or hit.get("id"))
        eid = _safe_text(hit.get("id") or hit.get("evidence_id"))
        line = u"- [%s] %s (id=%s)" % (source, title, eid)
        if hit.get("doi"):
            line += u" DOI:%s" % _safe_text(hit.get("doi"))
        if hit.get("pmid"):
            line += u" PMID:%s" % _safe_text(hit.get("pmid"))
        snippet = _safe_text(hit.get("abstract_or_snippet")).strip()
        if snippet:
            line += u" — " + snippet[:220]
        if total + len(line) > int(max_chars):
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    return u"向量 RAG 证据命中：\n" + u"\n".join(lines)


def _ingestion_payload(hit, embedding_model=None):
    if not isinstance(hit, dict):
        return None
    evidence_id = hit.get("evidence_id") or hit.get("id")
    title = hit.get("title")
    if not evidence_id or not title:
        return None
    now = time.time()
    return {
        "evidence_id": _safe_text(evidence_id),
        "source": _safe_text(hit.get("source")),
        "title": _safe_text(title),
        "venue": _safe_text(hit.get("venue")),
        "year": hit.get("year"),
        "url": _safe_text(hit.get("url")),
        "doi": _safe_text(hit.get("doi")),
        "pmid": _safe_text(hit.get("pmid")),
        "abstract_or_snippet": _safe_text(hit.get("abstract_or_snippet")),
        "retrieved_at": hit.get("retrieved_at") or now,
        "embedding_model": embedding_model or _env("RAG_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        "embedding_created_at": now,
    }


def ingest_evidence(hit, vector=None, vector_url=None, collection=None, api_key=None,
                    embedding_model=None, timeout_s=DEFAULT_TIMEOUT_S):
    """Upsert one evidence item into Qdrant when a vector is available."""
    payload = _ingestion_payload(hit, embedding_model=embedding_model)
    if payload is None:
        return {"ok": False, "reason": "invalid_evidence"}
    vec = vector
    if vec is None:
        text = (payload.get("title") or "") + "\n" + (payload.get("abstract_or_snippet") or "")
        vec = embed_query(text, embedding_model=payload.get("embedding_model"), timeout_s=timeout_s)
    base = _clean_url(vector_url or _env("RAG_VECTOR_URL"))
    coll = collection or _env("RAG_VECTOR_COLLECTION", DEFAULT_COLLECTION)
    if not base:
        return {"ok": False, "reason": "vector_unavailable"}
    point_id = payload["evidence_id"]
    body = {"points": [{"id": point_id, "vector": vec, "payload": payload}]}
    url = "%s/collections/%s/points?wait=true" % (base, urllib.parse.quote(coll))
    _json_request(
        url,
        payload=body,
        timeout_s=timeout_s,
        headers=_auth_headers(api_key or _env("RAG_VECTOR_API_KEY")),
        method="PUT",
    )
    return {"ok": True, "evidence_id": point_id, "collection": coll}


def search_vector_knowledge(query, limit=DEFAULT_LIMIT, timeout_s=DEFAULT_TIMEOUT_S,
                            allow_online_bootstrap=True, min_score=0.0,
                            vector_url=None, embedding_url=None,
                            collection=None, api_key=None):
    raw_query = _safe_text(query).strip()
    limit = _coerce_limit(limit)
    if not raw_query:
        return {
            "ok": False,
            "source_mode": "vector",
            "reason": "empty_query",
            "message": "向量 RAG 查询为空",
            "query": raw_query,
            "hits": [],
            "context": "",
        }

    errors = []
    embedding_model = _env("RAG_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    try:
        vector = embed_query(
            raw_query,
            embedding_url=embedding_url,
            embedding_model=embedding_model,
            timeout_s=timeout_s,
        )
        data = _qdrant_search(
            vector,
            limit=limit,
            vector_url=vector_url,
            collection=collection,
            api_key=api_key,
            timeout_s=timeout_s,
        )
        hits = _parse_qdrant_hits(data, limit)
        if min_score:
            hits = [h for h in hits if float(h.get("score") or 0.0) >= float(min_score)]
        if hits:
            return {
                "ok": True,
                "source_mode": "vector",
                "reason": "rag_hit",
                "message": "向量 RAG 已命中",
                "query": raw_query,
                "hits": hits,
                "context": build_vector_context(raw_query, hits),
                "errors": errors,
                "retrieved_at": time.time(),
                "vector": {
                    "store": "qdrant",
                    "collection": collection or _env("RAG_VECTOR_COLLECTION", DEFAULT_COLLECTION),
                    "embedding_model": embedding_model,
                },
            }
        errors.append({"provider": "qdrant", "error": "weak_or_empty_result"})
        vector_reason = "vector_unavailable"
    except Exception as exc:
        vector_reason = "vector_unavailable"
        errors.append({
            "provider": "vector",
            "error": type(exc).__name__ + ":" + _safe_text(exc)[:160],
        })

    if allow_online_bootstrap and online_knowledge is not None:
        online = online_knowledge.search_online_knowledge(
            raw_query,
            limit=limit,
            timeout_s=timeout_s,
        )
        online_hits = online.get("hits") if isinstance(online, dict) else []
        if online_hits:
            return {
                "ok": True,
                "source_mode": "online_pending_vector_ingest",
                "reason": vector_reason,
                "message": "向量 RAG 暂不可用，已检索外部来源等待入库",
                "query": raw_query,
                "hits": online_hits,
                "context": online.get("context") or "",
                "errors": errors + (online.get("errors") or []),
                "retrieved_at": time.time(),
                "vector": {
                    "store": "qdrant",
                    "collection": collection or _env("RAG_VECTOR_COLLECTION", DEFAULT_COLLECTION),
                    "embedding_model": embedding_model,
                    "ingest_pending": True,
                },
            }
        return {
            "ok": False,
            "source_mode": "vector",
            "reason": online.get("reason") or vector_reason,
            "message": "向量 RAG 与在线来源均不可用",
            "query": raw_query,
            "hits": [],
            "context": "",
            "errors": errors + (online.get("errors") or []),
        }

    return {
        "ok": False,
        "source_mode": "vector",
        "reason": vector_reason,
        "message": "向量 RAG 不可用",
        "query": raw_query,
        "hits": [],
        "context": "",
        "errors": errors,
    }


def status_snapshot(timeout_s=1.5, vector_url=None, api_key=None, collection=None):
    """Return a read-only vector store status without exposing secrets."""
    base = _clean_url(vector_url or _env("RAG_VECTOR_URL"))
    coll = collection or _env("RAG_VECTOR_COLLECTION", DEFAULT_COLLECTION)
    status = {
        "configured": bool(base),
        "vector_store": "qdrant",
        "online": False,
        "collection": coll,
        "embedding_model": _env("RAG_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        "evidence_count": None,
        "last_sync_time": None,
        "latest_error": "",
    }
    if not base:
        status["latest_error"] = "RAG_VECTOR_URL not configured"
        return status
    try:
        _json_request(base + "/healthz", timeout_s=timeout_s,
                      headers=_auth_headers(api_key or _env("RAG_VECTOR_API_KEY")))
        status["online"] = True
    except Exception as exc:
        status["latest_error"] = type(exc).__name__ + ":" + _safe_text(exc)[:120]
        return status
    try:
        data = _json_request(
            "%s/collections/%s/points/count" % (base, urllib.parse.quote(coll)),
            payload={"exact": False},
            timeout_s=timeout_s,
            headers=_auth_headers(api_key or _env("RAG_VECTOR_API_KEY")),
        )
        result = data.get("result") if isinstance(data, dict) else {}
        if isinstance(result, dict) and result.get("count") is not None:
            status["evidence_count"] = int(result.get("count"))
    except Exception as exc:
        status["latest_error"] = type(exc).__name__ + ":" + _safe_text(exc)[:120]
    return status


def _embedding_health_url(url):
    clean = _clean_url(url)
    if not clean:
        return ""
    try:
        parsed = urllib.parse.urlsplit(clean)
        if not parsed.scheme or not parsed.netloc:
            return clean + "/health"
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/health", "", ""))
    except Exception:
        return clean + "/health"


def embedding_status_snapshot(timeout_s=1.5, embedding_url=None):
    """Return a read-only embedding service status without exposing secrets."""
    url = _clean_url(embedding_url or _env("RAG_EMBEDDING_URL"))
    status = {
        "configured": bool(url),
        "online": False,
        "model": _env("RAG_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        "endpoint": "configured" if url else "",
        "latest_error": "",
        "backend": "",
        "model_loaded": None,
        "dim": None,
    }
    if not url:
        status["latest_error"] = "RAG_EMBEDDING_URL not configured"
        return status
    health_url = _embedding_health_url(url)
    try:
        data = _json_request(health_url, timeout_s=timeout_s)
        status["online"] = bool(data.get("ok", True)) if isinstance(data, dict) else True
        if isinstance(data, dict):
            status["backend"] = _safe_text(data.get("backend")).strip()
            if data.get("model_loaded") is not None:
                status["model_loaded"] = bool(data.get("model_loaded"))
            if data.get("model"):
                status["model"] = _safe_text(data.get("model")).strip()
    except Exception as exc:
        status["latest_error"] = type(exc).__name__ + ":" + _safe_text(exc)[:120]
        return status
    try:
        vector = embed_query("IronBuddy embedding health check", embedding_url=url,
                             embedding_model=status["model"], timeout_s=timeout_s)
        status["dim"] = len(vector)
    except Exception as exc:
        status["latest_error"] = type(exc).__name__ + ":" + _safe_text(exc)[:120]
    return status


__all__ = [
    "DEFAULT_COLLECTION",
    "DEFAULT_EMBEDDING_MODEL",
    "build_vector_context",
    "embed_query",
    "embedding_status_snapshot",
    "ingest_evidence",
    "search_vector_knowledge",
    "status_snapshot",
]
