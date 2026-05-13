# -*- coding: utf-8 -*-
"""Online professional knowledge retrieval for IronBuddy.

The local repo owns rules and product contracts only. Professional fitness,
fatigue, and sEMG evidence should come from external open academic sources.
This module is stdlib-only and Python 3.7 compatible for the Toybrick board.
"""

from __future__ import absolute_import

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_CACHE_PATH = (
    "/dev/shm/ironbuddy_online_knowledge_cache.json"
    if os.path.isdir("/dev/shm")
    else "/tmp/ironbuddy_online_knowledge_cache.json"
)
DEFAULT_CACHE_TTL_S = 900.0
DEFAULT_TIMEOUT_S = 4.0
DEFAULT_LIMIT = 3
DEFAULT_PROVIDERS = ("pubmed", "openalex", "crossref", "semantic_scholar")
USER_AGENT = "IronBuddy/1.0 (professional-fitness-rag; contact=local)"


def _env_first(*names):
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _safe_text(value):
    if value is None:
        return u""
    try:
        return str(value)
    except Exception:
        return u""


def _clean_space(text):
    return re.sub(r"\s+", " ", _safe_text(text)).strip()


def _coerce_limit(limit):
    try:
        limit = int(limit)
    except Exception:
        limit = DEFAULT_LIMIT
    return max(1, min(8, limit))


def _query_terms(query):
    text = _safe_text(query).lower()
    terms = [
        "resistance training",
        "fatigue",
    ]
    if any(token in text for token in ("肌电", "emg", "semg", "肌肉疲劳")):
        terms.extend(["surface EMG", "muscle fatigue", "RMS", "median frequency"])
    elif any(token in text for token in ("速度", "加速度", "视觉", "动作", "标准", "次数")):
        terms.extend(["velocity loss", "neuromuscular fatigue"])
    elif any(token in text for token in ("膝盖", "疼", "痛", "酸")):
        terms.extend(["knee pain", "exercise"])
    elif any(token in text for token in ("计划", "强度", "训练")):
        terms.extend(["ACSM", "exercise prescription"])
    terms.append(_safe_text(query))
    seen = []
    for term in terms:
        term = _clean_space(term)
        if term and term not in seen:
            seen.append(term)
    return " ".join(seen)[:260]


def _request_json(url, timeout_s=DEFAULT_TIMEOUT_S, headers=None):
    req_headers = {"User-Agent": USER_AGENT}
    if isinstance(headers, dict):
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(req, timeout=float(timeout_s or DEFAULT_TIMEOUT_S)) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw)


def _doi_from_value(value):
    text = _safe_text(value).strip()
    if not text:
        return ""
    text = re.sub(r"^doi:\s*", "", text, flags=re.I)
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    return text.strip()


def _hit_id(prefix, title, url):
    seed = ("%s|%s|%s" % (prefix, title, url)).encode("utf-8")
    return "%s:%s" % (prefix, hashlib.sha1(seed).hexdigest()[:12])


def _normalise_hit(provider, title, source="", year=None, url="", doi="",
                   pmid="", snippet="", raw_id="", retrieved_at=None):
    title = _clean_space(title)
    if not title:
        return None
    doi = _doi_from_value(doi)
    pmid = _safe_text(pmid).strip()
    url = _safe_text(url).strip()
    if not url and pmid:
        url = "https://pubmed.ncbi.nlm.nih.gov/%s/" % pmid
    if not url and doi:
        url = "https://doi.org/%s" % doi
    try:
        year = int(str(year)[:4])
    except Exception:
        year = None
    source_name = _clean_space(source) or provider
    ident = _safe_text(raw_id).strip() or _hit_id(provider.lower().replace(" ", "_"), title, url)
    return {
        "id": ident,
        "title": title,
        "source": provider,
        "venue": source_name,
        "year": year,
        "url": url,
        "doi": doi,
        "pmid": pmid,
        "abstract_or_snippet": _clean_space(snippet)[:700],
        "retrieved_at": float(retrieved_at if retrieved_at is not None else time.time()),
    }


def _abstract_from_openalex_index(index):
    if not isinstance(index, dict):
        return ""
    words = []
    for word, positions in index.items():
        if not isinstance(positions, list):
            continue
        for pos in positions:
            try:
                words.append((int(pos), _safe_text(word)))
            except Exception:
                pass
    words.sort(key=lambda item: item[0])
    return " ".join(word for _pos, word in words)


def search_pubmed(query, limit=DEFAULT_LIMIT, timeout_s=DEFAULT_TIMEOUT_S):
    term = _query_terms(query)
    limit = _coerce_limit(limit)
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "retmax": str(limit),
        "sort": "relevance",
        "tool": "IronBuddy",
    }
    email = _env_first("NCBI_EMAIL", "IRONBUDDY_CONTACT_EMAIL")
    if email:
        params["email"] = email
    api_key = _env_first("NCBI_API_KEY", "PUBMED_API_KEY")
    if api_key:
        params["api_key"] = api_key
    params = urllib.parse.urlencode(params)
    search_data = _request_json(base + "esearch.fcgi?" + params, timeout_s=timeout_s)
    ids = (((search_data or {}).get("esearchresult") or {}).get("idlist") or [])[:limit]
    if not ids:
        return []
    summary_params = urllib.parse.urlencode({
        "db": "pubmed",
        "id": ",".join(ids),
        "retmode": "json",
    })
    data = _request_json(base + "esummary.fcgi?" + summary_params, timeout_s=timeout_s)
    result = (data or {}).get("result") or {}
    hits = []
    for pmid in ids:
        item = result.get(str(pmid)) or {}
        if not isinstance(item, dict):
            continue
        doi = ""
        for aid in item.get("articleids") or []:
            if isinstance(aid, dict) and str(aid.get("idtype") or "").lower() == "doi":
                doi = aid.get("value") or ""
                break
        hits.append(_normalise_hit(
            "PubMed",
            item.get("title"),
            source=item.get("source") or "PubMed",
            year=item.get("pubdate"),
            doi=doi or item.get("elocationid", ""),
            pmid=pmid,
            raw_id="pubmed:%s" % pmid,
        ))
    return [hit for hit in hits if hit is not None]


def search_openalex(query, limit=DEFAULT_LIMIT, timeout_s=DEFAULT_TIMEOUT_S):
    term = _query_terms(query)
    params = {
        "search": term,
        "per-page": str(_coerce_limit(limit)),
    }
    api_key = _env_first("OPENALEX_API_KEY")
    if api_key:
        params["api_key"] = api_key
    params = urllib.parse.urlencode(params)
    data = _request_json("https://api.openalex.org/works?" + params, timeout_s=timeout_s)
    hits = []
    for item in (data or {}).get("results") or []:
        if not isinstance(item, dict):
            continue
        location = item.get("primary_location") or {}
        source = (location.get("source") or {}) if isinstance(location, dict) else {}
        hits.append(_normalise_hit(
            "OpenAlex",
            item.get("display_name") or item.get("title"),
            source=source.get("display_name") if isinstance(source, dict) else "",
            year=item.get("publication_year"),
            url=(location.get("landing_page_url") if isinstance(location, dict) else "") or item.get("id"),
            doi=item.get("doi"),
            snippet=_abstract_from_openalex_index(item.get("abstract_inverted_index")),
            raw_id="openalex:%s" % _safe_text(item.get("id")).rsplit("/", 1)[-1],
        ))
    return [hit for hit in hits if hit is not None]


def search_crossref(query, limit=DEFAULT_LIMIT, timeout_s=DEFAULT_TIMEOUT_S):
    term = _query_terms(query)
    params = {
        "query": term,
        "rows": str(_coerce_limit(limit)),
        "select": "DOI,title,container-title,published-print,published-online,URL,abstract",
    }
    mailto = _env_first("CROSSREF_MAILTO", "IRONBUDDY_CONTACT_EMAIL")
    if mailto:
        params["mailto"] = mailto
    headers = {}
    plus_key = _env_first("CROSSREF_PLUS_API_KEY", "CROSSREF_API_KEY")
    if plus_key:
        headers["Crossref-Plus-API-Token"] = "Bearer " + plus_key
    data = _request_json(
        "https://api.crossref.org/works?" + urllib.parse.urlencode(params),
        timeout_s=timeout_s,
        headers=headers,
    )
    hits = []
    for item in (((data or {}).get("message") or {}).get("items") or []):
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or [""])[0] if isinstance(item.get("title"), list) else item.get("title")
        venue = ""
        if isinstance(item.get("container-title"), list) and item.get("container-title"):
            venue = item.get("container-title")[0]
        year = None
        for key in ("published-print", "published-online"):
            parts = ((item.get(key) or {}).get("date-parts") or [])
            if parts and parts[0]:
                year = parts[0][0]
                break
        doi = item.get("DOI") or ""
        hits.append(_normalise_hit(
            "Crossref",
            title,
            source=venue or "Crossref",
            year=year,
            url=item.get("URL"),
            doi=doi,
            snippet=re.sub(r"<[^>]+>", " ", _safe_text(item.get("abstract"))),
            raw_id="crossref:%s" % doi if doi else "",
        ))
    return [hit for hit in hits if hit is not None]


def search_semantic_scholar(query, limit=DEFAULT_LIMIT, timeout_s=DEFAULT_TIMEOUT_S):
    term = _query_terms(query)
    params = urllib.parse.urlencode({
        "query": term,
        "limit": str(_coerce_limit(limit)),
        "fields": "title,year,venue,url,abstract,externalIds",
    })
    headers = {}
    api_key = _env_first("SEMANTIC_SCHOLAR_API_KEY", "S2_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    data = _request_json(
        "https://api.semanticscholar.org/graph/v1/paper/search?" + params,
        timeout_s=timeout_s,
        headers=headers,
    )
    hits = []
    for item in (data or {}).get("data") or []:
        if not isinstance(item, dict):
            continue
        ext = item.get("externalIds") or {}
        hits.append(_normalise_hit(
            "Semantic Scholar",
            item.get("title"),
            source=item.get("venue") or "Semantic Scholar",
            year=item.get("year"),
            url=item.get("url"),
            doi=ext.get("DOI") if isinstance(ext, dict) else "",
            pmid=ext.get("PubMed") if isinstance(ext, dict) else "",
            snippet=item.get("abstract"),
            raw_id="semantic:%s" % _safe_text(item.get("paperId")),
        ))
    return [hit for hit in hits if hit is not None]


PROVIDER_FUNCS = {
    "pubmed": search_pubmed,
    "openalex": search_openalex,
    "crossref": search_crossref,
    "semantic_scholar": search_semantic_scholar,
}


def _read_cache(path):
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}


def _write_cache(path, cache):
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, ensure_ascii=False, sort_keys=True)
        os.rename(tmp, path)
    except Exception:
        pass


def _cache_key(query, providers, limit):
    raw = json.dumps({
        "q": _safe_text(query).strip(),
        "providers": list(providers or []),
        "limit": int(limit),
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _dedupe_hits(hits, limit):
    seen = set()
    out = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        key = (hit.get("doi") or hit.get("pmid") or hit.get("url") or hit.get("title") or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(hit)
        if len(out) >= limit:
            break
    return out


def build_online_context(query, hits, max_chars=900):
    lines = []
    total = 0
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        parts = [
            "[%s]" % _safe_text(hit.get("source") or "Online"),
            _safe_text(hit.get("title") or "Untitled"),
        ]
        if hit.get("year"):
            parts.append("(%s)" % hit.get("year"))
        if hit.get("doi"):
            parts.append("DOI:%s" % hit.get("doi"))
        if hit.get("pmid"):
            parts.append("PMID:%s" % hit.get("pmid"))
        if hit.get("url"):
            parts.append(_safe_text(hit.get("url")))
        line = "- " + " ".join(parts)
        if hit.get("abstract_or_snippet"):
            line += " :: " + _safe_text(hit.get("abstract_or_snippet"))[:240]
        if total + len(line) > int(max_chars):
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    return "在线专业知识库命中：\n" + "\n".join(lines)


def search_online_knowledge(query, limit=DEFAULT_LIMIT, cache_path=DEFAULT_CACHE_PATH,
                            cache_ttl_s=DEFAULT_CACHE_TTL_S,
                            timeout_s=DEFAULT_TIMEOUT_S,
                            providers=DEFAULT_PROVIDERS):
    raw_query = _safe_text(query).strip()
    limit = _coerce_limit(limit)
    providers = tuple(providers or DEFAULT_PROVIDERS)
    if not raw_query:
        return {
            "ok": False,
            "source_mode": "online",
            "reason": "empty_query",
            "message": "在线知识库查询为空",
            "query": raw_query,
            "hits": [],
            "context": "",
        }

    now = time.time()
    key = _cache_key(raw_query, providers, limit)
    cache = _read_cache(cache_path)
    cached = cache.get(key) if isinstance(cache, dict) else None
    if isinstance(cached, dict):
        try:
            age = now - float(cached.get("cached_at") or 0.0)
        except Exception:
            age = 999999.0
        if age <= float(cache_ttl_s or 0.0):
            result = dict(cached.get("result") or {})
            result["cache_hit"] = True
            return result

    hits = []
    errors = []
    for provider in providers:
        func = PROVIDER_FUNCS.get(provider)
        if func is None:
            continue
        try:
            hits.extend(func(raw_query, limit=limit, timeout_s=timeout_s))
        except Exception as exc:
            errors.append({
                "provider": provider,
                "error": type(exc).__name__ + ":" + _safe_text(exc)[:160],
            })
        if len(hits) >= limit:
            break

    hits = _dedupe_hits(hits, limit)
    ok = bool(hits)
    result = {
        "ok": ok,
        "source_mode": "online",
        "reason": "rag_hit" if ok else "online_unavailable",
        "message": "在线知识库已命中" if ok else "在线知识库不可用",
        "query": raw_query,
        "hits": hits,
        "context": build_online_context(raw_query, hits),
        "errors": errors,
        "retrieved_at": now,
        "cache_hit": False,
    }
    if ok:
        cache[key] = {"cached_at": now, "result": result}
        _write_cache(cache_path, cache)
    return result


__all__ = [
    "DEFAULT_CACHE_PATH",
    "build_online_context",
    "search_online_knowledge",
    "search_pubmed",
    "search_openalex",
    "search_crossref",
    "search_semantic_scholar",
]
