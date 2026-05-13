"""RAG hit delivery helper for IronBuddy.

This module is intentionally stdlib-only and Python 3.7 compatible. It does
not send Feishu messages; it prepares the last-hit payload and the interactive
card input that a caller can hand to the existing Feishu delivery endpoint.
"""

from __future__ import absolute_import

import hashlib
import json
import os
import re
import time

from hardware_engine.cognitive import online_knowledge
try:
    from hardware_engine.cognitive import adp_knowledge
except Exception:
    adp_knowledge = None
try:
    from hardware_engine.cognitive import vector_knowledge
except Exception:
    vector_knowledge = None


DEFAULT_RUNTIME_PATH = "/dev/shm/ironbuddy_rag_delivery.json"
DEFAULT_COOLDOWN_S = 60.0
DEFAULT_LIMIT = 3
DEFAULT_MAX_CONTEXT_CHARS = 480


def _truthy_env(name, default="0"):
    raw = os.environ.get(name, default)
    try:
        raw = str(raw).strip().lower()
    except Exception:
        raw = default
    return raw in ("1", "true", "yes", "on")


def vector_fallback_enabled():
    """Return whether the old self-hosted vector RAG may answer user flows."""
    return _truthy_env("IRONBUDDY_ENABLE_VECTOR_FALLBACK", "0")


def _safe_text(value):
    if value is None:
        return u""
    try:
        return str(value)
    except Exception:
        return u""


def _normalise_query(query):
    text = _safe_text(query).strip().lower()
    text = re.sub(r"[\s,，。.!！?？:：;；、\"“”'‘’]+", u"", text)
    return text


def query_key(query):
    """Return a stable short key for per-query runtime bookkeeping."""
    norm = _normalise_query(query)
    if not norm:
        return u""
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
    return u"q_" + digest


def _coerce_limit(limit):
    try:
        limit = int(limit)
    except Exception:
        limit = DEFAULT_LIMIT
    return max(1, min(8, limit))


def _empty_result(source_mode, reason, message, query, ok=False, errors=None):
    return {
        "ok": bool(ok),
        "source_mode": _safe_text(source_mode or "adp"),
        "reason": _safe_text(reason or "adp_unavailable"),
        "message": _safe_text(message or u"ADP 专业知识库暂时不可用"),
        "query": _safe_text(query).strip(),
        "hits": [],
        "context": u"",
        "errors": errors if isinstance(errors, list) else [],
    }


def search_professional_knowledge(query, limit=DEFAULT_LIMIT, allow_vector_fallback=None):
    """ADP-first user-facing retrieval.

    The previous BGE-M3/Qdrant vector path stays available only behind an
    explicit emergency switch so it cannot silently outrank the managed ADP app.
    """
    raw_query = _safe_text(query).strip()
    limit = _coerce_limit(limit)
    if not raw_query:
        return _empty_result("adp", "empty_query", u"ADP 查询为空", raw_query)
    if allow_vector_fallback is None:
        allow_vector_fallback = vector_fallback_enabled()

    adp_result = None
    if adp_knowledge is not None:
        try:
            adp_result = adp_knowledge.search_adp_knowledge(raw_query, limit=limit)
        except Exception as exc:
            adp_result = _empty_result(
                "adp",
                "adp_exception",
                u"ADP 专业知识库暂时不可用",
                raw_query,
                errors=[{"provider": "adp", "error": _safe_text(exc)[:160]}],
            )
        if isinstance(adp_result, dict) and adp_result.get("hits"):
            return adp_result

    if allow_vector_fallback and vector_knowledge is not None:
        try:
            return vector_knowledge.search_vector_knowledge(raw_query, limit=limit)
        except Exception as exc:
            if isinstance(adp_result, dict):
                errors = adp_result.get("errors") or []
                if isinstance(errors, list):
                    errors.append({"provider": "vector", "error": _safe_text(exc)[:160]})
                    adp_result["errors"] = errors
                return adp_result
            return _empty_result(
                "vector",
                "vector_exception",
                u"向量 RAG 不可用",
                raw_query,
                errors=[{"provider": "vector", "error": _safe_text(exc)[:160]}],
            )

    if isinstance(adp_result, dict):
        adp_result["source_mode"] = adp_result.get("source_mode") or "adp"
        return adp_result
    return _empty_result(
        "adp",
        "adp_provider_unavailable",
        u"ADP 专业知识库暂时不可用",
        raw_query,
    )


def _coerce_cooldown(value):
    try:
        return max(0.0, float(value))
    except Exception:
        return DEFAULT_COOLDOWN_S


def _public_hit(hit):
    if not isinstance(hit, dict):
        return {}
    out = {}
    for key in (
        "id", "title", "source", "venue", "year", "url", "doi", "pmid",
        "abstract_or_snippet", "retrieved_at", "score",
    ):
        value = hit.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, list):
            out[key] = [_safe_text(x) for x in value]
        else:
            out[key] = _safe_text(value)
    return out


def _read_json(path):
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}


def _atomic_write_json(path, payload):
    if not path:
        return False
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, sort_keys=True)
    os.rename(tmp, path)
    return True


def _make_body_text(query, hits, context):
    lines = [
        u"用户问题：%s" % _safe_text(query).strip(),
        u"",
        u"在线专业知识库命中：",
    ]
    for idx, hit in enumerate(hits[:3], 1):
        title = _safe_text(hit.get("title") or hit.get("id") or u"未命名条目")
        source = _safe_text(hit.get("source") or u"Online")
        year = _safe_text(hit.get("year") or u"")
        line = u"%d. [%s] %s" % (idx, source, title)
        if year:
            line += u"（%s）" % year
        if hit.get("doi"):
            line += u" DOI:%s" % _safe_text(hit.get("doi"))
        if hit.get("pmid"):
            line += u" PMID:%s" % _safe_text(hit.get("pmid"))
        if hit.get("url"):
            line += u" %s" % _safe_text(hit.get("url"))
        lines.append(line)
        snippet = _safe_text(hit.get("abstract_or_snippet") or u"").strip()
        if snippet:
            lines.append(u"   %s" % snippet[:240])
    if context:
        lines.extend([u"", u"RAG 上下文：", _safe_text(context).strip()])
    return u"\n".join(lines).strip()


def _mk_md(content):
    return {"tag": "markdown", "content": _safe_text(content)}


def _build_detail_card(title, body_text, last_hit):
    footer = u"IronBuddy RAG · %s" % time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(float(last_hit.get("ts") or time.time())),
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": _safe_text(title)},
        },
        "elements": [
            _mk_md(u"**知识库命中详情**\n" + _safe_text(body_text)),
            {"tag": "hr"},
            _mk_md(
                u"**调试信息**\nquery_key: `%s`\nturn_id: `%s`\nhit_count: %s\nsource_mode: `%s`"
                % (
                    _safe_text(last_hit.get("query_key")),
                    _safe_text(last_hit.get("turn_id") or "-"),
                    int(last_hit.get("hit_count") or 0),
                    _safe_text(last_hit.get("source_mode") or "online"),
                )
            ),
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": footer}],
            },
        ],
    }


def build_last_hit_payload(query, hits, context, turn_id="", now=None,
                           source_mode="online", errors=None):
    """Build a serialisable last-hit payload from retrieved RAG hits."""
    now = time.time() if now is None else float(now)
    public_hits = [_public_hit(hit) for hit in (hits or []) if isinstance(hit, dict)]
    key = query_key(query)
    top_hit = public_hits[0] if public_hits else {}
    return {
        "kind": "rag_hit",
        "query": _safe_text(query).strip(),
        "query_key": key,
        "normalised_query": _normalise_query(query),
        "turn_id": _safe_text(turn_id).strip(),
        "ts": now,
        "hit_count": len(public_hits),
        "top_hit": top_hit,
        "hits": public_hits,
        "context": _safe_text(context),
        "source_mode": _safe_text(source_mode or "online"),
        "errors": errors if isinstance(errors, list) else [],
    }


def build_feishu_detail_payload(last_hit):
    """Return Feishu card input and preview card for a RAG hit.

    The returned dict is pure data. It intentionally does not send anything.
    Callers can render ``card`` in a dry-run UI or hand ``delivery_payload`` to
    a real Feishu sender after the user has chosen to send it.
    """
    if not isinstance(last_hit, dict):
        last_hit = {}
    query = last_hit.get("query") or u""
    hits = last_hit.get("hits") or []
    if not isinstance(hits, list):
        hits = []
    title = u"IronBuddy 在线知识库命中"
    top_title = u""
    if hits and isinstance(hits[0], dict):
        top_title = _safe_text(hits[0].get("title") or hits[0].get("id"))
    if top_title:
        title += u" · " + top_title
    body_text = _make_body_text(query, hits, last_hit.get("context") or u"")
    card = _build_detail_card(title, body_text, last_hit)
    card_input = {
        "type": "rag_detail",
        "title": title,
        "text": body_text,
        "body": body_text,
        "prompt": body_text,
        "dry_run": True,
        "msg_type": "interactive",
        "card": card,
        "stats": {},
        "degraded": False,
        "rag": last_hit,
    }
    return {
        "msg_type": "interactive",
        "title": title,
        "body_text": body_text,
        "card_input": card_input,
        "card": card,
        "delivery_payload": {
            "msg_type": "interactive",
            "card": card,
        },
    }


def _dedupe_reason(runtime, key, turn_id, now, cooldown_s):
    if cooldown_s <= 0:
        return None, 0
    queries = runtime.get("queries")
    if not isinstance(queries, dict):
        queries = {}
    prev = queries.get(key)
    if not isinstance(prev, dict):
        return None, 0
    prev_turn = _safe_text(prev.get("last_turn_id")).strip()
    if turn_id and prev_turn == turn_id:
        return "duplicate_query_turn", 0
    try:
        last_ts = float(prev.get("last_ts") or 0.0)
    except Exception:
        last_ts = 0.0
    elapsed = max(0.0, float(now) - last_ts)
    if elapsed < cooldown_s:
        remaining = int(round(cooldown_s - elapsed))
        return "cooldown", max(1, remaining)
    return None, 0


def _record_hit(runtime, last_hit, now):
    queries = runtime.get("queries")
    if not isinstance(queries, dict):
        queries = {}
    key = last_hit.get("query_key") or u""
    if key:
        queries[key] = {
            "last_ts": float(now),
            "last_turn_id": _safe_text(last_hit.get("turn_id")).strip(),
            "last_query": _safe_text(last_hit.get("query")).strip(),
            "top_hit_id": _safe_text((last_hit.get("top_hit") or {}).get("id")),
        }
    runtime["version"] = 1
    runtime["updated_ts"] = float(now)
    runtime["last_hit"] = last_hit
    runtime["queries"] = queries
    return runtime


def prepare_rag_delivery(query, turn_id="", runtime_path=DEFAULT_RUNTIME_PATH,
                         now=None, cooldown_s=DEFAULT_COOLDOWN_S,
                         limit=DEFAULT_LIMIT,
                         max_context_chars=DEFAULT_MAX_CONTEXT_CHARS):
    """Retrieve RAG hits and prepare a de-duplicated delivery payload."""
    now = time.time() if now is None else float(now)
    raw_query = _safe_text(query).strip()
    if not raw_query:
        return {
            "ok": False,
            "should_deliver": False,
            "reason": "empty_query",
            "query": raw_query,
            "query_key": "",
        }

    limit = _coerce_limit(limit)
    cooldown_s = _coerce_cooldown(cooldown_s)
    key = query_key(raw_query)
    runtime = _read_json(runtime_path)
    reason, remaining = _dedupe_reason(
        runtime,
        key,
        _safe_text(turn_id).strip(),
        now,
        cooldown_s,
    )
    if reason:
        out = {
            "ok": True,
            "should_deliver": False,
            "reason": reason,
            "query": raw_query,
            "query_key": key,
        }
        if remaining:
            out["cooldown_remaining_s"] = remaining
        previous = runtime.get("last_hit")
        if isinstance(previous, dict):
            out["last_hit"] = previous
        return out

    rag_result = search_professional_knowledge(raw_query, limit=limit)
    hits = rag_result.get("hits") if isinstance(rag_result, dict) else []
    source_mode = (
        rag_result.get("source_mode") if isinstance(rag_result, dict) else ""
    ) or "vector"
    if not hits:
        rag_ok = bool(rag_result.get("ok")) if isinstance(rag_result, dict) else False
        reason = "no_hit" if rag_ok else (
            (rag_result.get("reason") if isinstance(rag_result, dict) else None) or "adp_unavailable"
        )
        return {
            "ok": rag_ok,
            "should_deliver": False,
            "reason": reason,
            "message": u"专业证据无相关命中" if reason == "no_hit" else u"专业证据不可用",
            "source_mode": source_mode,
            "query": raw_query,
            "query_key": key,
            "hits": [],
            "context": u"",
            "errors": (rag_result.get("errors") if isinstance(rag_result, dict) else []) or [],
        }
    context = (rag_result.get("context") if isinstance(rag_result, dict) else "")
    if not context:
        if source_mode == "adp" and adp_knowledge is not None:
            context = adp_knowledge.build_adp_context(raw_query, hits, max_chars=max_context_chars)
        elif source_mode == "vector" and vector_knowledge is not None:
            context = vector_knowledge.build_vector_context(raw_query, hits, max_chars=max_context_chars)
        else:
            context = online_knowledge.build_online_context(raw_query, hits, max_chars=max_context_chars)
    last_hit = build_last_hit_payload(
        raw_query,
        hits,
        context,
        turn_id=turn_id,
        now=now,
        source_mode=source_mode,
        errors=(rag_result.get("errors") if isinstance(rag_result, dict) else []),
    )
    feishu = build_feishu_detail_payload(last_hit)
    runtime = _record_hit(runtime, last_hit, now)
    _atomic_write_json(runtime_path, runtime)
    return {
        "ok": True,
        "should_deliver": True,
        "reason": "rag_hit",
        "source_mode": source_mode,
        "query": raw_query,
        "query_key": key,
        "last_hit": last_hit,
        "feishu": feishu,
    }


__all__ = [
    "DEFAULT_COOLDOWN_S",
    "DEFAULT_RUNTIME_PATH",
    "build_feishu_detail_payload",
    "build_last_hit_payload",
    "prepare_rag_delivery",
    "query_key",
]
