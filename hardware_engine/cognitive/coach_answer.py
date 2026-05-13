# -*- coding: utf-8 -*-
"""Domain formatting layer for IronBuddy coach answers.

This module is intentionally stdlib-only and Python 3.7 compatible. It does
not retrieve evidence or call LLM/network services; it only turns existing
RAG/training context into bounded, user-facing coaching sections.
"""

from __future__ import absolute_import

import re


_RISK_HINTS = (
    u"疼", u"疼痛", u"痛", u"刺痛", u"酸痛", u"不适", u"受伤", u"拉伤",
    u"扭伤", u"肿", u"麻", u"膝盖", u"膝", u"腰", u"肩", u"肘", u"头晕",
    u"胸闷",
)
_PLAN_HINTS = (
    u"计划", u"安排", u"怎么练", u"如何练", u"训练", u"组", u"次数",
    u"重量", u"强度", u"加重量", u"减重量", u"目标",
)
_FATIGUE_HINTS = (
    u"疲劳", u"累", u"力竭", u"没力", u"乏力", u"发力下降", u"速度下降",
    u"代偿",
)
_RECOVERY_HINTS = (
    u"恢复", u"休息", u"拉伸", u"睡眠", u"明天", u"热身", u"放松",
)


def _safe_text(value):
    if value is None:
        return u""
    try:
        return str(value)
    except Exception:
        return u""


def _clean_space(value):
    return re.sub(r"\s+", u" ", _safe_text(value)).strip()


def _contains_any(text, hints):
    text = _safe_text(text).lower()
    return any(hint.lower() in text for hint in hints)


def classify_question(query):
    """Classify a user query into the coach answer domain."""
    text = _safe_text(query).lower()
    if _contains_any(text, _RISK_HINTS):
        return "risk"
    if _contains_any(text, _FATIGUE_HINTS):
        return "fatigue"
    if _contains_any(text, _RECOVERY_HINTS):
        return "recovery"
    if _contains_any(text, _PLAN_HINTS):
        return "plan"
    return "general"


def _hits_from_rag(rag_result):
    if isinstance(rag_result, dict):
        hits = rag_result.get("hits") or []
    elif isinstance(rag_result, list):
        hits = rag_result
    else:
        hits = []
    return [hit for hit in hits if isinstance(hit, dict)]


def _compact_sources(rag_result, limit=3):
    sources = []
    for hit in _hits_from_rag(rag_result)[:limit]:
        source = {
            "id": _clean_space(hit.get("id") or hit.get("evidence_id")),
            "title": _clean_space(hit.get("title")),
            "source": _clean_space(hit.get("source")),
            "venue": _clean_space(hit.get("venue")),
            "year": hit.get("year"),
            "url": _clean_space(hit.get("url")),
        }
        doi = _clean_space(hit.get("doi"))
        pmid = _clean_space(hit.get("pmid"))
        if doi:
            source["doi"] = doi
        if pmid:
            source["pmid"] = pmid
        sources.append(source)
    return sources


def _evidence_ids(sources):
    ids = []
    for source in sources:
        ident = _clean_space(source.get("id"))
        if ident:
            ids.append(ident)
    return ids


def _source_mode(rag_result):
    if isinstance(rag_result, dict):
        return _clean_space(rag_result.get("source_mode"))
    return u""


def _exercise_label(query, training_context):
    if isinstance(training_context, dict):
        label = _clean_space(training_context.get("exercise_label"))
        if label:
            return label
        exercise = _clean_space(training_context.get("exercise"))
        if exercise in ("bicep_curl", "curl"):
            return u"哑铃弯举"
        if exercise == "squat":
            return u"深蹲"
    text = _safe_text(query)
    if u"弯举" in text or "curl" in text.lower():
        return u"哑铃弯举"
    if u"深蹲" in text or "squat" in text.lower():
        return u"深蹲"
    return u"当前动作"


def _has_sources(sources):
    return bool([s for s in sources if s.get("title") or s.get("url")])


def _build_sections(answer_type, query, sources, training_context):
    exercise = _exercise_label(query, training_context)
    evidence_phrase = (
        u"结合当前训练状态和可用专业证据"
        if _has_sources(sources)
        else u"基于当前训练问题和通用训练原则"
    )

    if answer_type == "risk":
        return {
            "conclusion": u"先把安全放在第一位，疼痛或刺痛出现时不建议硬撑完成本组。",
            "recommendation": u"立即降低强度或暂停相关动作，检查动作轨迹、关节位置和热身是否充分。",
            "reason": u"%s，疼痛常提示负荷、动作控制或恢复状态不匹配。" % evidence_phrase,
            "risk": u"如果疼痛持续、加重、伴随肿胀/麻木，或影响日常活动，应停止训练并咨询医生或运动康复专业人员。",
            "next_step": u"下一步先做无痛活动度测试；能无痛完成后，再用更轻重量和更慢节奏恢复训练。",
        }

    if answer_type == "plan":
        return {
            "conclusion": u"今天的训练应以可完成、可控制、可复盘为主。",
            "recommendation": u"先做热身，再进行%s 3 组左右，每组保留 1-3 次余力；动作质量下降时停止加量。" % exercise,
            "reason": u"%s，稳定的组数、次数和疲劳阈值比临时冲重量更适合持续进步。" % evidence_phrase,
            "risk": u"若出现明显疼痛、头晕或动作失控，不要继续追求目标次数。",
            "next_step": u"下一步记录每组完成次数、失败次数和疲劳变化，再决定下一组是否加量或减量。",
        }

    if answer_type == "fatigue":
        return {
            "conclusion": u"疲劳上升时，训练目标应从冲次数切回保持动作质量。",
            "recommendation": u"先降低节奏或重量，优先让每次动作幅度完整、发力路径稳定。",
            "reason": u"%s，速度下降、代偿增加和主观乏力都可能说明当前组接近疲劳上限。" % evidence_phrase,
            "risk": u"疲劳状态下继续硬撑会增加动作变形和关节压力。",
            "next_step": u"下一步比较上一组与当前组的完成质量；若连续下降，就延长休息或结束训练。",
        }

    if answer_type == "recovery":
        return {
            "conclusion": u"恢复安排要服务下一次训练质量，而不是只看今天是否还能继续。",
            "recommendation": u"先补水、轻量活动和睡眠，酸胀明显时把下一次训练强度下调一级。",
            "reason": u"%s，恢复不足会让同样负荷产生更高疲劳和更差动作控制。" % evidence_phrase,
            "risk": u"如果酸痛变成尖锐疼痛或单侧关节痛，应按风险问题处理并暂停相关动作。",
            "next_step": u"下一步在明天训练前重新评估疼痛、活动度和热身后的动作稳定性。",
        }

    return {
        "conclusion": u"可以先用一个小目标回答这个训练问题。",
        "recommendation": u"先明确动作、目标次数和当前不适/疲劳状态，再选择保守的下一组安排。",
        "reason": u"%s，训练建议需要同时看目标、动作质量和身体反馈。" % evidence_phrase,
        "risk": u"任何疼痛、头晕或动作失控都应优先停止并重新评估。",
        "next_step": u"下一步补充动作名称、目标和当前感受，我再给出更具体的训练建议。",
    }


def _format_user_text(sections):
    ordered = ("conclusion", "recommendation", "reason", "risk", "next_step")
    lines = []
    for key in ordered:
        value = _clean_space(sections.get(key))
        if value:
            lines.append(u"%s: %s" % (key, value))
    return u"\n".join(lines)


def build_coach_answer(query, rag_result=None, training_context=None,
                       llm_text=None):
    """Build a bounded coaching answer from already-retrieved context."""
    answer_type = classify_question(query)
    sources = _compact_sources(rag_result)
    sections = _build_sections(answer_type, query, sources, training_context)
    user_text = _format_user_text(sections)

    debug = {
        "source_mode": _source_mode(rag_result),
        "evidence_ids": _evidence_ids(sources),
        "llm_text_present": bool(_clean_space(llm_text)),
    }
    result = {
        "ok": True,
        "answer_type": answer_type,
        "conclusion": sections["conclusion"],
        "recommendation": sections["recommendation"],
        "reason": sections["reason"],
        "risk": sections["risk"],
        "next_step": sections["next_step"],
        "user_text": user_text,
        "sources": sources,
        "debug": debug,
    }
    return result
