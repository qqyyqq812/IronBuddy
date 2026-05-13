# -*- coding: utf-8 -*-
"""Tencent ADP application adapter for IronBuddy Lane A.

This module calls the published ADP HTTP SSE chat endpoint with the configured
AppKey.  It exposes a RAG-like result shape so the existing IronBuddy pipeline
can prefer ADP and fall back to the vector/online sources when ADP is unavailable.
"""

from __future__ import absolute_import

import json
import os
import time
import uuid
import urllib.error
import urllib.request


DEFAULT_TIMEOUT_S = 18.0
DEFAULT_ENDPOINT = "https://wss.lke.cloud.tencent.com/adp/v2/chat"
USER_AGENT = "IronBuddy/1.0 (adp-knowledge)"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API_CONFIG_PATH = os.path.join(PROJECT_ROOT, ".api_config.json")


def _safe_text(value):
    if value is None:
        return u""
    try:
        return str(value)
    except Exception:
        return u""


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


def _coerce_limit(limit):
    try:
        limit = int(limit)
    except Exception:
        limit = 3
    return max(1, min(8, limit))


def _make_payload(query, app_key, visitor_id=None, conversation_id=None):
    return {
        "RequestId": uuid.uuid4().hex,
        "ConversationId": conversation_id or str(uuid.uuid4()),
        "AppKey": app_key,
        "VisitorId": visitor_id or "ironbuddy_lane_a",
        "Contents": [{"Type": "text", "Text": _safe_text(query)}],
        "Incremental": True,
        "EnableMultiIntent": True,
        "Stream": "enable",
        "SearchNetwork": "disable",
    }


def _parse_sse_text(resp):
    text = u""
    events = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        if line.startswith("event:"):
            events.append(line[6:].strip())
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except Exception:
            continue
        typ = obj.get("Type")
        if typ == "text.delta":
            text += _safe_text(obj.get("Text"))
            continue
        if typ == "message.done":
            message = obj.get("Message") if isinstance(obj.get("Message"), dict) else {}
            contents = message.get("Contents") if isinstance(message.get("Contents"), list) else []
            for content in contents:
                if isinstance(content, dict) and content.get("Type") == "text" and content.get("Text"):
                    text = _safe_text(content.get("Text"))
                    break
    return text.strip(), events


def ask_adp(query, app_key=None, endpoint=None, timeout_s=DEFAULT_TIMEOUT_S,
            visitor_id=None, conversation_id=None):
    """Return raw ADP answer text and safe metadata."""
    raw_query = _safe_text(query).strip()
    key = app_key or _env("ADP_APP_KEY")
    if not raw_query:
        return {"ok": False, "reason": "empty_query", "answer": ""}
    if not key:
        return {"ok": False, "reason": "missing_app_key", "answer": ""}
    url = endpoint or _env("ADP_CHAT_URL", DEFAULT_ENDPOINT)
    payload = _make_payload(
        raw_query,
        key,
        visitor_id=visitor_id,
        conversation_id=conversation_id,
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=float(timeout_s or DEFAULT_TIMEOUT_S)) as resp:
            text, events = _parse_sse_text(resp)
        elapsed = time.time() - started
        if not text:
            return {
                "ok": False,
                "reason": "empty_answer",
                "answer": "",
                "elapsed_s": round(elapsed, 2),
                "events": events[:12],
            }
        return {
            "ok": True,
            "reason": "adp_answer",
            "answer": text,
            "elapsed_s": round(elapsed, 2),
            "events": events[:12],
        }
    except Exception as exc:
        return {
            "ok": False,
            "reason": "adp_unavailable",
            "answer": "",
            "errors": [{
                "provider": "adp",
                "error": type(exc).__name__ + ":" + _safe_text(exc)[:160],
            }],
        }


def search_adp_knowledge(query, limit=3, timeout_s=DEFAULT_TIMEOUT_S,
                         app_key=None, endpoint=None):
    """Return ADP answer in the same broad shape as other knowledge providers."""
    raw_query = _safe_text(query).strip()
    limit = _coerce_limit(limit)
    if not raw_query:
        return {
            "ok": False,
            "source_mode": "adp",
            "reason": "empty_query",
            "message": "ADP 查询为空",
            "query": raw_query,
            "hits": [],
            "context": "",
        }
    result = ask_adp(
        raw_query,
        app_key=app_key,
        endpoint=endpoint,
        timeout_s=timeout_s,
    )
    if not result.get("ok"):
        return {
            "ok": False,
            "source_mode": "adp",
            "reason": result.get("reason") or "adp_unavailable",
            "message": "ADP 知识库不可用",
            "query": raw_query,
            "hits": [],
            "context": "",
            "errors": result.get("errors") or [],
        }
    answer = _safe_text(result.get("answer")).strip()
    hit = {
        "id": "adp:answer:%s" % uuid.uuid5(uuid.NAMESPACE_URL, raw_query).hex[:12],
        "evidence_id": "adp:answer:%s" % uuid.uuid5(uuid.NAMESPACE_URL, raw_query).hex[:12],
        "title": "ADP 教练回答",
        "source": "Tencent ADP Knowledge App",
        "abstract_or_snippet": answer[:700],
        "retrieved_at": time.time(),
    }
    return {
        "ok": True,
        "source_mode": "adp",
        "reason": "rag_hit",
        "message": "ADP 知识库已回答",
        "query": raw_query,
        "hits": [hit][:limit],
        "context": build_adp_context(raw_query, [hit]),
        "errors": [],
        "retrieved_at": time.time(),
        "adp": {
            "elapsed_s": result.get("elapsed_s"),
            "events": result.get("events") or [],
        },
    }


def build_adp_context(query, hits, max_chars=900):
    lines = []
    total = 0
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        line = u"- [腾讯 ADP] %s — %s" % (
            _safe_text(hit.get("title") or "教练回答"),
            _safe_text(hit.get("abstract_or_snippet")).strip()[:520],
        )
        if total + len(line) > int(max_chars):
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    return u"ADP 专业教练知识命中：\n" + u"\n".join(lines)


def status_snapshot(timeout_s=1.5):
    return {
        "configured": bool(_env("ADP_APP_KEY")),
        "online": None,
        "provider": "tencent_adp",
        "endpoint": _env("ADP_CHAT_URL", DEFAULT_ENDPOINT),
        "mode": "http_sse",
    }


__all__ = [
    "ask_adp",
    "build_adp_context",
    "search_adp_knowledge",
    "status_snapshot",
]
