"""Lightweight IronBuddy local rules and manual retrieval.

This is intentionally stdlib-only and Python 3.7 compatible. It is a
local rules/manual layer: short IronBuddy operation cards are retrieved by
keyword overlap. Professional fitness, fatigue, and sEMG knowledge is handled
by the online RAG layer, not by local JSON files.
"""

from __future__ import absolute_import

import json
import os
import re
import time


_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_KB_DIR = os.path.join(_PROJECT_ROOT, "data", "coach_kb")
_KB_FILES = ("ironbuddy_manual.json",)
_CACHE = {"mtime_key": None, "items": None}


_CAPABILITY_HINTS = (
    u"你能做什么", u"有什么功能", u"介绍功能", u"简要介绍", u"使用手册",
    u"怎么用", u"如何使用", u"使用方法", u"功能介绍", u"说明一下",
    u"你会什么", u"有哪些功能", u"操作手册", u"介绍一下自己",
    u"自我介绍", u"你是谁", u"你能帮我什么", u"你有什么用",
    u"介绍你的功能", u"介绍一下功能", u"你有什么功能", u"有什么用",
    u"你能干嘛", u"能干嘛", u"你是干什么的", u"怎么使用你",
    u"使用说明", u"介绍你自己", u"介绍自己",
)

_GUIDE_HINTS = (
    u"怎么切", u"如何切", u"怎么切换", u"如何切换", u"怎么进入",
    u"如何进入", u"怎么开始", u"怎么推送", u"如何推送", u"怎么静音",
    u"怎么解除", u"怎么测试", u"怎么校准", u"命令", u"口令",
)


def _safe_lower(text):
    try:
        return (text or u"").lower()
    except Exception:
        return u""


def _normalise_intent_text(text):
    text = _safe_lower(text)
    for token in (
        u"教练", u"叫练", u"交练", u"焦练",
        u"请", u"帮我", u"给我", u"麻烦", u"简要", u"简单", u"一下",
    ):
        text = text.replace(token, u"")
    text = re.sub(r"[\s,，。.!！?？:：;；、\"“”'‘’]+", u"", text)
    return text


def _tokenize(text):
    """Tokenize Chinese/English text with a tiny, robust heuristic."""
    text = _safe_lower(text)
    tokens = set()
    for token in re.findall(r"[a-zA-Z0-9_]+", text):
        if len(token) >= 2:
            tokens.add(token)
    # Chinese keyword retrieval works better with overlapping 2-char grams.
    chars = [c for c in text if u"\u4e00" <= c <= u"\u9fff"]
    for i in range(0, max(0, len(chars) - 1)):
        tokens.add(u"".join(chars[i:i + 2]))
    for i in range(0, max(0, len(chars) - 2)):
        tokens.add(u"".join(chars[i:i + 3]))
    return tokens


def _load_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data.get("items", [])
        if isinstance(data, list):
            return data
    except Exception:
        return []
    return []


def _normalise_item(raw, source):
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or raw.get("id") or "").strip()
    answer = str(raw.get("answer") or raw.get("content") or "").strip()
    if not title or not answer:
        return None
    keywords = raw.get("keywords") or []
    if not isinstance(keywords, list):
        keywords = []
    tags = raw.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    text = u" ".join([title, answer] + [str(x) for x in keywords] + [str(x) for x in tags])
    return {
        "id": str(raw.get("id") or title),
        "title": title,
        "answer": answer,
        "keywords": [str(x) for x in keywords],
        "tags": [str(x) for x in tags],
        "source": source,
        "_tokens": _tokenize(text),
    }


def _mtime_key():
    key = []
    for name in _KB_FILES:
        path = os.path.join(_KB_DIR, name)
        try:
            st = os.stat(path)
            key.append("%s:%d:%d" % (name, int(st.st_mtime), int(st.st_size)))
        except OSError:
            key.append("%s:missing" % name)
    return "|".join(key)


def load_knowledge_items(force=False):
    key = _mtime_key()
    if (not force) and _CACHE.get("items") is not None and _CACHE.get("mtime_key") == key:
        return list(_CACHE.get("items") or [])
    items = []
    for name in _KB_FILES:
        path = os.path.join(_KB_DIR, name)
        for raw in _load_json_file(path):
            item = _normalise_item(raw, name)
            if item is not None:
                items.append(item)
    _CACHE["mtime_key"] = key
    _CACHE["items"] = items
    return list(items)


def is_capability_question(text):
    text = _normalise_intent_text(text)
    return any(h in text for h in _CAPABILITY_HINTS)


def is_manual_question(text):
    text = _normalise_intent_text(text)
    return is_capability_question(text) or any(h in text for h in _GUIDE_HINTS)


def get_capabilities():
    """Return user-facing coach capabilities as stable short lines."""
    return [
        u"视觉系统会观察深蹲和弯举过程，及时发现动作幅度、节奏和姿态问题。",
        u"结合传感数据后，系统能判断发力变化、疲劳趋势和可能的代偿。",
        u"训练过程中可以询问动作、膝盖不适、疲劳和训练安排，系统会保留训练记录。",
        u"训练结束后，关键数据和建议可以整理成飞书卡片，方便复盘。",
    ]


def format_capability_reply(max_items=5):
    items = get_capabilities()[:max_items]
    return u"我是 IronBuddy 智能健身伙伴。" + u" ".join(items)


def format_manual_reply(query, max_hits=3):
    """Fast usage-manual answer for demo commands without waiting for DeepSeek."""
    query = query or u""
    hits = search_knowledge(query, limit=max_hits)
    if not hits:
        return format_capability_reply(max_items=5)
    parts = [u"可以这样用："]
    for hit in hits:
        answer = hit.get("answer", "")
        if answer:
            parts.append(answer)
    # Keep TTS readable; details can still be inspected through rag_query.
    return u" ".join(parts)[:180]


def search_knowledge(query, limit=3):
    """Return ranked KB hits for query.

    Result entries are serialisable dicts without private token fields.
    """
    try:
        limit = int(limit)
    except Exception:
        limit = 3
    limit = max(1, min(8, limit))
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []
    scored = []
    for item in load_knowledge_items():
        tokens = item.get("_tokens") or set()
        overlap = q_tokens.intersection(tokens)
        score = len(overlap)
        title = item.get("title", "")
        for kw in item.get("keywords") or []:
            if kw and kw in (query or ""):
                score += 4
        if title and title in (query or ""):
            score += 5
        if score <= 0:
            continue
        public = {}
        for key in ("id", "title", "answer", "keywords", "tags", "source"):
            public[key] = item.get(key)
        public["score"] = score
        scored.append(public)
    scored.sort(key=lambda x: (-int(x.get("score", 0)), str(x.get("id", ""))))
    return scored[:limit]


def build_rag_context(query, limit=3, max_chars=480):
    hits = search_knowledge(query, limit=limit)
    lines = []
    total = 0
    for hit in hits:
        line = u"- %s：%s" % (hit.get("title", ""), hit.get("answer", ""))
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return u""
    return u"IronBuddy 本地规则/手册命中：\n" + u"\n".join(lines)


def status_snapshot():
    items = load_knowledge_items()
    return {
        "ok": True,
        "role": "local_rules_and_manual_only",
        "kb_dir": _KB_DIR,
        "files": list(_KB_FILES),
        "item_count": len(items),
        "capability_count": len(get_capabilities()),
        "ts": time.time(),
    }
