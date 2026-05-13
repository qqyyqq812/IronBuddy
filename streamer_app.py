"""
IronBuddy 推流中台 v3 — 精简重写版
剔除 ASR/Microphone/Audio 全部不可用模块，专注视频推流 + FSM 状态 + DeepSeek 教练
V3.1: + 管理面板 (/admin)
"""
import os
import json
import time
import base64
import math
# V7.37: force CST (Asia/Shanghai) so all UI-facing time strings match the
# daemon (which is started by systemd with the same TZ). Board defaults UTC.
os.environ.setdefault("TZ", "Asia/Shanghai")
try:
    time.tzset()
except Exception:
    pass
import io
import logging  # V7.21 (2026-04-21): 补 import —— 原代码未导入, feishu_smart_push 降级路径炸 NameError → 500
import fcntl
import sqlite3
import subprocess
import threading
import traceback
import glob as glob_mod
import re
import shlex
from urllib.parse import urlsplit, urlunsplit
import requests
from flask import Flask, Response, request, redirect

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
try:
    from hardware_engine import rag_delivery, training_plan, training_report
except Exception as _training_helper_exc:
    logging.warning("[training_helpers] import failed: %s", _training_helper_exc)
    rag_delivery = None
    training_plan = None
    training_report = None
try:
    from hardware_engine.cognitive import adp_knowledge
except Exception as _adp_kb_exc:
    logging.warning("[adp_knowledge] import failed: %s", _adp_kb_exc)
    adp_knowledge = None
try:
    from hardware_engine.cognitive import online_knowledge
except Exception as _online_kb_exc:
    logging.warning("[online_knowledge] import failed: %s", _online_kb_exc)
    online_knowledge = None
try:
    from hardware_engine.cognitive import vector_knowledge
except Exception as _vector_kb_exc:
    logging.warning("[vector_knowledge] import failed: %s", _vector_kb_exc)
    vector_knowledge = None
try:
    from hardware_engine.cognitive.coach_knowledge import (
        build_rag_context,
        format_capability_reply,
        format_manual_reply,
        get_capabilities,
        is_manual_question,
        search_knowledge,
        status_snapshot as coach_kb_status,
    )
except Exception as _coach_kb_exc:
    logging.warning("[coach_kb] import failed: %s", _coach_kb_exc)

    def build_rag_context(query, limit=3, max_chars=480):
        return ""

    def format_capability_reply(max_items=5):
        return "我是 IronBuddy 智能健身伙伴，可以指导深蹲、弯举、动作纠正和飞书训练总结。"

    def format_manual_reply(query, max_hits=3):
        return format_capability_reply(max_items=5)

    def get_capabilities():
        return []

    def is_manual_question(text):
        return False

    def search_knowledge(query, limit=3):
        return []

    def coach_kb_status():
        return {"ok": False, "error": "coach_kb import failed"}

try:
    from hardware_engine.integrations.feishu_client import FeishuClient
except Exception as _feishu_client_exc:
    logging.warning("[feishu_client] import failed: %s", _feishu_client_exc)
    FeishuClient = None

template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templates'))
app = Flask(__name__, template_folder=template_dir)

# V7.21: 全局异常兜底 —— 任何未捕获异常都返回 JSON, 绝不落到 Flask 原生 HTML 500
@app.errorhandler(Exception)
def _json_error_handler(e):
    logging.error("[Unhandled] %s: %s\n%s", type(e).__name__, e, traceback.format_exc())
    body = json.dumps({"ok": False, "error": type(e).__name__, "detail": str(e)[:300]}, ensure_ascii=False)
    return Response(body, status=500, mimetype='application/json')

# V7.21: 飞书推送互斥锁 —— 防止并发/重复点击把后端挤爆
_FEISHU_PUSH_LOCK = threading.Lock()
_FEISHU_PUSH_STARTED_AT = [0.0]
CHAT_EVENTS_FILE = "/dev/shm/chat_events.jsonl"
CHAT_EVENTS_SEQ_FILE = "/dev/shm/chat_events.seq"
TRAINING_PLAN_PATH = (
    training_plan.DEFAULT_STATE_PATH
    if training_plan is not None else
    ("/dev/shm/ironbuddy_training_plan.json"
     if os.path.isdir("/dev/shm") else "/tmp/ironbuddy_training_plan.json")
)
TRAINING_SESSION_PATH = "/dev/shm/ironbuddy_training_session.json"
PLAN_MIN_FATIGUE_TARGET = (
    training_plan.MIN_FATIGUE_TARGET if training_plan is not None else 300
)
PLAN_MAX_FATIGUE_TARGET = (
    training_plan.MAX_FATIGUE_TARGET if training_plan is not None else 1500
)
PLAN_DEFAULT_FATIGUE_TARGET = (
    training_plan.DEFAULT_FATIGUE_TARGET if training_plan is not None else 600
)
PLAN_RECOVERY_FATIGUE_TARGET = (
    training_plan.RECOVERY_FATIGUE_TARGET if training_plan is not None else 450
)
PLAN_ADVANCED_FATIGUE_TARGET = (
    training_plan.ADVANCED_FATIGUE_TARGET if training_plan is not None else 750
)
PLAN_FATIGUE_TARGET_STEP = (
    training_plan.FATIGUE_TARGET_STEP if training_plan is not None else 100
)
RAG_DELIVERY_PATH = (
    rag_delivery.DEFAULT_RUNTIME_PATH
    if rag_delivery is not None else
    ("/dev/shm/ironbuddy_rag_delivery.json"
     if os.path.isdir("/dev/shm") else "/tmp/ironbuddy_rag_delivery.json")
)
DAILY_PLAN_PATH = (
    "/dev/shm/ironbuddy_daily_plan.json"
    if os.path.isdir("/dev/shm") else "/tmp/ironbuddy_daily_plan.json"
)
OPERATOR_RECORD_ROOT = os.path.join(
    PROJECT_ROOT, "docs", "test_runs", "ironbuddy_operator"
)


def _atomic_write_json_file(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    tmp = "%s.tmp.%s" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, sort_keys=True)
    os.replace(tmp, path)


def _append_chat_event(role, text, kind="api_status", stage="assistant_reply"):
    if not text:
        return 0
    try:
        lock_path = CHAT_EVENTS_FILE + ".lock"
        lock_f = open(lock_path, "a")
        try:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            seq = _read_chat_event_seq() + 1
            payload = {
                "seq": seq,
                "ts": time.time(),
                "turn_id": "",
                "role": role,
                "kind": kind,
                "stage": stage,
                "text": str(text).strip(),
            }
            with open(CHAT_EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            _atomic_write_json_file(CHAT_EVENTS_SEQ_FILE + ".json", {"seq": seq})
            with open(CHAT_EVENTS_SEQ_FILE + ".tmp", "w") as sf:
                sf.write(str(seq))
            os.rename(CHAT_EVENTS_SEQ_FILE + ".tmp", CHAT_EVENTS_SEQ_FILE)
            return seq
        finally:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
                lock_f.close()
            except Exception:
                pass
    except Exception as exc:
        logging.debug("append chat event failed: %s", exc)
    return 0


def _log_real_voice_session(trigger_src, transcript, response,
                            duration_s=0.0, summary=None):
    """Best-effort real conversation persistence for dashboard/database views."""
    try:
        db = _get_db()
        if db is not None:
            db.log_voice_session(
                trigger_src=trigger_src or "chat",
                transcript=transcript or "",
                response=response or "",
                summary=summary,
                duration_s=float(duration_s or 0.0),
            )
            return True
    except Exception as exc:
        logging.debug("voice session persist skipped: %s", exc)
    return False


def _log_real_llm_event(trigger, prompt, response, tokens_in=0, tokens_out=0):
    """Best-effort SQLite LLM trace; never affects runtime responses."""
    try:
        db = _get_db()
        if db is not None:
            db.log_llm(
                trigger or "ui_event",
                prompt or "",
                response or "",
                int(tokens_in or 0),
                int(tokens_out or 0),
            )
            return True
    except Exception as exc:
        logging.debug("llm event persist skipped: %s", exc)
    return False


def _read_tts_volume(default=7):
    try:
        if os.path.exists("/dev/shm/tts_volume.json"):
            with open("/dev/shm/tts_volume.json", "r", encoding="utf-8") as f:
                return max(1, min(15, int(json.load(f).get("vol", default))))
    except Exception:
        pass
    return default


def _volume_to_percent(vol):
    vol = max(1, min(15, int(vol)))
    return max(5, min(100, int(round((vol / 15.0) * 100))))


def _apply_tts_mixer(vol=None, muted=False):
    if vol is None:
        vol = _read_tts_volume()
    vol = max(1, min(15, int(vol)))
    percent = 0 if muted else _volume_to_percent(vol)
    result = {}
    try:
        subprocess.run(
            ["sudo", "-n", "amixer", "-c", "0", "cset",
             "numid=1,iface=MIXER,name=Playback Path", "2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2, check=False)
    except Exception:
        pass
    for ctrl in ("Playback", "Speaker", "Master", "Headphone"):
        cmd = ["sudo", "-n", "amixer", "-c", "0", "sset", ctrl, "%d%%" % percent]
        if muted:
            cmd.append("mute")
        else:
            cmd.append("unmute")
        try:
            ret = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=2,
                                 check=False)
            result[ctrl] = int(getattr(ret, "returncode", 1))
        except Exception:
            result[ctrl] = -1
    return {"vol": vol, "percent": percent, "muted": muted, "result": result}

# ===== JPEG 压缩配置 =====
SNAPSHOT_QUALITY = 65       # JPEG 质量 (1-100)，65 约 35-40KB（保持文字清晰）
_snapshot_last_mtime = 0    # 帧去重：上次文件修改时间
_snapshot_cache = b''       # 帧去重：缓存压缩结果


@app.route('/')
def index():
    """主页 — 直接读文件返回，绕过 Jinja2 模板缓存"""
    try:
        html_path = os.path.join(template_dir, 'index.html')
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        resp = Response(html_content, mimetype='text/html')
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    except Exception as e:
        return f"<h1>模板加载失败</h1><p>{e}</p>", 500

@app.route('/manifest.json')
def pwa_manifest():
    """PWA manifest for standalone app experience."""
    manifest = {
        "name": "IronBuddy",
        "short_name": "IronBuddy",
        "start_url": "/",
        "display": "standalone",
        "orientation": "landscape",
        "background_color": "#0a0e17",
        "theme_color": "#0a0e17",
        "icons": []
    }
    return Response(json.dumps(manifest), mimetype='application/json')


@app.route('/snapshot')
def snapshot():
    """核心管线：cv2 压缩 JPEG (97KB→~15KB) + 帧去重"""
    global _snapshot_last_mtime, _snapshot_cache
    try:
        st = os.stat("/dev/shm/result.jpg")

        # 帧去重：文件未变化则直接返回缓存
        if st.st_mtime_ns == _snapshot_last_mtime and _snapshot_cache:
            resp = Response(_snapshot_cache, mimetype='image/jpeg')
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            resp.headers['Content-Length'] = str(len(_snapshot_cache))
            return resp

        with open("/dev/shm/result.jpg", "rb") as f:
            raw = f.read()

        _snapshot_last_mtime = st.st_mtime_ns
        _snapshot_cache = raw

        resp = Response(raw, mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        resp.headers['Content-Length'] = str(len(raw))
        return resp
    except FileNotFoundError:
        return Response(b'', status=204)


# V2.2: MJPEG 流 — 浏览器原生解码，替代 JS 链式轮询
@app.route('/video_feed')
def video_feed():
    """MJPEG multipart stream backed by /dev/shm/result.jpg.

    This is the Flask fallback for the vision process' direct :8080 stream.
    Keep the polling interval close to the vision writer rate; a 0.1s sleep
    capped the fallback at 10fps and made demos look like the camera had died
    whenever the browser could not attach to :8080.
    """
    try:
        poll_interval = float(os.environ.get("IRONBUDDY_VIDEO_FEED_INTERVAL", "0.035"))
    except Exception:
        poll_interval = 0.035
    poll_interval = max(0.015, min(0.10, poll_interval))

    def gen_frames():
        last_mtime = 0
        last_yield_time = time.time()
        while True:
            try:
                st = os.stat("/dev/shm/result.jpg")
                if st.st_mtime_ns != last_mtime:
                    last_mtime = st.st_mtime_ns
                    with open("/dev/shm/result.jpg", "rb") as f:
                        raw = f.read()

                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n'
                           b'Content-Length: ' + str(len(raw)).encode() + b'\r\n\r\n' + raw + b'\r\n')
                    last_yield_time = time.time()
            except FileNotFoundError:
                pass
            # 10秒无新帧则终止流，让浏览器触发重连
            if time.time() - last_yield_time > 10.0:
                return
            time.sleep(poll_interval)

    resp = Response(gen_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/state_feed')
def state_feed():
    """FSM 深蹲状态（JSON）— 附加式合并 mute / fatigue_limit 字段（T2 voice UI）"""
    # The main UI polls this endpoint frequently. A tiny cache absorbs duplicate
    # browser tabs and bursty FORCE refreshes without hiding human-visible reps.
    now = time.time()
    cache = getattr(state_feed, "_cache", None)
    if cache and now - cache.get("ts", 0.0) < 0.12:
        return Response(cache.get("body", "{}"), mimetype='application/json')
    base = {"state": "NO_PERSON", "good": 0, "failed": 0, "angle": 0}
    try:
        if os.path.exists("/dev/shm/fsm_state.json"):
            with open("/dev/shm/fsm_state.json", "r") as f:
                base = json.loads(f.read())
    except Exception:
        pass
    # Merge mute state from voice daemon
    try:
        if os.path.exists("/dev/shm/mute_signal.json"):
            with open("/dev/shm/mute_signal.json", "r") as f:
                mute_data = json.loads(f.read())
                base["muted"] = bool(mute_data.get("muted", False))
    except Exception:
        pass
    if "muted" not in base:
        base["muted"] = False
    # Merge current TTS volume so the main UI can stay in sync without polling admin endpoints.
    try:
        if os.path.exists("/dev/shm/tts_volume.json"):
            with open("/dev/shm/tts_volume.json", "r") as f:
                vol_data = json.loads(f.read())
                base["tts_volume"] = int(vol_data.get("vol", 7))
    except Exception:
        pass
    if "tts_volume" not in base:
        base["tts_volume"] = 7
    # V7.6: fatigue_limit 来源优先级 FSM (最权威) > ui_fatigue_limit.json > Lane E 默认目标
    if "fatigue_limit" not in base:
        try:
            if os.path.exists("/dev/shm/ui_fatigue_limit.json"):
                with open("/dev/shm/ui_fatigue_limit.json", "r") as f:
                    base["fatigue_limit"] = int(json.loads(f.read()).get("limit", PLAN_DEFAULT_FATIGUE_TARGET))
            else:
                base["fatigue_limit"] = PLAN_DEFAULT_FATIGUE_TARGET
        except Exception:
            base["fatigue_limit"] = PLAN_DEFAULT_FATIGUE_TARGET
    try:
        if os.path.exists("/dev/shm/angle_debug.json"):
            with open("/dev/shm/angle_debug.json", "r", encoding="utf-8") as f:
                base["angle_diag"] = json.load(f)
    except Exception:
        pass
    body = json.dumps(base, ensure_ascii=False)
    state_feed._cache = {"ts": now, "body": body}
    return Response(body, mimetype='application/json')


@app.route('/llm_reply_feed')
def llm_reply_feed():
    """DeepSeek 教练回复"""
    try:
        if os.path.exists("/dev/shm/llm_reply.txt"):
            with open("/dev/shm/llm_reply.txt", "r", encoding="utf-8") as f:
                reply = f.read().strip()
            return Response(json.dumps({"reply": reply}, ensure_ascii=False), mimetype='application/json')
    except Exception:
        pass
    return Response('{"reply":""}', mimetype='application/json')


@app.route('/reset_session', methods=['POST'])
def reset_session():
    """重置 FSM 计数"""
    try:
        with open("/dev/shm/fsm_reset_signal", "w") as f:
            f.write("reset")
        return Response('{"ok":true}', mimetype='application/json')
    except Exception:
        return Response('{"ok":false}', mimetype='application/json', status=500)


@app.route('/trigger_deepseek', methods=['POST'])
def trigger_deepseek():
    """手动触发 DeepSeek 教练点评"""
    try:
        with open("/dev/shm/trigger_deepseek", "w") as f:
            f.write("trigger")
        return Response('{"ok":true}', mimetype='application/json')
    except Exception:
        return Response('{"ok":false}', mimetype='application/json', status=500)


@app.route('/api/chat', methods=['POST'])
def chat_input():
    """接收用户语音/文字消息，写入共享内存供 main_loop 读取"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        text = data.get("text", "").strip()
        if not text:
            return Response('{"ok":false,"error":"empty"}', mimetype='application/json', status=400)
        with open("/dev/shm/chat_input.txt.tmp", "w", encoding="utf-8") as f:
            f.write(text)
        os.rename("/dev/shm/chat_input.txt.tmp", "/dev/shm/chat_input.txt")
        return Response('{"ok":true}', mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}), mimetype='application/json', status=500)


@app.route('/api/coach/capabilities', methods=['GET'])
def coach_capabilities():
    """Return IronBuddy coach feature list for UI/debug use."""
    body = {
        "ok": True,
        "items": get_capabilities(),
        "reply": format_capability_reply(max_items=7),
        "kb": coach_kb_status(),
    }
    return Response(json.dumps(body, ensure_ascii=False), mimetype='application/json')


@app.route('/api/coach/rag_query', methods=['POST'])
def coach_rag_query():
    """Debug professional RAG retrieval without calling DeepSeek."""
    data = request.get_json(force=True, silent=True) or {}
    query = str(data.get("query") or data.get("text") or "").strip()
    try:
        limit = int(data.get("limit", 3))
    except Exception:
        limit = 3
    if not query:
        return Response(json.dumps({"ok": False, "error": "empty query"}, ensure_ascii=False),
                        mimetype='application/json', status=400)
    online = _search_lane_a_professional_knowledge(query, limit=limit)
    hits = online.get("hits") or []
    context = online.get("context") or ""
    delivery = None
    if bool(data.get("deliver", False)) and rag_delivery is not None:
        delivery = _prepare_and_maybe_send_rag_delivery(
            query,
            turn_id=str(data.get("turn_id") or "debug"),
            dry_run=bool(data.get("dry_run", True)),
        )
    return Response(json.dumps({
        "ok": True,
        "query": query,
        "manual_intent": is_manual_question(query),
        "manual_reply": format_manual_reply(query, max_hits=limit),
        "source_mode": online.get("source_mode") or "adp",
        "online_ok": bool(online.get("ok")),
        "online_reason": online.get("reason"),
        "message": online.get("message") or ("专业证据已命中" if hits else "专业证据不可用"),
        "hits": hits,
        "context": context,
        "errors": online.get("errors") or [],
        "vector": online.get("vector") or {},
        "vector_fallback_enabled": _vector_fallback_enabled(),
        "delivery": delivery,
    }, ensure_ascii=False), mimetype='application/json')


def _rag_auto_send_enabled():
    raw = os.environ.get("IRONBUDDY_RAG_AUTO_SEND", "1").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _vector_fallback_enabled():
    if rag_delivery is not None and hasattr(rag_delivery, "vector_fallback_enabled"):
        try:
            return bool(rag_delivery.vector_fallback_enabled())
        except Exception:
            pass
    raw = os.environ.get("IRONBUDDY_ENABLE_VECTOR_FALLBACK", "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _search_lane_a_professional_knowledge(query, limit=3):
    if rag_delivery is not None and hasattr(rag_delivery, "search_professional_knowledge"):
        return rag_delivery.search_professional_knowledge(query, limit=limit)
    if adp_knowledge is not None:
        try:
            return adp_knowledge.search_adp_knowledge(query, limit=limit)
        except Exception as exc:
            return {
                "ok": False,
                "source_mode": "adp",
                "reason": "adp_exception",
                "message": "ADP 专业知识库暂时不可用",
                "hits": [],
                "context": "",
                "errors": [{"provider": "adp", "error": str(exc)[:160]}],
            }
    return {
        "ok": False,
        "source_mode": "adp",
        "reason": "adp_provider_unavailable",
        "message": "ADP 专业知识库暂时不可用",
        "hits": [],
        "context": "",
        "errors": [],
    }


def _compact_adp_answer(online, max_chars=220):
    hits = (online or {}).get("hits") or []
    if not hits:
        return ""
    first = hits[0] if isinstance(hits[0], dict) else {}
    text = str(first.get("abstract_or_snippet") or (online or {}).get("context") or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars].rstrip()


def _is_auto_rag_candidate(query):
    """Route most non-control user questions through ADP before LLM fallback."""
    text = str(query or "").strip()
    if not text:
        return False
    compact = text.lower()
    for token in (" ", "\t", "\n", "\r", "，", "。", "！", "？", "!", "?", ",", "."):
        compact = compact.replace(token, "")
    if compact in ("教练", "叫练", "交练", "焦练", "嗯", "好的", "好"):
        return False
    control_hints = (
        "静音", "解除静音", "下一组", "上限", "设置", "调整",
        "切换", "启动", "停止", "一键", "云端", "本地",
        "纯视觉", "视觉加传感", "视觉+传感", "现在几点", "几点",
        "当前时间", "时间",
    )
    if any(hint in compact for hint in control_hints):
        return False
    if is_manual_question(text):
        return True
    if len(compact) < 4:
        return False
    return True


def _prepare_and_maybe_send_rag_delivery(query, turn_id="", dry_run=True,
                                         cooldown_s=None):
    if rag_delivery is None:
        return {"ok": False, "should_deliver": False, "reason": "rag_delivery_unavailable"}
    if cooldown_s is None:
        cooldown_s = rag_delivery.DEFAULT_COOLDOWN_S
    result = rag_delivery.prepare_rag_delivery(
        query,
        turn_id=turn_id,
        runtime_path=RAG_DELIVERY_PATH,
        cooldown_s=cooldown_s,
    )
    if not result.get("should_deliver"):
        return result
    if dry_run and _rag_auto_send_enabled():
        dry_run = False
    feishu = result.get("feishu") or {}
    card = feishu.get("card")
    send_result = {"ok": True, "dry_run": True, "skipped": True}
    if isinstance(card, dict):
        send_result = _send_feishu_card(card, _load_api_config(), dry_run=dry_run)
    top = ((result.get("last_hit") or {}).get("top_hit") or {})
    title = "ADP" if result.get("source_mode") == "adp" else (top.get("title") or top.get("id") or "知识库")
    if send_result.get("ok"):
        answer = _compact_adp_answer({
            "hits": [top],
            "context": ((result.get("last_hit") or {}).get("context") or ""),
        })
        if answer:
            _append_chat_event(
                "coach",
                answer,
                kind="rag_answer",
            )
        suffix = (
            "飞书自动推送未启用"
            if dry_run else
            "已自动发送飞书详报"
        )
        _append_chat_event(
            "coach",
            "知识库命中：%s，%s。" % (title, suffix),
            kind="rag_hit_notice",
        )
    else:
        _append_chat_event(
            "coach",
            "知识库命中：%s，飞书详报发送失败：%s。" %
            (title, str(send_result.get("error") or "unknown")[:30]),
            kind="rag_hit_notice_failed",
        )
    result["send_result"] = send_result
    result["dry_run"] = bool(dry_run)
    try:
        runtime = _read_json_file(RAG_DELIVERY_PATH)
        runtime["last_send_result"] = send_result
        runtime["last_send_dry_run"] = bool(dry_run)
        runtime["last_send_ts"] = time.time()
        runtime["last_send_mode"] = "dry_run" if dry_run else "real_send"
        _atomic_write_json_file(RAG_DELIVERY_PATH, runtime)
    except Exception:
        pass
    return result


def _latest_user_chat_event():
    try:
        if not os.path.exists(CHAT_EVENTS_FILE):
            return None
        latest = None
        with open(CHAT_EVENTS_FILE, "r", encoding="utf-8") as f:
            for line in f.readlines()[-120:]:
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get("role") == "user" and str(ev.get("text") or "").strip():
                    latest = ev
        return latest
    except Exception:
        return None


def _maybe_rag_delivery_from_latest_chat():
    ev = _latest_user_chat_event()
    if not ev:
        return None
    try:
        seq = int(ev.get("seq") or 0)
    except Exception:
        seq = 0
    if seq <= 0:
        return None
    state = _read_json_file(RAG_DELIVERY_PATH)
    if int(state.get("last_seen_user_seq") or 0) >= seq:
        return None
    state["last_seen_user_seq"] = seq
    try:
        _atomic_write_json_file(RAG_DELIVERY_PATH, state)
    except Exception:
        pass
    if not _is_auto_rag_candidate(str(ev.get("text") or "")):
        state["last_skipped_query"] = str(ev.get("text") or "")
        state["last_skipped_reason"] = "not_knowledge_question"
        state["last_skipped_seq"] = seq
        state["updated_ts"] = time.time()
        try:
            _atomic_write_json_file(RAG_DELIVERY_PATH, state)
        except Exception:
            pass
        return {
            "ok": True,
            "should_deliver": False,
            "reason": "not_knowledge_question",
        }
    return _prepare_and_maybe_send_rag_delivery(
        str(ev.get("text") or ""),
        turn_id=str(ev.get("turn_id") or seq),
        dry_run=(not _rag_auto_send_enabled()),
    )


def _mark_rag_delivery_skipped(query, reason="not_knowledge_question", seq=None):
    state = _read_json_file(RAG_DELIVERY_PATH)
    state["last_skipped_query"] = str(query or "")
    state["last_skipped_reason"] = reason
    if seq is not None:
        state["last_skipped_seq"] = seq
    state["updated_ts"] = time.time()
    try:
        _atomic_write_json_file(RAG_DELIVERY_PATH, state)
    except Exception:
        pass
    return {
        "ok": True,
        "should_deliver": False,
        "reason": reason,
        "query": str(query or ""),
    }


@app.route('/api/rag/delivery', methods=['GET', 'POST'])
def api_rag_delivery():
    """Prepare or send the detailed Feishu card for a RAG hit."""
    if request.method == 'POST':
        data = request.get_json(force=True, silent=True) or {}
        query = str(data.get("query") or data.get("text") or "").strip()
        send = bool(data.get("send", False))
        dry_run = bool(data.get("dry_run", not send))
        manual_send = bool(send and not dry_run)
    else:
        data = {}
        query = str(request.args.get("query") or "").strip()
        send = str(request.args.get("send") or "0").lower() in ("1", "true", "yes")
        dry_run = not send
        manual_send = bool(send and not dry_run)
        if not query:
            runtime = _read_json_file(RAG_DELIVERY_PATH)
            return Response(json.dumps({
                "ok": True,
                "runtime_path": RAG_DELIVERY_PATH,
                "last_hit": runtime.get("last_hit"),
                "queries": runtime.get("queries", {}),
                "send_result": runtime.get("last_send_result"),
                "dry_run": runtime.get("last_send_dry_run"),
                "send_mode": runtime.get("last_send_mode"),
                "auto_send_enabled": _rag_auto_send_enabled(),
                "last_skipped_query": runtime.get("last_skipped_query"),
                "last_skipped_reason": runtime.get("last_skipped_reason"),
                "updated_ts": runtime.get("updated_ts"),
            }, ensure_ascii=False, default=str), mimetype='application/json')
    if not query:
        latest = _latest_user_chat_event()
        if latest:
            query = str(latest.get("text") or "").strip()
            data["turn_id"] = latest.get("turn_id") or latest.get("seq")
    if query and not _is_auto_rag_candidate(query):
        result = _mark_rag_delivery_skipped(query, reason="not_knowledge_question")
        return Response(json.dumps(result, ensure_ascii=False, default=str),
                        mimetype='application/json')
    result = _prepare_and_maybe_send_rag_delivery(
        query,
        turn_id=str(data.get("turn_id") or ("manual-send-%d" % int(time.time() * 1000) if manual_send else "manual")),
        dry_run=dry_run,
        cooldown_s=(0 if manual_send else None),
    )
    status = 200 if result.get("ok", True) else 400
    return Response(json.dumps(result, ensure_ascii=False, default=str),
                    mimetype='application/json', status=status)


def _read_voice_turn():
    """V7.30 S1: read /dev/shm/voice_turn.json and return (turn_id, stage) or ('', '')."""
    try:
        if os.path.exists("/dev/shm/voice_turn.json"):
            with open("/dev/shm/voice_turn.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("turn_id", ""), data.get("stage", "")
    except Exception:
        pass
    return "", ""


def _read_chat_event_seq():
    try:
        if os.path.exists("/dev/shm/chat_events.seq"):
            with open("/dev/shm/chat_events.seq", "r") as f:
                return int((f.read() or "0").strip() or "0")
    except Exception:
        pass
    return 0


@app.route('/api/chat_events')
def chat_events():
    """Ordered display bubble timeline for the main shooting UI."""
    try:
        _maybe_rag_delivery_from_latest_chat()
    except Exception as exc:
        logging.debug("rag delivery bridge skipped: %s", exc)
    try:
        since = int(request.args.get("since", "0") or "0")
    except Exception:
        since = 0
    events = []
    latest_seq = _read_chat_event_seq()
    try:
        path = "/dev/shm/chat_events.jsonl"
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    try:
                        seq = int(ev.get("seq", 0))
                    except Exception:
                        seq = 0
                    if seq <= since:
                        continue
                    text = str(ev.get("text", "")).strip()
                    if not text:
                        continue
                    events.append({
                        "seq": seq,
                        "ts": ev.get("ts", 0),
                        "turn_id": ev.get("turn_id", ""),
                        "role": ev.get("role", "coach"),
                        "kind": ev.get("kind", ""),
                        "stage": ev.get("stage", ""),
                        "text": text,
                    })
        if events:
            latest_seq = max(latest_seq, max(int(e.get("seq", 0)) for e in events))
        return Response(json.dumps(
            {"ok": True, "events": events[-80:], "latest_seq": latest_seq},
            ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps(
            {"ok": False, "events": [], "latest_seq": latest_seq, "error": str(e)},
            ensure_ascii=False), mimetype='application/json', status=500)


@app.route('/api/chat_reply')
def chat_reply():
    """读取 DeepSeek 对话回复 (V7.30: 附带 turn_id 让前端去重气泡)"""
    try:
        if os.path.exists("/dev/shm/chat_reply.txt"):
            with open("/dev/shm/chat_reply.txt", "r", encoding="utf-8") as f:
                reply = f.read().strip()
            mtime = os.path.getmtime("/dev/shm/chat_reply.txt")
            turn_id, stage = _read_voice_turn()
            return Response(json.dumps(
                {"reply": reply, "ts": mtime, "turn_id": turn_id, "stage": stage},
                ensure_ascii=False), mimetype='application/json')
    except Exception:
        pass
    return Response('{"reply":"","ts":0,"turn_id":"","stage":""}', mimetype='application/json')


@app.route('/api/chat_input')
def get_chat_input():
    """读取用户语音识别内容 (V7.5: 去除 [voice-handled] 内部标记; V7.30: 附 turn_id)"""
    try:
        if os.path.exists("/dev/shm/chat_input.txt"):
            with open("/dev/shm/chat_input.txt", "r", encoding="utf-8") as f:
                content = f.read().strip()
            # V7.5: 剥离 FSM 路由控制标记
            content = content.replace("[voice-handled]", "").strip()
            mtime = os.path.getmtime("/dev/shm/chat_input.txt")
            turn_id, stage = _read_voice_turn()
            return Response(json.dumps(
                {"text": content, "ts": mtime, "turn_id": turn_id, "stage": stage},
                ensure_ascii=False), mimetype='application/json')
    except Exception:
        pass
    return Response('{"text":"","ts":0,"turn_id":"","stage":""}', mimetype='application/json')


@app.route('/api/voice_turn')
def get_voice_turn():
    """V7.30: expose current voice turn metadata for UI bubble dedupe (S1)."""
    try:
        if os.path.exists("/dev/shm/voice_turn.json"):
            with open("/dev/shm/voice_turn.json", "r", encoding="utf-8") as f:
                return Response(f.read(), mimetype='application/json')
    except Exception:
        pass
    return Response('{"turn_id":"","stage":"","ts":0}', mimetype='application/json')

@app.route('/api/nn_inference')
def nn_inference():
    """Read neural network inference results"""
    try:
        if os.path.exists("/dev/shm/fsm_state.json"):
            with open("/dev/shm/fsm_state.json", "r") as f:
                data = json.load(f)
            return Response(json.dumps({
                "similarity": data.get("similarity", 0),
                "classification": data.get("classification", "unknown"),
                "nn_confidence": data.get("nn_confidence", 0)
            }), mimetype='application/json')
    except Exception:
        pass
    return Response('{"similarity":0,"classification":"unknown","nn_confidence":0}', mimetype='application/json')


@app.route('/api/voice_debug')
def get_voice_debug():
    try:
        if os.path.exists("/dev/shm/voice_debug.json"):
            with open("/dev/shm/voice_debug.json", "r", encoding="utf-8") as f:
                return Response(f.read(), mimetype='application/json')
    except Exception:
        pass
    return Response('{"energy":0, "threshold":150, "text":""}', mimetype='application/json')


MANUAL_VOICE_RECORD_PATH = "/dev/shm/manual_voice_record.request"
MANUAL_VOICE_STOP_PATH = "/dev/shm/manual_voice_stop.request"
MANUAL_VOICE_STATUS_PATH = "/dev/shm/manual_voice_status.json"


def _read_manual_voice_status():
    data = _read_json_file(MANUAL_VOICE_STATUS_PATH)
    if not isinstance(data, dict) or not data:
        return {"state": "idle", "ts": 0, "text": ""}
    data.setdefault("state", "idle")
    data.setdefault("ts", 0)
    data.setdefault("text", "")
    return data


@app.route('/api/voice_manual/start', methods=['POST'])
def api_voice_manual_start():
    """Signal voice_daemon to start one manual voice recording fallback turn."""
    try:
        payload = {
            "cmd": "start",
            "ts": time.time(),
            "src": "ui",
        }
        _atomic_write_json_file(MANUAL_VOICE_RECORD_PATH, payload)
        _atomic_write_json_file(MANUAL_VOICE_STATUS_PATH, {
            "state": "requested",
            "ts": payload["ts"],
            "text": "",
            "src": "ui",
        })
        return Response(json.dumps({"ok": True, "status": _read_manual_voice_status()},
                                   ensure_ascii=False),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False),
                        mimetype='application/json', status=500)


@app.route('/api/voice_manual/stop', methods=['POST'])
def api_voice_manual_stop():
    """Signal voice_daemon to stop the current manual recording fallback turn."""
    try:
        payload = {
            "cmd": "stop",
            "ts": time.time(),
            "src": "ui",
        }
        _atomic_write_json_file(MANUAL_VOICE_STOP_PATH, payload)
        status = _read_manual_voice_status()
        if status.get("state") in ("requested", "recording"):
            status.update({"state": "stop_requested", "ts": payload["ts"]})
            _atomic_write_json_file(MANUAL_VOICE_STATUS_PATH, status)
        return Response(json.dumps({"ok": True, "status": _read_manual_voice_status()},
                                   ensure_ascii=False),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False),
                        mimetype='application/json', status=500)


@app.route('/api/voice_manual/status')
def api_voice_manual_status():
    return Response(json.dumps({"ok": True, "status": _read_manual_voice_status()},
                               ensure_ascii=False),
                    mimetype='application/json')


@app.route('/api/chat_draft')
def chat_draft():
    """读取正在识别的草稿文字"""
    try:
        if os.path.exists("/dev/shm/chat_draft.txt"):
            with open("/dev/shm/chat_draft.txt", "r", encoding="utf-8") as f:
                content = f.read().strip()
            return Response(json.dumps({"text": content}, ensure_ascii=False), mimetype='application/json')
    except Exception:
        pass
    return Response('{"text":""}', mimetype='application/json')


# ===== V4: Mute / Vision Mode Toggle =====

@app.route('/api/mute', methods=['POST'])
def api_mute():
    """Mute/unmute: signal voice daemon and recover mixer from hard mute."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        muted = bool(data.get("muted", False))
        replay_requested = not muted
        payload = json.dumps({
            "muted": muted,
            "ts": time.time(),
            "src": "ui",
            "replay": replay_requested,
        })
        tmp_path = "/dev/shm/mute_signal.json.tmp"
        target_path = "/dev/shm/mute_signal.json"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.rename(tmp_path, target_path)
        replay_signal = {}
        mixer = {}
        try:
            if muted:
                try:
                    with open("/dev/shm/voice_interrupt", "w", encoding="utf-8") as vf:
                        vf.write(str(time.time()))
                except Exception:
                    pass
                subprocess.run(["sudo", "-n", "killall", "-9", "aplay"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            else:
                try:
                    replay_payload = json.dumps({"ts": time.time(), "src": "ui"})
                    with open("/dev/shm/replay_last_tts.json.tmp", "w", encoding="utf-8") as rf:
                        rf.write(replay_payload)
                    os.rename("/dev/shm/replay_last_tts.json.tmp", "/dev/shm/replay_last_tts.json")
                    replay_signal = {"path": "/dev/shm/replay_last_tts.json"}
                except Exception as replay_e:
                    replay_signal = {"error": str(replay_e)[:120]}
            mixer = _apply_tts_mixer(muted=muted)
        except Exception as mix_e:
            mixer = {"error": str(mix_e)[:120]}
        return Response(json.dumps({
            "ok": True,
            "muted": muted,
            "mixer": mixer,
            "replay_requested": replay_requested,
            "replay_signal": replay_signal,
        }),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/fatigue_limit', methods=['POST'])
def api_fatigue_limit():
    """Set fatigue limit (T2): write actuation signal for FSM + persist display value for UI."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        limit = int(data.get("limit", PLAN_DEFAULT_FATIGUE_TARGET))
        if limit < 100 or limit > 10000:
            return Response(json.dumps({"ok": False, "error": "limit out of range (100-10000)"}),
                            mimetype='application/json', status=400)
        ts = time.time()
        # V7.30 R3: dual-write canonical + intent + ui display
        act_payload = json.dumps({"limit": limit, "ts": ts, "src": "ui"})
        # 1) Actuation signal: FSM consumes + deletes
        _atomic_write_json("/dev/shm/fatigue_limit.json", act_payload)
        # 2) Intent signal: FSM intent watcher (manual-pending) reads this
        _atomic_write_json("/dev/shm/intent_fatigue_limit.json", act_payload)
        # 3) Display signal: UI reads for current-value display (FSM doesn't touch this one)
        _atomic_write_json("/dev/shm/ui_fatigue_limit.json", act_payload)
        return Response(json.dumps({"ok": True, "limit": limit}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/tts_volume', methods=['GET', 'POST'])
def api_tts_volume():
    """Read or set TTS volume for the UI. Values are clamped to Baidu TTS 1-15."""
    path = "/dev/shm/tts_volume.json"
    if request.method == 'GET':
        vol = _read_tts_volume(default=7)
        return Response(json.dumps({"ok": True, "vol": vol}), mimetype='application/json')

    try:
        data = request.get_json(force=True, silent=True) or {}
        raw = data.get("vol", data.get("volume", 7))
        vol = max(1, min(15, int(raw)))
        payload = json.dumps({"vol": vol, "ts": time.time(), "src": "ui"})
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.rename(tmp_path, path)

        # Also nudge the speaker mixer so an already-playing clip changes level quickly.
        muted = False
        try:
            if os.path.exists("/dev/shm/mute_signal.json"):
                with open("/dev/shm/mute_signal.json", "r") as f:
                    muted = bool(json.loads(f.read()).get("muted", False))
        except Exception:
            muted = False
        mixer = _apply_tts_mixer(vol=vol, muted=muted)
        return Response(json.dumps({"ok": True, "vol": vol, "mixer": mixer}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


def _atomic_write_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
    os.rename(tmp, path)


def _write_cloud_switch_status(mode, phase=None, detail=None, segment=None):
    # type: (str, object, object, object) -> None
    """Reset cloud handshake status when the UI explicitly switches vision.

    The frontend polls this file immediately after /api/switch_vision. If an
    old "failed" record is left in place, the UI can report failure before the
    vision process has even consumed /dev/shm/vision_mode.json.
    """
    path = os.environ.get("IRONBUDDY_CLOUD_STATUS_PATH",
                          "/dev/shm/cloud_rtmpose_status.json")
    if phase is None:
        phase = "ready" if mode == "local" else "connecting"
    if detail is None:
        detail = "local backend selected" if mode == "local" else "switch requested"
    backend = "local" if mode == "local" else "cloud"
    payload = {
        "phase": phase,
        "ts": time.time(),
        "detail": detail,
        "backend": backend,
        "requested_mode": mode,
    }
    if segment:
        payload["segment"] = segment
    body = json.dumps(payload, ensure_ascii=False)
    _atomic_write_json(path, body)


def _probe_cloud_health(timeout=2.0):
    """Probe board-local tunnel health for cloud RTMPose."""
    url = os.environ.get(
        "IRONBUDDY_CLOUD_HEALTH_URL",
        "http://127.0.0.1:6006/health",
    )
    try:
        resp = requests.get(url, timeout=timeout)
        detail = ""
        status_value = ""
        try:
            payload = resp.json()
            status_value = str(payload.get("status") or payload.get("phase") or "")
            detail = status_value or str(payload.get("ready", ""))
        except Exception:
            payload = {}
            detail = resp.text[:80]
        ready = (
            resp.status_code == 200 and
            (status_value in ("ready", "ok") or payload.get("ready") is True)
        )
        return {
            "ok": bool(ready),
            "status_code": resp.status_code,
            "detail": detail or ("HTTP %s" % resp.status_code),
            "segment": None if ready else "cloud_health_failed",
            "url": url,
        }
    except requests.exceptions.ConnectionError as e:
        return {
            "ok": False,
            "detail": str(e)[:160],
            "segment": "tunnel_down",
            "url": url,
        }
    except Exception as e:
        return {
            "ok": False,
            "detail": str(e)[:160],
            "segment": "cloud_health_failed",
            "url": url,
        }


def _ensure_cloud_tunnel(blocking=True):
    """Ensure 127.0.0.1:6006 is usable before switching to cloud vision."""
    first = _probe_cloud_health(timeout=1.5)
    if first.get("ok"):
        return {"ok": True, "segment": None, "detail": "cloud health ready"}

    root = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(root, "scripts", "cloud_tunnel.sh")
    if not os.path.exists(script):
        return {
            "ok": False,
            "segment": "tunnel_down",
            "detail": "cloud_tunnel.sh not found",
        }

    try:
        if not blocking:
            log = open("/tmp/cloud_tunnel_ensure.log", "a")
            subprocess.Popen(
                ["bash", script],
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            return {
                "ok": False,
                "segment": first.get("segment") or "tunnel_down",
                "detail": "cloud_tunnel best-effort start requested",
            }
        proc = subprocess.run(
            ["bash", script],
            cwd=root,
            capture_output=True,
            timeout=22,
        )
        if proc.returncode != 0:
            return {
                "ok": False,
                "segment": "tunnel_down",
                "detail": "cloud_tunnel.sh failed",
            }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "segment": "tunnel_down",
            "detail": "cloud_tunnel.sh timeout",
        }
    except Exception as e:
        return {
            "ok": False,
            "segment": "tunnel_down",
            "detail": str(e)[:160],
        }

    second = _probe_cloud_health(timeout=3.0)
    if second.get("ok"):
        return {"ok": True, "segment": None, "detail": "cloud health ready"}
    return {
        "ok": False,
        "segment": second.get("segment") or "cloud_health_failed",
        "detail": second.get("detail") or "cloud health failed",
    }


def _probe_rag_tunnel_health(timeout=1.5):
    vector_status = {}
    embedding_status = {}
    try:
        if vector_knowledge is not None:
            vector_status = vector_knowledge.status_snapshot(timeout_s=timeout)
            embedding_status = vector_knowledge.embedding_status_snapshot(timeout_s=timeout)
    except Exception as exc:
        vector_status = {
            "configured": False,
            "online": False,
            "latest_error": str(exc)[:120],
        }
    return {
        "ok": bool(vector_status.get("online") and embedding_status.get("online")),
        "vector_status": vector_status,
        "embedding_status": embedding_status,
    }


def _ensure_rag_tunnel(blocking=True):
    """Ensure board localhost vector/embedding tunnels are usable."""
    first = _probe_rag_tunnel_health(timeout=1.2)
    if first.get("ok"):
        return {"ok": True, "segment": None, "detail": "rag tunnel ready",
                "health": first}

    root = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(root, "scripts", "rag_tunnel.sh")
    if not os.path.exists(script):
        return {
            "ok": False,
            "segment": "rag_tunnel_down",
            "detail": "rag_tunnel.sh not found",
            "health": first,
        }

    try:
        if not blocking:
            log = open("/tmp/rag_tunnel_ensure.log", "a")
            subprocess.Popen(
                ["bash", script],
                cwd=root,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            return {
                "ok": False,
                "segment": "rag_tunnel_down",
                "detail": "rag tunnel best-effort start requested",
                "health": first,
            }
        proc = subprocess.run(
            ["bash", script],
            cwd=root,
            capture_output=True,
            timeout=24,
        )
        if proc.returncode != 0:
            return {
                "ok": False,
                "segment": "rag_tunnel_down",
                "detail": "rag_tunnel.sh failed",
                "health": first,
            }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "segment": "rag_tunnel_down",
            "detail": "rag_tunnel.sh timeout",
            "health": first,
        }
    except Exception as e:
        return {
            "ok": False,
            "segment": "rag_tunnel_down",
            "detail": str(e)[:160],
            "health": first,
        }

    second = _probe_rag_tunnel_health(timeout=2.5)
    if second.get("ok"):
        return {"ok": True, "segment": None, "detail": "rag tunnel ready",
                "health": second}
    return {
        "ok": False,
        "segment": "rag_health_failed",
        "detail": "rag tunnel up but vector or embedding health failed",
        "health": second,
    }


def _parse_cloud_ssh_command(raw):
    text = str(raw or "").strip()
    if not text:
        raise ValueError("empty ssh command")
    parts = shlex.split(text)
    if not parts or os.path.basename(parts[0]) != "ssh":
        raise ValueError("expected ssh command")
    port = None
    target = ""
    i = 1
    while i < len(parts):
        token = parts[i]
        if token == "-p":
            if i + 1 >= len(parts):
                raise ValueError("missing ssh port")
            port = parts[i + 1]
            i += 2
            continue
        if token.startswith("-p") and len(token) > 2:
            port = token[2:]
            i += 1
            continue
        if token.startswith("-"):
            if token in ("-i", "-o", "-L", "-R", "-D", "-J") and i + 1 < len(parts):
                i += 2
            else:
                i += 1
            continue
        target = token
        i += 1
    if not target or "@" not in target:
        raise ValueError("expected user@host")
    user, host = target.rsplit("@", 1)
    if not port:
        port = "22"
    try:
        port_i = int(port)
    except Exception:
        raise ValueError("invalid ssh port")
    if port_i < 1 or port_i > 65535:
        raise ValueError("invalid ssh port")
    if not re.match(r"^[A-Za-z0-9._-]+$", user):
        raise ValueError("invalid ssh user")
    if not re.match(r"^[A-Za-z0-9._:-]+$", host):
        raise ValueError("invalid ssh host")
    return {"user": user, "host": host, "port": port_i}


def _kill_cloud_gpu_tunnels():
    patterns = (
        "[s]sh.*-L.*6006:127.0.0.1:6006",
        "[c]loud_tunnel.py",
        "[c]loud_tunnel.exp",
        "[s]sh.*-L.*6333:127.0.0.1:6333.*8008:127.0.0.1:8008",
        "[r]ag_tunnel.py",
        "[r]ag_tunnel.exp",
    )
    results = []
    for pat in patterns:
        try:
            proc = subprocess.run(["pkill", "-f", pat], capture_output=True, timeout=5)
            results.append({"pattern": pat, "returncode": proc.returncode})
        except Exception as exc:
            results.append({"pattern": pat, "error": str(exc)[:80]})
    return results


def _cloud_gpu_public_config(cfg):
    return {
        "host": _pick_config(cfg, "CLOUD_SSH_HOST"),
        "port": _pick_config(cfg, "CLOUD_SSH_PORT"),
        "user": _pick_config(cfg, "CLOUD_SSH_USER") or "root",
        "password_configured": bool(_pick_config(cfg, "CLOUD_SSH_PASSWORD")),
    }


def _cloud_gpu_status_path():
    return "/dev/shm/cloud_gpu_connect_status.json" if os.path.isdir("/dev/shm") else "/tmp/cloud_gpu_connect_status.json"


def _cloud_gpu_reconnect_worker():
    try:
        root = os.path.dirname(os.path.abspath(__file__))
        bootstrap_script = os.path.join(root, "scripts", "cloud_gpu_bootstrap.py")
        bootstrap = {"ok": False, "detail": "cloud_gpu_bootstrap.py not found"}
        if os.path.exists(bootstrap_script):
            try:
                proc = subprocess.run(
                    ["python3", bootstrap_script],
                    cwd=root,
                    capture_output=True,
                    timeout=90,
                )
                bootstrap = {
                    "ok": proc.returncode == 0,
                    "detail": (
                        proc.stdout.decode("utf-8", "replace")[-500:]
                        if proc.stdout else
                        proc.stderr.decode("utf-8", "replace")[-500:]
                    ),
                }
            except subprocess.TimeoutExpired:
                bootstrap = {"ok": False, "detail": "cloud bootstrap timeout"}
            except Exception as exc:
                bootstrap = {"ok": False, "detail": str(exc)[:160]}
        _kill_cloud_gpu_tunnels()
        time.sleep(0.5)
        cloud = _ensure_cloud_tunnel(blocking=True)
        rag = _ensure_rag_tunnel(blocking=True)
        status = {
            "ok": bool(cloud.get("ok") and rag.get("ok")),
            "bootstrap": bootstrap,
            "cloud": cloud,
            "rag": rag,
            "updated_ts": time.time(),
        }
    except Exception as exc:
        status = {
            "ok": False,
            "error": str(exc)[:160],
            "updated_ts": time.time(),
        }
    try:
        _atomic_write_json_file(_cloud_gpu_status_path(), status)
    except Exception:
        pass


@app.route('/api/exercise_mode', methods=['POST'])
def api_exercise_mode():
    """Switch exercise mode (T2): write signal for FSM to hot-swap squat↔curl.

    V7.30 R3 race fix: also publishes /dev/shm/intent_exercise_mode.json so
    a future FSM intent watcher can observe UI-originated requests
    separately from voice-originated authoritative writes."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        mode = data.get("mode", "squat")
        if mode not in ("squat", "curl", "bicep_curl"):
            return Response(json.dumps({"ok": False, "error": "invalid mode"}),
                            mimetype='application/json', status=400)
        # Normalize: FSM/voice daemon use "squat" or "curl"
        norm_mode = "curl" if mode in ("curl", "bicep_curl") else "squat"
        src = str(data.get("src") or "ui")[:32]
        payload = json.dumps({"mode": norm_mode, "ts": time.time(), "src": src, "ttl_s": 30})
        _atomic_write_json("/dev/shm/exercise_mode.json", payload)
        _atomic_write_json("/dev/shm/intent_exercise_mode.json", payload)
        return Response(json.dumps({"ok": True, "mode": norm_mode}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/mvc_calibration', methods=['GET', 'POST'])
def api_mvc_calibration():
    """MVC 标定 (T3, plan §3.3):
    - GET: 读取当前 /dev/shm/emg_calibration.json, 返回状态
    - POST: 写入新标定（板未连时 peak_mvc 值可为 null, 仅记录 protocol + 动作 + 时间戳）
    契约参照 plan §2.1: {peak_mvc:{ch0,ch1}, protocol:'SENIAM-2000', exercise:'curl|squat', std_pct, ts}
    """
    target_path = "/dev/shm/emg_calibration.json"
    if request.method == 'GET':
        try:
            if os.path.exists(target_path):
                with open(target_path, "r", encoding="utf-8") as f:
                    return Response(f.read(), mimetype='application/json')
        except Exception:
            pass
        return Response(json.dumps({"calibrated": False}), mimetype='application/json')
    # POST
    try:
        data = request.get_json(force=True, silent=True) or {}
        exercise = data.get("exercise", "squat")
        if exercise not in ("squat", "curl", "bicep_curl"):
            return Response(json.dumps({"ok": False, "error": "invalid exercise"}),
                            mimetype='application/json', status=400)
        norm_ex = "curl" if exercise in ("curl", "bicep_curl") else "squat"
        peak_mvc = data.get("peak_mvc", {"ch0": None, "ch1": None})
        std_pct = data.get("std_pct", None)
        payload = {
            "calibrated": True,
            "protocol": "SENIAM-2000",
            "exercise": norm_ex,
            "peak_mvc": peak_mvc,
            "std_pct": std_pct,
            "ts": time.time(),
        }
        tmp_path = target_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.rename(tmp_path, target_path)
        return Response(json.dumps({"ok": True, **payload}, ensure_ascii=False),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/mvc_calibrate', methods=['POST'])
def api_mvc_calibrate():
    """V4.7 动态 MVC 校准触发端点（与 /api/mvc_calibration 区分：本接口真正触发硬件采集）。
    协议：前端 POST → 写 /dev/shm/mvc_calibrate.request → udp_emg_server 进入 3 秒峰值采集
          → udp_emg_server 写 /dev/shm/mvc_calibrate.result → 本端点轮询返回。
    返回 {ok, target, comp, duration_ms} 或 {ok:false, error:"timeout"}。
    """
    req_path = '/dev/shm/mvc_calibrate.request'
    res_path = '/dev/shm/mvc_calibrate.result'
    t0 = time.time()
    try:
        # 清理旧结果 + 提交请求
        try:
            os.remove(res_path)
        except OSError:
            pass
        with open(req_path, 'w') as _rf:
            _rf.write(str(t0))
        # 轮询 5 秒等 udp_emg_server 完成 3 秒采集 + 写盘
        for _ in range(25):
            time.sleep(0.2)
            if os.path.exists(res_path):
                with open(res_path, 'r') as _f:
                    payload = json.load(_f)
                return Response(json.dumps({
                    "ok": True,
                    "target": payload.get("target"),
                    "comp": payload.get("comp"),
                    "duration_ms": int((time.time() - t0) * 1000),
                }), mimetype='application/json')
        return Response(json.dumps({"ok": False, "error": "timeout"}),
                        mimetype='application/json', status=504)
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


# ========================================================================
# V7.16 测试采集 (FSM-对齐 rep) — 3 端点, 协议同 mvc_calibrate (shm trigger)
# ========================================================================
_TEST_CAPTURE_SESSION_SHM = '/dev/shm/test_capture.session'
_TEST_CAPTURE_STOP_SHM = '/dev/shm/test_capture.stop'
_TEST_CAPTURE_RESULT_SHM = '/dev/shm/test_capture.result'
_TEST_CAPTURE_ACK_SHM = '/dev/shm/test_capture.session.ack'
_TEST_CAPTURE_EXERCISES = ('squat', 'bicep_curl')
_TEST_CAPTURE_LABELS = ('standard', 'compensating', 'non_standard')

def _test_capture_root():
    # PROJECT_ROOT 定义在 L742, 本段代码位置更靠前, 所以懒加载
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'data', 'test_capture')


def _test_capture_safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


@app.route('/api/test_capture/start', methods=['POST'])
def api_test_capture_start():
    """
    V7.16 启动一次测试采集会话.
    入参: {"exercise":"squat"|"bicep_curl", "label":"standard"|"compensating"|"non_standard"}
    动作:
      1) 调 FitnessDB.start_session(exercise) 拿 session_id (失败则用时间戳兜底)
      2) mkdir -p data/test_capture/{exercise}/{label}/{YYYYMMDD_HHMMSS}_{sid}
      3) 写 /dev/shm/test_capture.session
      4) sleep 0.5s 检查 .session.ack, 未 ack 仅警告不阻断
    出参: {"ok", "session_id", "out_dir", "ack"}
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        exercise = str(data.get("exercise", "")).strip()
        label = str(data.get("label", "")).strip()
        if exercise not in _TEST_CAPTURE_EXERCISES:
            return Response(json.dumps({"ok": False, "error": "invalid exercise"}),
                            mimetype='application/json', status=400)
        if label not in _TEST_CAPTURE_LABELS:
            return Response(json.dumps({"ok": False, "error": "invalid label"}),
                            mimetype='application/json', status=400)

        # 已有 active session? 拒绝重复启动
        if os.path.exists(_TEST_CAPTURE_SESSION_SHM):
            return Response(json.dumps({
                "ok": False,
                "error": "session already active, stop it first"
            }), mimetype='application/json', status=409)

        # DB session_id (失败时用 int(time.time()) 兜底)
        sid = None
        db = _get_db()
        if db is not None:
            try:
                sid = db.start_session(exercise)
            except Exception as e:
                logging.warning("test_capture start_session 失败: %s", e)
        if not isinstance(sid, int):
            sid = int(time.time()) % 100000

        # 组装目录
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(_test_capture_root(), exercise, label,
                               "{}_{}".format(ts_str, sid))
        # 冲突时追加 _b
        if os.path.exists(out_dir):
            out_dir = out_dir + "_b"
        os.makedirs(out_dir, exist_ok=True)

        # 清掉旧的 result / ack
        _test_capture_safe_remove(_TEST_CAPTURE_RESULT_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_ACK_SHM)

        # 写 session 信号
        payload = {
            "enabled": True,
            "session_id": sid,
            "exercise": exercise,
            "label": label,
            "out_dir": out_dir,
            "started_ts": time.time(),
        }
        tmp = _TEST_CAPTURE_SESSION_SHM + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.rename(tmp, _TEST_CAPTURE_SESSION_SHM)

        # 等 0.5s 看模拟器是否 ack (确认存活)
        ack = False
        for _ in range(5):
            time.sleep(0.1)
            if os.path.exists(_TEST_CAPTURE_ACK_SHM):
                ack = True
                break

        resp = {
            "ok": True,
            "session_id": sid,
            "exercise": exercise,
            "label": label,
            "out_dir": out_dir,
            "ack": ack,
        }
        if not ack:
            resp["warning"] = "simulator did not ack within 0.5s — ensure simulate_emg_from_{mia,bicep}.py is running"
        return Response(json.dumps(resp, ensure_ascii=False),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/test_capture/stop', methods=['POST'])
def api_test_capture_stop():
    """
    V7.16 停止采集并落盘.
    动作:
      1) 写 /dev/shm/test_capture.stop {"discard": false}
      2) 轮询 /dev/shm/test_capture.result (最多 5s, 0.2s 一次)
      3) 解析结果, 调 FitnessDB.end_session (best-effort)
      4) 清 session/stop/ack 信号
    出参: {"ok", "rep_count", "raw_rows", "duration_s", "out_dir"} 或 {"ok":false,"error":"timeout"}
    """
    try:
        # 无活动 session 时直接返回
        if not (os.path.exists(_TEST_CAPTURE_SESSION_SHM) or
                os.path.exists(_TEST_CAPTURE_RESULT_SHM)):
            return Response(json.dumps({"ok": False, "error": "no active capture session"}),
                            mimetype='application/json', status=409)

        # 写 stop 信号
        with open(_TEST_CAPTURE_STOP_SHM + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"discard": False}, f)
        os.rename(_TEST_CAPTURE_STOP_SHM + ".tmp", _TEST_CAPTURE_STOP_SHM)

        # 轮询 result (5s 上限)
        result = None
        for _ in range(25):
            time.sleep(0.2)
            if os.path.exists(_TEST_CAPTURE_RESULT_SHM):
                try:
                    with open(_TEST_CAPTURE_RESULT_SHM, "r", encoding="utf-8") as f:
                        result = json.load(f)
                    break
                except (IOError, ValueError):
                    continue
        if result is None:
            # 超时但仍清理信号
            _test_capture_safe_remove(_TEST_CAPTURE_STOP_SHM)
            return Response(json.dumps({"ok": False, "error": "timeout waiting for simulator flush"}),
                            mimetype='application/json', status=504)

        # best-effort DB end_session
        sid = result.get("session_id")
        if isinstance(sid, int):
            db = _get_db()
            if db is not None:
                try:
                    reps = int(result.get("rep_count", 0))
                    # 简化: good=rep_count, failed=0, peak_fatigue=0 (capture 模式不追踪)
                    db.end_session(sid, good=reps, failed=0, fatigue_peak=0.0)
                except Exception as e:
                    logging.warning("test_capture end_session 失败: %s", e)

        # 清理所有信号
        _test_capture_safe_remove(_TEST_CAPTURE_RESULT_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_STOP_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_ACK_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_SESSION_SHM)

        return Response(json.dumps(result, ensure_ascii=False),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/test_capture/clear', methods=['POST'])
def api_test_capture_clear():
    """
    V7.16 清空当前缓冲不落盘.
    动作: 写 /dev/shm/test_capture.stop {"discard": true}, 轮询 result.
    出参: {"ok", "discarded": true}
    """
    try:
        if not os.path.exists(_TEST_CAPTURE_SESSION_SHM):
            return Response(json.dumps({"ok": False, "error": "no active capture session"}),
                            mimetype='application/json', status=409)

        with open(_TEST_CAPTURE_STOP_SHM + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"discard": True}, f)
        os.rename(_TEST_CAPTURE_STOP_SHM + ".tmp", _TEST_CAPTURE_STOP_SHM)

        result = None
        for _ in range(15):
            time.sleep(0.2)
            if os.path.exists(_TEST_CAPTURE_RESULT_SHM):
                try:
                    with open(_TEST_CAPTURE_RESULT_SHM, "r", encoding="utf-8") as f:
                        result = json.load(f)
                    break
                except (IOError, ValueError):
                    continue

        # 清所有信号
        _test_capture_safe_remove(_TEST_CAPTURE_RESULT_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_STOP_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_ACK_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_SESSION_SHM)

        if result is None:
            return Response(json.dumps({"ok": True, "discarded": True,
                                        "note": "simulator did not respond; signals cleared"}),
                            mimetype='application/json')
        return Response(json.dumps(result, ensure_ascii=False),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/test_capture/status')
def api_test_capture_status():
    """V7.16 查询当前采集状态 (前端轮询用)."""
    active = os.path.exists(_TEST_CAPTURE_SESSION_SHM)
    ack = os.path.exists(_TEST_CAPTURE_ACK_SHM)
    payload = {"active": active, "ack": ack}
    if active:
        try:
            with open(_TEST_CAPTURE_SESSION_SHM, "r", encoding="utf-8") as f:
                sess = json.load(f)
            payload["session_id"] = sess.get("session_id")
            payload["exercise"] = sess.get("exercise")
            payload["label"] = sess.get("label")
            payload["started_ts"] = sess.get("started_ts")
            # FSM 实时 rep 计数
            try:
                with open("/dev/shm/fsm_state.json", "r", encoding="utf-8") as ff:
                    s = json.load(ff)
                payload["fsm_reps"] = int(s.get("good", 0)) + int(s.get("failed", 0)) + int(s.get("comp", 0))
            except (IOError, ValueError):
                payload["fsm_reps"] = -1
        except (IOError, ValueError):
            pass
    return Response(json.dumps(payload, ensure_ascii=False),
                    mimetype='application/json')


@app.route('/api/switch_vision', methods=['POST'])
def api_switch_vision():
    """Write vision mode signal (cloud / local)."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        mode = data.get("mode", "cloud")
        if mode not in ("cloud", "local"):
            return Response(json.dumps({"ok": False, "error": "invalid mode"}),
                            mimetype='application/json', status=400)
        ensure = None
        if mode == "cloud":
            ensure = _ensure_cloud_tunnel()
            if not ensure.get("ok"):
                segment = ensure.get("segment") or "tunnel_down"
                _write_cloud_switch_status(
                    mode,
                    phase="failed",
                    detail=segment,
                    segment=segment,
                )
                return Response(json.dumps({
                    "ok": False,
                    "mode": mode,
                    "phase": "failed",
                    "segment": segment,
                    "error": ensure.get("detail") or segment,
                }, ensure_ascii=False), mimetype='application/json', status=503)
        payload = json.dumps({"mode": mode, "ts": time.time()})
        tmp_path = "/dev/shm/vision_mode.json.tmp"
        target_path = "/dev/shm/vision_mode.json"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.rename(tmp_path, target_path)
        if mode == "cloud":
            # Failure segment reserved for the vision worker handoff path:
            # vision_worker_failed
            _write_cloud_switch_status(
                mode,
                phase="connecting",
                detail="cloud tunnel ready; waiting for vision worker",
            )
        else:
            _write_cloud_switch_status(mode)
        # V4.8: drop signal so cloud_rtmpose_client drains its queue (prevents stuck request freeze)
        try:
            with open("/dev/shm/vision_reset.flag", "w") as _f:
                _f.write("1")
        except Exception:
            pass
        return Response(json.dumps({
            "ok": True,
            "mode": mode,
            "cloud_tunnel": ensure,
        }, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/switch_inference_mode', methods=['POST'])
def api_switch_inference_mode():
    """Switch between pure_vision (if-else only) and vision_sensor (NN + EMG).

    V7.30 R3: dual-write canonical + intent file (see api_exercise_mode)."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        mode = data.get("mode", "pure_vision")
        if mode not in ("pure_vision", "vision_sensor"):
            return Response(json.dumps({"ok": False, "error": "invalid mode"}),
                            mimetype='application/json', status=400)
        src = str(data.get("src") or "ui")[:32]
        payload = json.dumps({"mode": mode, "ts": time.time(), "src": src, "ttl_s": 30})
        _atomic_write_json("/dev/shm/inference_mode.json", payload)
        _atomic_write_json("/dev/shm/intent_inference_mode.json", payload)
        return Response(json.dumps({"ok": True, "mode": mode}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/inference_mode')
def get_inference_mode():
    """Read current inference mode (pure_vision or vision_sensor)."""
    try:
        path = "/dev/shm/inference_mode.json"
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return Response(f.read(), mimetype='application/json')
    except Exception:
        pass
    return Response('{"mode":"pure_vision","ts":0}', mimetype='application/json')


@app.route('/api/vision_mode')
def get_vision_mode():
    """Read current vision mode from signal file."""
    try:
        vm_path = "/dev/shm/vision_mode.json"
        if os.path.exists(vm_path):
            with open(vm_path, "r", encoding="utf-8") as f:
                data = f.read()
            return Response(data, mimetype='application/json')
    except Exception:
        pass
    return Response('{"mode":"local","ts":0}', mimetype='application/json')


def _read_cloud_handshake_status(path):
    """Read cloud RTMPose handshake state file written by cloud_rtmpose_client.

    Returns a dict with keys ok, phase, plus passthrough fields. Phase is one
    of: connecting | ready | failed | unknown. Never raises.
    """
    try:
        if not os.path.exists(path):
            return {"ok": True, "phase": "unknown", "detail": "no status file"}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"ok": False, "phase": "unknown", "error": "not a json object"}
        data.setdefault("phase", "unknown")
        data["ok"] = True
        return data
    except Exception as e:
        return {"ok": False, "phase": "unknown", "error": str(e)}


@app.route('/api/cloud_handshake_status')
def cloud_handshake_status():
    """Cloud RTMPose handshake state. phase: connecting|ready|failed|unknown."""
    path = os.environ.get("IRONBUDDY_CLOUD_STATUS_PATH",
                          "/dev/shm/cloud_rtmpose_status.json")
    body = _read_cloud_handshake_status(path)
    return Response(json.dumps(body, ensure_ascii=False),
                    mimetype='application/json')


@app.route('/api/code_graph')
def api_code_graph():
    """Return data/code_graph/graph.json built by tools/build_code_graph.py.

    Override path with IRONBUDDY_CODE_GRAPH_PATH env var (used by tests).
    Returns {ok: false, message} when the file is missing — the frontend
    shows a placeholder hint instead of erroring.
    """
    default_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "data", "code_graph", "graph.json")
    path = os.environ.get("IRONBUDDY_CODE_GRAPH_PATH", default_path)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["ok"] = True
            return Response(json.dumps(data, ensure_ascii=False),
                            mimetype='application/json')
        return Response(json.dumps({
            "ok": False,
            "message": "graph.json 未生成，请运行 python3 tools/build_code_graph.py --refresh"
        }, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json')


# ===== HDMI Status API =====

@app.route('/api/hdmi_status')
def hdmi_status():
    """Check if HDMI display is active. Hardware status overrides signal file."""
    hw_connected = False
    try:
        with open("/sys/class/drm/card0-HDMI-A-1/status", "r") as f:
            hw_connected = "connected" in f.read()
    except Exception:
        pass

    # If hardware is disconnected, HDMI is definitely inactive
    if not hw_connected:
        return Response('{"active":false,"hw_connected":false}', mimetype='application/json')

    # Hardware connected — check if vision process is actually using it
    try:
        path = "/dev/shm/hdmi_status.json"
        if os.path.exists(path):
            with open(path, "r") as f:
                data = json.loads(f.read())
                data["hw_connected"] = True
                return Response(json.dumps(data), mimetype='application/json')
    except Exception:
        pass

    return Response(json.dumps({"active": False, "hw_connected": True}),
                    mimetype='application/json')


# ===== Feishu Smart Push API (Nexus Enabled) =====

def _pick_config(api_cfg, *keys):
    for k in keys:
        v = api_cfg.get(k)
        if v:
            return v
    return ""


def _sh_quote(value):
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def _read_current_fsm_data():
    fsm_data = {}
    try:
        if os.path.exists("/dev/shm/fsm_state.json"):
            with open("/dev/shm/fsm_state.json", "r", encoding="utf-8") as f:
                fsm_data = json.load(f)
    except Exception:
        fsm_data = {}
    return fsm_data


def _feishu_title_for_type(push_type):
    titles = {
        "plan": "IronBuddy 训练规划与处方",
        "summary": "IronBuddy 训练总结",
        "reminder": "IronBuddy 身体警钟与状态通报",
        "weekly": "IronBuddy 训练周报",
        "daily": "IronBuddy 训练早报",
    }
    return titles.get(push_type, "IronBuddy 助理播报")


def _build_feishu_training_card(push_type, body_text, fsm_data=None,
                                degraded=False, ds_error=None):
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if FeishuClient is not None:
        footer = "IronBuddy · " + now
        if degraded and ds_error:
            footer += " · AI 降级: " + str(ds_error)[:60]
        return FeishuClient.build_training_card(
            _feishu_title_for_type(push_type),
            body_text,
            stats=fsm_data or {},
            push_type=push_type,
            degraded=degraded,
            footer=footer,
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "yellow" if degraded else "blue",
            "title": {"tag": "plain_text", "content": _feishu_title_for_type(push_type)},
        },
        "elements": [
            {"tag": "markdown", "content": "**教练建议**\n" + str(body_text or "（暂无内容）")},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "IronBuddy · " + now}],
            },
        ],
    }


def _send_feishu_card(card, api_cfg, dry_run=False):
    if not isinstance(card, dict):
        return {"ok": False, "error": "card must be dict"}
    feishu_app_id = _pick_config(api_cfg, "FEISHU_APP_ID", "feishu_app_id")
    feishu_app_secret = _pick_config(api_cfg, "FEISHU_APP_SECRET", "feishu_app_secret")
    feishu_chat_id = _pick_config(api_cfg, "FEISHU_CHAT_ID", "feishu_chat_id")
    feishu_webhook = _pick_config(api_cfg, "FEISHU_WEBHOOK", "feishu_webhook")
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "msg_type": "interactive",
            "card_preview": card,
        }
    if FeishuClient is not None and feishu_app_id and feishu_app_secret and feishu_chat_id:
        client = FeishuClient(
            app_id=feishu_app_id,
            app_secret=feishu_app_secret,
            chat_id=feishu_chat_id,
            dry_run=False,
            timeout=15,
        )
        return client.send_card(card)
    if feishu_webhook:
        try:
            import urllib.request
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            payload = json.dumps({
                "msg_type": "interactive",
                "card": card,
            }).encode("utf-8")
            req = urllib.request.Request(
                feishu_webhook,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            data = json.loads(urllib.request.urlopen(req, timeout=15, context=ctx).read().decode("utf-8"))
            if data.get("StatusCode") == 0 or data.get("code") == 0:
                return {"ok": True, "msg_type": "interactive", "via": "webhook"}
            return {"ok": False, "error": "webhook send failed", "detail": data}
        except Exception as exc:
            return {"ok": False, "error": "webhook exception", "detail": str(exc)}
    return {"ok": False, "error": "飞书凭证未配置 (需要 APP_ID+SECRET+CHAT_ID 或 WEBHOOK)"}

@app.route('/api/feishu/ping', methods=['GET'])
def feishu_ping():
    """V7.20 (2026-04-20): 纯飞书链路自检 —— 只测 token+msg, 不调 DeepSeek.
    返回 {ok, token_ms, send_ms, total_ms, error?} 方便快速定位"是否真的是飞书失败"。
    """
    import urllib.request, urllib.error, ssl
    api_cfg = _load_api_config()
    def _pick(*keys):
        for k in keys:
            v = api_cfg.get(k)
            if v: return v
        return ""
    fid = _pick("FEISHU_APP_ID", "feishu_app_id")
    fsec = _pick("FEISHU_APP_SECRET", "feishu_app_secret")
    fcid = _pick("FEISHU_CHAT_ID", "feishu_chat_id")
    if not (fid and fsec and fcid):
        return Response(json.dumps({"ok": False, "error": "缺少 FEISHU_APP_ID/SECRET/CHAT_ID"}),
                        mimetype='application/json', status=400)
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time()
    try:
        tok_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": fid, "app_secret": fsec}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        tok_resp = json.loads(urllib.request.urlopen(tok_req, timeout=8, context=ctx).read())
        t1 = time.time()
        if tok_resp.get("code") != 0:
            return Response(json.dumps({"ok": False, "stage": "token", "token_ms": int((t1-t0)*1000),
                                        "error": tok_resp.get("msg", "unknown"), "code": tok_resp.get("code")}),
                            mimetype='application/json', status=502)
        tok = tok_resp.get("tenant_access_token", "")
        msg_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=json.dumps({"receive_id": fcid, "msg_type": "text",
                             "content": json.dumps({"text": "🏓 IronBuddy feishu-ping " + time.strftime('%H:%M:%S')})}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok})
        msg_resp = json.loads(urllib.request.urlopen(msg_req, timeout=10, context=ctx).read())
        t2 = time.time()
        ok = (msg_resp.get("code") == 0)
        return Response(json.dumps({"ok": ok, "token_ms": int((t1-t0)*1000), "send_ms": int((t2-t1)*1000),
                                    "total_ms": int((t2-t0)*1000),
                                    "error": None if ok else msg_resp.get("msg", "unknown"),
                                    "code": msg_resp.get("code")}),
                        mimetype='application/json', status=200 if ok else 502)
    except urllib.error.HTTPError as e:
        return Response(json.dumps({"ok": False, "error": "HTTPError %d" % e.code, "detail": str(e)}),
                        mimetype='application/json', status=502)
    except urllib.error.URLError as e:
        return Response(json.dumps({"ok": False, "error": "URLError", "detail": str(e.reason)}),
                        mimetype='application/json', status=504)
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": "exception", "detail": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/feishu/card_push', methods=['POST'])
def feishu_card_push():
    """Unified interactive-card push endpoint.

    Default dry_run=True for manual API tests; pass {"dry_run": false} to send.
    """
    api_cfg = _load_api_config()
    data = request.get_json(force=True, silent=True) or {}
    push_type = data.get("type", "summary")
    dry_run = bool(data.get("dry_run", True))
    body_text = data.get("text") or data.get("body") or data.get("prompt") or "IronBuddy 训练卡片测试"
    fsm_data = data.get("stats") if isinstance(data.get("stats"), dict) else _read_current_fsm_data()
    card = _build_feishu_training_card(
        push_type,
        body_text,
        fsm_data=fsm_data,
        degraded=bool(data.get("degraded", False)),
        ds_error=data.get("ds_error"),
    )
    result = _send_feishu_card(card, api_cfg, dry_run=dry_run)
    status = 200 if result.get("ok") else 502
    return Response(json.dumps({
        "ok": bool(result.get("ok")),
        "result": result,
        "card": card,
        "type_triggered": push_type,
        "dry_run": dry_run,
    }, ensure_ascii=False), mimetype='application/json', status=status)


@app.route('/api/feishu/push', methods=['POST'])
@app.route('/api/feishu/send_plan', methods=['POST'])
def feishu_smart_push():
    """Generate intelligent content via DeepSeek + SQLite Nexus and push to Feishu.
    V7.21: 互斥锁 + max_tokens + 降级 + 精准错误（防止"服务器忙碌"假警报）"""
    # V7.21: 并发互斥 —— 第二次点击立刻返回 429，不排队阻塞
    if not _FEISHU_PUSH_LOCK.acquire(blocking=False):
        _busy_for = int(time.time() - _FEISHU_PUSH_STARTED_AT[0])
        return Response(json.dumps({"ok": False, "error": "上一次推送仍在进行", "busy_for_s": _busy_for}),
                        mimetype='application/json', status=429)
    _FEISHU_PUSH_STARTED_AT[0] = time.time()
    try:
        return _feishu_smart_push_impl()
    finally:
        _FEISHU_PUSH_LOCK.release()


def _feishu_smart_push_impl():
    api_cfg = _load_api_config()
    # V4.8: case-insensitive config read (.api_config.json uses UPPERCASE,
    # but some legacy callers write lowercase). Always try both.
    def _pick(*keys):
        return _pick_config(api_cfg, *keys)
    ds_key = _pick("DEEPSEEK_API_KEY", "deepseek_api_key")
    feishu_app_id = _pick("FEISHU_APP_ID", "feishu_app_id")
    feishu_app_secret = _pick("FEISHU_APP_SECRET", "feishu_app_secret")
    feishu_chat_id = _pick("FEISHU_CHAT_ID", "feishu_chat_id")
    feishu_webhook = _pick("FEISHU_WEBHOOK", "feishu_webhook")

    if not ds_key:
        return Response(json.dumps({"ok": False, "error": "DeepSeek API Key 未配置"}),
                        mimetype='application/json', status=400)
    if not feishu_app_id or not feishu_chat_id:
        if not feishu_webhook:
            return Response(json.dumps({"ok": False,
                "error": "飞书凭证未配置 (需要 APP_ID+CHAT_ID 或 WEBHOOK)"}),
                            mimetype='application/json', status=400)

    # Read current instantaneous training state
    fsm_data = _read_current_fsm_data()

    # Extract dynamic properties from request
    req_data = request.get_json(silent=True) or {}
    push_type = req_data.get("type", "plan")
    custom_prompt = req_data.get("prompt", "")

    # Engage Cognitive Nexus to build enriched prompts
    try:
        import sys
        _he_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hardware_engine')
        if _he_path not in sys.path:
            sys.path.append(_he_path)
        from cognitive.cognitive_nexus import CognitiveNexus
        nexus_proxy = CognitiveNexus()
        prompts = nexus_proxy.build_prompt_for_type(push_type, fsm_data, custom_prompt)
        sys_prompt = prompts["system"]
        user_prompt = prompts["user"]
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": "Cognitive Nexus 挂载失败: " + str(e)}),
                        mimetype='application/json', status=500)

    # Call DeepSeek with historical context
    import urllib.request
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def _call_deepseek(sys_p, user_p, timeout_s):
        _payload = json.dumps({
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": sys_p},
                {"role": "user", "content": user_p}
            ],
            "temperature": 0.6,
            "max_tokens": 400,   # V7.21: 硬限输出长度 —— 高峰期 DeepSeek 吐 2000+ token 能拖 25s+, 限 400 通常 5-10s 可返
        }).encode("utf-8")
        _req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=_payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + ds_key,
            },
        )
        _t0 = time.time()
        _resp = json.loads(urllib.request.urlopen(_req, timeout=timeout_s, context=ctx).read())
        _elapsed = time.time() - _t0
        return _resp["choices"][0]["message"]["content"], _elapsed

    # V7.21 (2026-04-21): DeepSeek 失败不再 return 500 —— 改为降级纯数据模板, 仍推飞书
    # 原链路: DeepSeek 挂 → 500 → voice_daemon 兜底"服务器忙碌" → 用户看不到任何飞书消息
    # 新链路: DeepSeek 挂 → degraded=True + 模板文本 → 飞书仍推送 → 用户至少收到基础战报
    bot_reply = ""
    degraded = False
    deepseek_elapsed = 0.0
    deepseek_err = None
    for _attempt in (1, 2):   # V7.21: 20s → 15s 两次, 总最坏 35s < voice_daemon 75s 超时
        _tmo = 20 if _attempt == 1 else 15
        try:
            bot_reply, deepseek_elapsed = _call_deepseek(sys_prompt, user_prompt, _tmo)
            logging.info("[feishu_push] DeepSeek 成功 尝试%d 耗时%.1fs 长度%d", _attempt, deepseek_elapsed, len(bot_reply))
            break
        except Exception as e:
            deepseek_err = str(e)[:120]
            logging.warning("[feishu_push] DeepSeek 失败 尝试%d: %s", _attempt, deepseek_err)

    if not bot_reply:
        # DeepSeek 连续两次失败 → 降级模板
        degraded = True
        _good = fsm_data.get("good", 0)
        _failed = fsm_data.get("failed", 0)
        _comp = fsm_data.get("comp", 0)
        _fatigue = fsm_data.get("fatigue", 0)
        _ex = "弯举" if fsm_data.get("exercise") == "bicep_curl" else "深蹲"
        bot_reply = (
            "⚠️ AI 点评暂不可用（%s）—— 以下为原始战报：\n\n"
            "**动作**：%s\n"
            "**标准**：%s 次\n"
            "**不标准**：%s 次\n"
            "**代偿**：%s 次\n"
            "**疲劳池**：%.0f / %d\n\n"
            "_（DeepSeek 可能在限流或超时，飞书推送仍保障送达）_"
        ) % (
            deepseek_err or "unknown",
            _ex,
            _good,
            _failed,
            _comp,
            float(_fatigue or 0),
            int(fsm_data.get("fatigue_limit") or PLAN_DEFAULT_FATIGUE_TARGET),
        )

    # Push intelligent reply back to Feishu
    try:
        # Get token
        token_data = json.dumps({
            "app_id": feishu_app_id,
            "app_secret": feishu_app_secret,
        }).encode("utf-8")
        token_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=token_data,
            headers={"Content-Type": "application/json"},
        )
        # V7.20: 飞书 token timeout 10→8s
        token_resp = json.loads(urllib.request.urlopen(token_req, timeout=8, context=ctx).read())
        access_token = token_resp.get("tenant_access_token", "")

        # Format Final Feishu Message as one unified interactive card.
        card = _build_feishu_training_card(
            push_type,
            bot_reply,
            fsm_data=fsm_data,
            degraded=degraded,
            ds_error=deepseek_err,
        )
        msg_data = json.dumps({
            "receive_id": feishu_chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }).encode("utf-8")

        msg_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=msg_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + access_token,
            },
        )
        msg_resp = json.loads(urllib.request.urlopen(msg_req, timeout=15, context=ctx).read())
        if msg_resp.get("code") == 0:
            # V7.21: 回传 degraded + elapsed_s, 供 voice_daemon 和前端区分提示语
            logging.info("[feishu_push] ✅ 飞书送达 degraded=%s ds_elapsed=%.1fs", degraded, deepseek_elapsed)
            return Response(json.dumps({
                "ok": True,
                "type_triggered": push_type,
                "plan": bot_reply,
                "degraded": degraded,
                "elapsed_s": round(deepseek_elapsed, 2),
                "ds_error": deepseek_err if degraded else None,
                "msg_type": "interactive",
            }), mimetype='application/json')
        else:
            return Response(json.dumps({"ok": False, "error": "飞书发送失败", "detail": str(msg_resp)}),
                            mimetype='application/json', status=502)
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": "飞书推送链路中断: " + str(e), "plan": bot_reply}),
                        mimetype='application/json', status=500)


# ===== V2: 肌肉激活热力图 API =====

@app.route('/api/muscle_activation')
def muscle_activation():
    """读取肌肉激活数据（由 main_claw_loop V2 管线写入）"""
    # Absorb legacy pages that still poll this endpoint at 10Hz or faster.
    now = time.time()
    cache = getattr(muscle_activation, "_cache", None)
    if cache and now - cache.get("ts", 0.0) < 0.35:
        resp = Response(cache.get("body", "{}"), mimetype='application/json')
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        resp.headers['X-IronBuddy-Cache'] = 'hit'
        return resp
    body = '{"activations":{},"warnings":[],"exercise":null}'
    try:
        if os.path.exists("/dev/shm/muscle_activation.json"):
            with open("/dev/shm/muscle_activation.json", "r", encoding="utf-8") as f:
                body = f.read()
    except Exception:
        pass
    muscle_activation._cache = {"ts": now, "body": body}
    resp = Response(body, mimetype='application/json')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


def _emg_to_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def _emg_pct(sorted_vals, q):
    if not sorted_vals:
        return None
    idx = int(round((len(sorted_vals) - 1) * q))
    idx = max(0, min(len(sorted_vals) - 1, idx))
    return sorted_vals[idx]


def _emg_raw_stats(samples):
    ts_vals = []
    channels = [[], []]
    for row in samples or []:
        if isinstance(row, (list, tuple)) and len(row) >= 3:
            ts = _emg_to_float(row[0])
            if ts is not None:
                ts_vals.append(ts)
            for ch in (0, 1):
                val = _emg_to_float(row[ch + 1])
                if val is not None:
                    channels[ch].append(val)

    time_stats = {"span_s": None, "rate_hz": None, "dt_p95_ms": None, "dt_max_ms": None}
    if len(ts_vals) >= 2:
        dts = [max(0.0, ts_vals[i] - ts_vals[i - 1]) for i in range(1, len(ts_vals))]
        span_s = max(0.0, ts_vals[-1] - ts_vals[0])
        time_stats = {
            "span_s": round(span_s, 3),
            "rate_hz": round((len(ts_vals) - 1) / span_s, 1) if span_s > 0 else None,
            "dt_p95_ms": round((_emg_pct(sorted(dts), 0.95) or 0.0) * 1000.0, 2),
            "dt_max_ms": round(max(dts) * 1000.0, 2) if dts else None,
        }

    channel_stats = []
    for vals in channels:
        if not vals:
            channel_stats.append({"railish_ratio": None, "zero_ratio": None, "mean_abs_jump": None})
            continue
        n = float(len(vals))
        jumps = [abs(vals[i] - vals[i - 1]) for i in range(1, len(vals))]
        channel_stats.append({
            "zero_ratio": round(sum(1 for x in vals if x <= 5) / n, 3),
            "low_ratio": round(sum(1 for x in vals if x <= 100) / n, 3),
            "high_ratio": round(sum(1 for x in vals if x >= 3500) / n, 3),
            "railish_ratio": round(sum(1 for x in vals if x <= 100 or x >= 3500) / n, 3),
            "mean_abs_jump": round(sum(jumps) / float(len(jumps)), 3) if jumps else 0.0,
        })
    return {"time": time_stats, "channels": channel_stats}


def _read_emg_stream_buffer():
    path = "/dev/shm/emg_stream_buffer.json"
    body = {
        "ok": False,
        "source": path,
        "columns": [
            "ts", "raw0", "raw1", "filtered0", "filtered1",
            "rms0", "rms1", "pct0", "pct1", "packet_count"
        ],
        "channels": ["target_ch0", "comp_ch1"],
        "samples": [],
        "samples_count": 0,
        "age_s": None,
        "detail": "no stream buffer",
    }
    try:
        if not os.path.exists(path):
            return body
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        samples = data.get("samples", []) if isinstance(data, dict) else []
        if not isinstance(samples, list):
            samples = []
        ts = data.get("ts") if isinstance(data, dict) else None
        if not isinstance(ts, (int, float)):
            ts = os.path.getmtime(path)
        age_s = round(max(0.0, time.time() - float(ts)), 3) if ts else None
        body.update({
            "ok": bool(samples),
            "samples": samples[-1000:],
            "samples_count": len(samples[-1000:]),
            "sample_count": data.get("sample_count", len(samples)) if isinstance(data, dict) else len(samples),
            "packet_count": data.get("packet_count") if isinstance(data, dict) else None,
            "channels": data.get("channels") if isinstance(data, dict) else body["channels"],
            "columns": data.get("columns") if isinstance(data, dict) else body["columns"],
            "age_s": age_s,
            "detail": "ok" if samples else "empty stream buffer",
        })
    except Exception as exc:
        body["detail"] = str(exc)
    return body


@app.route('/api/emg_stream')
def emg_stream():
    """Low-latency EMG SSE batches for Lane B Sensor Lab."""
    try:
        interval_ms = int(request.args.get("interval_ms", "20"))
    except Exception:
        interval_ms = 20
    interval_s = max(0.016, min(0.2, interval_ms / 1000.0))

    def generate():
        last_packet = None
        last_send = 0.0
        while True:
            data = _read_emg_stream_buffer()
            samples = data.get("samples") or []
            batch = samples
            if last_packet is not None:
                filtered = []
                for row in samples:
                    pkt = None
                    if isinstance(row, (list, tuple)) and len(row) >= 10:
                        pkt = _emg_to_float(row[9])
                    if pkt is not None and pkt > last_packet:
                        filtered.append(row)
                batch = filtered
            if batch:
                last = batch[-1]
                if isinstance(last, (list, tuple)) and len(last) >= 10:
                    pkt = _emg_to_float(last[9])
                    if pkt is not None:
                        last_packet = pkt
            now = time.time()
            payload = {
                "ok": bool(data.get("ok")),
                "ts": now,
                "age_s": data.get("age_s"),
                "packet_count": data.get("packet_count"),
                "samples": batch[-220:],
                "samples_returned": len(batch[-220:]),
                "samples_count": data.get("samples_count"),
                "columns": data.get("columns"),
                "channels": data.get("channels"),
                "source": data.get("source"),
                "detail": data.get("detail"),
            }
            if batch or now - last_send >= 1.0:
                last_send = now
                yield "event: emg\n"
                yield "data: %s\n\n" % json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
            time.sleep(interval_s)

    resp = Response(generate(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


def _emg_signal_gate(age_s, debug, stats):
    transport_ok = bool(age_s is not None and age_s < 3.0)
    if not transport_ok:
        return {"transport_ok": False, "signal_mode": "udp_missing", "valid_for_gru": False}
    debug = debug if isinstance(debug, dict) else {}
    pct = debug.get("pct") if isinstance(debug.get("pct"), list) else [
        debug.get("target_pct"), debug.get("comp_pct")
    ]
    pct_nums = [_emg_to_float(v, 0.0) for v in (pct or [])[:2]]
    while len(pct_nums) < 2:
        pct_nums.append(0.0)
    channel_stats = stats.get("channels") if isinstance(stats, dict) else []
    railish = []
    jumps = []
    if isinstance(channel_stats, list):
        for ch in channel_stats[:2]:
            if isinstance(ch, dict):
                railish.append(_emg_to_float(ch.get("railish_ratio"), 0.0) or 0.0)
                jumps.append(_emg_to_float(ch.get("mean_abs_jump"), 0.0) or 0.0)
    railish_max = max(railish) if railish else 0.0
    jump_max = max(jumps) if jumps else 0.0
    pct_saturated = all(v >= 99.0 for v in pct_nums[:2])
    if pct_saturated and (railish_max > 0.25 or jump_max > 300.0):
        mode = "floating_no_contact"
        valid = False
    elif max(pct_nums) >= 20.0:
        mode = "active_candidate"
        valid = True
    else:
        mode = "contact_rest_candidate"
        valid = True
    return {
        "transport_ok": True,
        "signal_mode": mode,
        "valid_for_gru": valid,
        "railish_max": round(railish_max, 3),
        "mean_abs_jump_max": round(jump_max, 3),
        "pct_saturated": pct_saturated,
    }


@app.route('/api/emg_fast')
def emg_fast():
    """Read raw ADC waveform snapshots produced by udp_emg_server.py."""
    path = "/dev/shm/emg_raw_waveform.json"
    full = str(request.args.get("full") or "").lower() in ("1", "true", "yes")
    try:
        sample_limit = int(request.args.get("limit", 420 if full else 160))
    except Exception:
        sample_limit = 420 if full else 160
    sample_limit = max(20, min(420, sample_limit))
    cache_key = "full=%s;limit=%d" % ("1" if full else "0", sample_limit)
    now = time.time()
    cache_map = getattr(emg_fast, "_cache", {})
    cache = cache_map.get(cache_key)
    if cache and now - cache.get("ts", 0.0) < 0.45:
        resp = Response(cache.get("body", "{}"), mimetype='application/json')
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        resp.headers['X-IronBuddy-Cache'] = 'hit'
        return resp
    body = {
        "ok": False,
        "samples": [],
        "samples_count": 0,
        "samples_returned": 0,
        "sample_limit": sample_limit,
        "age_s": None,
        "source": path,
        "detail": "no raw waveform samples",
    }
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            samples = data.get("samples", []) if isinstance(data, dict) else []
            if not isinstance(samples, list):
                samples = []
            samples_all = samples[-420:]
            samples_returned = samples_all if full else samples_all[-sample_limit:]
            ts = data.get("ts") if isinstance(data, dict) else None
            if not isinstance(ts, (int, float)):
                ts = os.path.getmtime(path)
            age_s = round(max(0.0, time.time() - float(ts)), 3) if ts else None
            stats = _emg_raw_stats(samples_all)
            debug = {}
            try:
                with open("/dev/shm/emg_debug_snapshot.json", "r", encoding="utf-8") as df:
                    debug = json.load(df)
            except Exception:
                debug = {}
            gate = _emg_signal_gate(age_s, debug, stats)
            body.update({
                "ok": bool(samples_all),
                "samples": samples_returned,
                "samples_count": len(samples_all),
                "samples_returned": len(samples_returned),
                "age_s": age_s,
                "packet_count": data.get("packet_count") if isinstance(data, dict) else None,
                "sample_count": data.get("sample_count") if isinstance(data, dict) else len(samples_all),
                "channels": data.get("channels") if isinstance(data, dict) else None,
                "stats": stats,
                "signal_mode": gate.get("signal_mode"),
                "transport_ok": gate.get("transport_ok"),
                "valid_for_gru": gate.get("valid_for_gru"),
                "railish_max": gate.get("railish_max"),
                "mean_abs_jump_max": gate.get("mean_abs_jump_max"),
                "detail": "ok" if samples_all else "no raw waveform samples",
            })
    except Exception as e:
        body["detail"] = str(e)
    body_text = json.dumps(body, ensure_ascii=False)
    cache_map[cache_key] = {"ts": now, "body": body_text}
    emg_fast._cache = cache_map
    resp = Response(body_text, mimetype='application/json')
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp


@app.route('/api/user_profile', methods=['POST'])
def user_profile():
    """接收用户身体参数（身高/体重/动作/器材重量），写入共享内存供主循环读取"""
    try:
        data = request.get_json(force=True)
        with open("/dev/shm/user_profile.json.tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.rename("/dev/shm/user_profile.json.tmp", "/dev/shm/user_profile.json")
        return Response(json.dumps({"status": "ok"}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"status": "error", "msg": str(e)}), mimetype='application/json'), 500


# ===== V2.5: 训练历史页面 =====

@app.route('/history')
def history_page():
    """训练历史页面"""
    try:
        html_path = os.path.join(template_dir, 'history.html')
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        resp = Response(html_content, mimetype='text/html')
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    except Exception as e:
        return f"<h1>页面加载失败</h1><p>{e}</p>", 500


@app.route('/api/training_log')
def training_log():
    """返回训练日志 JSON（由 main_claw_loop 写入板端文件）"""
    log_path = "/home/toybrick/agent_memory/training_log.json"
    try:
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                data = f.read()
            resp = Response(data, mimetype='application/json')
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return resp
    except Exception:
        pass
    return Response('{}', mimetype='application/json')


def _latest_db_session_state():
    db_path = _db_view_path()
    if not os.path.exists(db_path):
        return {}
    try:
        import sqlite3 as _sq
        conn = _sq.connect(db_path, timeout=2.0)
        conn.row_factory = _sq.Row
        row = conn.execute(
            "SELECT * FROM training_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            conn.close()
            return {}
        data = dict(row)
        sid = data.get("id")
        sets = []
        if sid is not None:
            try:
                reps = conn.execute(
                    "SELECT * FROM rep_events WHERE session_id=? ORDER BY id ASC",
                    (sid,),
                ).fetchall()
                if reps:
                    good = 0
                    failed = 0
                    comp = 0
                    fatigue = data.get("fatigue_peak", 0) or 0
                    for rep in reps:
                        rd = dict(rep)
                        if int(rd.get("is_good") or 0):
                            good += 1
                        elif rd.get("model_class") == "compensating":
                            comp += 1
                        else:
                            failed += 1
                    sets.append({
                        "set_index": 1,
                        "good": good,
                        "failed": failed,
                        "comp": comp,
                        "fatigue": fatigue,
                    })
            except Exception:
                pass
        if sets:
            data["sets"] = sets
        conn.close()
        return data
    except Exception:
        return {}


def _read_training_session_state():
    state = _read_json_file(TRAINING_SESSION_PATH)
    return state if isinstance(state, dict) else {}


def _write_training_session_state(state):
    state = state if isinstance(state, dict) else {}
    state["updated_ts"] = time.time()
    _atomic_write_json_file(TRAINING_SESSION_PATH, state)
    return state


def _new_training_session_state(exercise, plan=None):
    now = time.time()
    return {
        "schema_version": 1,
        "exercise": exercise or "squat",
        "sets": [],
        "current_set": int((plan or {}).get("current_set") or 1),
        "started_ts": now,
        "plan_active": bool(plan),
        "src": "streamer_training_plan",
    }


def _current_exercise_from_fsm(default="bicep_curl"):
    fsm = _read_current_fsm_data()
    return fsm.get("exercise") or default


def _current_plan_state():
    if training_plan is None:
        return {}
    return training_plan.read_plan_state(
        path=TRAINING_PLAN_PATH,
        exercise=_current_exercise_from_fsm("bicep_curl"),
    )


def _current_training_report():
    if training_report is None:
        return {}
    plan = _current_plan_state()
    fsm = _read_current_fsm_data()
    session = _read_training_session_state() or _latest_db_session_state()
    return training_report.build_training_report(
        fsm_state=fsm,
        session_state=session,
        plan_state=plan,
    )


def _recent_db_rep_events(limit=8):
    db_path = _db_view_path()
    if not os.path.exists(db_path):
        return []
    try:
        import sqlite3 as _sq
        conn = _sq.connect(db_path, timeout=2.0)
        conn.row_factory = _sq.Row
        rows = conn.execute(
            "SELECT * FROM rep_events ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception:
        return []


def _exercise_label_for_ui(exercise):
    ex = (exercise or "squat")
    if training_plan is not None:
        try:
            return training_plan.exercise_label(ex)
        except Exception:
            pass
    return "哑铃弯举" if ex in ("curl", "bicep_curl") else "深蹲"


def _query_db_rows(table, order_by, limit=5, columns="*"):
    import sqlite3 as _sq
    db_path = _db_view_path()
    if not os.path.exists(db_path):
        return []
    try:
        conn = _sq.connect(db_path, timeout=2.0)
        conn.row_factory = _sq.Row
        sql = "SELECT %s FROM %s ORDER BY %s LIMIT ?" % (columns, table, order_by)
        rows = [dict(row) for row in conn.execute(sql, (int(limit),)).fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def _db_scalar(sql, params=None, default=0):
    import sqlite3 as _sq
    db_path = _db_view_path()
    if not os.path.exists(db_path):
        return default
    try:
        conn = _sq.connect(db_path, timeout=2.0)
        row = conn.execute(sql, params or ()).fetchone()
        conn.close()
        if not row:
            return default
        return row[0] if row[0] is not None else default
    except Exception:
        return default


def _latest_db_write_ts():
    try:
        db_path = _db_view_path()
        if os.path.exists(db_path):
            return time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(os.path.getmtime(db_path))
            )
    except Exception:
        pass
    return ""


def _deferred_db_status():
    try:
        from hardware_engine.persistence.db import FitnessDB
        return FitnessDB.get_deferred_status()
    except Exception:
        return {
            "enabled": False,
            "path": "",
            "updated_at": "",
            "pending_sessions": 0,
            "pending_rep_events": 0,
            "pending_llm_log": 0,
            "pending_voice_sessions": 0,
            "pending_total": 0,
        }


def _build_data_overview():
    plan = _current_plan_state()
    report = _current_training_report() if training_report is not None else {}
    today = {}
    stats = []
    try:
        db = _get_db()
        if db is not None:
            today = db.get_daily_summary(time.strftime("%Y-%m-%d")) or {}
            stats = db.get_range_stats(days=7) or []
    except Exception:
        today = {}
        stats = []
    recent_sessions = _query_db_rows(
        "training_sessions", "started_at DESC", limit=5,
        columns="id, started_at, ended_at, exercise, good_count, failed_count, fatigue_peak, duration_sec"
    )
    recent_voice = _query_db_rows(
        "voice_sessions", "ts DESC", limit=5,
        columns="id, ts, transcript, response, trigger_src"
    )
    recent_llm = _query_db_rows(
        "llm_log", "ts DESC", limit=5,
        columns="id, ts, trigger, prompt, response"
    )
    deferred = _deferred_db_status()
    return {
        "ok": True,
        "generated_ts": time.time(),
        "plan": plan,
        "report": report,
        "today": today,
        "range_stats": stats,
        "recent_sessions": recent_sessions,
        "recent_voice": recent_voice,
        "recent_llm": recent_llm,
        "db_buffer": deferred,
        "training_db_pending_flush": bool(deferred.get("pending_total")),
        "last_db_write": _latest_db_write_ts(),
        "db_path": _db_view_path(),
    }


@app.route('/api/data/overview', methods=['GET'])
def api_data_overview():
    return Response(json.dumps(_build_data_overview(), ensure_ascii=False, default=str),
                    mimetype='application/json')


@app.route('/api/db/overview', methods=['GET'])
def api_db_overview():
    data = _build_data_overview()
    try:
        tables = json.loads(api_db_tables().get_data(as_text=True))
    except Exception:
        tables = {"tables": [], "db_path": _db_view_path(), "db_exists": False}
    data["tables"] = tables.get("tables", [])
    data["db_exists"] = tables.get("db_exists", False)
    data["db_size_kb"] = tables.get("db_size_kb", 0)
    data["db_buffer"] = _deferred_db_status()
    return Response(json.dumps(data, ensure_ascii=False, default=str),
                    mimetype='application/json')


def _score_recent_training(rows):
    total_good = 0
    total_failed = 0
    fatigue_peak = 0.0
    for row in rows:
        try:
            total_good += int(row.get("good_count") or 0)
            total_failed += int(row.get("failed_count") or 0)
            fatigue_peak = max(fatigue_peak, float(row.get("fatigue_peak") or 0.0))
        except Exception:
            pass
    total = total_good + total_failed
    quality = float(total_good) / float(total) if total else 0.0
    return total, quality, fatigue_peak


def _extract_json_object(text):
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            return None
    return None


def _daily_plan_history_context(rows, fsm):
    rows = rows if isinstance(rows, list) else []
    total, quality, fatigue_peak = _score_recent_training(rows)
    latest = rows[0] if rows else {}
    return {
        "recent_session_count": len(rows),
        "recent_total_reps": total,
        "recent_quality_pct": round(quality * 100.0, 1) if total else 0.0,
        "fatigue_peak": round(fatigue_peak, 1),
        "latest_session": {
            "started_at": latest.get("started_at") if latest else "",
            "exercise": latest.get("exercise") if latest else "",
            "good_count": int((latest or {}).get("good_count") or 0),
            "failed_count": int((latest or {}).get("failed_count") or 0),
            "fatigue_peak": float((latest or {}).get("fatigue_peak") or 0.0),
        },
        "current": {
            "state": fsm.get("state") or fsm.get("status") or "",
            "exercise": fsm.get("exercise") or "squat",
            "good": int(fsm.get("good") or 0),
            "failed": int(fsm.get("failed") or 0),
            "comp": int(fsm.get("comp") or 0),
            "fatigue": float(fsm.get("fatigue") or 0.0),
            "total_reps": int(fsm.get("total_reps") or 0),
            "inference_mode": fsm.get("inference_mode") or "",
        },
    }


def _daily_plan_rows_and_context():
    fsm = _read_current_fsm_data()
    rows = _query_db_rows(
        "training_sessions", "started_at DESC", limit=7,
        columns="id, started_at, exercise, good_count, failed_count, fatigue_peak, duration_sec"
    )
    return rows, _daily_plan_history_context(rows, fsm)


def _make_rule_daily_plan(exercise=None):
    fsm = _read_current_fsm_data()
    exercise = exercise or fsm.get("exercise") or "squat"
    if exercise == "curl":
        exercise = "bicep_curl"
    rows = _query_db_rows(
        "training_sessions", "started_at DESC", limit=7,
        columns="id, started_at, exercise, good_count, failed_count, fatigue_peak, duration_sec"
    )
    recent_total, quality, fatigue_peak = _score_recent_training(rows)
    current_fatigue = 0.0
    try:
        current_fatigue = float(fsm.get("fatigue") or 0.0)
    except Exception:
        current_fatigue = 0.0
    effective_peak = max(float(fatigue_peak or 0.0), current_fatigue)
    set_count = 3
    reps = 8
    fatigue_base = PLAN_DEFAULT_FATIGUE_TARGET
    if recent_total >= 40 and quality >= 0.85 and effective_peak < PLAN_ADVANCED_FATIGUE_TARGET:
        reps = 10
        fatigue_base = PLAN_ADVANCED_FATIGUE_TARGET
        intensity = "进阶"
        reason = "在线知识库不可用，按本地规则保守生成疲劳目标；最近动作质量稳定，可以小幅增加强度。"
    elif effective_peak >= PLAN_MAX_FATIGUE_TARGET or (recent_total and quality < 0.65):
        reps = 6
        fatigue_base = PLAN_RECOVERY_FATIGUE_TARGET
        intensity = "恢复"
        reason = "在线知识库不可用，按本地规则保守生成疲劳目标；最近疲劳或动作质量压力偏高。"
    else:
        intensity = "稳态"
        reason = "在线知识库不可用，按本地规则保守生成疲劳目标；历史数据处在可控区间。"
    sets = [reps for _ in range(set_count)]
    fatigue_targets = [
        min(PLAN_MAX_FATIGUE_TARGET, fatigue_base + PLAN_FATIGUE_TARGET_STEP * idx)
        for idx in range(set_count)
    ]
    label = _exercise_label_for_ui(exercise)
    summary = "今日建议：%s %d 组，目标疲劳 %d/%d/%d，强度为%s。" % (
        label, set_count, fatigue_targets[0], fatigue_targets[1], fatigue_targets[2], intensity
    )
    return {
        "ok": True,
        "schema_version": 1,
        "status": "generated",
        "source": "rule_fallback",
        "exercise": exercise,
        "exercise_label": label,
        "target_type": "fatigue",
        "set_count": set_count,
        "reps_per_set": reps,
        "set_targets": fatigue_targets,
        "fatigue_targets": fatigue_targets,
        "estimated_rep_range": [max(3, reps - 2), reps + 2],
        "evidence_ids": [],
        "intensity": intensity,
        "summary": summary,
        "reason": reason,
        "stages": [
            {"key": "history", "label": "读取历史", "done": True},
            {"key": "thinking", "label": "思考中", "done": True},
            {"key": "plan", "label": "生成计划", "done": True},
            {"key": "accept", "label": "等待采纳", "done": False},
        ],
        "context": {
            "recent_session_count": len(rows),
            "recent_total_reps": recent_total,
            "recent_quality_pct": round(quality * 100.0, 1) if recent_total else 0.0,
            "fatigue_peak": round(fatigue_peak, 1),
            "current_fatigue": round(current_fatigue, 1),
            "effective_fatigue_peak": round(effective_peak, 1),
            "current_status": fsm.get("status") or fsm.get("state") or "",
        },
        "created_ts": time.time(),
        "updated_ts": time.time(),
    }


def _daily_plan_prompt(prompt_text, context):
    rag_evidence = (context or {}).get("rag_evidence") or {}
    evidence_hits = rag_evidence.get("hits") if isinstance(rag_evidence, dict) else []
    evidence_label = "向量 RAG"
    if isinstance(rag_evidence, dict):
        mode = str(rag_evidence.get("source_mode") or "")
        if mode == "online_pending_vector_ingest":
            evidence_label = "外部来源待入库证据"
        elif mode == "online":
            evidence_label = "在线外部来源证据"
    evidence_ids = []
    if isinstance(evidence_hits, list):
        for hit in evidence_hits:
            if isinstance(hit, dict) and hit.get("id"):
                evidence_ids.append(str(hit.get("id")))
    system_prompt = (
        "你是 IronBuddy 的现场健身教练。你必须基于真实训练历史和当前状态，"
        "以及真实专业证据，快速生成今天可直接执行的深蹲计划。\n"
        "只输出 JSON，不要 Markdown，不要解释 JSON 外的文字。\n"
        "硬性边界：exercise 必须是 squat；set_count 必须是 3；target_type 必须是 fatigue；"
        "fatigue_targets 必须是 3 个整数，每个在 %d 到 %d 之间；"
        "estimated_rep_range 是两个整数，仅作为预计次数范围，不是完成目标；"
        "evidence_ids 必须引用真实上下文里的证据 id；intensity 只能是 恢复、稳态、进阶；"
        "summary 和 reason 用中文，reason 必须说明疲劳目标依据，coach_line 是一句适合语音播报的短句。"
    ) % (PLAN_MIN_FATIGUE_TARGET, PLAN_MAX_FATIGUE_TARGET)
    user_prompt = (
        "用户请求：%s\n\n"
        "真实上下文：%s\n\n"
        "可引用%s id：%s\n\n"
        "请输出 JSON，字段为："
        "exercise, set_count, target_type, fatigue_targets, estimated_rep_range, evidence_ids, intensity, summary, reason, coach_line。"
    ) % (
        prompt_text or "请帮我规划今日训练",
        json.dumps(context or {}, ensure_ascii=False, sort_keys=True),
        evidence_label,
        json.dumps(evidence_ids, ensure_ascii=False),
    )
    return system_prompt, user_prompt


def _normalize_deepseek_daily_plan(raw, context):
    if not isinstance(raw, dict):
        return None, "not_json_object"
    exercise = str(raw.get("exercise") or "squat").strip()
    if exercise in ("curl", "bicep_curl"):
        return None, "exercise_not_allowed"
    exercise = "squat"
    try:
        set_count = int(raw.get("set_count") or 3)
    except Exception:
        set_count = 3
    if set_count != 3:
        return None, "invalid_set_count"
    targets = raw.get("fatigue_targets")
    if not isinstance(targets, list) or len(targets) != 3:
        return None, "invalid_fatigue_targets"
    clean_targets = []
    for item in targets:
        try:
            v = int(round(float(item)))
        except Exception:
            return None, "invalid_fatigue_target_value"
        if v < PLAN_MIN_FATIGUE_TARGET or v > PLAN_MAX_FATIGUE_TARGET:
            return None, "fatigue_target_out_of_range"
        clean_targets.append(v)
    evidence_ids = raw.get("evidence_ids")
    if not isinstance(evidence_ids, list):
        return None, "missing_online_evidence"
    clean_evidence_ids = [str(item).strip() for item in evidence_ids if str(item).strip()]
    ctx_hits = (((context or {}).get("rag_evidence") or {}).get("hits") or [])
    allowed_ids = set()
    if isinstance(ctx_hits, list):
        for hit in ctx_hits:
            if isinstance(hit, dict) and hit.get("id"):
                allowed_ids.add(str(hit.get("id")))
    if not allowed_ids:
        return None, "missing_online_evidence"
    if not clean_evidence_ids or not any(eid in allowed_ids for eid in clean_evidence_ids):
        return None, "missing_online_evidence"
    estimated = raw.get("estimated_rep_range")
    clean_estimated = [5, 10]
    if isinstance(estimated, list) and len(estimated) >= 2:
        try:
            lo = max(1, min(50, int(round(float(estimated[0])))))
            hi = max(lo, min(60, int(round(float(estimated[1])))))
            clean_estimated = [lo, hi]
        except Exception:
            clean_estimated = [5, 10]
    intensity = str(raw.get("intensity") or "稳态").strip()
    if intensity not in ("恢复", "稳态", "进阶"):
        return None, "invalid_intensity"
    summary = str(raw.get("summary") or "").strip()
    reason = str(raw.get("reason") or "").strip()
    coach_line = str(raw.get("coach_line") or "").strip()
    if not summary:
        summary = "今日建议：深蹲 3 组，目标疲劳为 %d/%d/%d，强度为%s。" % (
            clean_targets[0], clean_targets[1], clean_targets[2], intensity
        )
    if not reason:
        reason = "已结合在线专业证据、最近训练质量、疲劳峰值和当前状态生成。"
    if not coach_line:
        coach_line = summary
    plan = {
        "ok": True,
        "schema_version": 1,
        "status": "generated",
        "source": "deepseek",
        "exercise": exercise,
        "exercise_label": _exercise_label_for_ui(exercise),
        "target_type": "fatigue",
        "set_count": 3,
        "reps_per_set": clean_estimated[1],
        "set_targets": clean_targets,
        "fatigue_targets": clean_targets,
        "estimated_rep_range": clean_estimated,
        "evidence_ids": clean_evidence_ids,
        "intensity": intensity,
        "summary": summary[:160],
        "reason": reason[:260],
        "coach_line": coach_line[:120],
        "stages": [
            {"key": "history", "label": "读取历史", "done": True},
            {"key": "state", "label": "分析状态", "done": True},
            {"key": "rag", "label": "在线知识库", "done": True},
            {"key": "deepseek", "label": "DeepSeek 规划", "done": True},
            {"key": "accept", "label": "等待采纳", "done": False},
        ],
        "context": context or {},
        "created_ts": time.time(),
        "updated_ts": time.time(),
    }
    return plan, ""


def _call_deepseek_daily_plan(prompt_text, context, timeout_s=8.0):
    api_cfg = _load_api_config()
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        api_key = _pick_config(api_cfg, "DEEPSEEK_API_KEY", "deepseek_api_key")
    if not api_key:
        return None, {"ok": False, "error": "missing_api_key"}
    context = dict(context or {})
    if not context.get("rag_evidence"):
        query = "%s resistance training fatigue surface EMG velocity loss" % (
            prompt_text or "今日训练计划"
        )
        context["rag_evidence"] = _search_lane_a_professional_knowledge(query, limit=3)
    system_prompt, user_prompt = _daily_plan_prompt(prompt_text, context)
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 420,
        "stream": False,
    }
    t0 = time.time()
    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
            },
            json=payload,
            timeout=float(timeout_s or 8.0),
        )
        elapsed = time.time() - t0
        if resp.status_code >= 400:
            return None, {
                "ok": False,
                "error": "http_%s" % resp.status_code,
                "elapsed_s": round(elapsed, 2),
            }
        data = resp.json()
        content = data["choices"][0]["message"].get("content", "")
        raw = _extract_json_object(content)
        plan, reason = _normalize_deepseek_daily_plan(raw, context)
        if not plan:
            return None, {
                "ok": False,
                "error": reason or "invalid_plan",
                "elapsed_s": round(elapsed, 2),
                "raw_preview": str(content)[:180],
            }
        plan["deepseek_elapsed_s"] = round(elapsed, 2)
        trace = json.dumps({
            "prompt": prompt_text,
            "context": context,
            "model_reply": content,
        }, ensure_ascii=False, sort_keys=True)
        _log_real_llm_event("daily_plan_deepseek", user_prompt, content)
        return plan, {"ok": True, "elapsed_s": round(elapsed, 2), "trace": trace}
    except Exception as exc:
        return None, {
            "ok": False,
            "error": type(exc).__name__ + ":" + str(exc)[:120],
            "elapsed_s": round(time.time() - t0, 2),
        }


def _read_daily_plan_state():
    state = _read_json_file(DAILY_PLAN_PATH)
    return state if isinstance(state, dict) else {}


def _write_daily_plan_state(state):
    state = state if isinstance(state, dict) else {}
    state["updated_ts"] = time.time()
    _atomic_write_json_file(DAILY_PLAN_PATH, state)
    return state


def _publish_plan_reply(plan):
    text = (plan or {}).get("coach_line") or (plan or {}).get("summary") or "今日训练方案已生成，等待采纳。"
    source = (plan or {}).get("source") or ""
    prefix = "DeepSeek 已生成。" if source == "deepseek" else "基础方案已生成。"
    reply = prefix + text + " 你说采纳方案或点击采纳，就直接开始。"
    _append_chat_event("assistant", reply, kind="daily_plan", stage="assistant_reply")
    _log_real_voice_session(
        "daily_plan",
        (plan or {}).get("prompt") or "请帮我规划今日训练",
        reply,
        summary=(plan or {}).get("summary"),
    )
    _log_real_llm_event(
        "daily_plan",
        (plan or {}).get("prompt") or "请帮我规划今日训练",
        reply,
    )
    try:
        tmp = "/dev/shm/chat_reply.txt.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(reply)
        os.rename(tmp, "/dev/shm/chat_reply.txt")
        with open("/dev/shm/chat_reply.txt.seq.tmp", "w") as sf:
            sf.write(str(_read_chat_event_seq()))
        os.rename("/dev/shm/chat_reply.txt.seq.tmp", "/dev/shm/chat_reply.txt.seq")
    except Exception:
        pass
    return reply


@app.route('/api/training_plan/daily', methods=['GET', 'POST'])
def api_daily_training_plan():
    if request.method == 'GET':
        state = _read_daily_plan_state()
        if not state:
            state = _make_rule_daily_plan(_current_exercise_from_fsm("squat"))
            state["status"] = "idle"
        return Response(json.dumps({"ok": True, "plan": state, "path": DAILY_PLAN_PATH},
                                   ensure_ascii=False, default=str),
                        mimetype='application/json')
    body = request.get_json(force=True, silent=True) or {}
    prompt_text = str(body.get("prompt") or "请帮我规划今日训练")[:240]
    context_rows, context = _daily_plan_rows_and_context()
    plan, ds_info = _call_deepseek_daily_plan(
        prompt_text, context, timeout_s=float(body.get("timeout_s") or 8.0)
    )
    if not plan:
        plan = _make_rule_daily_plan("squat")
        plan["source"] = "rule_fallback"
        plan["fallback_reason"] = (ds_info or {}).get("error") or "deepseek_unavailable"
        plan["deepseek"] = ds_info or {"ok": False}
        plan["context"] = context
    else:
        plan["deepseek"] = {
            "ok": True,
            "elapsed_s": (ds_info or {}).get("elapsed_s"),
        }
    plan["prompt"] = prompt_text
    _write_daily_plan_state(plan)
    if body.get("speak", True):
        reply = _publish_plan_reply(plan)
    else:
        reply = ""
        trace = ((plan.get("summary") or "") + " " + (plan.get("reason") or "")).strip()
        _log_real_voice_session(
            "daily_plan",
            prompt_text,
            trace,
            summary=plan.get("summary"),
        )
        _log_real_llm_event("daily_plan", prompt_text, trace)
    return Response(json.dumps({"ok": True, "plan": plan, "spoken_reply": reply},
                               ensure_ascii=False, default=str),
                    mimetype='application/json')


@app.route('/api/training_plan/daily/accept', methods=['POST'])
def api_daily_training_plan_accept():
    if training_plan is None:
        return Response(json.dumps({"ok": False, "error": "training_plan unavailable"}),
                        mimetype='application/json', status=503)
    body = request.get_json(force=True, silent=True) or {}
    plan_state = _read_daily_plan_state() or _make_rule_daily_plan(
        body.get("exercise") or _current_exercise_from_fsm("squat")
    )
    exercise = plan_state.get("exercise") or "squat"
    fatigue_targets = plan_state.get("fatigue_targets") or plan_state.get("set_targets") or []
    if not isinstance(fatigue_targets, list) or not fatigue_targets:
        fatigue_targets = [PLAN_DEFAULT_FATIGUE_TARGET] * int(plan_state.get("set_count") or 3)
    estimated = plan_state.get("estimated_rep_range") or [6, 10]
    reps_hint = estimated[1] if isinstance(estimated, list) and len(estimated) >= 2 else plan_state.get("reps_per_set") or 8
    # Keep the existing exercise switch contract so FSM consumes the same signal.
    norm_mode = "curl" if exercise in ("curl", "bicep_curl") else "squat"
    switch_payload = json.dumps({"mode": norm_mode, "ts": time.time(),
                                 "src": "daily_plan_accept", "ttl_s": 30})
    try:
        _atomic_write_json("/dev/shm/exercise_mode.json", switch_payload)
        _atomic_write_json("/dev/shm/intent_exercise_mode.json", switch_payload)
    except Exception:
        pass
    runtime_plan = training_plan.update_plan_state(
        path=TRAINING_PLAN_PATH,
        exercise=exercise,
        set_count=len(fatigue_targets),
        reps_per_set=reps_hint,
        current_set=1,
        set_targets=dict((idx + 1, reps_hint) for idx, _value in enumerate(fatigue_targets)),
        fatigue_targets=dict((idx + 1, value) for idx, value in enumerate(fatigue_targets)),
    )
    try:
        first_limit = int(fatigue_targets[0])
        _atomic_write_json_file("/dev/shm/fatigue_limit.json", {
            "limit": first_limit,
            "src": "daily_plan_accept",
            "ts": time.time(),
        })
        _atomic_write_json_file("/dev/shm/ui_fatigue_limit.json", {
            "limit": first_limit,
            "src": "daily_plan_accept",
            "ts": time.time(),
        })
    except Exception:
        pass
    session = _new_training_session_state(runtime_plan.get("exercise"), plan=runtime_plan)
    session.update({
        "plan_active": True,
        "plan_started_ts": time.time(),
        "src": "daily_plan_accept",
    })
    _write_training_session_state(session)
    try:
        with open("/dev/shm/fsm_reset_signal", "w", encoding="utf-8") as f:
            f.write("reset")
    except Exception:
        pass
    plan_state["status"] = "accepted"
    plan_state["accepted_ts"] = time.time()
    _write_daily_plan_state(plan_state)
    reply = "已采纳今日方案，%s第 1 组开始，目标疲劳 %d。" % (
        _exercise_label_for_ui(exercise), int(fatigue_targets[0] or PLAN_DEFAULT_FATIGUE_TARGET)
    )
    _append_chat_event("assistant", reply, kind="daily_plan", stage="assistant_reply")
    _log_real_voice_session(
        "daily_plan_accept",
        "采纳方案",
        reply,
        summary=plan_state.get("summary"),
    )
    _log_real_llm_event("daily_plan_accept", "采纳方案", reply)
    return Response(json.dumps({
        "ok": True,
        "daily_plan": plan_state,
        "plan": runtime_plan,
        "session": session,
        "reply": reply,
    }, ensure_ascii=False, default=str), mimetype='application/json')


def _operator_run_dir():
    today = time.strftime("%Y%m%d")
    path = os.path.join(OPERATOR_RECORD_ROOT, "embedded_ui_" + today)
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    return path


@app.route('/api/operator/record', methods=['GET', 'POST'])
def api_operator_record():
    run_dir = _operator_run_dir()
    events_path = os.path.join(run_dir, "events.jsonl")
    if request.method == 'GET':
        items = []
        try:
            if os.path.exists(events_path):
                with open(events_path, "r", encoding="utf-8") as f:
                    for line in f.readlines()[-30:]:
                        try:
                            items.append(json.loads(line))
                        except Exception:
                            pass
        except Exception:
            items = []
        return Response(json.dumps({
            "ok": True,
            "run_dir": run_dir,
            "events_path": events_path,
            "items": items,
        }, ensure_ascii=False, default=str), mimetype='application/json')
    body = request.get_json(force=True, silent=True) or {}
    note = str(body.get("note") or "")[:1200]
    kind = str(body.get("kind") or "note")[:40]
    screenshot_path = ""
    image_data = body.get("image_data")
    if isinstance(image_data, str) and image_data.startswith("data:image/"):
        try:
            header, b64 = image_data.split(",", 1)
            ext = "png"
            if "jpeg" in header or "jpg" in header:
                ext = "jpg"
            raw = base64.b64decode(b64)
            shot_dir = os.path.join(run_dir, "screenshots")
            if not os.path.isdir(shot_dir):
                os.makedirs(shot_dir, exist_ok=True)
            screenshot_path = os.path.join(
                shot_dir, "ui_%s.%s" % (time.strftime("%H%M%S"), ext)
            )
            with open(screenshot_path, "wb") as f:
                f.write(raw)
        except Exception as exc:
            screenshot_path = "decode_failed: " + str(exc)[:120]
    event = {
        "ts": time.time(),
        "ts_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "note": note,
        "screenshot_path": screenshot_path,
        "fsm": _read_current_fsm_data(),
        "plan": _current_plan_state(),
    }
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    return Response(json.dumps({
        "ok": True,
        "run_dir": run_dir,
        "event": event,
    }, ensure_ascii=False, default=str), mimetype='application/json')


@app.route('/api/training_session/evidence')
def api_training_session_evidence():
    """Recording-facing evidence for the current take, not old DB tables."""
    session = _read_training_session_state()
    plan = _current_plan_state()
    report = _current_training_report() if training_report is not None else {}
    return Response(json.dumps({
        "ok": True,
        "session": session,
        "plan": plan,
        "report": report,
        "db_latest_session": _latest_db_session_state(),
        "recent_rep_events": _recent_db_rep_events(limit=8),
        "runtime_session_path": TRAINING_SESSION_PATH,
        "runtime_plan_path": TRAINING_PLAN_PATH,
        "db_path": _db_view_path(),
    }, ensure_ascii=False, default=str), mimetype='application/json')


@app.route('/api/training_plan', methods=['GET', 'POST'])
def api_training_plan():
    """Runtime training plan: default 3 sets x 8 reps for current exercise."""
    if training_plan is None:
        return Response(json.dumps({"ok": False, "error": "training_plan unavailable"}),
                        mimetype='application/json', status=503)
    if request.method == 'GET':
        plan = _current_plan_state()
        report = _current_training_report() if training_report is not None else {}
        return Response(json.dumps({
            "ok": True,
            "plan": plan,
            "report": report,
            "state_path": TRAINING_PLAN_PATH,
            "session_path": TRAINING_SESSION_PATH,
        }, ensure_ascii=False, default=str), mimetype='application/json')

    data = request.get_json(force=True, silent=True) or {}
    exercise = data.get("exercise") or _current_exercise_from_fsm("bicep_curl")
    set_targets = data.get("set_targets")
    if isinstance(set_targets, list):
        set_targets = dict((idx + 1, value) for idx, value in enumerate(set_targets))
    fatigue_targets = data.get("fatigue_targets")
    if isinstance(fatigue_targets, list):
        fatigue_targets = dict((idx + 1, value) for idx, value in enumerate(fatigue_targets))
    plan = training_plan.update_plan_state(
        path=TRAINING_PLAN_PATH,
        exercise=exercise,
        set_count=data.get("set_count"),
        reps_per_set=data.get("reps_per_set"),
        current_set=data.get("current_set"),
        set_targets=set_targets if isinstance(set_targets, dict) else None,
        fatigue_targets=fatigue_targets if isinstance(fatigue_targets, dict) else None,
        weight_kg=data.get("weight_kg"),
    )
    if bool(data.get("activate", True)):
        reset_session = bool(data.get("reset_session", data.get("reset", False)))
        if reset_session:
            state = _new_training_session_state(plan.get("exercise"), plan=plan)
        else:
            state = _read_training_session_state()
        state.update({
            "exercise": plan.get("exercise"),
            "sets": state.get("sets") if isinstance(state.get("sets"), list) else [],
            "current_set": plan.get("current_set", 1),
            "started_ts": state.get("started_ts") or time.time(),
            "plan_active": True,
            "plan_started_ts": state.get("plan_started_ts") or time.time(),
        })
        _write_training_session_state(state)
    return Response(json.dumps({"ok": True, "plan": plan}, ensure_ascii=False),
                    mimetype='application/json')


@app.route('/api/training_plan/next_set', methods=['POST'])
def api_training_plan_next_set():
    if training_plan is None:
        return Response(json.dumps({"ok": False, "error": "training_plan unavailable"}),
                        mimetype='application/json', status=503)
    plan = _current_plan_state()
    current = int(plan.get("current_set") or 1)
    next_set = min(current + 1, len(plan.get("sets") or [1]))
    plan = training_plan.update_plan_state(
        path=TRAINING_PLAN_PATH,
        exercise=plan.get("exercise"),
        current_set=next_set,
    )
    state = _read_training_session_state()
    state["current_set"] = next_set
    _write_training_session_state(state)
    next_target = PLAN_DEFAULT_FATIGUE_TARGET
    try:
        sets = plan.get("sets") or []
        if next_set <= len(sets):
            next_target = int((sets[next_set - 1] or {}).get("target_fatigue") or PLAN_DEFAULT_FATIGUE_TARGET)
        _atomic_write_json_file("/dev/shm/fatigue_limit.json", {
            "limit": next_target,
            "src": "training_plan_next_set",
            "ts": time.time(),
        })
        _atomic_write_json_file("/dev/shm/ui_fatigue_limit.json", {
            "limit": next_target,
            "src": "training_plan_next_set",
            "ts": time.time(),
        })
    except Exception:
        pass
    try:
        _atomic_write_json_file("/dev/shm/next_set.request", {
            "ts": time.time(),
            "exercise": plan.get("exercise"),
            "completed_set": current,
            "next_set": next_set,
            "next_target_fatigue": next_target,
            "src": "training_plan_api",
        })
    except Exception:
        pass
    return Response(json.dumps({
        "ok": True,
        "plan": plan,
        "completed_set": current,
        "next_set": next_set,
    }, ensure_ascii=False),
                    mimetype='application/json')


@app.route('/api/training_report', methods=['GET', 'POST'])
def api_training_report():
    if training_report is None:
        return Response(json.dumps({"ok": False, "error": "training_report unavailable"}),
                        mimetype='application/json', status=503)
    data = request.get_json(force=True, silent=True) if request.method == 'POST' else {}
    data = data or {}
    report = _current_training_report()
    send = bool(data.get("send", False))
    dry_run = bool(data.get("dry_run", not send))
    card = training_report.build_feishu_session_report_card(report)
    send_result = None
    if request.method == 'POST':
        send_result = _send_feishu_card(card, _load_api_config(), dry_run=dry_run)
    return Response(json.dumps({
        "ok": True if send_result is None else bool(send_result.get("ok")),
        "report": report,
        "card": card,
        "send_result": send_result,
        "dry_run": dry_run,
    }, ensure_ascii=False, default=str), mimetype='application/json')


# ===== V3.1: Admin Management Panel =====
BOARD_IP = os.environ.get("IRONBUDDY_BOARD_IP", "10.29.10.224")
BOARD_TARGET = "toybrick@%s" % BOARD_IP
BOARD_KEY_PATH = os.path.expanduser("~/.ssh/id_rsa_toybrick")
CLOUD_SSH = os.environ.get("CLOUD_SSH_HOST", "root@<set-CLOUD_SSH_HOST-env-var>")
CLOUD_PORT = 50203  # 2026-05-04 competition sprint cloud instance
CLOUD_KEY_PATH = os.path.expanduser("~/.ssh/id_cloud_autodl")

# Service process signatures for pgrep
SERVICE_SIGNATURES = {
    "vision": "cloud_rtmpose_client.py",
    "streamer": "streamer_app.py",
    "fsm": "main_claw_loop.py",
    "emg": "udp_emg_server.py",
    "voice": "voice_daemon.py",
}


def _run_cmd(cmd, timeout=5):
    """Run a shell command, return (success, stdout)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip()
    except Exception as e:
        return False, str(e)


def _service_pids_by_signature(sig):
    """Return live PIDs whose /proc cmdline contains sig.

    This avoids broad pkill/pgrep patterns matching the Flask request shell
    while the main UI is trying to stop training services for recording.
    """
    pids = []
    try:
        proc_root = "/proc"
        for entry in os.listdir(proc_root):
            if not entry.isdigit():
                continue
            cmdline_path = os.path.join(proc_root, entry, "cmdline")
            try:
                with open(cmdline_path, "rb") as cf:
                    raw = cf.read()
            except Exception:
                continue
            if not raw:
                continue
            argv = [x.decode("utf-8", "ignore") for x in raw.split(b"\x00") if x]
            if sig.endswith(".py"):
                sig_base = os.path.basename(sig)
                if any(os.path.basename(arg) == sig_base for arg in argv):
                    pids.append(entry)
            else:
                text = " ".join(argv)
                if sig in text:
                    pids.append(entry)
    except Exception:
        pass
    return pids


def _local_cmd(cmd, timeout=5):
    """Run command locally on the board (APP runs on board itself)."""
    return _run_cmd(cmd, timeout=timeout)


def _ssh_board_cmd(cmd, timeout=5):
    """Run command on board — local first, SSH fallback."""
    # If we're ON the board, run locally (detect by hostname or always try local)
    import platform
    if 'toybrick' in platform.node() or 'debian10' in platform.node() or os.path.exists('/dev/shm/pose_data.json'):
        return _local_cmd(cmd, timeout=timeout)
    # Otherwise SSH
    if not os.path.exists(BOARD_KEY_PATH):
        return False, "SSH key not found"
    ssh = 'ssh -i {} -o StrictHostKeyChecking=no -o ConnectTimeout=3 {} "{}"'.format(
        BOARD_KEY_PATH, BOARD_TARGET, cmd
    )
    return _run_cmd(ssh, timeout=timeout)


@app.route('/admin')
def admin_page():
    """Admin panel now integrated into main page — redirect."""
    return redirect('/', code=302)


@app.route('/api/admin/overview')
def admin_overview():
    """Quick overview: board online, service count, data count"""
    board_ok, _ = _ssh_board_cmd("echo ok", timeout=3)

    data_dir = os.path.join(PROJECT_ROOT, "data")
    csv_count = len(glob_mod.glob(os.path.join(data_dir, "**", "*.csv"), recursive=True))

    model_path = os.path.join(PROJECT_ROOT, "models", "extreme_fusion_gru.pt")
    model_exists = os.path.exists(model_path)
    model_size = os.path.getsize(model_path) if model_exists else 0

    result = {
        "board_online": board_ok,
        "board_ip": BOARD_IP,
        "csv_count": csv_count,
        "model_exists": model_exists,
        "model_size_kb": round(model_size / 1024, 1) if model_exists else 0,
    }
    return Response(json.dumps(result), mimetype='application/json')


@app.route('/api/admin/services')
def admin_services():
    """Check which services are running on the board"""
    result = {}
    board_ok, _ = _ssh_board_cmd("echo ok", timeout=3)
    if not board_ok:
        for name in SERVICE_SIGNATURES:
            result[name] = {"running": False, "pid": None, "error": "board offline"}
        return Response(json.dumps({"board_online": False, "services": result}), mimetype='application/json')

    for name, sig in SERVICE_SIGNATURES.items():
        if os.path.isdir("/home/toybrick/streamer_v3") or os.path.exists("/dev/shm"):
            pids = _service_pids_by_signature(sig)
            pid = pids[0] if pids else None
        else:
            ok, out = _ssh_board_cmd(
                "python3 - <<'PY'\n"
                "import os\n"
                "sig = %r\n"
                "base = os.path.basename(sig)\n"
                "for entry in os.listdir('/proc'):\n"
                "    if not entry.isdigit():\n"
                "        continue\n"
                "    try:\n"
                "        raw = open('/proc/%%s/cmdline' %% entry, 'rb').read()\n"
                "    except Exception:\n"
                "        continue\n"
                "    argv = [x.decode('utf-8', 'ignore') for x in raw.split(b'\\0') if x]\n"
                "    if sig.endswith('.py') and any(os.path.basename(a) == base for a in argv):\n"
                "        print(entry)\n"
                "        break\n"
                "    if not sig.endswith('.py') and sig in ' '.join(argv):\n"
                "        print(entry)\n"
                "        break\n"
                "PY" % sig, timeout=3)
            pid = out.strip() if ok and out.strip() else None
        result[name] = {"running": pid is not None, "pid": pid}

    return Response(json.dumps({"board_online": True, "services": result}), mimetype='application/json')


# Service launch commands (board-local, no SSH)
# Each service: (process_signature, launch_command, log_file)
_SERVICE_LAUNCHERS = {
    "vision": {
        "sig": "cloud_rtmpose_client.py",
        "cmd": "cd {root} && ENABLE_HDMI={hdmi} DISPLAY=:0 VISION_MODE=local LOCAL_POSE_MODEL=/home/toybrick/deploy_rknn_yolo/YOLOv5-Style/data/weights/pose-5s6-640-uint8.rknn CAMERA_WIDTH=1280 CAMERA_HEIGHT=720 CAMERA_FOURCC=MJPG CLOUD_TARGET_FPS=15 CLOUD_JPEG_QUALITY=72 PREVIEW_SCALE=1.0 JPEG_STRIDE=1 PYTHONUNBUFFERED=1 python3 hardware_engine/ai_sensory/cloud_rtmpose_client.py",
        "log": "/tmp/vision_local.log",
    },
    "fsm": {
        "sig": "main_claw_loop.py",
        "cmd": "cd {root} && IRONBUDDY_DB_DEFER_WRITES=1 PYTHONUNBUFFERED=1 python3 hardware_engine/main_claw_loop.py",
        "log": "/tmp/fsm_loop.log",
    },
    "emg": {
        "sig": "udp_emg_server.py",
        "cmd": "cd {root} && PYTHONUNBUFFERED=1 python3 hardware_engine/sensor/udp_emg_server.py",
        "log": "/tmp/emg_server.log",
    },
    "voice": {
        "sig": "voice_daemon.py",
        "cmd": "cd {root} && IRONBUDDY_DB_DEFER_WRITES=1 PYTHONUNBUFFERED=1 bash scripts/start_voice_with_env.sh",
        "log": "/tmp/voice_daemon.log",
    },
}


def _remove_if_exists(path):
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False


def _prepare_clean_take_baseline():
    """Reset demo-time runtime state without clearing historical DB rows."""
    ts = time.time()
    removed = []
    for path in (
        "/dev/shm/voice_turn.json",
        "/dev/shm/voice_turn.json.tmp",
        "/dev/shm/voice_interrupt",
        "/dev/shm/chat_active",
        "/dev/shm/voice_speaking",
        "/dev/shm/auto_trigger.json",
        "/dev/shm/auto_mvc.json",
        "/dev/shm/llm_inflight",
        "/dev/shm/llm_reply.txt",
        "/dev/shm/chat_input.txt",
        "/dev/shm/chat_reply.txt",
        "/dev/shm/chat_input.txt.seq",
        "/dev/shm/chat_reply.txt.seq",
        "/dev/shm/llm_reply.txt.seq",
        "/dev/shm/chat_events.jsonl",
        "/dev/shm/chat_events.seq",
        "/dev/shm/replay_last_tts.json",
        "/dev/shm/violation_alert.txt",
        "/dev/shm/voice_debug.json",
        MANUAL_VOICE_RECORD_PATH,
        MANUAL_VOICE_STOP_PATH,
        MANUAL_VOICE_STATUS_PATH,
        "/dev/shm/intent_exercise_mode.json",
        "/dev/shm/intent_inference_mode.json",
        "/dev/shm/intent_fatigue_limit.json",
        "/dev/shm/next_set.request",
        "/dev/shm/trigger_deepseek",
        "/dev/shm/angle_debug.json",
        "/dev/shm/ironbuddy_rag_delivery.json",
        RAG_DELIVERY_PATH,
        "/dev/shm/ironbuddy_training_plan.json",
        "/dev/shm/ironbuddy_training_session.json",
        TRAINING_PLAN_PATH,
        TRAINING_SESSION_PATH,
    ):
        if _remove_if_exists(path):
            removed.append(path)

    # These signals are intentionally created, not just removed: if FSM is
    # already running it consumes them and opens a fresh in-memory/DB session;
    # if FSM is stopped, the new process starts from a clean state anyway.
    try:
        with open("/dev/shm/fsm_reset_signal", "w", encoding="utf-8") as f:
            f.write("reset")
    except Exception:
        pass
    try:
        with open("/dev/shm/fatigue_reset.request.tmp", "w", encoding="utf-8") as f:
            f.write(str(ts))
        os.rename("/dev/shm/fatigue_reset.request.tmp", "/dev/shm/fatigue_reset.request")
    except Exception:
        pass

    _atomic_write_json_file("/dev/shm/inference_mode.json", {
        "mode": "pure_vision",
        "ts": ts,
        "src": "clean_take",
        "ttl_s": 30,
    })
    _atomic_write_json_file("/dev/shm/vision_mode.json", {
        "mode": "local",
        "ts": ts,
        "src": "clean_take",
    })
    _atomic_write_json_file("/dev/shm/exercise_mode.json", {
        "mode": "squat",
        "exercise": "squat",
        "ts": ts,
        "src": "clean_take",
        "ttl_s": 30,
    })
    _atomic_write_json_file("/dev/shm/user_profile.json", {
        "exercise": "squat",
        "ts": ts,
        "src": "clean_take",
    })
    _atomic_write_json_file("/dev/shm/fatigue_limit.json", {
        "limit": PLAN_DEFAULT_FATIGUE_TARGET,
        "ts": ts,
        "src": "clean_take",
    })
    _atomic_write_json_file("/dev/shm/ui_fatigue_limit.json", {
        "limit": PLAN_DEFAULT_FATIGUE_TARGET,
        "ts": ts,
        "src": "clean_take",
    })
    _atomic_write_json_file("/dev/shm/mute_signal.json", {
        "muted": False,
        "ts": ts,
        "src": "clean_take",
    })
    _atomic_write_json_file("/dev/shm/fsm_state.json", {
        "state": "NO_PERSON",
        "good": 0,
        "failed": 0,
        "comp": 0,
        "angle": 0,
        "fatigue": 0,
        "exercise": "squat",
        "rep_in_progress": False,
        "total_reps": 0,
        "inference_mode": "pure_vision",
        "total_good": 0,
        "total_failed": 0,
        "total_comp": 0,
        "fatigue_limit": PLAN_DEFAULT_FATIGUE_TARGET,
    })
    _write_training_session_state({
        "schema_version": 1,
        "exercise": "squat",
        "sets": [],
        "current_set": 1,
        "started_ts": ts,
        "plan_active": False,
        "src": "clean_take",
    })
    return {"ok": True, "removed": removed, "ts": ts}


@app.route('/api/admin/start', methods=['POST'])
def admin_start():
    """Start individual or all services directly on the board."""
    data = request.get_json(silent=True) or {}
    target = data.get("service", "all")  # "all" or specific service name

    clean_take = None
    if target == "all":
        try:
            clean_take = _prepare_clean_take_baseline()
        except Exception as e:
            clean_take = {"ok": False, "error": str(e)[:160]}

    results = {}
    services_to_start = _SERVICE_LAUNCHERS if target == "all" else {target: _SERVICE_LAUNCHERS.get(target)}

    for name, info in services_to_start.items():
        if info is None:
            results[name] = {"ok": False, "error": "unknown service"}
            continue
        running_pids = _service_pids_by_signature(info["sig"])
        if running_pids:
            results[name] = {"ok": True, "status": "already running", "pid": running_pids[0]}
            continue
        # Launch in background with nohup
        # Auto-detect HDMI for vision + write vision_mode signal
        hdmi_val = "0"
        if name == "vision":
            try:
                with open("/sys/class/drm/card0-HDMI-A-1/status", "r") as hf:
                    hdmi_val = "1" if "connected" in hf.read() else "0"
            except Exception:
                pass
            # Write vision mode signal file
            _run_cmd('echo \'{"mode":"local","ts":' + str(int(time.time())) + '}\'>/dev/shm/vision_mode.json', timeout=2)
        launch = info["cmd"].format(root=PROJECT_ROOT, hdmi=hdmi_val)
        if name == "voice":
            # The voice wrapper re-reads .api_config.json and sets ALSA paths.
            # Do not synthesize env-var shell scripts here: those can leave
            # credentials in /tmp/_launch_voice.sh and delay the startup prompt
            # until hot-load catches up.
            log = info["log"]
            script_path = "/tmp/_launch_{}.sh".format(name)
            try:
                with open(script_path, "w") as sf:
                    sf.write("#!/bin/bash\n")
                    sf.write(launch + "\n")
                os.chmod(script_path, 0o755)
            except Exception as e:
                results[name] = {"ok": False, "error": "script write failed: " + str(e)}
                continue
            full_cmd = "nohup {} >{} 2>&1 &".format(script_path, log)
            _run_cmd(full_cmd, timeout=8)
            import time as _t
            _t.sleep(1.5)
            pids2 = _service_pids_by_signature(info["sig"])
            results[name] = {"ok": bool(pids2), "pid": pids2[0] if pids2 else None}
            if not pids2:
                _, err_tail = _run_cmd("tail -5 {}".format(log), timeout=2)
                results[name]["error"] = err_tail
            continue
        # Inject API config env vars for vision/FSM. Voice uses its wrapper.
        if name in ("vision", "fsm"):
            api_cfg = _load_api_config()
            env_prefix = ""
            if name == "vision":
                cloud_url = _pick_config(api_cfg, "CLOUD_RTMPOSE_URL")
                if cloud_url:
                    env_prefix += "CLOUD_RTMPOSE_URL={} ".format(_sh_quote(cloud_url))
            else:
                api_key = _pick_config(api_cfg, "DEEPSEEK_API_KEY", "deepseek_api_key")
                llm_backend = _pick_config(api_cfg, "LLM_BACKEND", "llm_backend") or "direct"
                feishu_app_id = _pick_config(api_cfg, "FEISHU_APP_ID", "feishu_app_id")
                feishu_app_secret = _pick_config(api_cfg, "FEISHU_APP_SECRET", "feishu_app_secret")
                feishu_chat_id = _pick_config(api_cfg, "FEISHU_CHAT_ID", "feishu_chat_id")
                if api_key:
                    env_prefix += "DEEPSEEK_API_KEY={} LLM_BACKEND={} ".format(
                        _sh_quote(api_key), _sh_quote(llm_backend))
                if feishu_app_id:
                    env_prefix += "FEISHU_APP_ID={} FEISHU_APP_SECRET={} FEISHU_CHAT_ID={} ".format(
                        _sh_quote(feishu_app_id), _sh_quote(feishu_app_secret),
                        _sh_quote(feishu_chat_id))
            if env_prefix:
                launch = env_prefix + launch
        log = info["log"]
        # Write launch command to a temp script
        # Split env vars from command so exports work across && chains
        script_path = "/tmp/_launch_{}.sh".format(name)
        try:
            with open(script_path, "w") as sf:
                sf.write("#!/bin/bash\n")
                # Keep the board speaker path aligned with voice_daemon and
                # start_voice_with_env.sh. Older docs used 6 (SPK_HP), but the
                # current verified onboard speaker path is 2 (SPK).
                if name in ("fsm", "voice"):
                    sf.write("sudo amixer -c 0 cset numid=1,iface=MIXER,name='Playback Path' 2 >/dev/null 2>&1\n")
                sf.write("export " + " ".join(
                    "{}='{}'".format(k, v) for k, v in [
                        ("PYTHONUNBUFFERED", "1"),
                    ]
                ) + "\n")
                # Extract and export env vars from launch string
                # launch = "ENV1=val1 ENV2=val2 cd /path && ... python3 ..."
                parts = launch.split()
                env_exports = []
                cmd_start = 0
                for i, p in enumerate(parts):
                    if '=' in p and not p.startswith('-') and not p.startswith('/'):
                        env_exports.append(p)
                    else:
                        cmd_start = i
                        break
                if env_exports:
                    sf.write("export " + " ".join(env_exports) + "\n")
                sf.write(" ".join(parts[cmd_start:]) + "\n")
            os.chmod(script_path, 0o755)
        except Exception as e:
            results[name] = {"ok": False, "error": "script write failed: " + str(e)}
            continue
        full_cmd = "nohup {} >{} 2>&1 &".format(script_path, log)
        ok_launch, launch_out = _run_cmd(full_cmd, timeout=8)
        # Verify it started
        import time as _t
        _t.sleep(1.5)
        pids2 = _service_pids_by_signature(info["sig"])
        results[name] = {"ok": bool(pids2), "pid": pids2[0] if pids2 else None}
        if not pids2:
            # Process died — capture error from log
            _, err_tail = _run_cmd("tail -5 {}".format(log), timeout=2)
            results[name]["error"] = err_tail

    if target == "all":
        try:
            results["cloud_tunnel"] = _ensure_cloud_tunnel(blocking=False)
        except Exception as e:
            results["cloud_tunnel"] = {
                "ok": False,
                "segment": "tunnel_down",
                "detail": str(e)[:160],
            }

    return Response(json.dumps({"ok": True, "services": results, "clean_take": clean_take}), mimetype='application/json')


@app.route('/api/admin/stop', methods=['POST'])
def admin_stop():
    """Stop training services only; keep streamer_app.py control surface alive."""
    data = request.get_json(silent=True) or {}
    target = data.get("service", "all")

    if target == "streamer":
        return Response(json.dumps({
            "ok": False,
            "error": "refuse_stop_streamer_control_surface",
            "message": "主网页控制面不会被 /api/admin/stop 停止；如需恢复/重启网页，请使用 scripts/recover_streamer.sh。",
            "services": {
                "streamer": {
                    "ok": False,
                    "running": bool(_service_pids_by_signature(SERVICE_SIGNATURES["streamer"])),
                    "status": "refuse_stop_train_services_only",
                }
            },
        }, ensure_ascii=False), mimetype='application/json', status=400)

    results = {}
    sigs = SERVICE_SIGNATURES if target == "all" else {target: SERVICE_SIGNATURES.get(target)}

    # Collect all PIDs first from /proc cmdline. Do not use broad pkill -f
    # here: during recording it can match the current Flask/sh command and
    # drop the web console before it returns the stop result.
    all_pids = []
    for name, sig in sigs.items():
        if sig is None or name == "streamer":
            continue
        all_pids.extend(_service_pids_by_signature(sig))

    # Kill all at once with SIGKILL (no mercy — TERM was unreliable)
    if all_pids:
        pid_list = ' '.join(all_pids)
        _run_cmd("kill -9 {} 2>/dev/null".format(pid_list), timeout=3)

    # Wait for processes to die
    time.sleep(1.5)

    # Clean up zombie arecord/aplay from voice daemon
    _run_cmd("killall -9 arecord aplay 2>/dev/null", timeout=2)

    flush_result = _flush_runtime_db_buffers(finalize_open_sessions=True)

    # Verify and build results
    for name, sig in sigs.items():
        if sig is None:
            results[name] = {"ok": False, "error": "unknown service"}
            continue
        if name == "streamer":
            results[name] = {
                "ok": True,
                "running": True,
                "status": "控制台保留运行",
            }
            continue
        still_alive = bool(_service_pids_by_signature(sig))
        results[name] = {"ok": not still_alive, "status": "stopped" if not still_alive else "kill failed"}

    return Response(
        json.dumps(
            {"ok": True, "services": results, "db_flush": flush_result},
            ensure_ascii=False
        ),
        mimetype='application/json'
    )


@app.route('/api/admin/training_data')
def admin_training_data():
    """List all training CSV files grouped by exercise/label"""
    data_dir = os.path.join(PROJECT_ROOT, "data")
    result = {}
    if not os.path.isdir(data_dir):
        return Response(json.dumps(result), mimetype='application/json')

    for exercise in sorted(os.listdir(data_dir)):
        ex_path = os.path.join(data_dir, exercise)
        if not os.path.isdir(ex_path):
            continue
        result[exercise] = {}
        for label in sorted(os.listdir(ex_path)):
            label_path = os.path.join(ex_path, label)
            if not os.path.isdir(label_path):
                continue
            files = []
            for f in sorted(os.listdir(label_path)):
                if f.endswith('.csv'):
                    fp = os.path.join(label_path, f)
                    st = os.stat(fp)
                    # count lines
                    with open(fp, 'r') as fh:
                        line_count = sum(1 for _ in fh)
                    files.append({
                        "name": f,
                        "size_kb": round(st.st_size / 1024, 1),
                        "lines": line_count,
                        "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                    })
            if files:
                result[exercise][label] = files

    return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')


@app.route('/api/admin/system_info')
def admin_system_info():
    """System status: GPU, board, connectivity"""
    info = {
        "board": {"online": False, "ip": BOARD_IP},
        "cloud_gpu": {"online": False, "info": ""},
        "openclaw": {"status": "unknown"},
    }

    # Board check
    ok, out = _ssh_board_cmd("cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 0", timeout=5)
    if ok:
        info["board"]["online"] = True
        try:
            temp_raw = int(out.strip())
            info["board"]["cpu_temp"] = round(temp_raw / 1000.0, 1) if temp_raw > 1000 else temp_raw
        except ValueError:
            info["board"]["cpu_temp"] = 0
        # board uptime
        ok2, out2 = _ssh_board_cmd("uptime -p 2>/dev/null || uptime", timeout=3)
        if ok2:
            info["board"]["uptime"] = out2

    # Cloud GPU check
    if os.path.exists(CLOUD_KEY_PATH):
        ok, out = _run_cmd(
            'ssh -p {} -i {} -o StrictHostKeyChecking=no -o ConnectTimeout=3 {} '
            '"nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>/dev/null"'.format(
                CLOUD_PORT, CLOUD_KEY_PATH, CLOUD_SSH
            ),
            timeout=8
        )
        if ok and out:
            info["cloud_gpu"]["online"] = True
            info["cloud_gpu"]["info"] = out

    return Response(json.dumps(info, ensure_ascii=False), mimetype='application/json')


def _tail_file(path, max_lines=12):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [x.rstrip("\n") for x in lines[-max_lines:]]
    except Exception:
        return []


def _file_snapshot(path):
    try:
        st = os.stat(path)
        return {
            "exists": True,
            "path": path,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "mtime_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        }
    except OSError:
        return {"exists": False, "path": path}


# ── API Config (DeepSeek key, LLM backend) ────────────────────────────────
API_CONFIG_PATH = os.path.join(PROJECT_ROOT, ".api_config.json")


def _load_api_config():
    try:
        with open(API_CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_api_config(cfg):
    tmp = API_CONFIG_PATH + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.rename(tmp, API_CONFIG_PATH)


# Keys that get masked in GET responses (sensitive).
_API_CONFIG_SENSITIVE_KEYS = (
    'DEEPSEEK_API_KEY', 'deepseek_api_key', 'BAIDU_API_KEY', 'BAIDU_SECRET_KEY',
    'FEISHU_APP_SECRET', 'FEISHU_WEBHOOK', 'CLOUD_SSH_PASSWORD',
    'RAG_VECTOR_API_KEY',
)

# Keys accepted from POST requests (SSH credentials intentionally excluded).
_API_CONFIG_WRITE_WHITELIST = (
    'deepseek_api_key', 'llm_backend',
    'BAIDU_APP_ID', 'BAIDU_API_KEY', 'BAIDU_SECRET_KEY',
    'FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_CHAT_ID', 'FEISHU_WEBHOOK',
    'CLOUD_RTMPOSE_URL',
    'RAG_VECTOR_URL', 'RAG_VECTOR_API_KEY', 'RAG_VECTOR_COLLECTION',
    'RAG_EMBEDDING_URL', 'RAG_EMBEDDING_MODEL',
)


def _mask_secret(val):
    if not isinstance(val, str) or not val:
        return val
    if len(val) > 10:
        return val[:6] + '****' + val[-4:]
    return '****'


def _read_json_file(path):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return {}
    return {}


def _tail_file(path, max_lines=20):
    try:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [x.rstrip("\n") for x in lines[-max_lines:]]
    except Exception:
        return []


def _openclaw_schedule():
    """Read schedule env vars (matches opencloud_reminder_daemon._should_fire)."""
    return {
        "weekly_hour": int(os.environ.get("IRONBUDDY_WEEKLY_HOUR", "20")),
        "weekly_dow": int(os.environ.get("IRONBUDDY_WEEKLY_DOW", "6")),
        "morning_hour": int(os.environ.get("IRONBUDDY_MORNING_HOUR", "9")),
        "evening_hour": int(os.environ.get("IRONBUDDY_EVENING_HOUR", "21")),
    }


def _openclaw_next_push(schedule, now=None):
    """Predict next push event by walking forward up to 8 days. Stdlib only."""
    import datetime as _dt
    base = now or _dt.datetime.now()
    candidates = []
    for offset in range(0, 8 * 24):
        cand = base + _dt.timedelta(hours=offset)
        cand = cand.replace(minute=0, second=0, microsecond=0)
        if cand <= base:
            continue
        if cand.hour == schedule["morning_hour"]:
            candidates.append(("morning", cand))
        if cand.hour == schedule["evening_hour"]:
            candidates.append(("evening", cand))
        if cand.weekday() == schedule["weekly_dow"] and cand.hour == schedule["weekly_hour"]:
            candidates.append(("weekly", cand))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[1])
    mode, when = candidates[0]
    return mode, when.timestamp()


@app.route('/api/opencloud/status', methods=['GET'])
@app.route('/api/openclaw/status', methods=['GET'])
def opencloud_status():
    """Read-only OpenClaw reminder status. Never returns secret values."""
    cfg = _load_api_config()
    runtime_status_paths = [
        os.environ.get("OPENCLOUD_REMINDER_STATUS_PATH", ""),
        os.path.join(PROJECT_ROOT, "data", "runtime", "opencloud_reminder_status.json"),
        "/tmp/opencloud_reminder_status.json",
    ]
    runtime_status = {}
    status_path_used = ""
    for path in runtime_status_paths:
        if not path:
            continue
        runtime_status = _read_json_file(path)
        if runtime_status:
            status_path_used = path
            break

    ok_proc, proc_out = _run_cmd(
        "pgrep -af '[o]pencloud_reminder_daemon.py|[o]penclaw_daemon.py'",
        timeout=3,
    )
    configured = {
        "feishu_app_id": bool(_pick_config(cfg, "FEISHU_APP_ID", "feishu_app_id")),
        "feishu_chat_id": bool(_pick_config(cfg, "FEISHU_CHAT_ID", "feishu_chat_id")),
        "feishu_webhook": bool(_pick_config(cfg, "FEISHU_WEBHOOK", "feishu_webhook")),
        "deepseek_api_key": bool(_pick_config(cfg, "DEEPSEEK_API_KEY", "deepseek_api_key")),
        "opencloud_board_url": bool(os.environ.get("IRONBUDDY_BOARD_URL")),
    }
    # V7.37: prefer schedule reported by the daemon itself (sees systemd env);
    # fall back to streamer's own env when daemon hasn't run yet.
    schedule = _openclaw_schedule()
    sched_path = os.path.join(PROJECT_ROOT, "data", "runtime",
                               "opencloud_schedule.json")
    daemon_sched = _read_json_file(sched_path) or \
        ((runtime_status or {}).get("schedule") or {})
    if daemon_sched:
        schedule = {
            "weekly_hour": int(daemon_sched.get("weekly_hour", schedule["weekly_hour"])),
            "weekly_dow": int(daemon_sched.get("weekly_dow", schedule["weekly_dow"])),
            "morning_hour": int(daemon_sched.get("morning_hour", schedule["morning_hour"])),
            "evening_hour": int(daemon_sched.get("evening_hour", schedule["evening_hour"])),
        }
    next_mode, next_ts = _openclaw_next_push(schedule)
    body = {
        "ok": True,
        "presentation_name": "OpenClaw 后台提醒",
        "primary_runtime": "board",
        "board_daemon": "systemd_or_loop",
        "status_path": status_path_used,
        "runtime_status": runtime_status,
        "daemon_running": bool(ok_proc and proc_out.strip()),
        "local_process_running": bool(ok_proc and proc_out.strip()),
        "local_processes": proc_out.splitlines()[:8] if proc_out else [],
        "configured": configured,
        "weekly_hour": schedule["weekly_hour"],
        "weekly_dow": schedule["weekly_dow"],
        "morning_hour": schedule["morning_hour"],
        "evening_hour": schedule["evening_hour"],
        "next_push_mode": next_mode or "",
        "next_push_ts": next_ts,
        "last_push_ts": runtime_status.get("last_push_ts"),
        "last_push_mode": runtime_status.get("mode"),
        "last_push_ok": bool(runtime_status.get("ok")),
        "trigger_files": {
            "daily_plan": "/dev/shm/openclaw_trigger_daily_plan",
            "weekly_report": "/dev/shm/openclaw_trigger_weekly_report",
            "preference_learning": "/dev/shm/openclaw_trigger_preference_learning",
        },
        "logs": {
            "opencloud_reminder": _tail_file("/tmp/opencloud_reminder.log", max_lines=12),
            "openclaw_daemon": _tail_file("/tmp/openclaw_daemon.log", max_lines=12),
        },
    }
    return Response(json.dumps(body, ensure_ascii=False), mimetype='application/json')


@app.route('/api/openclaw/once', methods=['POST'])
def openclaw_once():
    """Trigger one OpenClaw reminder run. Body: {mode, send}.

    mode: morning | evening | weekly | auto
    send: bool (default false → dry-run)
    """
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "weekly")
    if mode not in ("morning", "evening", "weekly", "auto"):
        return Response(json.dumps({"ok": False, "error": "bad mode"}),
                        mimetype='application/json'), 400
    send = bool(body.get("send", False))
    daemon_path = os.path.join(PROJECT_ROOT, "scripts",
                               "opencloud_reminder_daemon.py")
    args = ["python3", "-u", daemon_path, "--mode", mode, "--once"]
    if send:
        args.append("--send")
    else:
        args.append("--dry-run")
    try:
        proc = subprocess.run(args, capture_output=True, timeout=20,
                              cwd=PROJECT_ROOT)
        rc = proc.returncode
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json')
    return Response(json.dumps({
        "ok": rc == 0,
        "mode": mode,
        "send": send,
        "rc": rc,
        "stdout_tail": out[-1200:],
        "stderr_tail": err[-500:],
    }, ensure_ascii=False), mimetype='application/json')


@app.route('/api/openclaw/insights', methods=['GET'])
def openclaw_insights():
    """Aggregate from real tables for 4-block reminder card.

    Returns 训练统计 + 高频提问 + LLM 触发分布 + 数据时间窗，all 7-day.
    """
    import sqlite3 as _sq
    db_path = os.path.join(PROJECT_ROOT, "data", "ironbuddy.db")
    out = {
        "ok": False,
        "weekly_training": {},
        "hot_voice_topics": [],
        "llm_triggers": [],
        "rag_todo_hint": [],
        "data_window_days": 7,
        "db_path": db_path,
    }
    try:
        conn = _sq.connect(db_path, timeout=2.0)
        cur = conn.cursor()
        # Block 1: weekly training stats
        try:
            cur.execute(
                "SELECT COUNT(*) sessions, "
                "COALESCE(SUM(good_count),0) g, "
                "COALESCE(SUM(failed_count),0) f, "
                "COALESCE(MAX(fatigue_peak),0) fp "
                "FROM training_sessions WHERE started_at >= datetime('now','-7 day')"
            )
            row = cur.fetchone()
            if row:
                out["weekly_training"] = {
                    "sessions": int(row[0] or 0),
                    "good": int(row[1] or 0),
                    "failed": int(row[2] or 0),
                    "fatigue_peak": float(row[3] or 0),
                }
        except Exception:
            pass
        # Block 2: hot voice topics (transcript length >= 4 chars to filter noise)
        try:
            cur.execute(
                "SELECT transcript, COUNT(*) c FROM voice_sessions "
                "WHERE ts >= datetime('now','-7 day') "
                "AND length(transcript) >= 4 "
                "AND is_demo_seed = 0 "
                "GROUP BY transcript ORDER BY c DESC LIMIT 3"
            )
            out["hot_voice_topics"] = [
                {"text": (r[0] or "")[:60], "count": int(r[1])}
                for r in cur.fetchall()
            ]
        except Exception:
            pass
        # Block 3: LLM trigger distribution
        try:
            cur.execute(
                "SELECT trigger, COUNT(*) c FROM llm_log "
                "WHERE ts >= datetime('now','-7 day') "
                "GROUP BY trigger ORDER BY c DESC LIMIT 5"
            )
            out["llm_triggers"] = [
                {"trigger": r[0] or "?", "count": int(r[1])}
                for r in cur.fetchall()
            ]
        except Exception:
            pass
        # Block 3b: RAG todo hint — voice_chat trigger entries with very generic responses
        try:
            cur.execute(
                "SELECT prompt FROM llm_log "
                "WHERE ts >= datetime('now','-7 day') AND trigger='voice_chat' "
                "ORDER BY ts DESC LIMIT 3"
            )
            out["rag_todo_hint"] = [
                {"prompt_head": (r[0] or "")[:80]}
                for r in cur.fetchall()
            ]
        except Exception:
            pass
        out["ok"] = True
        conn.close()
    except Exception as e:
        out["error"] = str(e)
    return Response(json.dumps(out, ensure_ascii=False),
                    mimetype='application/json')


@app.route('/api/openclaw/history', methods=['GET'])
def openclaw_history():
    """Return last N reminder runs from opencloud_reminder_history.jsonl."""
    try:
        n = int(request.args.get("n", "10"))
    except Exception:
        n = 10
    n = max(1, min(n, 100))
    history_path = os.path.join(PROJECT_ROOT, "data", "runtime",
                                "opencloud_reminder_history.jsonl")
    items = []
    try:
        if os.path.exists(history_path):
            with open(history_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines[-n:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json')
    return Response(json.dumps({
        "ok": True,
        "history_path": history_path,
        "count": len(items),
        "items": items,
    }, ensure_ascii=False), mimetype='application/json')


def _read_active_system_prompt():
    try:
        db = _get_db()
        if db is not None:
            return db.get_active_system_prompt(fallback="")
    except Exception:
        pass
    return ""


@app.route('/api/demo/rag_status', methods=['GET'])
def demo_rag_status():
    """Recording-facing RAG snapshot. Read-only; no DeepSeek call."""
    query = request.args.get("query", "膝盖酸痛怎么办").strip() or "膝盖酸痛怎么办"
    try:
        limit = int(request.args.get("limit", 3))
    except Exception:
        limit = 3
    online = _search_lane_a_professional_knowledge(query, limit=limit)
    vector_status = {}
    embedding_status = {}
    adp_status = {"configured": False, "provider": "tencent_adp"}
    try:
        if adp_knowledge is not None:
            adp_status = adp_knowledge.status_snapshot()
    except Exception:
        pass
    try:
        if vector_knowledge is not None:
            vector_status = vector_knowledge.status_snapshot()
            embedding_status = vector_knowledge.embedding_status_snapshot()
    except Exception as exc:
        vector_status = {"online": False, "latest_error": str(exc)[:120]}
    vector_status["adp_status"] = adp_status
    hits = online.get("hits") or []
    body = {
        "ok": bool(online.get("ok")),
        "query": query,
        "capabilities": get_capabilities(),
        "kb": {
            "local_role": "rules_and_manual_only",
            "adp_status": adp_status,
            "vector_status": vector_status,
            "embedding_status": embedding_status,
            "online_reason": online.get("reason"),
            "online_message": online.get("message") or ("专业证据已命中" if hits else "专业证据不可用"),
        },
        "source_mode": online.get("source_mode") or "adp",
        "message": online.get("message") or ("专业证据已命中" if hits else "专业证据不可用"),
        "hits": hits,
        "context": online.get("context") or "",
        "errors": online.get("errors") or [],
        "vector": online.get("vector") or {},
        "vector_fallback_enabled": _vector_fallback_enabled(),
        "embedding": embedding_status,
        "manual_reply": format_manual_reply(query, max_hits=limit),
        "active_prompt_preview": _read_active_system_prompt()[:800],
        "draft_mode": True,
        "note": "展示接口只读；用户可见专业回答默认走腾讯 ADP。旧 BGE-M3/Qdrant 向量库仅在 IRONBUDDY_ENABLE_VECTOR_FALLBACK=1 时作为应急 fallback。",
    }
    return Response(json.dumps(body, ensure_ascii=False), mimetype='application/json')


@app.route('/api/demo/opencloud_records', methods=['GET'])
def demo_opencloud_records():
    """Recording-facing OpenClaw records. Real data only.

    The route name keeps the older opencloud slug for compatibility.
    """
    status_json = json.loads(opencloud_status().get_data(as_text=True))
    tables = {}
    for table in ("daily_summary", "preference_history", "llm_log", "system_prompt_versions"):
        rows = []
        try:
            db = _get_db()
            if db is not None:
                import sqlite3 as _sq
                conn = _sq.connect(_db_view_path())
                conn.row_factory = _sq.Row
                if table in _DB_VIEW_WHITELIST:
                    sql = "SELECT * FROM %s ORDER BY %s LIMIT 20" % (
                        table, _DB_VIEW_WHITELIST[table]["order_by"])
                    rows = [dict(r) for r in conn.execute(sql).fetchall()]
                conn.close()
        except Exception as e:
            rows = [{"error": str(e)}]
        tables[table] = rows
    return Response(json.dumps({
        "ok": True,
        "status": status_json,
        "records": tables,
        "real_data_only": True,
        "empty_message": "暂无真实记录" if not any(tables.values()) else "",
    }, ensure_ascii=False, default=str), mimetype='application/json')


@app.route('/api/demo/debug_workbench', methods=['GET'])
def demo_debug_workbench():
    run_root = os.path.join(PROJECT_ROOT, "docs", "test_runs", "ironbuddy_operator")
    latest = ""
    try:
        runs = [x for x in os.listdir(run_root) if x[:8].isdigit()]
        latest = sorted(runs)[-1] if runs else ""
    except Exception:
        pass
    return Response(json.dumps({
        "ok": True,
        "operator_console_mode": "embedded",
        "operator_record_url": "/api/operator/record",
        "run_root": run_root,
        "latest_run": latest,
        "summary_path": os.path.join(run_root, latest, "summary.md") if latest else "",
        "events_path": os.path.join(run_root, latest, "events.jsonl") if latest else "",
        "features": ["步骤验收", "截图上传", "备注记录", "主网页保存"],
    }, ensure_ascii=False), mimetype='application/json')


@app.route('/api/demo/code_graph', methods=['GET'])
def demo_code_graph():
    """Small read-only code structure graph for the recording workbench."""
    nodes = [
        {"data": {"id": "ui", "label": "templates/index.html", "kind": "frontend"}},
        {"data": {"id": "api", "label": "streamer_app.py", "kind": "api"}},
        {"data": {"id": "voice", "label": "voice_daemon.py", "kind": "voice"}},
        {"data": {"id": "fsm", "label": "main_claw_loop.py", "kind": "fsm"}},
        {"data": {"id": "kb", "label": "coach_knowledge.py", "kind": "rag"}},
        {"data": {"id": "db", "label": "data/ironbuddy.db", "kind": "db"}},
        {"data": {"id": "cloud", "label": "OpenClaw", "kind": "cloud"}},
        {"data": {"id": "operator", "label": "operator console", "kind": "debug"}},
    ]
    edges = [
        {"data": {"source": "ui", "target": "api", "label": "HTTP API"}},
        {"data": {"source": "api", "target": "voice", "label": "/dev/shm voice"}},
        {"data": {"source": "api", "target": "fsm", "label": "mode intents"}},
        {"data": {"source": "fsm", "target": "voice", "label": "auto summary"}},
        {"data": {"source": "api", "target": "kb", "label": "RAG query"}},
        {"data": {"source": "api", "target": "db", "label": "SQLite viewer"}},
        {"data": {"source": "cloud", "target": "db", "label": "preferences/prompts"}},
        {"data": {"source": "operator", "target": "api", "label": "acceptance evidence"}},
    ]
    return Response(json.dumps({
        "ok": True,
        "library_hint": "cytoscape.js",
        "nodes": nodes,
        "edges": edges,
        "read_only": True,
    }, ensure_ascii=False), mimetype='application/json')


@app.route('/api/admin/api_config', methods=['GET', 'POST'])
def admin_api_config():
    if request.method == 'GET':
        cfg = _load_api_config()
        masked = {}
        for k, v in cfg.items():
            if k in _API_CONFIG_SENSITIVE_KEYS:
                masked[k] = _mask_secret(v)
            else:
                masked[k] = v
        return Response(json.dumps(masked), mimetype='application/json')
    else:
        data = request.get_json(silent=True) or {}
        cfg = _load_api_config()
        for key in _API_CONFIG_WRITE_WHITELIST:
            if key not in data:
                continue
            val = data[key]
            # Skip masked placeholders — caller didn't actually re-enter the secret.
            if isinstance(val, str) and '****' in val:
                continue
            cfg[key] = val
        _save_api_config(cfg)
        return Response(json.dumps({"ok": True}), mimetype='application/json')


@app.route('/api/admin/cloud_gpu/connect', methods=['GET', 'POST'])
def admin_cloud_gpu_connect():
    """Save a cloned GPU SSH endpoint and rebuild cloud/RAG tunnels.

    Secret values are accepted on POST but never returned in responses.
    """
    if request.method == 'GET':
        cfg = _load_api_config()
        runtime = _read_json_file(_cloud_gpu_status_path())
        cloud = _probe_cloud_health(timeout=1.0)
        rag = _probe_rag_tunnel_health(timeout=1.0)
        return Response(json.dumps({
            "ok": bool(cloud.get("ok") and rag.get("ok")),
            "config": _cloud_gpu_public_config(cfg),
            "cloud": cloud,
            "rag": rag,
            "runtime": runtime,
        }, ensure_ascii=False), mimetype='application/json')

    data = request.get_json(force=True, silent=True) or {}
    try:
        parsed = _parse_cloud_ssh_command(data.get("ssh_command") or data.get("ssh") or "")
    except Exception as exc:
        return Response(json.dumps({
            "ok": False,
            "error": "SSH 登录指令解析失败: " + str(exc),
        }, ensure_ascii=False), mimetype='application/json', status=400)
    cfg = _load_api_config()
    password = str(data.get("password") or "").strip()
    if not password or "****" in password:
        password = str(_pick_config(cfg, "CLOUD_SSH_PASSWORD") or "").strip()
    if not password:
        return Response(json.dumps({
            "ok": False,
            "error": "需要填写本次 GPU 密码",
        }, ensure_ascii=False), mimetype='application/json', status=400)

    cfg["CLOUD_SSH_HOST"] = parsed["host"]
    cfg["CLOUD_SSH_PORT"] = parsed["port"]
    cfg["CLOUD_SSH_USER"] = parsed["user"]
    cfg["CLOUD_SSH_PASSWORD"] = password
    cfg.setdefault("CLOUD_LOCAL_TUNNEL_PORT", 6006)
    cfg.setdefault("CLOUD_RTMPOSE_URL", "http://127.0.0.1:6006/infer")
    cfg.setdefault("RAG_VECTOR_LOCAL_PORT", 6333)
    cfg.setdefault("RAG_EMBEDDING_LOCAL_PORT", 8008)
    cfg.setdefault("RAG_VECTOR_URL", "http://127.0.0.1:6333")
    cfg.setdefault("RAG_VECTOR_COLLECTION", "ironbuddy_evidence")
    cfg.setdefault("RAG_EMBEDDING_URL", "http://127.0.0.1:8008/embed")
    cfg.setdefault("RAG_EMBEDDING_MODEL", "bge-m3")
    _save_api_config(cfg)
    try:
        _atomic_write_json_file(_cloud_gpu_status_path(), {
            "ok": False,
            "running": True,
            "updated_ts": time.time(),
        })
    except Exception:
        pass

    worker = threading.Thread(target=_cloud_gpu_reconnect_worker)
    worker.daemon = True
    worker.start()
    cloud = _probe_cloud_health(timeout=0.8)
    rag_health = _probe_rag_tunnel_health(timeout=0.8)
    return Response(json.dumps({
        "ok": True,
        "reconnect_started": True,
        "config": _cloud_gpu_public_config(cfg),
        "cloud": cloud,
        "rag": {"ok": bool(rag_health.get("ok")), "health": rag_health},
    }, ensure_ascii=False, default=str), mimetype='application/json',
                    status=202)


@app.route('/api/admin/cloud_verify', methods=['GET'])
def admin_cloud_verify():
    """Probe the same cloud RTMPose endpoint used by the vision process."""
    cfg = _load_api_config()
    url = _pick_config(cfg, 'CLOUD_RTMPOSE_URL')
    if not url:
        return Response(json.dumps({"ok": False, "error": "CLOUD_RTMPOSE_URL 未配置"}), mimetype='application/json')
    try:
        parts = urlsplit(url)
        health_url = urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": "URL 解析失败: " + str(e)}), mimetype='application/json')

    probes = []
    t0 = time.time()
    try:
        resp = requests.get(health_url, timeout=3)
        t1 = time.time()
        detail = ""
        try:
            payload = resp.json()
            detail = payload.get('status') or payload.get('phase') or payload.get('ready')
        except Exception:
            detail = resp.text[:80]
        probes.append({
            "name": "health",
            "url": health_url,
            "http_status": resp.status_code,
            "latency_ms": int((t1 - t0) * 1000),
            "detail": str(detail)[:120],
        })
        health_ok = resp.status_code == 200
    except Exception as e:
        probes.append({"name": "health", "url": health_url, "error": str(e)[:160]})
        health_ok = False

    infer_ok = False
    if not health_ok:
        t2 = time.time()
        try:
            resp2 = requests.post(
                url,
                files={"frame": ("probe.jpg", b"", "image/jpeg")},
                data={"seq_id": "probe"},
                timeout=3,
            )
            t3 = time.time()
            probes.append({
                "name": "infer",
                "url": url,
                "http_status": resp2.status_code,
                "latency_ms": int((t3 - t2) * 1000),
                "detail": resp2.text[:120],
            })
            infer_ok = resp2.status_code != 404
        except Exception as e:
            probes.append({"name": "infer", "url": url, "error": str(e)[:160]})

    if health_ok or infer_ok:
        return Response(json.dumps({
            "ok": True,
            "status": "ready" if health_ok else "infer_endpoint_found",
            "latency_ms": probes[0].get("latency_ms", 0),
            "probes": probes,
        }), mimetype='application/json')

    first_error = probes[0].get("error") or ("HTTP %s" % probes[0].get("http_status"))
    if "127.0.0.1:6006" in url:
        first_error = "板端 127.0.0.1:6006 隧道未连通: " + first_error
    return Response(json.dumps({
        "ok": False,
        "error": first_error,
        "probes": probes,
    }, ensure_ascii=False), mimetype='application/json')


@app.route('/api/admin/reload_service', methods=['POST'])
def admin_reload_service():
    """V4.8: Restart a service so it picks up freshly-saved .api_config.json values.
    Accepts body {"service": "voice" | "tunnel"}. Safe on WSL dev host (no-op)."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        svc = data.get("service", "voice")
        root = "/home/toybrick/streamer_v3"
        if not os.path.isdir(root):
            return Response(json.dumps({"ok": False, "error": "not on board"}),
                            mimetype='application/json')
        import subprocess as _sp
        if svc == "voice":
            _sp.run(["pkill", "-f", "[v]oice_daemon"], timeout=5)
            time.sleep(1)
            # Relaunch via wrapper that re-reads .api_config.json
            _sp.Popen(
                ["setsid", "nohup", "bash", root + "/scripts/start_voice_with_env.sh"],
                stdout=open("/tmp/voice.log", "a"),
                stderr=_sp.STDOUT,
                stdin=_sp.DEVNULL,
                start_new_session=True,
            )
            return Response(json.dumps({"ok": True, "service": "voice",
                                        "msg": "voice_daemon 已重启，新凭证已加载"}),
                            mimetype='application/json')
        if svc == "tunnel":
            _sp.run(["pkill", "-f", "[s]sh.*-L.*6006:127.0.0.1:6006"], timeout=5)
            _sp.run(["pkill", "-f", "[c]loud_tunnel.py"], timeout=5)
            time.sleep(1)
            out = _sp.run(
                ["bash", root + "/scripts/cloud_tunnel.sh"],
                capture_output=True, timeout=20,
            )
            return Response(json.dumps({
                "ok": out.returncode == 0,
                "service": "tunnel",
                "msg": out.stdout.decode(errors='replace')[-200:]
                if out.stdout else out.stderr.decode(errors='replace')[-200:],
            }), mimetype='application/json')
        return Response(json.dumps({"ok": False, "error": "unknown service: " + svc}),
                        mimetype='application/json', status=400)
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


# ============================================================================
# 视觉特征探测 (Phase 0 · env-gated, 可一键回退)
# 启用：环境变量 IRONBUDDY_PROBE_ENABLED=1 启动 streamer 即激活 UI+接口
# 关闭：unset 环境变量重启 streamer，所有探测 UI 隐藏、接口返回 403
# 依赖：tools/vision_feature_probe_v2.py
# ============================================================================
_PROBE_ENABLED = os.environ.get('IRONBUDDY_PROBE_ENABLED', '0') == '1'
_PROBE_SCRIPT = os.path.join(PROJECT_ROOT, 'tools', 'vision_feature_probe_v2.py')
_PROBE_PID_FILE = '/dev/shm/probe_v2.pid'
_PROBE_FEATURES_JSONL = '/dev/shm/rep_features.jsonl'
_PROBE_LOG = '/tmp/probe_v2.log'


def _probe_is_running():
    """Check if probe_v2.py is alive by PID file + proc existence."""
    try:
        if not os.path.exists(_PROBE_PID_FILE):
            return False, None
        with open(_PROBE_PID_FILE, 'r') as f:
            pid = f.read().strip()
        if not pid or not pid.isdigit():
            return False, None
        if os.path.exists('/proc/{}'.format(pid)):
            return True, int(pid)
        # 陈旧 PID 文件，清掉
        try:
            os.remove(_PROBE_PID_FILE)
        except OSError:
            pass
        return False, None
    except Exception:
        return False, None


@app.route('/api/probe/enabled')
def probe_enabled():
    """UI 启动时查询, 只有启用时前端才会显示探测面板."""
    return Response(json.dumps({"enabled": _PROBE_ENABLED}), mimetype='application/json')


@app.route('/api/probe/start', methods=['POST'])
def probe_start():
    if not _PROBE_ENABLED:
        return Response(json.dumps({"ok": False, "error": "probe disabled"}),
                        mimetype='application/json', status=403)
    running, pid = _probe_is_running()
    if running:
        return Response(json.dumps({"ok": True, "status": "already running", "pid": pid}),
                        mimetype='application/json')
    if not os.path.exists(_PROBE_SCRIPT):
        return Response(json.dumps({"ok": False, "error": "script not found: " + _PROBE_SCRIPT}),
                        mimetype='application/json', status=500)
    try:
        import subprocess as _sp
        # nohup + setsid 保活，不占 streamer 子进程表
        _sp.Popen(
            ['nohup', 'python3', '-u', _PROBE_SCRIPT],
            stdout=open(_PROBE_LOG, 'a'),
            stderr=_sp.STDOUT,
            stdin=_sp.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.5)
        running, pid = _probe_is_running()
        return Response(json.dumps({"ok": bool(running), "pid": pid}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/probe/stop', methods=['POST'])
def probe_stop():
    if not _PROBE_ENABLED:
        return Response(json.dumps({"ok": False, "error": "probe disabled"}),
                        mimetype='application/json', status=403)
    _run_cmd("pkill -f 'vision_feature_probe_v2.py' 2>/dev/null", timeout=3)
    try:
        if os.path.exists(_PROBE_PID_FILE):
            os.remove(_PROBE_PID_FILE)
    except OSError:
        pass
    return Response(json.dumps({"ok": True}), mimetype='application/json')


@app.route('/api/probe/state')
def probe_state():
    """返回 {running, pid, current_label, features: [...]}"""
    if not _PROBE_ENABLED:
        return Response(json.dumps({"enabled": False, "running": False, "features": []}),
                        mimetype='application/json')
    running, pid = _probe_is_running()
    feats = []
    try:
        if os.path.exists(_PROBE_FEATURES_JSONL):
            with open(_PROBE_FEATURES_JSONL, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        feats.append(json.loads(line))
                    except ValueError:
                        continue
            feats = feats[-100:]  # 6 类录制需要更大窗口
    except (IOError, OSError):
        pass
    # 当前标签
    cur_label = "unlabeled"
    try:
        if os.path.exists('/dev/shm/probe_label.txt'):
            with open('/dev/shm/probe_label.txt', 'r') as f:
                cur_label = f.read().strip() or "unlabeled"
    except (IOError, OSError):
        pass
    return Response(json.dumps({
        "enabled": True,
        "running": bool(running),
        "pid": pid,
        "current_label": cur_label,
        "features": feats,
    }), mimetype='application/json')


_PROBE_ALLOWED_LABELS = {
    "squat_standard", "squat_compensating", "squat_non_standard",
    "curl_standard", "curl_compensating", "curl_non_standard",
    "unlabeled",
}


@app.route('/api/probe/set_label', methods=['POST'])
def probe_set_label():
    """UI 6 按钮调此接口切换当前标注. 写 /dev/shm/probe_label.txt (probe_v2 每 rep 结算时读)."""
    if not _PROBE_ENABLED:
        return Response(json.dumps({"ok": False, "error": "probe disabled"}),
                        mimetype='application/json', status=403)
    try:
        data = request.get_json(force=True, silent=True) or {}
        label = (data.get('label') or 'unlabeled').strip()
        if label not in _PROBE_ALLOWED_LABELS:
            return Response(json.dumps({"ok": False, "error": "invalid label: " + label}),
                            mimetype='application/json', status=400)
        tmp = '/dev/shm/probe_label.txt.tmp'
        with open(tmp, 'w') as f:
            f.write(label)
        os.rename(tmp, '/dev/shm/probe_label.txt')
        return Response(json.dumps({"ok": True, "label": label}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/probe/clear', methods=['POST'])
def probe_clear():
    """清空 rep_features.jsonl, 重新开始采集."""
    if not _PROBE_ENABLED:
        return Response(json.dumps({"ok": False, "error": "probe disabled"}),
                        mimetype='application/json', status=403)
    try:
        if os.path.exists(_PROBE_FEATURES_JSONL):
            os.remove(_PROBE_FEATURES_JSONL)
        return Response(json.dumps({"ok": True}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/admin/fatigue_reset', methods=['POST'])
def admin_fatigue_reset():
    """V4.8: Drop signal file that FSM watches to zero out fatigue counter.
    Fires either from voice '清空疲劳' or auto-trigger when UI sees fatigue >= limit."""
    try:
        with open("/dev/shm/fatigue_reset.request.tmp", "w") as f:
            f.write(str(time.time()))
        os.rename("/dev/shm/fatigue_reset.request.tmp", "/dev/shm/fatigue_reset.request")
        return Response(json.dumps({"ok": True}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/admin/voice_diag', methods=['GET'])
def admin_voice_diag():
    """V4.8: Diagnose why voice is silent. Returns full status tree:
      - baidu_configured: are all 3 BAIDU keys non-empty in .api_config.json?
      - voice_running: is voice_daemon process alive?
      - last_log_line: last error/info line from /tmp/voice.log
      - tts_volume: current vol level
      - alsa_mixer: current Playback Path value"""
    result = {}
    # 1. Check baidu keys
    try:
        cfg = _load_api_config()
        def _pick(*ks):
            for k in ks:
                v = cfg.get(k)
                if v:
                    return True
            return False
        result["baidu_configured"] = (
            _pick("BAIDU_APP_ID", "baidu_app_id") and
            _pick("BAIDU_API_KEY", "baidu_api_key") and
            _pick("BAIDU_SECRET_KEY", "baidu_secret_key")
        )
        result["baidu_app_id_head"] = (cfg.get("BAIDU_APP_ID") or cfg.get("baidu_app_id") or "")[:6]
    except Exception as e:
        result["baidu_configured"] = False
        result["baidu_err"] = str(e)

    # 2. Check voice_daemon process
    try:
        import subprocess as _sp
        out = _sp.run(["pgrep", "-f", "[v]oice_daemon.py"],
                      capture_output=True, timeout=3)
        result["voice_running"] = out.returncode == 0
        result["voice_pids"] = out.stdout.decode().strip().split()
    except Exception as e:
        result["voice_running"] = False
        result["voice_err"] = str(e)

    # 3. Last 5 log lines
    result["wake_log_markers"] = []
    try:
        for log_path in ["/tmp/voice_daemon.log", "/tmp/voice.log"]:
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                lines = all_lines[-5:]
                result["voice_log_tail"] = [l.rstrip() for l in lines]
                markers = ("wake_ack_done", "second_listen_open", "second_record_done",
                           "second_no_speech_idle", "second_asr_empty")
                result["wake_log_markers"] = [
                    l.rstrip() for l in all_lines[-120:]
                    if any(m in l for m in markers)
                ][-12:]
                break
    except Exception:
        pass
    try:
        result["voice_boot_status"] = _read_json_file("/dev/shm/voice_boot_status.json")
    except Exception:
        result["voice_boot_status"] = {}

    # 4. TTS volume
    result["tts_volume"] = _read_tts_volume(default=7)

    # 5. ALSA mixer state (Playback Path)
    try:
        import subprocess as _sp
        out = _sp.run(
            ["amixer", "-c", "0", "cget",
             "numid=1,iface=MIXER,name=Playback Path"],
            capture_output=True, timeout=3)
        result["alsa_playback_path"] = out.stdout.decode()[-120:] if out.stdout else ""
    except Exception:
        pass

    # 6. Common mixer controls used by mute/volume recovery.
    try:
        import subprocess as _sp
        controls = {}
        for ctrl in ("Playback", "Speaker", "Master", "Headphone"):
            out = _sp.run(["amixer", "-c", "0", "sget", ctrl],
                          capture_output=True, timeout=2)
            if out.returncode == 0 and out.stdout:
                controls[ctrl] = out.stdout.decode(errors="ignore")[-180:]
        result["alsa_volume_controls"] = controls
    except Exception:
        pass

    return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')


@app.route('/api/admin/voice_test', methods=['POST'])
def admin_voice_test():
    """V4.8: Trigger a test TTS playback. Writes to /dev/shm/chat_reply.txt
    so the existing speak() listener in voice_daemon picks it up.
    If Baidu not configured, falls back to `aplay` of a tone."""
    try:
        data = request.get_json(silent=True) or {}
        msg = data.get("msg", "你好，我是 IronBuddy 教练，语音系统工作正常")

        # Path A: if voice_daemon is running, poke its chat_reply.txt watcher
        # (it auto-speaks any new content in that file)
        with open("/dev/shm/chat_reply.txt.tmp", "w", encoding="utf-8") as f:
            f.write(msg)
        os.rename("/dev/shm/chat_reply.txt.tmp", "/dev/shm/chat_reply.txt")

        # Path B: ALSA sanity - play a 440Hz beep via speaker-test or sox if available
        # This verifies the hardware output path even without Baidu
        import subprocess as _sp
        beep_ok = False
        for cmd in [
            ["speaker-test", "-c", "2", "-t", "sine", "-f", "440", "-l", "1"],
            ["sh", "-c", "timeout 1 aplay /usr/share/sounds/alsa/Front_Center.wav"],
        ]:
            try:
                r = _sp.run(cmd, capture_output=True, timeout=4)
                if r.returncode == 0:
                    beep_ok = True
                    break
            except Exception:
                continue

        return Response(json.dumps({
            "ok": True,
            "message_queued": msg,
            "note": "若听见内容就是 TTS 通路 OK；若只听到静音请检查百度凭证+板端喇叭",
            "beep_fallback": beep_ok,
        }, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/fsm_state')
def api_fsm_state():
    """V4.8: Passthrough read of /dev/shm/fsm_state.json for UI polling.
    UI previously had no direct way to read angle/classification/emg."""
    try:
        path = "/dev/shm/fsm_state.json"
        if os.path.exists(path):
            with open(path, "r") as f:
                return Response(f.read(), mimetype='application/json')
    except Exception:
        pass
    return Response(json.dumps({"state": "IDLE", "good": 0, "failed": 0,
                                "angle": 0, "fatigue": 0, "exercise": "squat",
                                "classification": "standard"}),
                    mimetype='application/json')


@app.route('/api/admin/logs')
def admin_logs():
    """Return recent service log lines as JSON array of {timestamp, source, message}."""
    log_files = {
        'streamer': '/tmp/streamer.log',
        'vision': '/tmp/vision_local.log',
        'fsm': '/tmp/fsm_loop.log',
        'emg': '/tmp/emg_server.log',
        'voice': '/tmp/voice_daemon.log',
    }
    lines = []
    for source, path in log_files.items():
        try:
            if not os.path.exists(path):
                continue
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                # Read last 50 lines per file
                all_lines = f.readlines()
                tail = all_lines[-50:] if len(all_lines) > 50 else all_lines
                for raw in tail:
                    raw = raw.strip()
                    if not raw:
                        continue
                    # Try to extract timestamp from common formats: [2024-01-01 12:00:00] or 2024-01-01 12:00:00
                    ts = ''
                    msg = raw
                    if raw.startswith('['):
                        bracket_end = raw.find(']')
                        if bracket_end > 0:
                            ts = raw[1:bracket_end]
                            msg = raw[bracket_end + 1:].strip()
                    elif len(raw) > 19 and raw[4] == '-' and raw[10] == ' ':
                        ts = raw[:19]
                        msg = raw[19:].strip()
                    lines.append({'timestamp': ts, 'source': source, 'message': msg})
        except Exception:
            pass

    # Sort by timestamp (best effort) and limit to last 200
    lines.sort(key=lambda x: x['timestamp'])
    lines = lines[-200:]
    return Response(json.dumps(lines, ensure_ascii=False), mimetype='application/json')


@app.route('/api/admin/project_info')
def admin_project_info():
    """Project metadata: git, model, config"""
    info = {"git": {}, "model": {}, "config": {}}

    # Git info
    ok, branch = _run_cmd("cd {} && git rev-parse --abbrev-ref HEAD".format(PROJECT_ROOT))
    if ok:
        info["git"]["branch"] = branch
    ok, commit = _run_cmd("cd {} && git log --oneline -5".format(PROJECT_ROOT))
    if ok:
        info["git"]["recent_commits"] = commit.split("\n")
    ok, status = _run_cmd("cd {} && git status --short".format(PROJECT_ROOT))
    if ok:
        info["git"]["uncommitted"] = len([l for l in status.split("\n") if l.strip()])

    # Model info
    model_path = os.path.join(PROJECT_ROOT, "models", "extreme_fusion_gru.pt")
    if os.path.exists(model_path):
        st = os.stat(model_path)
        info["model"] = {
            "path": "models/extreme_fusion_gru.pt",
            "size_kb": round(st.st_size / 1024, 1),
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "architecture": "CompensationGRU 7D->similarity+3class",
        }

    # Config
    info["config"] = {
        "board_ip": BOARD_IP,
        "cloud_url": "http://127.0.0.1:6006/infer",
        "flask_port": 5000,
        "emg_port": 8080,
        "openclaw_port": 18789,
    }

    # TB logs
    tb_dir = os.path.join(PROJECT_ROOT, "models", "tb_logs")
    if os.path.isdir(tb_dir):
        tb_runs = sorted(os.listdir(tb_dir))
        info["training_runs"] = tb_runs[-5:] if len(tb_runs) > 5 else tb_runs

    return Response(json.dumps(info, ensure_ascii=False), mimetype='application/json')


# ===== SQLite 历史数据 API (Sprint 5 新增, 懒加载 & 失败安全) =====
_db_singleton = [None]
def _get_db():
    if _db_singleton[0] is not None:
        return _db_singleton[0]
    try:
        from hardware_engine.persistence.db import FitnessDB
        _db = FitnessDB()
        _db.connect()
        _db_singleton[0] = _db
        return _db
    except Exception:
        return None


def _flush_runtime_db_buffers(finalize_open_sessions=False):
    try:
        from hardware_engine.persistence.db import FitnessDB
        db = FitnessDB()
        db.connect()
        return db.flush_deferred_writes(
            finalize_open_sessions=bool(finalize_open_sessions)
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.route('/api/history/sessions')
def api_history_sessions():
    db = _get_db()
    data = db.get_recent_sessions(limit=20) if db is not None else []
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json')

@app.route('/api/history/today')
def api_history_today():
    db = _get_db()
    data = db.compute_daily_summary() if db is not None else {}
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json')

@app.route('/api/history/stats')
def api_history_stats():
    db = _get_db()
    data = db.get_range_stats(days=7) if db is not None else []
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json')


# ===== 数据库可视化（Sprint 6, 一站式 DB Viewer）=====
_DB_VIEW_WHITELIST = {
    'training_sessions': {
        'order_by': 'started_at DESC',
        'exercise_col': 'exercise',
        'seed_col': 'is_demo_seed',
    },
    'rep_events': {
        'order_by': 'ts DESC',
        'exercise_col': 'exercise',
        'seed_col': 'is_demo_seed',
    },
    'voice_sessions': {
        'order_by': 'ts DESC',
        'exercise_col': None,
        'seed_col': 'is_demo_seed',
    },
    'preference_history': {
        'order_by': 'ts DESC',
        'exercise_col': None,
        'seed_col': 'is_demo_seed',
    },
    'system_prompt_versions': {
        'order_by': 'id DESC',
        'exercise_col': None,
        'seed_col': 'is_demo_seed',
    },
    'daily_summary': {
        'order_by': 'date DESC',
        'exercise_col': None,
        'seed_col': 'is_demo_seed',
    },
    'llm_log': {
        'order_by': 'ts DESC',
        'exercise_col': None,
        'seed_col': 'is_demo_seed',
    },
    'user_config': {
        'order_by': 'key ASC',
        'exercise_col': None,
        'seed_col': None,
    },
    'model_registry': {
        'order_by': 'exercise ASC, id ASC',
        'exercise_col': 'exercise',
        'seed_col': 'is_demo_seed',
    },
}


def _db_view_path():
    """Resolve local sqlite path for the DB viewer (dev machine)."""
    candidate = os.path.join(PROJECT_ROOT, 'data', 'ironbuddy.db')
    return candidate


@app.route('/database')
def database_page():
    """一站式数据库可视化页面。"""
    try:
        html_path = os.path.join(template_dir, 'database.html')
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        resp = Response(html_content, mimetype='text/html')
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return resp
    except Exception as e:
        return '<h1>数据库页面加载失败</h1><p>%s</p>' % e, 500


@app.route('/api/db/tables')
def api_db_tables():
    """列出白名单表 + 每张表真实行数。

    V5.1: 增加 exists 字段让前端能看出表不存在（之前只把 total 归 0 容易被
    误会成"表空"）。
    """
    import sqlite3 as _sq
    db_path = _db_view_path()
    db_exists = os.path.exists(db_path)
    db_size_kb = round(os.path.getsize(db_path) / 1024.0, 1) \
        if db_exists else 0
    result = []
    try:
        conn = _sq.connect(db_path)
        # 先扫一次 sqlite_master 拿到所有存在的表名
        existing = set(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        )
        for name, meta in _DB_VIEW_WHITELIST.items():
            exists = name in existing
            if not exists:
                result.append({
                    'name': name, 'total': 0,
                    'exists': False,
                    'error': 'table not in schema (migration 未推送)',
                })
                continue
            try:
                count_sql = 'SELECT COUNT(*) FROM ' + name
                if meta['seed_col']:
                    try:
                        total = conn.execute(
                            count_sql + ' WHERE ' + meta['seed_col'] + '=0'
                        ).fetchone()[0]
                    except Exception:
                        total = conn.execute(count_sql).fetchone()[0]
                else:
                    total = conn.execute(count_sql).fetchone()[0]
                # V7.37: discover last write timestamp using common column names
                last_ts = ''
                for ts_col in ('started_at', 'ts', 'date', 'updated_at',
                               'timestamp', 'created_at'):
                    try:
                        row = conn.execute(
                            'SELECT MAX(' + ts_col + ') FROM ' + name
                        ).fetchone()
                        if row and row[0]:
                            last_ts = str(row[0])
                            break
                    except Exception:
                        continue
                result.append({
                    'name': name,
                    'total': total,
                    'last_ts': last_ts,
                    'exists': True,
                })
            except Exception as e:
                result.append({
                    'name': name, 'total': 0,
                    'exists': exists,
                    'error': str(e),
                })
        conn.close()
    except Exception as e:
        return Response(
            json.dumps({
                'error': str(e),
                'db_path': db_path,
                'db_exists': db_exists,
            }), status=500,
            mimetype='application/json'
        )
    return Response(
        json.dumps({
            'tables': result,
            'db_path': db_path,
            'db_exists': db_exists,
            'db_size_kb': db_size_kb,
        }, ensure_ascii=False),
        mimetype='application/json'
    )


@app.route('/api/db/diag')
def api_db_diag():
    """数据库诊断：DB 路径 / 大小 / WAL 状态 / schema 校验 / 迁移建议。"""
    import sqlite3 as _sq
    out = {
        'db_path': _db_view_path(),
        'db_exists': False,
        'db_size_kb': 0,
        'journal_mode': None,
        'sqlite_version': None,
        'tables_existing': [],
        'tables_missing': [],
        'rows': {},
        'cwd': os.getcwd(),
        'project_root': PROJECT_ROOT,
        'migrations_found': [],
        'recommendations': [],
    }
    try:
        if os.path.exists(out['db_path']):
            out['db_exists'] = True
            out['db_size_kb'] = round(
                os.path.getsize(out['db_path']) / 1024.0, 1
            )
            conn = _sq.connect(out['db_path'])
            out['journal_mode'] = conn.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
            out['sqlite_version'] = conn.execute(
                "SELECT sqlite_version()"
            ).fetchone()[0]
            existing = set(
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            )
            for name in _DB_VIEW_WHITELIST:
                if name in existing:
                    out['tables_existing'].append(name)
                    try:
                        out['rows'][name] = conn.execute(
                            'SELECT COUNT(*) FROM ' + name
                        ).fetchone()[0]
                    except Exception as e:
                        out['rows'][name] = 'ERR: ' + str(e)
                else:
                    out['tables_missing'].append(name)
            conn.close()
        else:
            out['recommendations'].append(
                '数据库文件不存在: ' + out['db_path'] +
                ' — 请检查 Flask CWD 或执行 schema 迁移'
            )
        # 扫本地可用的 migration 脚本
        scripts_dir = os.path.join(PROJECT_ROOT, 'scripts')
        if os.path.isdir(scripts_dir):
            for fn in sorted(os.listdir(scripts_dir)):
                if fn.startswith('migrate_') and fn.endswith('.sql'):
                    out['migrations_found'].append(fn)
        # 建议
        if out['tables_missing']:
            out['recommendations'].append(
                '缺失 %d 张表 — 请执行 schema 迁移补齐结构'
                % len(out['tables_missing'])
            )
        if out['db_exists']:
            empty = [t for t, c in out['rows'].items() if c == 0]
            if len(empty) >= 3:
                out['recommendations'].append(
                    '%d 张表空 — 请先确认采集、对话或模型写入链路是否正常'
                    % len(empty)
                )
    except Exception as e:
        out['error'] = str(e)
    return Response(
        json.dumps(out, ensure_ascii=False, indent=2, default=str),
        mimetype='application/json',
    )


def _backup_db_file():
    db_path = _db_view_path()
    if not os.path.exists(db_path):
        return ""
    import shutil
    bak = db_path + '.bak_' + str(int(time.time()))
    shutil.copy2(db_path, bak)
    return bak


@app.route('/api/db/query/<table>')
def api_db_query(table):
    """读白名单表，默认只返回真实记录。

    Query params:
      exercise: 过滤 exercise 列（如果该表有）
      limit: 默认 200，上限 2000
    """
    import sqlite3 as _sq

    if table not in _DB_VIEW_WHITELIST:
        return Response(
            json.dumps({'error': 'table not in whitelist'}), status=400,
            mimetype='application/json'
        )
    meta = _DB_VIEW_WHITELIST[table]

    try:
        limit = int(request.args.get('limit', 200))
    except Exception:
        limit = 200
    limit = max(1, min(limit, 2000))

    exercise = request.args.get('exercise', '').strip()
    where = []
    params = []
    if exercise and meta['exercise_col']:
        where.append(meta['exercise_col'] + '=?')
        params.append(exercise)
    if meta['seed_col']:
        where.append(meta['seed_col'] + '=0')

    sql = 'SELECT * FROM ' + table
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY ' + meta['order_by']
    sql += ' LIMIT ' + str(limit)

    try:
        conn = _sq.connect(_db_view_path())
        conn.row_factory = _sq.Row
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        conn.close()
        visible_idx = [i for i, col in enumerate(cols) if col != 'is_demo_seed']
        if len(visible_idx) != len(cols):
            cols = [cols[i] for i in visible_idx]
            rows = [[row[i] for i in visible_idx] for row in rows]
        return Response(
            json.dumps({
                'table': table, 'columns': cols, 'rows': rows,
                'count': len(rows), 'limit': limit,
                'filter': {'exercise': exercise},
            }, ensure_ascii=False, default=str),
            mimetype='application/json'
        )
    except Exception as e:
        return Response(
            json.dumps({'error': str(e), 'sql': sql}), status=500,
            mimetype='application/json'
        )


@app.route('/api/db/update/<table>/<int:row_id>', methods=['POST'])
def api_db_update(table, row_id):
    """隐藏写接口：只允许 voice_sessions + 白名单字段。

    POST JSON: {"field": "transcript|response|summary|duration_s|trigger_src",
                "value": "..."}
    """
    if table != 'voice_sessions':
        return Response(
            json.dumps({'ok': False, 'error': 'table not editable'}),
            status=403, mimetype='application/json'
        )
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}
    field = body.get('field', '')
    value = body.get('value', '')
    if not field:
        return Response(
            json.dumps({'ok': False, 'error': 'field required'}),
            status=400, mimetype='application/json'
        )
    db = _get_db()
    if db is None:
        return Response(
            json.dumps({'ok': False, 'error': 'db unavailable'}),
            status=500, mimetype='application/json'
        )
    ok = db.update_voice_session_field(row_id, field, value)
    return Response(
        json.dumps({'ok': bool(ok), 'field': field, 'id': row_id},
                   ensure_ascii=False),
        status=(200 if ok else 400), mimetype='application/json'
    )


@app.route('/api/db/embeddings')
def api_db_embeddings():
    """(deprecated V5.0) 旧 PCA 散点接口，保留兼容；新前端已改用 feature_dist。
    """
    ex = request.args.get('exercise', '').strip() or None
    db = _get_db()
    points = db.get_feature_embeddings(ex) if db is not None else []
    return Response(
        json.dumps({'points': points, 'deprecated': True}, ensure_ascii=False),
        mimetype='application/json'
    )


# ============================================================
# V5.0 · 7D 维度对比：从真实 CSV 读 3 类样本，算直方图 + F 统计
# ============================================================

# 7 个维度定义（与 tools/dashboard.py 对齐）
_FEAT_DIMS = ["Ang_Vel", "Angle", "Ang_Accel", "Target_RMS", "Comp_RMS",
              "Symmetry_Score", "Phase_Progress"]
_FEAT_CN = {
    "Ang_Vel": "角速度", "Angle": "关节角度", "Ang_Accel": "角加速度",
    "Target_RMS": "目标肌 EMG", "Comp_RMS": "代偿肌 EMG",
    "Symmetry_Score": "对称性", "Phase_Progress": "动作阶段",
}

# label 映射：CSV 里是 golden/lazy/bad，前端想显示 standard/compensating/non_standard
_LABEL_MAP = {
    "golden": "standard",
    "lazy": "compensating",
    "bad": "non_standard",
}

# 每个 exercise × label 的 CSV 目录。优先真实采集，fallback 到 MIA/augmented。
_CSV_DIRS = {
    "bicep_curl": {
        "golden": [
            "data/bicep_curl/golden",
            "data/bicep_curl_augmented/golden",
        ],
        "lazy": [
            "data/bicep_curl/lazy",
            "data/bicep_curl_augmented/lazy",
        ],
        "bad": [
            "data/bicep_curl/bad",
            "data/bicep_curl_augmented/bad",
        ],
    },
    "squat": {
        "golden": ["data/mia/squat/golden"],
        "lazy":   ["data/mia/squat/lazy"],
        "bad":    ["data/mia/squat/bad"],
    },
}

# 每类最多合并多少个文件（控制内存与耗时）
_MAX_FILES_PER_LABEL = 6
# 每类最多采样多少行
_MAX_ROWS_PER_LABEL = 6000


def _load_feature_values(exercise, label, dim):
    """读取指定 exercise×label 的 CSV，抽取某维列，返回 float 列表。"""
    import csv as _csv
    dirs = _CSV_DIRS.get(exercise, {}).get(label, [])
    out = []
    files_used = 0
    for d in dirs:
        abs_dir = os.path.join(PROJECT_ROOT, d)
        if not os.path.isdir(abs_dir):
            continue
        for fn in sorted(os.listdir(abs_dir)):
            if not fn.endswith(".csv"):
                continue
            files_used += 1
            if files_used > _MAX_FILES_PER_LABEL:
                break
            fp = os.path.join(abs_dir, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    reader = _csv.DictReader(f)
                    for row in reader:
                        v = row.get(dim)
                        if v is None or v == "":
                            continue
                        try:
                            out.append(float(v))
                        except ValueError:
                            pass
                        if len(out) >= _MAX_ROWS_PER_LABEL:
                            return out
            except Exception:
                pass
        if files_used > _MAX_FILES_PER_LABEL:
            break
    return out


def _hist_bins(values, bin_count, vmin, vmax):
    """把 values 分到 bin_count 个 bin，返回每个 bin 的计数（归一化到比例）。"""
    if vmax <= vmin:
        return [0] * bin_count
    step = (vmax - vmin) / bin_count
    bins = [0] * bin_count
    for v in values:
        idx = int((v - vmin) / step)
        if idx < 0:
            idx = 0
        elif idx >= bin_count:
            idx = bin_count - 1
        bins[idx] += 1
    total = sum(bins) or 1
    return [round(b / total, 4) for b in bins]


def _f_statistic(groups):
    """单因素方差分析的 F 统计（越大越可分）。groups: list[list[float]]。"""
    groups = [g for g in groups if len(g) >= 2]
    if len(groups) < 2:
        return 0.0
    all_vals = [v for g in groups for v in g]
    grand_mean = sum(all_vals) / len(all_vals)
    # 类间 (between-group) 平方和
    ss_b = sum(len(g) * ((sum(g) / len(g) - grand_mean) ** 2) for g in groups)
    # 类内 (within-group) 平方和
    ss_w = 0.0
    for g in groups:
        m = sum(g) / len(g)
        ss_w += sum((v - m) ** 2 for v in g)
    df_b = len(groups) - 1
    df_w = len(all_vals) - len(groups)
    if df_w <= 0 or ss_w <= 0:
        return 0.0
    ms_b = ss_b / df_b
    ms_w = ss_w / df_w
    return ms_b / ms_w if ms_w > 0 else 0.0


@app.route('/api/db/feature_dist')
def api_db_feature_dist():
    """V5.0 · 7D 维度对比。
    Query:
      exercise: bicep_curl / squat （默认 bicep_curl）
      dim: 指定维度（若缺省，返回全部 7 个维度的 F 统计）
      bins: 直方图柱数（默认 24）
    Response:
      { dim, exercise, bins, range: [vmin, vmax],
        hist: { standard: [...], compensating: [...], non_standard: [...] },
        counts: { standard: N, compensating: N, non_standard: N },
        f_stat, all_dims_f: { dim: f_stat, ... } }
    """
    ex = request.args.get('exercise', 'bicep_curl').strip()
    dim = request.args.get('dim', '').strip()
    try:
        bins = int(request.args.get('bins', 24))
    except Exception:
        bins = 24
    bins = max(8, min(bins, 60))

    if ex not in _CSV_DIRS:
        return Response(
            json.dumps({'error': 'unknown exercise',
                        'available': list(_CSV_DIRS.keys())}),
            status=400, mimetype='application/json'
        )

    # 收集 3 类数据
    groups = {}  # label -> list[float]（指定 dim 列）
    all_dims_data = {}  # label -> { dim: list[float] }
    labels_raw = ("golden", "lazy", "bad")
    for lbl in labels_raw:
        if dim:
            groups[lbl] = _load_feature_values(ex, lbl, dim)
        else:
            # 全维度扫描（用于算 all_dims_f）
            all_dims_data[lbl] = {}
            for d in _FEAT_DIMS:
                all_dims_data[lbl][d] = _load_feature_values(ex, lbl, d)

    # 计算全部维度的 F 统计（给下方表格）
    all_dims_f = {}
    if dim:
        # 指定维度时只算这一个；其他维度要单独扫一遍（慢但数据小够快）
        for d in _FEAT_DIMS:
            if d == dim:
                gs = [groups[l] for l in labels_raw]
            else:
                gs = [_load_feature_values(ex, l, d) for l in labels_raw]
            all_dims_f[d] = round(_f_statistic(gs), 3)
        cur_dim = dim
    else:
        # 未指定 dim 时，挑 F 最大的那个做默认
        for d in _FEAT_DIMS:
            gs = [all_dims_data[l][d] for l in labels_raw]
            all_dims_f[d] = round(_f_statistic(gs), 3)
        cur_dim = max(all_dims_f.keys(), key=lambda k: all_dims_f[k])
        groups = {l: all_dims_data[l][cur_dim] for l in labels_raw}

    # 计算范围
    all_vals = [v for vs in groups.values() for v in vs]
    if not all_vals:
        return Response(
            json.dumps({
                'error': 'no data',
                'exercise': ex, 'dim': cur_dim,
                'hint': 'CSV 目录为空或列名不匹配',
            }, ensure_ascii=False),
            status=200, mimetype='application/json'
        )
    vmin = min(all_vals)
    vmax = max(all_vals)
    # 边缘留 5% 余量
    pad = (vmax - vmin) * 0.05
    vmin_p = vmin - pad
    vmax_p = vmax + pad

    hist = {}
    counts = {}
    for lbl_raw in labels_raw:
        lbl_new = _LABEL_MAP[lbl_raw]
        vs = groups.get(lbl_raw, [])
        hist[lbl_new] = _hist_bins(vs, bins, vmin_p, vmax_p)
        counts[lbl_new] = len(vs)

    # F 统计（仅 cur_dim）
    f_stat = all_dims_f.get(cur_dim, 0.0)
    # 雷达图数据：每类在每个维度上的均值（归一化到 [0,1]）
    radar = _compute_radar(ex, labels_raw)

    return Response(
        json.dumps({
            'exercise': ex,
            'dim': cur_dim,
            'dim_cn': _FEAT_CN.get(cur_dim, cur_dim),
            'bins': bins,
            'range': [round(vmin_p, 4), round(vmax_p, 4)],
            'hist': hist,
            'counts': counts,
            'f_stat': round(f_stat, 3),
            'all_dims_f': all_dims_f,
            'all_dims_cn': _FEAT_CN,
            'radar': radar,
        }, ensure_ascii=False),
        mimetype='application/json'
    )


def _compute_radar(exercise, labels_raw):
    """返回雷达图数据：每维度每类均值归一化到 [0,1]。
    归一化方式：把该维度下 3 类的全样本 min/max 映射到 0/1。
    """
    out = {"dims": _FEAT_DIMS, "dims_cn": [_FEAT_CN[d] for d in _FEAT_DIMS],
           "series": {}}
    dim_stats = {}  # dim -> (min, max, {lbl: mean})
    for d in _FEAT_DIMS:
        per_lbl = {}
        all_vals = []
        for lbl_raw in labels_raw:
            vs = _load_feature_values(exercise, lbl_raw, d)
            if vs:
                per_lbl[lbl_raw] = sum(vs) / len(vs)
                all_vals.extend(vs)
        if all_vals:
            dim_stats[d] = (min(all_vals), max(all_vals), per_lbl)
        else:
            dim_stats[d] = (0, 1, {})
    for lbl_raw in labels_raw:
        lbl_new = _LABEL_MAP[lbl_raw]
        series = []
        for d in _FEAT_DIMS:
            vmin, vmax, per_lbl = dim_stats[d]
            mean = per_lbl.get(lbl_raw)
            if mean is None or vmax <= vmin:
                series.append(0.5)
            else:
                series.append(round((mean - vmin) / (vmax - vmin), 4))
        out["series"][lbl_new] = series
    return out


@app.route('/api/db/exercises')
def api_db_exercises():
    """返回所有 distinct exercise 列表（用于前端 dropdown）。"""
    import sqlite3 as _sq
    names = set()
    try:
        conn = _sq.connect(_db_view_path())
        for t in ('training_sessions', 'rep_events'):
            try:
                for r in conn.execute(
                    'SELECT DISTINCT exercise FROM ' + t +
                    ' WHERE exercise IS NOT NULL'
                ):
                    if r[0]:
                        names.add(r[0])
            except Exception:
                pass
        conn.close()
    except Exception:
        pass
    return Response(
        json.dumps({'exercises': sorted(names)}, ensure_ascii=False),
        mimetype='application/json'
    )


if __name__ == '__main__':
    print("[*] IronBuddy v3.1 online -- 0.0.0.0:5000")
    print("[*] Admin panel: http://localhost:5000/admin")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
