#!/usr/bin/env python3
"""
IronBuddy V4 语音守护进程 — 百度 AipSpeech 版
- TTS: 百度在线语音合成 → WAV → aplay 播放
- STT: arecord 录音 + 自适应VAD → 百度在线短语音识别
- 唤醒: 录音→STT→关键词匹配 ("教练"等)
- 参考: docs/hardware_ref/main2.py (已验证方案)
- 抛弃: Vosk (ABI不兼容), edge-tts (依赖微软，不稳定), Google ASR
- V4.5 (2026-04-18): 单轮问答 / ASR 幻觉过滤 / 硬警报尊重静音 / TTS 串音修复 / preflush arecord
"""
import os
import sys
import time
import json
import wave
import logging
import subprocess
import threading
import signal
import collections
import struct
import ctypes
try:
    import urllib.request as _urllib_request
except Exception:
    _urllib_request = None

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# V7.30 voice subsystem (state machine + arecord gate + turn id)
from hardware_engine.voice.state import VoiceState, VoiceStateMachine
from hardware_engine.voice.recorder import VADConfig, ArecordGate
from hardware_engine.voice.turn import Turn, TurnWriter
try:
    from hardware_engine.cognitive import adp_knowledge
except Exception as _adp_import_exc:
    logging.warning(u"[ADP] 导入失败，ADP 专业知识增强停用: %s", _adp_import_exc)
    adp_knowledge = None
try:
    from hardware_engine.cognitive import online_knowledge
except Exception as _online_import_exc:
    logging.warning(u"[OnlineRAG] 导入失败，在线专业知识增强停用: %s", _online_import_exc)
    online_knowledge = None
try:
    from hardware_engine.cognitive.coach_knowledge import (
        build_rag_context,
        format_capability_reply,
        format_manual_reply,
        is_capability_question,
        is_manual_question,
    )
except Exception as _kb_import_exc:
    logging.warning(u"[CoachKB] 导入失败，知识库增强停用: %s", _kb_import_exc)

    def build_rag_context(query, limit=3, max_chars=480):
        return u""

    def format_capability_reply(max_items=5):
        return u"我是 IronBuddy 智能健身伙伴，可以指导深蹲、弯举和飞书训练总结。"

    def format_manual_reply(query, max_hits=3):
        return format_capability_reply(max_items=5)

    def is_capability_question(text):
        return False

    def is_manual_question(text):
        return False

# Proxy disabled
for k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
    os.environ.pop(k, None)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [VOICE_V4] - %(message)s',
    handlers=[logging.StreamHandler()]
)

# ===== ALSA 错误静音 (参考 main2.py) =====
try:
    ERROR_HANDLER_FUNC = ctypes.CFUNCTYPE(
        None, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p)
    def _py_error_handler(filename, line, function, err, fmt):
        pass
    _c_error_handler = ERROR_HANDLER_FUNC(_py_error_handler)
    _asound = ctypes.cdll.LoadLibrary('libasound.so.2')
    _asound.snd_lib_error_set_handler(_c_error_handler)
except Exception:
    pass

# ===== 配置 =====
BAIDU_APP_ID = os.environ.get("BAIDU_APP_ID", "")
BAIDU_API_KEY = os.environ.get("BAIDU_API_KEY", "")
BAIDU_SECRET_KEY = os.environ.get("BAIDU_SECRET_KEY", "")

DEVICE_REC = os.environ.get("VOICE_FORCE_MIC", "plughw:0,0")  # V7.0: plughw \u8ba9 ALSA \u81ea\u52a8\u91c7\u6837\u7387\u9002\u914d
DEVICE_SPK = os.environ.get("VOICE_SPK", "plughw:0,0")
# V7.0: \u76f4\u63a5\u5f55 16kHz mono, \u5bf9\u9f50\u767e\u5ea6 ASR \u539f\u751f\u683c\u5f0f, \u5220\u9664 audioop \u91cd\u91c7\u6837\u5931\u771f
REC_RATE = 16000       # \u767e\u5ea6 ASR \u539f\u751f\u91c7\u6837\u7387
ASR_RATE = 16000       # \u767e\u5ea6ASR\u8981\u6c4216kHz (\u4e0e REC_RATE \u4e00\u81f4, \u65e0\u9700\u91cd\u91c7\u6837)
SILENCE_LIMIT = float(os.environ.get("VOICE_SILENCE_LIMIT", "2.0"))  # 停顿多久算说完 (秒)
VAD_TIMEOUT = 12       # 最长录音 (秒)
WAKE_TIMEOUT = 6       # 唤醒监听超时 (秒)

# Demo-visible wake word is always "教练"; the other entries are ASR typo tolerance.
WAKE_WORDS = ["教练", "叫练", "交练", "焦练"]
CHAT_INPUT_FILE = "/dev/shm/chat_input.txt"
CHAT_EVENTS_FILE = "/dev/shm/chat_events.jsonl"
CHAT_EVENTS_SEQ_FILE = "/dev/shm/chat_events.seq"
VOICE_INTERRUPT_FILE = "/dev/shm/voice_interrupt"
BARGE_WAKE_FILE = "/dev/shm/voice_barge_wake.json"
LAST_TTS_PAYLOAD_FILE = "/dev/shm/last_tts_payload.json"
REPLAY_LAST_TTS_FILE = "/dev/shm/replay_last_tts.json"
VOICE_BOOT_STATUS_FILE = "/dev/shm/voice_boot_status.json"
MANUAL_VOICE_RECORD_PATH = "/dev/shm/manual_voice_record.request"
MANUAL_VOICE_STOP_PATH = "/dev/shm/manual_voice_stop.request"
MANUAL_VOICE_STATUS_PATH = "/dev/shm/manual_voice_status.json"
VOICE_WAKE_EMPTY_ACK_DELAY = float(os.environ.get("VOICE_WAKE_EMPTY_ACK_DELAY", "0.6"))
ENABLE_BARGE_WAKE_WATCHER = os.environ.get("VOICE_ENABLE_BARGE_WAKE", "0").strip().lower() in ("1", "true", "yes", "on")
ENABLE_BARGE_MUTE_WATCHER = os.environ.get("VOICE_ENABLE_BARGE_MUTE", "0").strip().lower() in ("1", "true", "yes", "on")
STARTUP_DELAY = 5
_BOOT_PROMPT_TEXT = u"IronBuddy " + u"已上线，随时准备指导"
_BOOT_PROMPT_TTL = 8.0
_boot_prompt_lock = threading.Lock()
_boot_prompt_enqueued = [False]


def _sanitize_coach_text(text):
    # type: (str) -> str
    """Remove spoken wake words from assistant output.

    User utterances may contain the wake word and should remain untouched for
    the demo timeline. Assistant output must avoid it so speaker leakage cannot
    self-trigger another turn.
    """
    if not text:
        return text
    out = str(text)
    replacements = [
        (u"IronBuddy 教练", u"IronBuddy"),
        (u"健身教练", u"健身伙伴"),
        (u"教练对话", u"语音对话"),
        (u"教练回复", u"系统回复"),
    ]
    for old, new in replacements:
        out = out.replace(old, new)
    for word in WAKE_WORDS:
        out = out.replace(word, u"唤醒词")
    return out


# ===== V4.8 \u6301\u4e45\u5316\u63a5\u5165 (\u95f2\u804a\u5165\u5e93 + \u52a8\u6001 system_prompt) =====
# \u61d2\u521d\u59cb\u5316 FitnessDB \u5355\u4f8b; \u5931\u8d25\u65f6\u8fd4\u56de None, \u4e3b\u8def\u5f84\u6c38\u4e0d\u5d29\u3002
_DB_SINGLETON = None  # type: object


def _get_db():
    # type: () -> object
    """\u60f0\u6027\u53d6 FitnessDB \u5355\u4f8b\u3002\u4efb\u4f55\u5f02\u5e38\u5403\u6389\u8fd4\u56de None\u3002"""
    global _DB_SINGLETON
    if _DB_SINGLETON is not None:
        return _DB_SINGLETON
    try:
        # \u5ef6\u8fdf import, \u907f\u514d voice_daemon \u542f\u52a8\u4f9d\u8d56 sqlite3 (\u5df2\u5185\u7f6e\u6ca1\u98ce\u9669)
        _root = os.path.dirname(os.path.abspath(__file__))
        if _root not in sys.path:
            sys.path.append(_root)
        from persistence.db import FitnessDB  # noqa
        _inst = FitnessDB()
        _inst.connect()
        _DB_SINGLETON = _inst
        logging.info(u"[V4.8] FitnessDB \u5355\u4f8b\u5df2\u8fde\u63a5: %s", _inst.path)
    except Exception as _e:
        logging.warning(u"[V4.8] FitnessDB \u521d\u59cb\u5316\u5931\u8d25: %s", _e)
        _DB_SINGLETON = None
    return _DB_SINGLETON


# ===== V7.0 \u62fc\u97f3\u6a21\u7cca\u5339\u914d (\u53d6\u4ee3\u624b\u5de5 _HOMOPHONES) =====
# \u5e95\u5c42\u60f3\u6cd5: \u767e\u5ea6 ASR \u8fd4\u56de\u9519\u5b57\u65f6,\u62fc\u97f3\u901a\u5e38\u76f8\u8fd1(\u5982\u201c\u73a9\u5177\u201dwanju vs \u201c\u5f2f\u4e3e\u201dwanju)\u3002
# \u7528 pypinyin \u628a text \u8f6c\u62fc\u97f3 \u2192 \u5728\u786c\u7f16\u7801\u8bcd\u62fc\u97f3\u91cc\u6ed1\u7a97 edit distance \u2192 \u547d\u4e2d\u5c31\u628a\u6b63\u786e\u8bcd\u6ce8\u56de text\u3002
HARDCODED_TOKENS = [
    u"\u89e3\u9664\u9759\u97f3", u"\u9759\u97f3",
    u"\u6df1\u8e72", u"\u5f2f\u4e3e",
    u"\u5173\u673a", u"\u518d\u89c1\u6559\u7ec3",
    u"\u98de\u4e66",
    u"\u7eaf\u89c6\u89c9", u"\u89c6\u89c9\u52a0\u4f20\u611f",
    u"\u75b2\u52b3",
    u"\u5927\u4e00\u70b9", u"\u5c0f\u4e00\u70b9",
    u"\u8c03\u9ad8\u97f3\u91cf", u"\u8c03\u4f4e\u97f3\u91cf",
    u"\u6559\u7ec3",
    u"\u4f7f\u7528\u624b\u518c", u"\u529f\u80fd\u4ecb\u7ecd",
    u"\u591a\u5c11\u4e2a", u"\u5b8c\u6210\u5ea6", u"\u62a5\u6570",
    u"\u5207\u6362",
]

_PINYIN_AVAILABLE = False
_TOKEN_PINYIN = {}
try:
    from pypinyin import lazy_pinyin as _lazy_pinyin
    _PINYIN_AVAILABLE = True
    for _w in HARDCODED_TOKENS:
        if all(u'\u4e00' <= c <= u'\u9fff' for c in _w):
            _TOKEN_PINYIN[_w] = "".join(_lazy_pinyin(_w))
        else:
            _TOKEN_PINYIN[_w] = _w.lower()
except ImportError:
    logging.warning(u"pypinyin \u672a\u5b89\u88c5,\u62fc\u97f3\u5339\u914d\u5931\u6548 (pip3 install --user pypinyin)")


def _edit_distance(a, b):
    # type: (str, str) -> int
    """Levenshtein edit distance (O(len(a) * len(b))). \u5c0f\u5b57\u7b26\u4e32\u8db3\u591f\u5feb\u3002"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


def _pinyin_fuzzy_normalize(text):
    # type: (str) -> str
    """V7.0: \u5728 text \u4e2d\u627e\u4e0e\u786c\u7f16\u7801\u8bcd\u62fc\u97f3\u8fd1\u4f3c\u7684\u7247\u6bb5, \u8865\u6ce8\u6b63\u786e\u8bcd\u3002
    \u89c4\u5219: text \u5168\u62fc\u62fc\u97f3 \u2192 \u5bf9\u6bcf\u4e2a\u786c\u7f16\u7801\u8bcd\u62fc\u97f3 pat \u505a\u6ed1\u7a97\u5339\u914d
    \u4e0e pat \u7f16\u8f91\u8ddd\u79bb \u2264 thr \u7684\u89c6\u4e3a\u547d\u4e2d, \u5728 text \u672b\u5c3e\u8ffd\u52a0\u6b63\u786e\u8bcd (\u4e0d\u7834\u574f\u539f\u6587\u5b57\u5e8f)\u3002
    thr \u89c4\u5219: \u62fc\u97f3\u957f\u5ea6 \u2264 4 \u2192 thr=0 (\u9700\u7cbe\u786e), \u2264 6 \u2192 thr=1, \u2265 7 \u2192 thr=2"""
    if not _PINYIN_AVAILABLE or not text:
        return text
    # \u53ea\u5bf9\u4e2d\u6587\u5b57\u7b26\u8f6c\u62fc\u97f3;\u82f1\u6587/\u6570\u5b57\u539f\u6837\u4fdd\u7559
    try:
        text_py_parts = []
        for c in text:
            if u'\u4e00' <= c <= u'\u9fff':
                text_py_parts.append(_lazy_pinyin(c)[0])
            else:
                text_py_parts.append(c.lower())
        text_py = "".join(text_py_parts)
    except Exception:
        return text
    hits = set()
    for word, pat in _TOKEN_PINYIN.items():
        L = len(pat)
        if L == 0:
            continue
        if L <= 4:
            thr = 0
        elif L <= 6:
            thr = 1
        else:
            thr = 2
        # \u76f4\u63a5\u5305\u542b(\u8ddd\u79bb 0) \u6216\u6ed1\u7a97\u5339\u914d
        if pat in text_py:
            hits.add(word)
            continue
        best = 99
        # \u6ed1\u7a97 \u00b12 \u5bbd\u5ea6
        for win in (L - 1, L, L + 1):
            if win <= 0:
                continue
            for i in range(max(0, len(text_py) - win + 1)):
                frag = text_py[i:i + win]
                d = _edit_distance(frag, pat)
                if d < best:
                    best = d
                if best == 0:
                    break
            if best == 0:
                break
        if best <= thr:
            hits.add(word)
    if hits:
        # V7.0.3: \u628a\u547d\u4e2d\u8bcd\u76f4\u63a5\u62fc\u63a5\u5230 text \u672b\u5c3e (\u65e0\u62ec\u53f7)
        # \u4e0b\u6e38\u7684 "\u5207\u6362\u5230\u5f2f\u4e3e" / "\u5f2f\u4e3e\u6a21\u5f0f" / "\u505a\u5f2f\u4e3e" \u5c31\u80fd\u5339\u914d (\u56e0"\u5f2f\u4e3e"\u5df2\u5728 text)
        # \u987a\u5e8f\u5f88\u5173\u952e: "\u5207\u6362" \u5982\u679c\u547d\u4e2d, \u653e\u5728\u5176\u4ed6\u8bcd\u524d\u9762, \u8fd9\u6837 "\u5207\u6362X" \u8fde\u63a5\u6210\u7acb
        order = [u"\u5207\u6362", u"\u89e3\u9664\u9759\u97f3", u"\u9759\u97f3",
                 u"\u6df1\u8e72", u"\u5f2f\u4e3e", u"\u5173\u673a", u"\u518d\u89c1\u6559\u7ec3", u"\u98de\u4e66",
                 u"\u7eaf\u89c6\u89c9", u"\u89c6\u89c9\u52a0\u4f20\u611f", u"\u75b2\u52b3",
                 u"\u5927\u4e00\u70b9", u"\u5c0f\u4e00\u70b9",
                 u"\u8c03\u9ad8\u97f3\u91cf", u"\u8c03\u4f4e\u97f3\u91cf", u"\u6559\u7ec3",
                 u"\u591a\u5c11\u4e2a", u"\u5b8c\u6210\u5ea6", u"\u62a5\u6570"]
        ordered_hits = [w for w in order if w in hits]
        suffix = u" " + u" ".join(ordered_hits)
        augmented = text + suffix
        logging.info(u"[\u62fc\u97f3] \u539f=%s \u547d\u4e2d=%s \u6ce8\u5165=%s",
                     text[:30], u",".join(hits), suffix.strip())
        return augmented
    return text


# ===== V7.1 \u610f\u56fe\u8bc6\u522b\u5668: A\u8def(\u547d\u4ee4) vs B\u8def(\u95f2\u804a) =====
# A \u8def: \u542b\u4e0b\u5217\u547d\u4ee4\u7c7b\u578b\u8bcd \u2192 \u5f3a\u5236\u8d70\u672c\u5730\u786c\u7f16\u7801, \u4e0d\u7ed9 DeepSeek \u5047\u88c5\u7406\u89e3\u7684\u673a\u4f1a
# B \u8def: \u7eaf\u95f2\u804a("\u51e0\u70b9\u4e86"/"\u4e0b\u96e8\u5417"/"\u6df1\u8e72\u819d\u76d6\u600e\u4e48\u7ad9") \u2192 DeepSeek
COMMAND_INTENT_WORDS = (
    # \u6a21\u5f0f/\u52a8\u4f5c/\u5207\u6362
    u"\u6a21\u5f0f", u"\u52a8\u4f5c", u"\u5207\u6362", u"\u5207\u5230", u"\u6362\u5230",
    # \u97f3\u91cf
    u"\u97f3\u91cf", u"\u58f0\u97f3", u"\u5927\u70b9", u"\u5c0f\u70b9", u"\u5927\u4e00\u70b9", u"\u5c0f\u4e00\u70b9",
    u"\u8c03\u4f4e", u"\u8c03\u9ad8", u"\u8c03\u5927", u"\u8c03\u5c0f", u"\u8c03\u5230", u"\u964d\u5230", u"\u683c", u"\u6863",
    # \u9759\u97f3\u7c7b
    u"\u9759\u97f3", u"\u95ed\u5634", u"\u522b\u8bf4", u"\u522b\u543d", u"\u5b89\u9759",
    # \u89e3\u9664\u7c7b
    u"\u89e3\u9664", u"\u6062\u590d", u"\u7ee7\u7eed", u"\u522b\u88c5\u6b7b",
    # \u98de\u4e66\u63a8\u9001
    u"\u98de\u4e66", u"\u63a8\u9001", u"\u603b\u7ed3", u"\u6c47\u62a5", u"\u53d1\u6d88\u606f", u"\u53d1\u9001",
    # \u7535\u6e90
    u"\u5173\u673a", u"\u4e0b\u7ebf", u"\u518d\u89c1", u"\u7ed3\u675f\u8bad\u7ec3", u"\u8c22\u8c22\u6559\u7ec3",
    # \u75b2\u52b3\u4e0a\u9650
    u"\u75b2\u52b3", u"\u9608\u503c", u"\u4e0a\u9650", u"\u6539\u4e3a", u"\u6539\u6210",
    # \u52a8\u4f5c\u67e5\u8be2 (V7.7: "\u591a\u5c11" \u5355\u72ec\u4e5f\u7b97,\u4fbf\u4e8e"\u5f53\u524d\u97f3\u91cf\u591a\u5c11"\u7c7b\u77ed\u8bc6\u522b\u547d\u4e2d)
    u"\u591a\u5c11\u4e2a", u"\u591a\u5c11", u"\u51e0\u4e2a", u"\u5b8c\u6210\u5ea6", u"\u6210\u7ee9", u"\u62a5\u6570",
    # V7.10 \u7ec4\u95f4\u4f11\u606f
    u"\u4e0b\u4e00\u7ec4", u"\u4e0b\u7ec4", u"\u7ee7\u7eed\u8bad\u7ec3", u"\u5f00\u59cb\u4e0b\u4e00\u7ec4",
    # 今日智能训练计划
    u"\u4eca\u65e5\u8bad\u7ec3", u"\u4eca\u5929\u8bad\u7ec3", u"\u4eca\u65e5\u65b9\u6848", u"\u4eca\u5929\u65b9\u6848",
    u"\u8bad\u7ec3\u89c4\u5212", u"\u89c4\u5212\u8bad\u7ec3", u"\u91c7\u7eb3\u65b9\u6848", u"\u63a5\u53d7\u65b9\u6848",
    # "\u8bf7\u5e2e\u6211" \u524d\u7f00
    u"\u8bf7\u5e2e\u6211", u"\u5e2e\u6211",
)

# \u52a8\u4f5c\u540d + \u660e\u786e\u6307\u4ee4\u8bcd \u2192 A\u8def; \u5355\u72ec\u52a8\u4f5c\u540d + "\u600e\u4e48/\u5982\u4f55" \u2192 B\u8def
_ACTION_NAMES = (u"\u6df1\u8e72", u"\u5f2f\u4e3e", u"\u54d1\u94c3", u"\u8e72\u8d77")
# V7.30 R1 fix: removed standalone "\u505a" \u2014 it caused "\u73b0\u5728\u9002\u5408\u505a\u6df1\u8e72\u5417" (a chat
# question) to route as a command. Replaced with compound "\u60f3\u505a" so explicit
# user-intent like "\u60f3\u505a\u6df1\u8e72" still hits A \u8def.
_EXPLICIT_CMD_MARKERS = (u"\u5207\u6362", u"\u5207\u5230", u"\u6362\u5230",
                          u"\u60f3\u505a", u"\u5f00\u59cb", u"\u6a21\u5f0f")

# ===== M1 (V7.13, 2026-04-20): 更灵敏的唤醒词识别 =====
# 原策略: any(w in text for w in WAKE_WORDS) 严格字串包含 -> 百度 ASR 错 1 个字就漏
# 升级: 1) 原字串 2) 拼音片段 3) 短文本编辑距离<=2
_WAKE_PINYIN_PATS = (
    u"jiaolian", u"jiaoli", u"jiaol",
    u"jiaoni", u"jiaony",
    u"jieli", u"jial", u"jiaoy",
    u"jiaolan", u"jiaoliang",
)


def _is_wake_word(text):
    # type: (str) -> (bool, str)
    """M1: 返回 (hit, stripped_text).
    三级匹配:
      1) WAKE_WORDS 原字串包含 (向后兼容, 最快)
      2) 全拼音串包含 _WAKE_PINYIN_PATS 任一片段
      3) 短文本 (<=5字) 拼音编辑距离 <= 2 于 'jiaolian'
    stripped_text: 去掉所有唤醒词后的剩余文本, 用于 route.
    """
    if not text:
        return False, u""
    # 1) 原字串包含
    for w in WAKE_WORDS:
        if w in text:
            stripped = text
            for w2 in WAKE_WORDS:
                stripped = stripped.replace(w2, u"")
            return True, stripped.strip(u" ,.!?\u3002\uff0c\uff01\uff1f")
    # 2) 拼音片段
    if _PINYIN_AVAILABLE:
        try:
            py = u"".join(_lazy_pinyin(text))
            for pat in _WAKE_PINYIN_PATS:
                if pat in py:
                    logging.info(u"[M1_wake] \u62fc\u97f3\u7247\u6bb5\u547d\u4e2d '%s' in '%s'", pat, py[:30])
                    return True, text
        except Exception:
            pass
    # 3) 短文本拼音编辑距离
    if _PINYIN_AVAILABLE and len(text) <= 5:
        try:
            py = u"".join(_lazy_pinyin(text))
            if py and (_edit_distance(py, u"jiaolian") <= 2 or _edit_distance(py, u"jiaol") <= 1):
                logging.info(u"[M1_wake] \u7f16\u8f91\u8ddd\u79bb\u547d\u4e2d py=%s", py[:20])
                return True, u""
        except Exception:
            pass
    return False, u""


def _strip_wake_words(text):
    # type: (str) -> str
    stripped = text or u""
    for w in WAKE_WORDS:
        stripped = stripped.replace(w, u"")
    return stripped.strip(u" ,.!?\u3002\uff0c\uff01\uff1f")


def _is_command_intent(text):
    # type: (str) -> bool
    """V7.5 \u5224\u65ad\u7528\u6237\u610f\u56fe: \u7cbe\u786e\u5b50\u4e32 + \u62fc\u97f3\u6a21\u7cca (\u9632 ASR \u8fd1\u97f3\u8bef\u8bc6\u522b)"""
    if not text:
        return False
    if any(a in text for a in _ACTION_NAMES) and \
       any(w in text for w in [u"\u9002\u5408", u"\u53ef\u4ee5", u"\u80fd\u4e0d\u80fd", u"\u597d\u4e0d\u597d"]):
        return False
    # Manual/how-to questions should go to coach knowledge + DeepSeek, not execute.
    if any(w in text for w in [u"\u600e\u4e48", u"\u5982\u4f55", u"\u600e\u6837", u"\u8bf4\u660e", u"\u4ecb\u7ecd", u"\u624b\u518c", u"\u600e\u4e48\u7528"]):
        return False
    # \u660e\u663e\u547d\u4ee4\u8bcd (\u7cbe\u786e\u5339\u914d)
    if any(w in text for w in COMMAND_INTENT_WORDS):
        return True
    # \u52a8\u4f5c\u540d + \u660e\u786e\u6307\u4ee4\u8bcd
    if any(a in text for a in _ACTION_NAMES) and any(m in text for m in _EXPLICIT_CMD_MARKERS):
        return True
    # V7.5 \u62fc\u97f3\u6a21\u7cca: \u628a text \u8f6c\u62fc\u97f3, \u5bf9 COMMAND_INTENT_WORDS \u505a\u5b50\u4e32\u5305\u542b
    # \u5305\u542b "\u5e2e\u6211"(bangwo) / "\u5207\u6362"(qiehuan) / "\u5f2f\u4e3e"(wanju) / "\u54d1\u94c3"(yaling) \u7b49 \u2192 True
    if _PINYIN_AVAILABLE:
        try:
            text_py = "".join(_lazy_pinyin(text))
            _INTENT_PY = (u"bangwo", u"qiehuan", u"qieh", u"huand", u"jingyin",
                          u"jiechu", u"guanji", u"zaijian", u"feishu", u"yinliang",
                          u"wanju", u"shendun", u"yaling", u"dongzuo", u"moshi",
                          u"chuangan", u"pilao")
            for pat in _INTENT_PY:
                if pat in text_py:
                    logging.info(u"[\u610f\u56fe\u62fc\u97f3] %s -> %s \u547d\u4e2d %s", text[:20], text_py[:30], pat)
                    return True
        except Exception:
            pass
    return False


# 静音状态 (V4.5: 改为 list[bool] 容器，支持跨线程 / 嵌套函数可变访问，无需 global)
_is_muted = [False]
_speech_lock = threading.Lock()
_play_proc = None  # current aplay process

# V4.8 TTS volume control - persisted to /dev/shm so survives voice restart
_TTS_VOLUME_FILE = "/dev/shm/tts_volume.json"
_VOLUME_LEVELS = [3, 5, 7, 9, 15]  # Baidu TTS vol range 0-15; steps for up/down commands
_DEFAULT_VOL_IDX = 2  # starts at 7
_last_replay_ts = [0.0]

def _get_tts_volume():
    # V7.5: \u9ed8\u8ba4\u97f3\u91cf\u7531 7 \u964d\u5230 4 (\u907f\u514d\u9ed8\u8ba4\u592a\u5927\u9707\u8033)
    try:
        with open(_TTS_VOLUME_FILE, "r") as f:
            d = json.load(f)
            return int(d.get("vol", 4))
    except Exception:
        return 4


def _volume_to_alsa_percent(vol):
    # type: (int) -> int
    vol = max(1, min(15, int(vol)))
    return max(5, min(100, int(round((vol / 15.0) * 100))))


def _apply_playback_volume(vol=None, muted=False, reason="volume"):
    # type: (object, bool, str) -> dict
    """Best-effort RK3399 playback mixer update.

    Baidu's vol parameter only affects newly synthesised WAV files. This helper
    also nudges common ALSA controls so UI volume/unmute can recover the board
    speaker from a hard mute without depending on one exact mixer name.
    """
    if vol is None:
        vol = _get_tts_volume()
    vol = max(1, min(15, int(vol)))
    percent = 0 if muted else _volume_to_alsa_percent(vol)
    controls = ["Playback", "Speaker", "Master", "Headphone"]
    results = {}
    try:
        subprocess.run(
            ["sudo", "-n", "amixer", "-c", "0", "cset",
             "numid=1,iface=MIXER,name=Playback Path", "2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=2, check=False)
    except Exception:
        pass
    for ctrl in controls:
        cmd = ["sudo", "-n", "amixer", "-c", "0", "sset", ctrl, "%d%%" % percent]
        if muted:
            cmd.append("mute")
        else:
            cmd.append("unmute")
        try:
            ret = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, timeout=2,
                                 check=False)
            results[ctrl] = int(getattr(ret, "returncode", 1))
        except Exception:
            results[ctrl] = -1
    logging.info(u"[Mixer] reason=%s muted=%s vol=%d percent=%d result=%s",
                 reason, muted, vol, percent, results)
    return {"vol": vol, "percent": percent, "muted": muted, "results": results}


def _set_tts_volume(vol):
    vol = max(1, min(15, int(vol)))
    try:
        with open(_TTS_VOLUME_FILE + ".tmp", "w") as f:
            json.dump({"vol": vol, "ts": time.time()}, f)
        os.rename(_TTS_VOLUME_FILE + ".tmp", _TTS_VOLUME_FILE)
    except Exception as e:
        logging.warning("set_tts_volume failed: %s", e)
    _apply_playback_volume(vol=vol, muted=False, reason="set_tts_volume")


def _remember_tts_payload(text, prio=None, allow_interrupt=True):
    # type: (str, int, bool) -> None
    """Persist the last normal assistant TTS line for UI mute recovery."""
    if prio is None:
        prio = 3
    if not text or prio <= 0:
        return
    if text == _BOOT_PROMPT_TEXT or u"已上线，随时准备指导" in text:
        logging.info(u"[MuteReplay] skip boot prompt payload")
        return
    try:
        payload = {
            "text": _sanitize_coach_text(text),
            "prio": int(prio),
            "allow_interrupt": bool(allow_interrupt),
            "ts": time.time(),
        }
        tmp = LAST_TTS_PAYLOAD_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.rename(tmp, LAST_TTS_PAYLOAD_FILE)
    except Exception as e:
        logging.debug(u"[MuteReplay] remember failed: %s", e)


def _record_boot_prompt_status(status, error=None):
    # type: (str, object) -> None
    """Persist boot prompt state for the recording checklist and diagnostics."""
    payload = {
        "status": status,
        "text": _BOOT_PROMPT_TEXT,
        "ts": time.time(),
        "pid": os.getpid(),
    }
    if error is not None:
        payload["error"] = str(error)[:160]
    try:
        tmp = VOICE_BOOT_STATUS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.rename(tmp, VOICE_BOOT_STATUS_FILE)
    except Exception as e:
        logging.debug(u"[BOOT_PROMPT] status write failed: %s", e)


def _read_boot_prompt_status(max_age=_BOOT_PROMPT_TTL):
    # type: (float) -> dict
    try:
        with open(VOICE_BOOT_STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = float(data.get("ts") or 0.0)
        if ts <= 0 or (time.time() - ts) > max_age:
            return {}
        return data
    except Exception:
        return {}


def _pid_alive(pid):
    # type: (object) -> bool
    try:
        pid_i = int(pid)
        if pid_i <= 0:
            return False
        os.kill(pid_i, 0)
        return True
    except Exception:
        return False


def _is_boot_prompt_echo(text):
    # type: (str) -> bool
    """Ignore ASR leakage from the startup prompt."""
    if not text:
        return False
    t = str(text).replace(u" ", u"").lower()
    if u"已上线" in t or u"上线" in t and (u"随时" in t or u"准备" in t or u"指导" in t):
        return True
    # Baidu ASR sometimes hears "IronBuddy" as "八零" / "艾伦" on the board.
    if (u"八零" in t or u"艾伦" in t or u"ironbuddy" in t) and u"上线" in t:
        return True
    return False


def _enqueue_boot_prompt_once():
    # type: () -> bool
    """Queue the startup prompt once per daemon start and suppress stale doubles."""
    with _boot_prompt_lock:
        if _boot_prompt_enqueued[0]:
            _record_boot_prompt_status("skipped", "already_enqueued")
            logging.info(u"[BOOT_PROMPT] skipped already_enqueued")
            return False
        prev = _read_boot_prompt_status()
        prev_pid = prev.get("pid")
        prev_status = prev.get("status")
        if prev_status in ("queued", "playing", "played") and _pid_alive(prev_pid) and int(prev_pid) != os.getpid():
            _boot_prompt_enqueued[0] = True
            _record_boot_prompt_status("skipped", "live_" + str(prev_status))
            logging.info(u"[BOOT_PROMPT] skipped live status=%s pid=%s", prev_status, prev_pid)
            return False
        _boot_prompt_enqueued[0] = True
        _record_boot_prompt_status("queued")
        _sm.enqueue(PRIO_HARDCMD, _BOOT_PROMPT_TEXT, allow_interrupt=False)
        logging.info(u"[BOOT_PROMPT] queued once")
        return True


def _read_last_tts_payload(max_age=90.0):
    # type: (float) -> dict
    try:
        with open(LAST_TTS_PAYLOAD_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        text = (payload.get("text") or u"").strip()
        ts = float(payload.get("ts") or 0.0)
        if not text or not ts or (time.time() - ts) > max_age:
            return {}
        return payload
    except Exception:
        return {}


def _replay_last_tts_payload(reason="external_unmute"):
    # type: (str) -> bool
    if time.time() - _last_replay_ts[0] < 1.2:
        logging.info(u"[MuteReplay] skip duplicate reason=%s", reason)
        return False
    payload = _read_last_tts_payload()
    text = (payload.get("text") or u"").strip()
    if not text:
        logging.info(u"[MuteReplay] no payload to replay reason=%s", reason)
        return False
    if _is_muted[0]:
        logging.info(u"[MuteReplay] skip muted reason=%s", reason)
        return False
    prio = int(payload.get("prio") or PRIO_LLM)
    if prio <= PRIO_ALARM:
        prio = PRIO_LLM
    allow_interrupt = bool(payload.get("allow_interrupt", True))
    logging.info(u"[MuteReplay] replay reason=%s prio=%d text=%s",
                 reason, prio, text[:40])
    _last_replay_ts[0] = time.time()
    _sm.enqueue(prio, text, allow_interrupt=allow_interrupt)
    return True

# ===== V5.0 SpeechManager: \u5355\u901a\u9053\u4e32\u884c + \u56db\u6863\u4f18\u5148\u7ea7\u961f\u5217 =====
# \u89e3\u51b3\u6839\u56e0 A/B/C: \u6240\u6709\u58f0\u97f3\u8f93\u51fa\u5fc5\u987b\u901a\u8fc7 SpeechManager, \u7981\u6b62\u5806 speak() \u6563\u90fd
PRIO_ALARM    = 0   # L0 \u8fdd\u89c4\u8b66\u62a5 (\u62a2\u5360\u4e00\u5207,\u65e0\u89c6\u9759\u97f3)
PRIO_HARDCMD  = 1   # \u786c\u7f16\u7801\u547d\u4ee4\u56de\u62a5 (\u9759\u97f3/\u5207\u6a21\u5f0f/\u5173\u673a\u7b49)
PRIO_USER_ACK = 2   # \u7cfb\u7edf\u7b80\u7b54 ("\u6211\u5728"/"\u6ca1\u542c\u6e05")
PRIO_LLM      = 3   # LLM \u56de\u590d/\u81ea\u52a8\u603b\u7ed3/chat_reply

# mic/speaker \u4e92\u65a5\u95e8: speaker \u5de5\u4f5c\u65f6 clear, \u95f2\u65f6 set
# record_with_vad() \u524d\u5fc5\u987b .wait(), \u89e3\u51b3\u6839\u56e0 D (\u9ea6\u98ce\u78b0\u559c\u53ed\u6f0f\u97f3)
_mic_allowed = threading.Event()
_mic_allowed.set()

class SpeechManager(object):
    """\u5355\u4f8b: \u6240\u6709\u58f0\u97f3\u8f93\u51fa\u7684\u552f\u4e00\u51fa\u53e3\u3002
    - \u56db\u6863\u4f18\u5148\u7ea7\u6876 (0=ALARM,1=HARDCMD,2=ACK,3=LLM)
    - \u5355 worker \u7ebf\u7a0b\u6309\u4f18\u5148\u7ea7 pop \u64ad\u653e
    - \u9ad8\u4f18\u5148\u7ea7\u5165\u6863\u65f6\u62a2\u5360\u5f53\u524d\u4f4e\u6863\u64ad\u653e + flush \u5176\u540e\u9762\u961f\u5217
    - \u64ad\u653e\u671f\u95f4 _mic_allowed.clear(), \u64ad\u5b8c .set() (PTT \u534a\u53cc\u5de5)
    """
    def __init__(self, client):
        self._client = client
        self._buckets = {PRIO_ALARM: [], PRIO_HARDCMD: [], PRIO_USER_ACK: [], PRIO_LLM: []}
        self._lock = threading.Lock()
        self._current_prio = [99]  # \u5f53\u524d\u6b63\u5728\u64ad\u7684\u6863\u4f4d (mutable holder)
        self._play_proc = [None]
        self._started = [False]

    def set_client(self, client):
        self._client = client

    def start(self):
        if self._started[0]:
            return
        self._started[0] = True
        _t = threading.Thread(target=self._worker, daemon=True)
        _t.start()

    def enqueue(self, prio, text, allow_interrupt=True):
        # type: (int, str, bool) -> None
        if not text:
            return
        text = _sanitize_coach_text(text)
        # V6.2 \u9759\u97f3: \u53ea\u6709 PRIO_ALARM (L0\u8fdd\u89c4) \u80fd\u7a7f\u900f, \u5176\u4f59\u5168\u90e8\u4e22\u5f03\u5305\u62ec TTS \u5408\u6210\u97f3
        # \u89e3\u9664\u9759\u97f3 ACK \u5728 _try_voice_command \u91cc\u5148\u628a _is_muted[0]=False \u518d\u8c03 speak, \u8fd9\u65f6\u5df2\u4e0d\u5728\u9759\u97f3\u6001
        if _is_muted[0] and prio > PRIO_ALARM:
            logging.info(u"[SM] \u9759\u97f3\u4e2d,\u5168\u90e8\u4e22\u5f03 prio=%d: %s", prio, text[:30])
            return
        _remember_tts_payload(text, prio=prio, allow_interrupt=allow_interrupt)
        # V5.1: \u7acb\u5373 clear \u9ea6\u98ce\u95e8 (\u4e0d\u7b49 worker \u5f00\u59cb\u64ad\u518d clear)
        # \u6839\u6cbb\u7ade\u6001: \u4e3b\u5faa\u73af\u7684 _mic_allowed.wait() \u4e0d\u4f1a\u5728 TTS \u5408\u6210\u671f\u95f4\u8bef\u653e\u884c
        _mic_allowed.clear()
        with self._lock:
            # \u62a2\u5360\u89c4\u5219: \u65b0 item \u4f18\u5148\u7ea7 \u4e25\u683c\u9ad8\u4e8e \u5f53\u524d\u6b63\u5728\u64ad\u7684 \u2192 \u622a\u65ad + flush \u66f4\u4f4e\u6863\u6876
            if prio < self._current_prio[0]:
                logging.info(u"[SM] \u62a2\u5360: new=%d < playing=%d", prio, self._current_prio[0])
                self._preempt_current()
                # flush \u6240\u6709\u4e25\u683c\u4f4e\u4e8e\u65b0 prio \u7684\u6876 (\u7b49\u4e8e\u7684\u4fdd\u7559)
                for p in sorted(self._buckets.keys()):
                    if p > prio:
                        self._buckets[p] = []
            self._buckets[prio].append((text, allow_interrupt))

    def _preempt_current(self):
        p = self._play_proc[0]
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
            time.sleep(0.15)
            try:
                p.kill()
            except Exception:
                pass
            try:
                subprocess.run(["killall", "-9", "aplay"],
                               stderr=subprocess.DEVNULL, timeout=1)
            except Exception:
                pass
            time.sleep(0.3)

    def interrupt_current(self, reason="manual", clear_pending=True):
        # type: (str, bool) -> None
        """Stop current playback and optionally drop queued speech."""
        logging.info(u"[SM] interrupt_current reason=%s clear_pending=%s", reason, clear_pending)
        try:
            open(VOICE_INTERRUPT_FILE, "w").close()
        except Exception:
            pass
        self._preempt_current()
        if clear_pending:
            try:
                with self._lock:
                    for p in self._buckets:
                        self._buckets[p] = []
            except Exception:
                pass
        self._current_prio[0] = 99
        self._play_proc[0] = None
        _mic_allowed.set()

    def _pop_next(self):
        with self._lock:
            for p in sorted(self._buckets.keys()):
                if self._buckets[p]:
                    item = self._buckets[p].pop(0)
                    return p, item
        return None, None

    def _has_pending(self):
        with self._lock:
            for bucket in self._buckets.values():
                if bucket:
                    return True
        return False

    def _worker(self):
        while True:
            prio, item = self._pop_next()
            if item is None:
                time.sleep(0.05)
                continue
            text, allow_interrupt = item
            # V5.1: \u8fdb\u5165\u64ad\u653e \u2014 \u9ea6\u98ce\u95e8\u5df2\u5728 enqueue \u65f6 clear
            self._current_prio[0] = prio
            _mic_allowed.clear()  # \u5192\u9662 defensive
            try:
                ok = text2sound(self._client, text)
                if ok:
                    if text == _BOOT_PROMPT_TEXT:
                        _record_boot_prompt_status("playing")
                    self._play_proc[0] = self._launch_aplay("/tmp/voice_tts.wav")
                    self._wait_aplay(allow_interrupt, prio)
                    if text == _BOOT_PROMPT_TEXT:
                        _record_boot_prompt_status("played")
                elif text == _BOOT_PROMPT_TEXT:
                    _record_boot_prompt_status("failed", "tts_failed")
                # V7.3 + M6: 冷却 0.15s->0.05s (ALARM 播完尤其需要快速让 mic 回监听)
                time.sleep(0.05 if prio == PRIO_ALARM else 0.15)
            except Exception as e:
                logging.error(u"[SM] \u64ad\u653e\u5f02\u5e38: %s", e)
            finally:
                self._current_prio[0] = 99
                self._play_proc[0] = None
                # V5.1: \u961f\u5217\u5168\u7a7a\u624d\u91ca\u653e\u9ea6\u98ce\u95e8
                if not self._has_pending():
                    _mic_allowed.set()

    def _launch_aplay(self, file_path):
        if not os.path.exists(file_path):
            return None
        subprocess.run(
            ["sudo", "amixer", "-c", "0", "cset",
             "numid=1,iface=MIXER,name=Playback Path", "2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cmd = ["sudo", "aplay", "-D" + DEVICE_SPK, "-q",
               "-t", "wav", "-r", "16000", "-f", "S16_LE", "-c", "1", file_path]
        return subprocess.Popen(cmd)

    def _wait_aplay(self, allow_interrupt, prio):
        p = self._play_proc[0]
        if p is None:
            return
        # M6 (V7.13, 2026-04-20): 警报播报可被教练/对话状态掐断
        # - voice_interrupt 文件 (FSM / UI / 主循环写入)
        # - _dialog_active 被 set (主循环喊"教练"命中后立即 set)
        # V7.26 (2026-04-21): 扩展到 PRIO_LLM 档 — LLM 长回复播报期间 UI 或外部可主动写 voice_interrupt 掐断
        #                      打断后清空 LLM 队列, 避免后续堆积 TTS 继续播; 留给主循环重新接受 "教练" 唤醒
        _interruptible = (prio in (PRIO_ALARM, PRIO_LLM))
        while p.poll() is None:
            if _interruptible:
                try:
                    if os.path.exists(VOICE_INTERRUPT_FILE):
                        logging.info(u"[V7.26] prio=%d 被 voice_interrupt 掐断", prio)
                        try:
                            p.terminate()
                        except Exception:
                            pass
                        try:
                            subprocess.run(["killall", "-9", "aplay"],
                                           stderr=subprocess.DEVNULL, timeout=1)
                        except Exception:
                            pass
                        # V7.26: LLM 档掐断后清空后续队列, 防止下一条 TTS 继续跑
                        if prio == PRIO_LLM:
                            try:
                                with self._lock:
                                    self._buckets[PRIO_LLM] = []
                            except Exception:
                                pass
                        try:
                            os.remove(VOICE_INTERRUPT_FILE)
                        except OSError:
                            pass
                        break
                    # _dialog_active 仅对 ALARM 档起作用 (主循环喊"教练"命中)
                    if prio == PRIO_ALARM and _dialog_active.is_set():
                        logging.info(u"[M6] ALARM 撞上对话状态, 立即掐断")
                        try:
                            p.terminate()
                        except Exception:
                            pass
                        try:
                            subprocess.run(["killall", "-9", "aplay"],
                                           stderr=subprocess.DEVNULL, timeout=1)
                        except Exception:
                            pass
                        break
                except Exception:
                    pass
            time.sleep(0.05)


# SpeechManager \u5355\u4f8b (main \u91cc init \u540e set_client)
_sm = SpeechManager(None)

# \u8fdd\u89c4\u8b66\u62a5\u76d1\u542c
VIOLATION_ALERT_FILE = "/dev/shm/violation_alert.txt"
_violation_mtime = 0

# T5 (\u53c2\u8003 main2.py hard_alarm_worker): L0 \u7ea7\u786c\u8b66\u62a5
# \u72ec\u7acb\u7ebf\u7a0b + Event\uff0c\u8fdd\u89c4\u8b66\u62a5\u7acb\u5373\u901a\u8fc7 SpeechManager \u62a2\u5360\u64ad\u653e
_violation_event = threading.Event()
_violation_text_latest = [""]  # mutable holder for thread communication

# M3 (V7.13, 2026-04-20): 对话期绝对静默
# 喊"教练"命中的那一刻 set, 整轮对话(ASR + 命令处理 + TTS回复)结束后 clear
# hard_alarm_worker 看到 is_set() -> 整条 violation 直接丢弃
# 死上限 30s, 防卡死
_dialog_active = threading.Event()
_dialog_set_ts = [0.0]  # mutable holder
_DIALOG_MAX_SEC = 30.0

# V7.30: voice subsystem singletons
ACTIVE_SPEECH_CAP = 5.0  # 长独白硬截断 (s) — 由 VADConfig.apply_to_voice_daemon 覆盖
_voice_sm = VoiceStateMachine()
_arecord_gate = ArecordGate()
_turn_writer = TurnWriter()
_current_turn = [None]  # mutable holder for active dialog turn
_auto_busy_lock = threading.Lock()
_auto_busy_token = [0]
_auto_busy_active = [False]
_AUTO_BUSY_FAILSAFE_SEC = 18.0
_AUTO_BUSY_SPEECH_WAIT_SEC = 14.0
_barge_lock = threading.Lock()
_barge_wake = [{"text": "", "ts": 0.0}]
_voice_mute_lock = threading.Lock()
_voice_mute_inflight = [False]
_primary_listen_active = threading.Event()
_primary_listen_reason = [None]
_vad_call_lock = threading.Lock()
_manual_voice_lock = threading.Lock()
_manual_voice_active = threading.Event()
_manual_voice_last_request_ts = [0.0]


def _is_voice_mute_command(text):
    # type: (str) -> bool
    """Return True for terse voice mute/stop-speech commands."""
    if not text:
        return False
    t = _strip_wake_words(text)
    return any(w in t for w in [u"闭嘴", u"静音", u"别说话", u"别吵", u"不要说话", u"停止播报"])


def _execute_voice_mute_command(source="voice"):
    # type: (str) -> bool
    """Interrupt current speech, briefly confirm, then enter muted mode."""
    with _voice_mute_lock:
        if _voice_mute_inflight[0]:
            logging.info(u"[VoiceMute] duplicate ignored source=%s", source)
            return True
        _voice_mute_inflight[0] = True
    try:
        logging.info(u"[VoiceMute] interrupt-first mute source=%s", source)
        _sm.interrupt_current(reason="voice_mute_command", clear_pending=True)
        _kill_active_recording("voice_mute_command")
        try:
            subprocess.run(["killall", "-9", "aplay"],
                           stderr=subprocess.DEVNULL, timeout=1)
        except Exception:
            pass
        try:
            os.remove(VOICE_INTERRUPT_FILE)
        except OSError:
            pass
        _is_muted[0] = False
        _apply_playback_volume(muted=False, reason="voice_mute_ack")
        _publish_coach_ack(u"好的，已静音", kind="mute", allow_interrupt=False)
        _wait_sm_idle(2.5)
        _is_muted[0] = True
        _write_signal("/dev/shm/mute_signal.json", {
            "muted": True,
            "ts": time.time(),
            "src": source,
            "interrupt_first": True,
        })
        _apply_playback_volume(muted=True, reason="voice_mute")
        logging.info(u"命令: 静音 (打断当前播报后进入静音)")
        return True
    finally:
        _voice_mute_inflight[0] = False


def _speech_manager_busy():
    # type: () -> bool
    try:
        return _sm._current_prio[0] < 99 or _sm._has_pending()
    except Exception:
        return False


def _primary_listen_begin(reason):
    # type: (str) -> None
    _primary_listen_reason[0] = reason
    _primary_listen_active.set()
    logging.info(u"[MainListen] begin reason=%s", reason)


def _primary_listen_end(reason):
    # type: (str) -> None
    _primary_listen_active.clear()
    if _primary_listen_reason[0] == reason:
        _primary_listen_reason[0] = None
    logging.info(u"[MainListen] end reason=%s", reason)


def _barge_record_with_vad(timeout=2):
    # type: (int) -> str
    """Barge watchers use a non-blocking VAD slot and never steal main listen."""
    if _primary_listen_active.is_set():
        return "BUSY"
    if not _vad_call_lock.acquire(False):
        return "BUSY"
    try:
        if _primary_listen_active.is_set():
            return "BUSY"
        return record_with_vad(timeout=timeout, fast_start=True)
    finally:
        try:
            _vad_call_lock.release()
        except RuntimeError:
            pass


def _primary_record_with_vad(reason, timeout=VAD_TIMEOUT, fast_start=True, stop_check=None):
    # type: (str, int, bool, object) -> str
    """Serialize the user-facing recording window against background barge VAD."""
    _primary_listen_begin(reason)
    _vad_call_lock.acquire()
    try:
        return record_with_vad(timeout=timeout, fast_start=fast_start, stop_check=stop_check)
    finally:
        try:
            _vad_call_lock.release()
        finally:
            _primary_listen_end(reason)


def _primary_manual_record_until_stop(reason, timeout=10, stop_check=None):
    # type: (str, int, object) -> str
    """Serialize manual click-window recording against background barge VAD."""
    _vad_call_lock.acquire()
    _primary_listen_begin(reason)
    try:
        return record_manual_until_stop(timeout=timeout, stop_check=stop_check)
    finally:
        try:
            _primary_listen_end(reason)
        finally:
            _vad_call_lock.release()


def _write_manual_voice_status(state, text="", error="", extra=None):
    # type: (str, str, str, object) -> None
    payload = {
        "state": state,
        "text": text or "",
        "error": error or "",
        "ts": time.time(),
    }
    if isinstance(extra, dict):
        payload.update(extra)
    try:
        tmp = MANUAL_VOICE_STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.rename(tmp, MANUAL_VOICE_STATUS_PATH)
    except Exception as e:
        logging.debug(u"[ManualVoice] status write failed: %s", e)


def _read_json_signal(path):
    # type: (str) -> dict
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def _manual_voice_stop_requested(start_ts):
    # type: (float) -> bool
    try:
        if not os.path.exists(MANUAL_VOICE_STOP_PATH):
            return False
        data = _read_json_signal(MANUAL_VOICE_STOP_PATH)
        ts = float(data.get("ts") or os.path.getmtime(MANUAL_VOICE_STOP_PATH))
        return ts >= start_ts
    except Exception:
        return False


def _make_manual_voice_stop_check(request_ts, min_record_s=0.8):
    # type: (float, float) -> object
    # Accept stop clicks from this request, but keep enough audio for a valid WAV.
    record_start_ts = [None]

    def _check():
        now = time.time()
        if record_start_ts[0] is None:
            record_start_ts[0] = now
        if now - record_start_ts[0] < min_record_s:
            return False
        return _manual_voice_stop_requested(request_ts)

    return _check


def _set_barge_wake(text):
    # type: (str) -> None
    with _barge_lock:
        _barge_wake[0] = {"text": text or u"教练", "ts": time.time()}
    try:
        tmp = BARGE_WAKE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_barge_wake[0], f, ensure_ascii=False)
        os.rename(tmp, BARGE_WAKE_FILE)
    except Exception:
        pass


def _pop_barge_wake(max_age=4.0):
    # type: (float) -> str
    item = None
    with _barge_lock:
        item = dict(_barge_wake[0])
        _barge_wake[0] = {"text": "", "ts": 0.0}
    text = (item.get("text") or u"").strip()
    ts = float(item.get("ts") or 0.0)
    if not text or not ts or (time.time() - ts) > max_age:
        return u""
    try:
        if os.path.exists(BARGE_WAKE_FILE):
            os.remove(BARGE_WAKE_FILE)
    except OSError:
        pass
    return text


def _barge_wake_watcher(client):
    # type: (object) -> None
    """Listen for the wake word while TTS is playing, then interrupt playback.

    The assistant output is sanitised so it should not contain the wake word.
    That makes this watcher practical for the demo: if ASR hears the wake word
    during playback, it is very likely the user trying to interrupt.
    """
    logging.info(u"[BargeIn] 播报打断监听启动")
    last_hit_ts = [0.0]
    while True:
        try:
            if _dialog_active_safe() or _primary_listen_active.is_set():
                time.sleep(0.10)
                continue
            if _is_muted[0] or not _speech_manager_busy():
                time.sleep(0.15)
                continue
            if time.time() - last_hit_ts[0] < 2.0:
                time.sleep(0.15)
                continue
            status = _barge_record_with_vad(timeout=2)
            if status != "SUCCESS":
                time.sleep(0.05)
                continue
            text = sound2text(client)
            if not text:
                continue
            is_wake, _unused = _is_wake_word(text)
            if not is_wake:
                logging.info(u"[BargeIn] 播报中非唤醒输入,忽略: %s", text[:40])
                continue
            last_hit_ts[0] = time.time()
            logging.info(u"[BargeIn] 命中唤醒,立即打断播报: %s", text[:40])
            _set_barge_wake(text)
            _sm.interrupt_current(reason="barge_wake", clear_pending=True)
            _kill_active_recording("barge_wake")
            _mic_allowed.set()
            _force_arecord_resume("barge_wake")
        except Exception as e:
            logging.debug(u"[BargeIn] %s", e)
            time.sleep(0.3)


def _barge_mute_watcher(client):
    # type: (object) -> None
    """During long TTS, listen only for voice mute/stop commands."""
    logging.info(u"[BargeMute] 播报中静音命令监听启动")
    last_hit_ts = [0.0]
    while True:
        try:
            if _dialog_active_safe() or _primary_listen_active.is_set():
                time.sleep(0.10)
                continue
            if _is_muted[0] or not _speech_manager_busy():
                time.sleep(0.15)
                continue
            if time.time() - last_hit_ts[0] < 2.0:
                time.sleep(0.15)
                continue
            status = _barge_record_with_vad(timeout=2)
            if status != "SUCCESS":
                time.sleep(0.05)
                continue
            text = sound2text(client)
            if not text:
                continue
            is_wake, stripped = _is_wake_word(text)
            candidate = stripped if is_wake else text
            direct_short = (not is_wake) and len(candidate) <= 10
            if is_wake or direct_short:
                if _is_voice_mute_command(candidate):
                    last_hit_ts[0] = time.time()
                    logging.info(u"[BargeMute] 命中静音打断: %s", text[:40])
                    _execute_voice_mute_command(source="barge_voice")
                    continue
            logging.info(u"[BargeMute] 播报中非静音输入,忽略: %s", text[:40])
        except Exception as e:
            logging.debug(u"[BargeMute] %s", e)
            time.sleep(0.3)


def _start_turn(stage="wake", text=None, extra=None):
    """V7.30: open a new dialog turn and broadcast to UI via voice_turn.json."""
    turn = Turn.new()
    _current_turn[0] = turn
    try:
        _turn_writer.write(turn, stage=stage, text=text, extra=extra)
    except OSError as e:
        logging.warning(u"[TURN] start write failed: %s", e)
    return turn


def _emit_turn_stage(stage, text=None, extra=None):
    """V7.30: write a stage update for the current turn (no-op if none)."""
    turn = _current_turn[0]
    if turn is None:
        return
    try:
        _turn_writer.write(turn, stage=stage, text=text, extra=extra)
    except OSError as e:
        logging.warning(u"[TURN] stage=%s write failed: %s", stage, e)


def _close_turn():
    """V7.30: emit closed stage and drop the active turn ref."""
    turn = _current_turn[0]
    if turn is None:
        return
    try:
        _turn_writer.write(turn, stage="closed")
    except OSError as e:
        logging.warning(u"[TURN] close write failed: %s", e)
    _current_turn[0] = None


_chat_events_lock = threading.Lock()


def _read_chat_event_seq():
    # type: () -> int
    try:
        if os.path.exists(CHAT_EVENTS_SEQ_FILE):
            with open(CHAT_EVENTS_SEQ_FILE, "r") as f:
                return int((f.read() or "0").strip() or "0")
    except Exception:
        pass
    return 0


def _append_chat_event(role, text, kind="system_ack", stage=None, turn_id=None):
    # type: (str, str, str, str, str) -> int
    """Append one display bubble event for the main page.

    This is intentionally independent from chat_input/chat_reply compatibility
    files so the UI can render a single ordered video-friendly timeline.
    """
    if not text:
        return 0
    text = str(text).strip()
    if role != "user":
        text = _sanitize_coach_text(text)
    if not text:
        return 0
    lock_f = None
    try:
        turn = _current_turn[0]
        event_turn_id = turn_id
        if event_turn_id is None and turn is not None:
            event_turn_id = turn.turn_id
        event_stage = stage or "event"
        with _chat_events_lock:
            try:
                import fcntl
                lock_f = open(CHAT_EVENTS_FILE + ".lock", "a")
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            except Exception:
                lock_f = None
            try:
                seq = _read_chat_event_seq() + 1
                payload = {
                    "seq": seq,
                    "ts": time.time(),
                    "turn_id": event_turn_id or "",
                    "role": role,
                    "kind": kind,
                    "stage": event_stage,
                    "text": text,
                }
                with open(CHAT_EVENTS_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                tmp = CHAT_EVENTS_SEQ_FILE + ".tmp"
                with open(tmp, "w") as sf:
                    sf.write(str(seq))
                os.rename(tmp, CHAT_EVENTS_SEQ_FILE)
                return seq
            finally:
                if lock_f is not None:
                    try:
                        fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
                        lock_f.close()
                    except Exception:
                        pass
    except Exception as e:
        logging.debug(u"[ChatEvents] append failed: %s", e)
    return 0


def _finish_unclear_turn(reason="unclear", say_ack=True):
    # type: (str, bool) -> None
    """End a failed recognition turn without opening another recording window."""
    logging.info(u"[TurnClose] %s -> 回到等待教练", reason)
    if say_ack:
        _speak_ack(u"没听清", allow_interrupt=False)
        _wait_sm_idle(3.0)
    try:
        subprocess.run(["killall", "-9", "arecord"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        if os.path.exists("/dev/shm/chat_active"):
            os.remove("/dev/shm/chat_active")
    except OSError:
        pass
    _dialog_exit()


def _finish_quiet_turn(reason="quiet"):
    # type: (str) -> None
    """Close a wake-only empty turn quietly after a real listening window.

    Pure silence after the wake ACK is not a failed recognition. It should not
    scold the user during recording; only real speech with empty ASR uses
    _finish_unclear_turn().
    """
    logging.info(u"[TurnClose] %s -> 二段收音空白, 静默回待机", reason)
    if VOICE_WAKE_EMPTY_ACK_DELAY > 0:
        time.sleep(VOICE_WAKE_EMPTY_ACK_DELAY)
    try:
        subprocess.run(["killall", "-9", "arecord"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    try:
        if os.path.exists("/dev/shm/chat_active"):
            os.remove("/dev/shm/chat_active")
    except OSError:
        pass
    _dialog_exit()


def _dialog_enter():
    """M3: 进入对话模式, 屏蔽所有警报 TTS。V7.30: 同步 state machine."""
    _dialog_active.set()
    _dialog_set_ts[0] = time.time()
    if _voice_sm.state != VoiceState.DIALOG:
        _voice_sm.transition(VoiceState.DIALOG, reason="dialog_enter")
    logging.info(u"[M3_dialog] 进入对话, 警报屏蔽 ON")


def _dialog_exit():
    """M3: 退出对话模式, 恢复警报。V7.30: 同步 state machine + 关闭 turn."""
    _dialog_active.clear()
    _dialog_set_ts[0] = 0.0
    _close_turn()
    if _voice_sm.state != VoiceState.LISTEN:
        _voice_sm.transition(VoiceState.LISTEN, reason="dialog_exit")
    logging.info(u"[M3_dialog] 退出对话, 警报屏蔽 OFF")


def _dialog_active_safe():
    """M3: 带 30s 死上限的查询; 超时自动 clear 防卡死"""
    if not _dialog_active.is_set():
        return False
    if _dialog_set_ts[0] > 0 and (time.time() - _dialog_set_ts[0]) > _DIALOG_MAX_SEC:
        logging.warning(u"[M3_dialog] 超过 %ds, 强制退出", int(_DIALOG_MAX_SEC))
        _dialog_exit()
        return False
    return True


def _wait_sm_idle(timeout_sec):
    # type: (float) -> bool
    """M3: 等 SpeechManager 队列播完 + 当前 item 播完.
    返回 True 表示真空闲, False 表示超时但已退出等待.
    """
    _deadline = time.time() + timeout_sec
    while time.time() < _deadline:
        try:
            if _sm._current_prio[0] >= 99 and not _sm._has_pending():
                return True
        except Exception:
            return True
        time.sleep(0.1)
    return False


def _force_arecord_resume(reason):
    # type: (str) -> None
    """Best-effort SIGCONT even if local gate state drifted."""
    try:
        ret = subprocess.run(["sudo", "-n", "killall", "-SIGCONT", "arecord", "sudo"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             check=False, timeout=2)
        rc = getattr(ret, "returncode", 0)
        if rc == 0:
            logging.info(u"[ARECORD_GATE] force resume requested (reason=%s)", reason)
        else:
            logging.warning(u"[ARECORD_GATE] force resume returned rc=%s (reason=%s)",
                            rc, reason)
    except Exception as e:
        logging.warning(u"[ARECORD_GATE] force resume failed: %s", e)
    try:
        _arecord_gate.resume()
    except Exception as e:
        logging.warning(u"[ARECORD_GATE] gate resume failed: %s", e)


def _invalidate_vad_baseline(reason):
    # type: (str) -> None
    """Force the next VAD window to recalibrate after playback or interruption."""
    try:
        _VAD_BASELINE_CACHE["ts"] = 0.0
        _VAD_BASELINE_CACHE["baseline"] = 0.0
        logging.info(u"[VAD] baseline invalidated reason=%s", reason)
    except Exception:
        pass


def _kill_active_recording(reason):
    # type: (str) -> None
    """Stop an in-flight arecord before/after TTS so playback cannot self-enter ASR."""
    try:
        subprocess.run(["killall", "-9", "arecord"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=1)
        logging.info(u"[ARECORD_GATE] killed active recording (reason=%s)", reason)
    except Exception as e:
        logging.debug(u"[ARECORD_GATE] kill active recording failed (%s): %s", reason, e)
    _invalidate_vad_baseline(reason)


def _release_auto_busy(token, reason):
    # type: (int, str) -> bool
    """Release auto-trigger BUSY state and resume arecord exactly once per token."""
    with _auto_busy_lock:
        if token != _auto_busy_token[0]:
            logging.info(u"[AutoTrigger] skip stale release token=%s current=%s reason=%s",
                         token, _auto_busy_token[0], reason)
            return False
        if not _auto_busy_active[0]:
            logging.info(u"[AutoTrigger] skip inactive release token=%s reason=%s",
                         token, reason)
            return False
        _auto_busy_active[0] = False

    _force_arecord_resume(reason)
    if _voice_sm.state == VoiceState.BUSY:
        _voice_sm.transition(VoiceState.LISTEN, reason="auto_release_" + reason)
    _close_turn()
    _mic_allowed.set()
    logging.info(u"[AutoTrigger] released BUSY (reason=%s)", reason)
    return True


def _schedule_auto_busy_release(token, reason, delay_sec=0.0, wait_speech=True):
    # type: (int, str, float, bool) -> None
    """Schedule BUSY release after TTS queue drains, with a bounded failsafe."""
    def _runner():
        try:
            if delay_sec > 0:
                time.sleep(delay_sec)
            if wait_speech:
                ok = _wait_sm_idle(_AUTO_BUSY_SPEECH_WAIT_SEC)
                if not ok:
                    logging.warning(u"[AutoTrigger] speech wait timeout before release (%s)", reason)
            _release_auto_busy(token, reason)
        except Exception as e:
            logging.warning(u"[AutoTrigger] release thread failed: %s", e)
            try:
                _release_auto_busy(token, "release_exception")
            except Exception:
                pass

    threading.Thread(target=_runner, daemon=True).start()


def _enter_auto_busy(reason, payload):
    # type: (str, dict) -> int
    """Enter auto-trigger BUSY and arm a failsafe release watchdog."""
    with _auto_busy_lock:
        _auto_busy_token[0] += 1
        token = _auto_busy_token[0]
        _auto_busy_active[0] = True

    if _voice_sm.state != VoiceState.BUSY:
        _voice_sm.transition(VoiceState.BUSY, reason="auto_" + reason)
    _arecord_gate.suspend()
    _start_turn(stage="auto", text=None,
                extra={"trigger_source": reason,
                       "stats": {
                           "good": payload.get("good", 0),
                           "failed": payload.get("failed", 0),
                           "comp": payload.get("comp", 0),
                       }})
    _schedule_auto_busy_release(token, "failsafe_" + reason,
                                delay_sec=_AUTO_BUSY_FAILSAFE_SEC,
                                wait_speech=True)
    return token


# ===== ALSA mixer 路径激活 (参考 main2.py + toybrick_board_rules §2) =====
# 板载 RK809 codec 掉电后 Playback Path 会归零，Capture MIC Path 默认 OFF。
# 启动时幂等激活一次；板上 sudoers.d/ironbuddy 已配置 NOPASSWD，无须交互。
_mixer_ready = False

def ensure_mixer_paths():
    global _mixer_ready
    if _mixer_ready:
        return
    try:
        # Capture MIC Path=1 (Main Mic) —— 激活板载模拟麦克风通路
        subprocess.run(
            ["sudo", "-n", "amixer", "-c", "0", "cset",
             "numid=2,iface=MIXER,name=Capture MIC Path", "1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3, check=False)
        # Playback Path=2 (SPK PH2.0 板载扬声器) —— 对齐老师标准命令 + main2.py；2026-04-18 decisions.md 坑 10 平反
        subprocess.run(
            ["sudo", "-n", "amixer", "-c", "0", "cset",
             "numid=1,iface=MIXER,name=Playback Path", "2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3, check=False)
        logging.info("ALSA mixer 路径已激活: Capture MIC Path=1, Playback Path=2")
        _mixer_ready = True
    except Exception as e:
        logging.warning("ensure_mixer_paths 执行失败: %s (继续运行，依赖开机预设)", e)


# ===== 百度 AipSpeech 初始化 =====
def _init_baidu():
    try:
        from aip import AipSpeech
        if not BAIDU_APP_ID or not BAIDU_API_KEY or not BAIDU_SECRET_KEY:
            logging.error("百度语音 API 凭证未配置 (BAIDU_APP_ID/API_KEY/SECRET_KEY)")
            return None
        client = AipSpeech(BAIDU_APP_ID, BAIDU_API_KEY, BAIDU_SECRET_KEY)
        logging.info("百度 AipSpeech 已就绪 (APP_ID: %s)", BAIDU_APP_ID)
        return client
    except ImportError:
        logging.error("baidu-aip 未安装，请执行: pip3 install --user baidu-aip")
        return None


# ===== TTS: 百度合成 + aplay 播放 =====
def text2sound(client, text, file_path="/tmp/voice_tts.wav"):
    # type: (object, str, str) -> bool
    text = _sanitize_coach_text(text)
    with _speech_lock:
        try:
            # V4.8: aue=6 returns PROPER WAV (with RIFF header @16kHz mono). Do NOT use aue=4 (raw PCM → aplay plays at default 44.1k → chipmunk noise).
            result = client.synthesis(text, 'zh', 1, {'vol': _get_tts_volume(), 'per': 4, 'aue': 6})
            if not isinstance(result, dict):
                with open(file_path, 'wb') as f:
                    f.write(result)
                return True
            logging.error("TTS 合成失败: %s", result)
            return False
        except Exception as e:
            logging.error("TTS 异常: %s", e)
            return False


def play_audio(file_path="/tmp/voice_tts.wav", allow_interrupt=True):
    # type: (str, bool) -> None
    # V7.24 (2026-04-21): 撤掉 V7.23 引入的 /dev/shm/voice_speaking 文件信号 —
    # SpeechManager._worker 不经过 play_audio, 导致信号残留永久卡死主循环 L1300.
    # 正确互斥通道是进程内 _mic_allowed Event (主循环 L1294 wait, SpeechManager 入队 clear).
    global _play_proc
    if not os.path.exists(file_path):
        return

    # V4.9: 延长到 10s 并三段式释放,彻底避免第二条 aplay 抢占未释放的 PCM
    # (根因: V4.8 的 2s 超时在长句 TTS 时会被第二条抢占,出现"加速尖锐")
    if _play_proc is not None and _play_proc.poll() is None:
        try:
            _play_proc.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            try:
                _play_proc.terminate()
            except Exception:
                pass
            time.sleep(0.3)
            try:
                _play_proc.kill()
            except Exception:
                pass
            time.sleep(0.3)

    # 设置音箱通道 (板端掉电会归零，V4.4 已改 Playback Path=2 对齐 main2.py)
    subprocess.run(
        ["sudo", "amixer", "-c", "0", "cset", "numid=1,iface=MIXER,name=Playback Path", "2"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # V4.8: Use plughw + explicit 16kHz/mono format. aue=6 returns real WAV so -t wav parses header;
    # -r/-f/-c act as safety fallbacks if header ever missing.
    cmd = ["sudo", "aplay", "-D" + DEVICE_SPK, "-q",
           "-t", "wav", "-r", "16000", "-f", "S16_LE", "-c", "1", file_path]
    _play_proc = subprocess.Popen(cmd)

    while _play_proc.poll() is None:
        if allow_interrupt and os.path.exists(VOICE_INTERRUPT_FILE):
            logging.info("语音播放被打断")
            try:
                _play_proc.terminate()
                subprocess.run(["killall", "-9", "aplay"], stderr=subprocess.DEVNULL)
            except Exception:
                pass
            try:
                os.remove(VOICE_INTERRUPT_FILE)
            except OSError:
                pass
            break
        time.sleep(0.05)


def speak(client, text, allow_interrupt=True, priority=0):
    """V5.0 thin wrapper: \u8f6c\u53d1\u5230 SpeechManager \u5355\u4f8b\u3002
    \u517c\u5bb9\u65e7\u8c03\u7528\u70b9\u7684 `priority` \u53c2\u6570:
      - priority 0 = \u786c\u7f16\u7801\u547d\u4ee4\u56de\u62a5 (PRIO_HARDCMD)
      - priority 1 = \u8fdd\u89c4 L0 \u8b66\u62a5 (PRIO_ALARM)
    """
    if not text:
        return
    # ALARM \u7279\u5f81: priority>=1 \u88ab\u6620\u5c04\u4e3a PRIO_ALARM (\u65e0\u89c6\u9759\u97f3)
    prio = PRIO_ALARM if priority >= 1 else PRIO_HARDCMD
    _sm.enqueue(prio, text, allow_interrupt=allow_interrupt)


def _speak_ack(text, allow_interrupt=True):
    """\u7cfb\u7edf\u7b80\u7b54 (\u6211\u5728/\u6ca1\u542c\u6e05) \u2014 PRIO_USER_ACK"""
    if text:
        _append_chat_event("coach", text, kind="ack", stage="assistant_reply")
        _sm.enqueue(PRIO_USER_ACK, text, allow_interrupt=allow_interrupt)


def _speak_llm(text, allow_interrupt=True):
    """LLM \u56de\u590d / FSM \u603b\u7ed3 \u2014 PRIO_LLM (\u6c38\u4e0d\u62a2\u5360)"""
    if text:
        _sm.enqueue(PRIO_LLM, text, allow_interrupt=allow_interrupt)


def _publish_coach_ack(text, kind="system_ack", allow_interrupt=False):
    # type: (str, str, bool) -> None
    """Show a fixed coach line in the bubble timeline and speak it."""
    if not text:
        return
    _append_chat_event("coach", text, kind=kind, stage="assistant_reply")
    speak(None, text, allow_interrupt=allow_interrupt)


def _publish_user_event(text, kind="asr"):
    # type: (str, str) -> None
    _append_chat_event("user", text, kind=kind, stage="user_input")


def _publish_coach_event(text, kind="llm_reply"):
    # type: (str, str) -> None
    _append_chat_event("coach", text, kind=kind, stage="assistant_reply")


# ===== STT: arecord + VAD + 百度识别 =====
# M2 (V7.13, 2026-04-20): baseline 缓存 + fast_start 模式, 唤醒后二次录音跳过噪声采样
# 空白期 2.1s -> 0.6s. 单进程内存缓存, 30s 失效后重采
_VAD_BASELINE_CACHE = {"baseline": 0.0, "ts": 0.0}
_VAD_BASELINE_TTL = 30.0

def record_with_vad(timeout=VAD_TIMEOUT, fast_start=False, stop_check=None):
    # type: (int, bool, object) -> str
    """
    录音，自适应VAD检测说话结束。
    返回: "SUCCESS" (录到了), "SILENCE" (没人说话), "INTERRUPTED"
    M2: fast_start=True 时, 复用 30s 内的 baseline, 跳过 12 帧预采样.
    """
    import numpy as np

    # V4.5 preflush: 清理僵尸 arecord
    subprocess.run(["killall", "-9", "arecord"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.05 if fast_start else 0.2)
    logging.info(u"[VAD] record_start timeout=%s fast_start=%s", timeout, fast_start)

    cmd = ["sudo", "arecord", "-D" + DEVICE_REC, "-r%d" % REC_RATE,
           "-f", "S16_LE", "-c", "1", "-q"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    started = False
    silence_time = 0
    speech_start = 0.0  # V7.30: 长独白硬截断起点
    audio_frames = []
    pre_roll = collections.deque(maxlen=15)
    start_time = time.time()

    VAD_MIN = int(os.environ.get("VOICE_VAD_MIN", "600"))
    _base_delta = int(os.environ.get("VOICE_VAD_DELTA", "400"))
    if fast_start:
        VAD_DELTA = _base_delta
    else:
        VAD_DELTA = int(os.environ.get("VOICE_WAKE_VAD_DELTA", str(_base_delta)))
    VAD_CAP = int(os.environ.get("VOICE_VAD_CAP", "1500"))
    VAD_DEBUG = os.environ.get("VOICE_VAD_DEBUG", "0") == "1"

    # M2: baseline 缓存决策
    _cache_fresh = (time.time() - _VAD_BASELINE_CACHE["ts"]) < _VAD_BASELINE_TTL \
                   and _VAD_BASELINE_CACHE["baseline"] > 0
    if fast_start and _cache_fresh:
        baseline = _VAD_BASELINE_CACHE["baseline"]
        # fast_start 仍丢 1 帧 (128ms) 去唇音 pop
        _discard = proc.stdout.read(4096)
        logging.info(u"[VAD] fast_start 复用 baseline=%.0f (缓存龄%.1fs)",
                     baseline, time.time() - _VAD_BASELINE_CACHE["ts"])
    else:
        # 冷启动: 丢 2 帧 + 采 4 帧中位数 (较原 4+8 省 768ms)
        time.sleep(0.1 if fast_start else 0.2)
        noise_samples = []
        for _ in range(2):
            _discard = proc.stdout.read(4096)
        for _ in range(4):
            data = proc.stdout.read(4096)
            if data:
                arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(np.square(arr))))
                noise_samples.append(rms)
        if noise_samples:
            _sorted = sorted(noise_samples)
            baseline_raw = _sorted[len(_sorted) // 2]
        else:
            baseline_raw = 300
        if baseline_raw > VAD_CAP:
            logging.warning(u"[VAD] baseline=%d 异常偏高(>%d), 降级为先验值 500",
                            int(baseline_raw), VAD_CAP)
            baseline = 500.0
        else:
            baseline = baseline_raw
        _VAD_BASELINE_CACHE["baseline"] = baseline
        _VAD_BASELINE_CACHE["ts"] = time.time()

    threshold = max(VAD_MIN, baseline + VAD_DELTA)
    logging.info("VAD校准: baseline=%.0f threshold=%.0f (min=%d delta=%d fast=%s)",
                 baseline, threshold, VAD_MIN, VAD_DELTA, fast_start)

    output_path = "/tmp/voice_record.wav"

    try:
        while True:
            if stop_check is not None and stop_check():
                logging.info("[VAD] stop requested")
                break
            if time.time() - start_time > timeout:
                logging.info("录音超时 (%ds)", timeout)
                break

            data = proc.stdout.read(4096)
            if not data:
                break

            arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(np.square(arr))))

            # Debug: 实时打印 RMS vs threshold
            if VAD_DEBUG:
                logging.info("[VAD_DBG] rms=%.0f thresh=%.0f started=%s", rms, threshold, started)

            if not started:
                if rms > threshold:
                    started = True
                    speech_start = time.time()
                    audio_frames.extend(pre_roll)
                    audio_frames.append(data)
                    logging.info("[VAD] 触发! rms=%.0f > thresh=%.0f", rms, threshold)
                else:
                    pre_roll.append(data)
            else:
                audio_frames.append(data)
                if rms >= threshold:
                    silence_time = 0
                else:
                    # V7.0: mono 16bit = 2 bytes/sample, 4096 bytes / (16000 * 2) = 128ms
                    silence_time += 4096.0 / (REC_RATE * 2)

                if silence_time > SILENCE_LIMIT:
                    logging.info("VAD: 停顿 %.1fs，录音结束", SILENCE_LIMIT)
                    break

                # V7.30 S6 fix: 长独白硬截断 (用户连续发声超过 ACTIVE_SPEECH_CAP 秒)
                if (time.time() - speech_start) > ACTIVE_SPEECH_CAP:
                    logging.warning("[VAD] 长独白截断 (>%.1fs), 强制结束", ACTIVE_SPEECH_CAP)
                    break
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        subprocess.run(["sudo", "killall", "arecord"], stderr=subprocess.DEVNULL)

    if not started or not audio_frames:
        logging.debug("VAD: 无人说话 (started=%s, frames=%d)", started, len(audio_frames))
        return "SILENCE"

    # V7.0: \u5df2\u662f 16kHz mono \u539f\u751f\u683c\u5f0f, \u76f4\u63a5\u5199 WAV \u65e0\u9700\u91cd\u91c7\u6837
    raw_bytes = b''.join(audio_frames)

    with wave.open(output_path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(ASR_RATE)
        wf.writeframes(raw_bytes)

    wav_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    duration = len(raw_bytes) / (ASR_RATE * 2)
    logging.info(u"\u5f55\u97f3\u5b8c\u6210 (16k mono \u76f4\u5f55): %.1fs, %d bytes, %d\u5e27",
                 duration, wav_size, len(audio_frames))

    if wav_size < 500:
        logging.warning("录音文件太小 (%d bytes), 可能没录到声音", wav_size)
        return "SILENCE"

    return "SUCCESS"


def record_manual_until_stop(timeout=10, stop_check=None):
    # type: (int, object) -> str
    """Record the full manual click window without VAD gating."""
    output_path = "/tmp/voice_record.wav"
    subprocess.run(["killall", "-9", "arecord"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.05)
    logging.info(u"[ManualVoice] raw_record_start timeout=%ss device=%s", timeout, DEVICE_REC)

    cmd = ["sudo", "arecord", "-D" + DEVICE_REC, "-r%d" % REC_RATE,
           "-f", "S16_LE", "-c", "1", "-q"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    audio_frames = []
    start_time = time.time()
    stop_seen = False

    try:
        while True:
            if stop_check is not None and stop_check():
                stop_seen = True
                logging.info(u"[ManualVoice] stop requested")
                break
            if time.time() - start_time > timeout:
                logging.info(u"[ManualVoice] raw录音超时 (%ds)", timeout)
                break
            data = proc.stdout.read(4096)
            if not data:
                break
            audio_frames.append(data)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        subprocess.run(["sudo", "killall", "arecord"], stderr=subprocess.DEVNULL)

    raw_bytes = b"".join(audio_frames)
    duration = len(raw_bytes) / float(REC_RATE * 2)
    if len(raw_bytes) < int(REC_RATE * 2 * 0.25):
        logging.warning(u"[ManualVoice] raw录音过短: %.2fs, %d bytes", duration, len(raw_bytes))
        return "SILENCE"

    with wave.open(output_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(ASR_RATE)
        wf.writeframes(raw_bytes)

    wav_size = os.path.getsize(output_path) if os.path.exists(output_path) else 0
    logging.info(u"[ManualVoice] raw录音完成: %.1fs, %d bytes, stop=%s",
                 duration, wav_size, stop_seen)
    return "SUCCESS"


def sound2text(client, file_path="/tmp/voice_record.wav"):
    # type: (object, str) -> str
    """百度短语音识别

    V4.5: 加入幻觉过滤 — 百度 ASR 对噪声 / 感叹词易乱识别，过短或纯感叹词直接舍弃，
    避免发给 DeepSeek 造成同音词污染和无意义轮询。
    """
    try:
        wav_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        logging.info("ASR 请求: %s (%d bytes)", file_path, wav_size)
        with open(file_path, 'rb') as fp:
            audio_data = fp.read()
            result = client.asr(audio_data, 'wav', ASR_RATE, {'dev_pid': 1537})
            logging.info("ASR 原始返回: err_no=%s, result=%s",
                         result.get('err_no'), str(result.get('result', ''))[:100])
            if result.get('err_no') == 0:
                text = result['result'][0] if 'result' in result and result['result'] else ''
                # V4.5 幻觉过滤：去标点 + 过短舍弃 + 感叹词舍弃
                text = text.strip().rstrip('。，！？.,!?')
                if len(text) < 2:
                    logging.info("[ASR] 过短舍弃: %r", text)
                    return ""
                if text in {"嗯", "啊", "哦", "呃", "嗯嗯", "啊啊", "哦哦", "嗯啊", "哎"}:
                    logging.info("[ASR] 感叹词舍弃: %r", text)
                    return ""
                logging.info("ASR 识别: %s", text)
                return text
            else:
                logging.warning("ASR 错误 (err_no=%s): %s", result.get('err_no'), result.get('err_msg', ''))
    except Exception as e:
        logging.error("ASR 异常: %s", e)
    return ""


# ===== Debug 输出 =====
def output_debug(energy, text):
    try:
        data = {"energy": float(energy), "threshold": 0, "text": text}
        with open("/dev/shm/voice_debug.json.tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.rename("/dev/shm/voice_debug.json.tmp", "/dev/shm/voice_debug.json")
    except Exception:
        pass


def _manual_voice_watcher(client, route_fn):
    # type: (object, object) -> None
    """UI fallback: record one utterance without requiring the wake word."""
    logging.info(u"[ManualVoice] watcher started")
    _write_manual_voice_status("idle")
    while True:
        time.sleep(0.20)
        try:
            if not os.path.exists(MANUAL_VOICE_RECORD_PATH):
                continue
            req = _read_json_signal(MANUAL_VOICE_RECORD_PATH)
            req_ts = float(req.get("ts") or os.path.getmtime(MANUAL_VOICE_RECORD_PATH))
            if req_ts <= _manual_voice_last_request_ts[0]:
                continue
            _manual_voice_last_request_ts[0] = req_ts
            if not _manual_voice_lock.acquire(False):
                _write_manual_voice_status("busy", error="manual recorder busy")
                continue
            try:
                if _is_muted[0]:
                    _write_manual_voice_status("error", error="muted")
                    continue
                active_reason = _primary_listen_reason[0]
                if _speech_manager_busy() or (
                    _primary_listen_active.is_set() and active_reason != "wake_idle"
                ):
                    _write_manual_voice_status("busy", error="voice busy")
                    continue
                _manual_voice_active.set()
                _kill_active_recording("manual_voice_start")
                time.sleep(0.10)
                _start_turn(stage="wake")
                _dialog_enter()
                _write_manual_voice_status("recording")
                _mic_allowed.wait()
                status = _primary_manual_record_until_stop(
                    "manual_voice",
                    timeout=min(VAD_TIMEOUT, 10),
                    stop_check=_make_manual_voice_stop_check(req_ts),
                )
                _write_manual_voice_status("recognizing", extra={"record_status": status})
                if status == "SUCCESS":
                    text = sound2text(client)
                    if text and len(text) >= 2:
                        _write_manual_voice_status("routed", text=text)
                        route_fn(text, display_text=text)
                    else:
                        _write_manual_voice_status("empty", error="asr_empty")
                        _finish_quiet_turn("manual_voice_empty")
                else:
                    _write_manual_voice_status("empty", error=status)
                    _finish_quiet_turn("manual_voice_" + status)
            except Exception as e:
                logging.warning(u"[ManualVoice] failed: %s", e)
                _write_manual_voice_status("error", error=str(e)[:160])
            finally:
                _kill_active_recording("manual_voice_done")
                _wait_sm_idle(3.0)
                _dialog_exit()
                _manual_voice_active.clear()
                try:
                    _manual_voice_lock.release()
                except RuntimeError:
                    pass
        except Exception as e:
            logging.debug(u"[ManualVoice] watcher loop: %s", e)


# ===== 主循环 =====
def main():
    global BAIDU_APP_ID, BAIDU_API_KEY, BAIDU_SECRET_KEY
    logging.info("等待 %ds 让其他服务初始化...", STARTUP_DELAY)
    time.sleep(STARTUP_DELAY)

    # 清理残留 arecord/aplay 进程
    subprocess.run(["sudo", "killall", "-9", "arecord", "aplay"],
                   stderr=subprocess.DEVNULL)
    # V4.8: 删除旧 TTS 文件避免前一次运行的损坏 WAV 被意外重放
    for _stale in ("/tmp/voice_tts.wav", "/tmp/voice_record.wav", REPLAY_LAST_TTS_FILE):
        try:
            if os.path.exists(_stale):
                os.remove(_stale)
        except Exception:
            pass
    # V5.0: 启动时 truncate 旧 shm 文件 + 清零 .seq,防止旧内容/旧 seq 触发首轮重播
    _startup_now = time.time()
    main._chat_reply_mtime = _startup_now
    for _shm in ("/dev/shm/chat_reply.txt", "/dev/shm/llm_reply.txt",
                 "/dev/shm/violation_alert.txt"):
        try:
            with open(_shm, "w", encoding="utf-8") as _cf:
                _cf.write("")
            os.utime(_shm, (_startup_now, _startup_now))
        except Exception:
            pass
    # seq 文件跟 watcher 的 last_seq 对齐: watcher 启动时读 seq 作为基线
    # FSM 累加 seq 所以启动时清零到 0 让 watcher 正确基线
    for _seq in ("/dev/shm/chat_reply.txt.seq", "/dev/shm/llm_reply.txt.seq"):
        try:
            with open(_seq, "w") as _sf:
                _sf.write("0")
        except Exception:
            pass

    # ===== M10 (V7.16, 2026-04-20): 语音守护启动初始化 =====
    # 背景: 上次会话意外中止可能残留以下文件, 让新 session 一启动就误判:
    #   - chat_active 残留 -> hard_alarm_worker 永远 skip (用户感觉"长期没反应")
    #   - voice_interrupt 残留 -> 启动第一段 TTS 被立刻掐断
    #   - mute_signal 残留 muted=true -> 整个语音静默
    # V7.24 (2026-04-21): 新增 voice_speaking 清理 —
    #   - V7.23 曾引入 /dev/shm/voice_speaking 作为 TTS 互斥信号, 但 SpeechManager 不走 play_audio
    #     导致 watcher touch 后从未 delete, 残留永久卡死主循环 L1300
    #   - 本次撤掉该信号 (V7.24), 但仍保留启动清理以覆盖任何历史残留 (含跨版本升级场景)
    _m10_voice_cleanup = ["/dev/shm/chat_active", "/dev/shm/voice_interrupt", "/dev/shm/voice_speaking"]
    _v_cleaned = 0
    for _f in _m10_voice_cleanup:
        try:
            if os.path.exists(_f):
                os.remove(_f)
                _v_cleaned += 1
        except OSError:
            pass
    # mute_signal 重置为非静音 (覆盖残留 true)
    try:
        with open("/dev/shm/mute_signal.json.tmp", "w") as _mf:
            json.dump({"muted": False, "ts": _startup_now}, _mf)
        os.rename("/dev/shm/mute_signal.json.tmp", "/dev/shm/mute_signal.json")
        _is_muted[0] = False
    except Exception as _e:
        logging.debug(u"[M10] mute_signal 重置失败: %s", _e)
    logging.info(u"🧹 [M10] 语音启动清理: 移除 %d 个残留信号, mute=false", _v_cleaned)

    # 激活 ALSA mixer 通路 (板载 MIC + SPK)；板重启后 Path 会归零
    ensure_mixer_paths()

    # === 启动诊断 ===
    logging.info("===== 启动诊断 =====")
    logging.info("BAIDU_APP_ID: %s", "已配置(%s...)" % BAIDU_APP_ID[:4] if BAIDU_APP_ID else "未配置!")
    logging.info("BAIDU_API_KEY: %s", "已配置(%s...)" % BAIDU_API_KEY[:4] if BAIDU_API_KEY else "未配置!")
    logging.info("BAIDU_SECRET_KEY: %s", "已配置" if BAIDU_SECRET_KEY else "未配置!")
    logging.info("录音设备: %s, 播放设备: %s", DEVICE_REC, DEVICE_SPK)
    # V7.30: install VAD caps from VADConfig (overrides legacy defaults)
    _vad_cfg = VADConfig()
    _vad_cfg.apply_to_voice_daemon(sys.modules[__name__])
    logging.info("VAD参数: SILENCE_LIMIT=%.1fs, WAKE_TIMEOUT=%ds, VAD_TIMEOUT=%ds, ACTIVE_SPEECH_CAP=%.1fs",
                 SILENCE_LIMIT, WAKE_TIMEOUT, VAD_TIMEOUT, ACTIVE_SPEECH_CAP)
    logging.info("====================")

    client = _init_baidu()
    if client is None:
        logging.error("百度 AipSpeech 初始化失败，进入轮询等待 (UI 保存百度凭证后 30s 内自动恢复)")
        # V4.8: 从 .api_config.json 热加载，避免 Settings 保存后要重启整个服务
        while client is None:
            output_debug(0, "百度API未配置,等待 Settings 保存")
            time.sleep(15)
            try:
                import json as _j
                cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".api_config.json")
                if os.path.exists(cfg_path):
                    with open(cfg_path, "r") as _cf:
                        _cfg = _j.load(_cf)
                    _bappid = _cfg.get("BAIDU_APP_ID") or _cfg.get("baidu_app_id") or ""
                    _bapik = _cfg.get("BAIDU_API_KEY") or _cfg.get("baidu_api_key") or ""
                    _bseck = _cfg.get("BAIDU_SECRET_KEY") or _cfg.get("baidu_secret_key") or ""
                    if _bappid and _bapik and _bseck:
                        BAIDU_APP_ID = _bappid
                        BAIDU_API_KEY = _bapik
                        BAIDU_SECRET_KEY = _bseck
                        logging.info("检测到 .api_config.json 已填写百度凭证，尝试重新初始化...")
                        client = _init_baidu()
                        if client is not None:
                            logging.info("✓ 百度 AipSpeech 热加载成功")
                            break
            except Exception as _hot_e:
                logging.debug("热加载检查失败: %s", _hot_e)

    # === 麦克风自测 ===
    # 若 env 强制指定麦克风则跳过自测（修复 hw:2,0 exit=1 但 size=176KB 的误判 bug）
    if os.environ.get("VOICE_FORCE_MIC"):
        logging.info("VOICE_FORCE_MIC=%s: 跳过麦克风自测，强信任", DEVICE_REC)
        test_ok = True
    else:
        logging.info("麦克风自测: 录制1秒...")
        test_ok = False
        # 优先板载 hw:0,0 (RK809)，USB Webcam hw:2,0 作为降级，ES7243 mic-array hw:3,0 兜底
        _fallback_devs = [DEVICE_REC, "hw:0,0", "hw:2,0", "hw:3,0"]
        _tried = set()
        for dev in _fallback_devs:
            if dev in _tried:
                continue
            _tried.add(dev)
            try:
                ret = subprocess.run(
                    ["sudo", "arecord", "-D" + dev, "-r%d" % REC_RATE,
                     "-f", "S16_LE", "-c", "2", "-d", "1", "-q", "/tmp/mic_test.wav"],
                    timeout=5, capture_output=True)
                sz = os.path.getsize("/tmp/mic_test.wav") if os.path.exists("/tmp/mic_test.wav") else 0
                # 宽松判据: size > 1000 即视为成功 (arecord 可能 exit=1 但数据录到了)
                if sz > 1000:
                    logging.info("麦克风自测通过: %s (录制 %d bytes, exit=%d)", dev, sz, ret.returncode)
                    if dev != DEVICE_REC:
                        logging.info("切换录音设备: %s → %s", DEVICE_REC, dev)
                        globals()['DEVICE_REC'] = dev
                    test_ok = True
                    break
                else:
                    logging.warning("设备 %s 录音失败 (exit=%d, size=%d)", dev, ret.returncode, sz)
            except Exception as e:
                logging.warning("设备 %s 测试异常: %s", dev, e)

        if not test_ok:
            logging.error("所有麦克风设备测试失败！语音功能不可用")
            output_debug(0, "麦克风离线")

    # V5.0: SpeechManager \u542f\u52a8 (\u5355\u901a\u9053 worker)
    _sm.set_client(client)
    _sm.start()

    # T5: L0 \u786c\u8b66\u62a5\u7ebf\u7a0b (\u53c2\u8003 main2.py hard_alarm_worker)
    try:
        _alarm_thread = threading.Thread(target=hard_alarm_worker, args=(client,), daemon=True)
        _alarm_thread.start()
    except Exception as e:
        logging.error("L0 \u8b66\u62a5\u7ebf\u7a0b\u542f\u52a8\u5931\u8d25: %s", e)

    # V5.0: llm_reply / chat_reply watcher \u7ebf\u7a0b (\u5355\u901a\u9053\u6d88\u8d39,\u53d6\u4ee3\u4e3b\u5faa\u73af\u8f6e\u8be2)
    try:
        threading.Thread(target=_llm_reply_watcher, daemon=True).start()
        threading.Thread(target=_chat_reply_watcher, daemon=True).start()
        # V7.30 Phase 3: auto trigger watcher (fatigue_max / manual button)
        threading.Thread(target=_auto_trigger_watcher, daemon=True).start()
        # V7.3: mute \u53cc\u5411\u540c\u6b65 (UI \u89e3\u9664\u9759\u97f3 \u2192 voice_daemon \u540c\u6b65)
        threading.Thread(target=_mute_signal_watcher, daemon=True).start()
        threading.Thread(target=_replay_signal_watcher, daemon=True).start()
        # V7.31: 播报中喊唤醒词可立即打断当前 TTS。录制前默认关闭,
        # 避免后台短录音抢占 wake-only 的二段收音窗口。
        if ENABLE_BARGE_MUTE_WATCHER:
            threading.Thread(target=_barge_mute_watcher, args=(client,), daemon=True).start()
        else:
            logging.info(u"[BargeMute] watcher disabled")
        if ENABLE_BARGE_WAKE_WATCHER:
            threading.Thread(target=_barge_wake_watcher, args=(client,), daemon=True).start()
        else:
            logging.info(u"[BargeIn] watcher disabled by default")
        # V7.8: mic \u9632\u6b7b\u9501 watchdog
        threading.Thread(target=_mic_allowed_watchdog, daemon=True).start()
    except Exception as e:
        logging.error("reply watcher \u542f\u52a8\u5931\u8d25: %s", e)

    # \u5f00\u673a\u97f3\u6548 - \u8d70 SpeechManager PRIO_HARDCMD (\u7f3a\u7701)
    try:
        _enqueue_boot_prompt_once()
    except Exception as _e:
        _record_boot_prompt_status("failed", _e)
        logging.warning("\u6b22\u8fce\u8bcd\u5165\u961f\u5931\u8d25: %s", _e)

    global _violation_mtime
    # V4.9: \u57fa\u7ebf\u5bf9\u9f50\u542f\u52a8\u65f6\u523b,\u907f\u514d\u65e7 violation_alert.txt \u89e6\u53d1\u9996\u8f6e L0 \u8b66\u62a5
    try:
        if os.path.exists(VIOLATION_ALERT_FILE):
            _violation_mtime = os.path.getmtime(VIOLATION_ALERT_FILE)
    except Exception:
        pass

    # V6.0: \u4f1a\u8bdd\u6301\u4e45\u5316 (\u5bf9\u9f50\u540c\u5b66 main2.py is_woken_up \u6001)
    # \u5524\u9192\u4e00\u6b21\u540e\u8fdb\u5165 AWAKE \u6001, \u4ee5\u540e\u6bcf\u53e5\u8bdd\u90fd\u8d70 \u786c\u7f16\u7801 \u2192 LLM \u8def\u7531,
    # \u65e0\u9700\u91cd\u590d\u5589"\u6559\u7ec3". \u6ca1\u8bf4\u8bdd\u8d85\u8fc7 SESSION_IDLE_TIMEOUT \u79d2 \u2192 \u56de\u5230 SLEEP \u6001.
    _session_state = ["SLEEP"]   # mutable holder: "SLEEP" or "AWAKE"
    _session_last_activity = [0.0]
    SESSION_IDLE_TIMEOUT = int(os.environ.get("VOICE_SESSION_TIMEOUT", "60"))  # 60s idle \u56de\u7761

    def _is_gibberish(text):
        """V7.0 \u4e71\u8bc6\u522b\u6805\u680f: ASR \u8f93\u51fa\u660e\u663e\u662f\u566a\u58f0\u65f6\u4e0d\u53d1\u7ed9 DeepSeek
        \u9632\u6b62 DeepSeek \u201c\u5047\u88c5\u7406\u89e3\u201d\u56de\u7b54\u4e71\u8bc6\u5185\u5bb9 (\u5982\u201c\u5df2\u5207\u6362\u4e3a\u5173\u7cfb\u6a21\u5f0f\u201d)"""
        if not text:
            return True
        t = text.strip()
        if len(t) < 2:
            return True
        if len(t) > 25:  # \u5065\u8eab\u547d\u4ee4\u90fd \u2264 10 \u5b57, \u8d85\u8fc7 25 \u5b57\u57fa\u672c\u662f\u4e71\u8bc6\u522b\u7684\u957f\u65c1\u767d
            return True
        # \u91cd\u590d\u5b57\u7b26\u6bd4\u4f8b\u8fc7\u9ad8 (\u5982 "55555" / "\u554a\u554a\u554a")
        if len(set(t)) < max(2, len(t) * 0.3):
            return True
        # \u5168\u611f\u53f9\u8bcd/\u8bed\u6c14\u8bcd
        if all(c in u"\u55ef\u554a\u54e6\u5443\u54ce\u5440\u54c8\u6c89\u7684\u4e86\u5462\u5417" for c in t):
            return True
        return False

    def _route_text(text, display_text=None):
        """V7.1 意图分流路由 + M4 原文即时入右侧栏:
        - M4: 每次路由一进门先把 ASR 原文写入 chat_input.txt (带 [voice-handled])
              UI 立即显示用户气泡; 不再因 gibberish / A路miss / B路fail 而丢失
        - 静音态: 只响应解除静音, 其他全丢
        - 乱识别: 仍回 "没听清" (但文本已写入, 气泡已出)
        - A 路 (命令意图): 强制本地硬编码; 未命中 -> "没听清"(不走 LLM)
        - B 路 (闲聊): 走 DeepSeek, 只走一次, 不再双通道
        """
        if not text or len(text) < 2:
            return False
        display_text = (display_text or text).strip()
        _session_last_activity[0] = time.time()

        # M4: 一进门先写原文到右侧栏 (带 FSM 跳过标记)
        _publish_chat_input_raw(display_text)

        # 1) 静音态
        if _is_muted[0]:
            _try_voice_command(text=text)
            logging.info(u"静音中, 仅响应解除静音: %s", text[:40])
            return True

        # M5: 两句硬编码闲聊 (最高优先级, 压过所有过滤)
        #      命中则替换 chat_input 为 canonical 原文, 直连 DeepSeek
        _hc = _try_hardcode_chat(text)
        if _hc:
            logging.info(u"[M5] 硬编码命中, 用规范原文替换: %s -> %s", text[:30], _hc)

            def _async_hardcode_chat(canonical):
                try:
                    reply = _try_deepseek_chat(canonical)
                    if reply:
                        logging.info(u"[M5] DeepSeek 回复: %s", reply[:60])
                        _publish_chat_reply(reply)
                    else:
                        logging.info(u"[M5] DeepSeek 失败")
                        _speak_ack(u"网络有点慢")
                except Exception as _ee:
                    logging.error(u"[M5] DeepSeek 异常: %s", _ee)
                    _speak_ack(u"网络有点慢")

            threading.Thread(target=_async_hardcode_chat, args=(_hc,), daemon=True).start()
            return True

        # Coach self-introduction: fixed reply, no API wait.
        if is_capability_question(text):
            _reply = format_capability_reply(max_items=7)
            logging.info(u"[CoachKB] 功能介绍命中: %s", text[:40])
            _publish_chat_reply(_reply)
            return True

        # Usage-manual questions ("怎么切弯举/怎么推飞书") should be fast and
        # must not execute the command being asked about.
        if is_manual_question(text):
            _reply = format_manual_reply(text, max_hits=3)
            logging.info(u"[CoachKB] 使用手册命中: %s", text[:40])
            _publish_chat_reply(_reply)
            return True

        # 2) 乱识别栅栏 (文本已写气泡, 仅 TTS 回"没听清")
        if _is_gibberish(text):
            logging.info(u"[栅栏] 乱识别丢弃: %s", text[:40])
            _finish_unclear_turn("gibberish")
            return True

        if _try_daily_plan_voice_command(text):
            logging.info(u"[DailyPlan] 语音意图命中: %s", text[:40])
            return True

        # 3) A 路: 命令意图 -> 强制本地
        if _is_command_intent(text):
            logging.info(u"[A路] 命令意图: %s", text[:40])
            if _try_daily_plan_voice_command(text):
                return True
            if _try_combo_voice_command(text):
                return True
            if _try_voice_command(text):
                return True
            logging.info(u"[A路] 未命中硬编码, 拒绝走 LLM")
            _finish_unclear_turn("command_miss")
            return True

        # M7: B 路保守化 — 长度护栏 + M5 邻近拦截
        # 规则:
        #  (a) len<4 或 len>15 直接拒 LLM, 避免 ASR 幻觉走 DeepSeek
        #  (b) 与 M5 两个 canonical 的拼音编辑距离 ≤ 3 时, 强制回落 M5
        if not _m7_allow_b_route(text):
            logging.info(u"[M7] B 路保守化拦截: %s", text[:40])
            _finish_unclear_turn("b_route_block")
            return True

        # 4) B 路: 闲聊 -> DeepSeek 异步 (M4 已写 chat_input, 此处不再重复写)
        logging.info(u"[B路] 闲聊异步: %s", text[:40])

        def _async_deepseek(txt):
            _t0 = time.time()
            try:
                reply = _try_deepseek_chat(txt)
                if reply:
                    logging.info(u"\u5f02\u6b65\u95f2\u804a\u56de\u590d: %s", reply[:60])
                    _publish_chat_reply(reply)
                    # V4.8: \u5199 voice_sessions (\u5931\u8d25\u5403\u6389, \u4e0d\u5f71\u54cd\u56de\u590d\u94fe\u8def)
                    try:
                        _db = _get_db()
                        if _db is not None:
                            _db.log_voice_session(
                                trigger_src="chat",
                                transcript=txt,
                                response=reply,
                                duration_s=float(time.time() - _t0),
                                summary=None,
                            )
                    except Exception as _dbe:
                        logging.warning(u"voice_session log fail: %s", _dbe)
                else:
                    logging.info(u"\u5f02\u6b65\u95f2\u804a\u5931\u8d25")
                    _finish_unclear_turn("deepseek_empty")
            except Exception as _ee:
                logging.error(u"\u5f02\u6b65\u95f2\u804a\u5f02\u5e38: %s", _ee)
                _finish_unclear_turn("deepseek_exception")

        threading.Thread(target=_async_deepseek, args=(text,), daemon=True).start()
        # V7.7 \u4e3b\u5faa\u73af\u7acb\u5373\u8fd4\u56de \u2192 \u56de SLEEP \u2192 \u9ea6\u98ce\u53ef\u7acb\u5373\u91cd\u65b0\u5524\u9192
        return True

    try:
        threading.Thread(target=_manual_voice_watcher, args=(client, _route_text), daemon=True).start()
    except Exception as e:
        logging.error("manual voice watcher \u542f\u52a8\u5931\u8d25: %s", e)

    def _session_awaken():
        """\u8fdb\u5165\u5524\u9192\u6001: \u64ad"\u6211\u5728"\u3001\u521b\u5efa chat_active \u6807\u5fd7"""
        _session_state[0] = "AWAKE"
        _session_last_activity[0] = time.time()
        _speak_ack(u"\u6211\u5728", allow_interrupt=False)
        try:
            open("/dev/shm/chat_active", "w").close()
        except OSError:
            pass
        logging.info(u"[\u4f1a\u8bdd] \u2192 AWAKE \u6001 (idle_timeout=%ds)", SESSION_IDLE_TIMEOUT)

    def _session_sleep(reason="idle"):
        _session_state[0] = "SLEEP"
        try:
            if os.path.exists("/dev/shm/chat_active"):
                os.remove("/dev/shm/chat_active")
        except OSError:
            pass
        logging.info(u"[\u4f1a\u8bdd] \u2192 SLEEP (\u7406\u7531: %s)", reason)

    while True:
        # === \u76d1\u542c\u8fdd\u89c4\u8b66\u62a5 (L0 hard_alarm \u7ebf\u7a0b) ===
        try:
            if os.path.exists(VIOLATION_ALERT_FILE):
                ts = os.path.getmtime(VIOLATION_ALERT_FILE)
                if ts != _violation_mtime:
                    _violation_mtime = ts
                    with open(VIOLATION_ALERT_FILE, "r", encoding="utf-8") as f:
                        alert_text = f.read().strip()
                    if alert_text:
                        logging.info("\u8fdd\u89c4\u8b66\u62a5 \u2192 L0 \u7ebf\u7a0b: %s", alert_text)
                        _violation_text_latest[0] = alert_text
                        _violation_event.set()
        except Exception:
            pass

        # V5.0 PTT \u534a\u53cc\u5de5: \u58f0\u7b52\u6b63\u5728\u64ad\u65f6\u963b\u585e,\u907f\u514d VAD \u6536\u5230\u6f0f\u97f3
        _mic_allowed.wait()

        # V7.22 (2026-04-21): LLM API \u8c03\u7528\u671f\u95f4\u7981\u9ea6 \u2014 \u7528\u6237\u5728\u8fd9\u671f\u95f4\u8bf4\u4efb\u4f55\u8bdd\u90fd\u4e0d\u5e94\u88ab\u5f55
        # V7.24 (2026-04-21): \u64a4\u9500 V7.23 \u7684 voice_speaking \u6587\u4ef6\u4fe1\u53f7 \u2014 \u5b83\u65e0\u4eba\u6e05\u7406\u4f1a\u6b8b\u7559\u6b7b\u9501\u4e3b\u5faa\u73af
        # \u771f\u6b63\u7684 TTS \u671f\u95f4\u4e92\u65a5\u7531\u4e0a\u884c L1294 _mic_allowed.wait() \u5b8c\u6210 (\u8fdb\u7a0b\u5185 Event, \u8fdb\u7a0b\u5d29\u6e83\u81ea\u52a8\u91ca\u653e)
        if os.path.exists("/dev/shm/llm_inflight"):
            time.sleep(0.2)
            continue

        if _manual_voice_active.is_set():
            time.sleep(0.05)
            continue

        if ENABLE_BARGE_WAKE_WATCHER:
            _barge_text = _pop_barge_wake()
            if _barge_text:
                logging.info(u"[BargeIn] 接管主循环,进入下一轮监听: %s", _barge_text[:40])
                _start_turn(stage="wake")
                _dialog_enter()
                _publish_chat_input_raw(_barge_text)
                _speak_ack(u"\u55ef", allow_interrupt=False)
                _mic_allowed.wait()
                status2 = _primary_record_with_vad(
                    "barge_second", timeout=VAD_TIMEOUT, fast_start=True)
                try:
                    if status2 == "SUCCESS":
                        text2 = sound2text(client)
                        if text2 and len(text2) >= 2:
                            logging.info(u"[BargeIn] 用户打断后指令: %s", text2[:50])
                            _route_text(text2, display_text=text2)
                        else:
                            _finish_unclear_turn("barge_second_empty")
                            continue
                    else:
                        _finish_unclear_turn("barge_second_" + status2)
                        continue
                finally:
                    _kill_active_recording("barge_turn_done")
                    _wait_sm_idle(3.0)
                    _dialog_exit()
                continue

        # === V7.2 \u5355\u8f6e\u5bf9\u8bdd: \u5589\u4e00\u6b21\u6559\u7ec3 = \u4e00\u6b21\u56de\u7b54 = \u56de SLEEP ===
        output_debug(0, u"\u5f85\u673a\u4e2d..." if not _is_muted[0] else u"\u9759\u97f3\u5f85\u673a...")
        status = _primary_record_with_vad("wake_idle", timeout=WAKE_TIMEOUT, fast_start=False)
        if status != "SUCCESS":
            continue

        text = sound2text(client)
        if not text:
            if _manual_voice_active.is_set():
                logging.info(u"[ManualVoice] skip wake_asr_empty kill during manual recording")
            else:
                _kill_active_recording("wake_asr_empty")
            continue
        output_debug(0, text)
        if _is_boot_prompt_echo(text):
            logging.info(u"[BOOT_PROMPT] ignore ASR echo: %s", text[:40])
            if _manual_voice_active.is_set():
                logging.info(u"[ManualVoice] skip boot_prompt_echo kill during manual recording")
            else:
                _kill_active_recording("boot_prompt_echo")
            continue

        # V7.22 race guard: \u5f55\u97f3\u671f\u95f4 LLM \u542f\u52a8\u4e86 \u2192 \u6b7b\u4e22\u7ed3\u679c, \u4e0d\u8d70\u5524\u9192/\u8def\u7531
        if os.path.exists("/dev/shm/llm_inflight"):
            logging.info(u"[V7.22] \u4e22\u5f03 LLM API \u671f\u95f4\u7684\u8bef\u5f55: %s", text[:40])
            continue

        # V7.3 \u65e0\u9700\u5524\u9192\u7684\u7d27\u6025\u547d\u4ee4: "\u89e3\u9664\u9759\u97f3"\u7c7b\u76f4\u63a5\u89e6\u53d1
        # \u8fd9\u662f\u552f\u4e00\u65e0\u9700\u5589"\u6559\u7ec3"\u7684\u672c\u5730\u547d\u4ee4
        _FORCE_UNMUTE_WORDS = (
            u"\u89e3\u9664\u9759\u97f3", u"\u53d6\u6d88\u9759\u97f3", u"\u5173\u95ed\u9759\u97f3",
            u"\u6062\u590d\u5bf9\u8bdd", u"\u6062\u590d\u8bf4\u8bdd", u"\u6253\u5f00\u58f0\u97f3",
            u"\u5f00\u58f0", u"\u6062\u590d\u58f0\u97f3", u"\u522b\u88c5\u6b7b",
        )
        if _is_muted[0] and any(w in text for w in _FORCE_UNMUTE_WORDS):
            logging.info(u"[\u7d27\u6025\u89e3\u9664] \u65e0\u9700\u5524\u9192\u76f4\u63a5\u89e3\u9664\u9759\u97f3: %s", text[:40])
            _start_turn(stage="user_input")
            _publish_chat_input_raw(text)
            _is_muted[0] = False
            _write_signal("/dev/shm/mute_signal.json", {"muted": False, "ts": time.time()})
            _apply_playback_volume(muted=False, reason="force_unmute")
            _publish_coach_ack(u"\u597d\u7684\uff0c\u5df2\u89e3\u9664\u9759\u97f3", kind="unmute", allow_interrupt=False)
            _close_turn()
            continue

        # M1: \u62fc\u97f3+\u7f16\u8f91\u8ddd\u79bb\u5bbd\u677e\u5524\u9192 (\u539f: \u4e25\u683c\u5b57\u4e32\u5305\u542b)
        is_wake, _stripped = _is_wake_word(text)
        if not is_wake:
            logging.info(u"\u975e\u5524\u9192\u8bed\u53e5 (SLEEP),\u5ffd\u7565: %s", text[:40])
            continue
        logging.info(u"[\u5524\u9192] \u547d\u4e2d: %s | \u53bb\u5524\u9192\u540e: %s", text[:30], _stripped[:40])

        # M3: 进入对话状态, 屏蔽所有警报 (含不标准/代偿)
        # V7.30: open new turn id for UI bubble dedupe (S1 fix)
        _start_turn(stage="wake")
        _dialog_enter()

        # \u6709\u6307\u4ee4 \u2192 \u76f4\u63a5\u8def\u7531\u4e00\u6b21 \u2192 \u56de SLEEP
        if _stripped and len(_stripped) >= 2:
            try:
                _route_text(_stripped, display_text=text)
                logging.info(u"[\u5355\u8f6e] \u5524\u9192+\u6307\u4ee4\u5904\u7406\u5b8c, \u56de SLEEP")
            finally:
                # M11 (V7.17): 单轮顽疾修复 - 立即掐断录音 + 快速回 SLEEP
                #   - killall -9 arecord 保证无残留录音 (防 "长对话" 错觉)
                #   - _wait_sm_idle 8.0s -> 3.0s (够短硬编码回复播完)
                #   - 下一轮必须重新喊"教练"才会响应
                _kill_active_recording("wake_inline_done")
                _wait_sm_idle(3.0)
                _dialog_exit()
                logging.info(u"[\u5355\u8f6e M11] \u5f55\u97f3\u5df2\u6389\uff0c\u5fc5\u987b\u91cd\u65b0\u5589\u6559\u7ec3")
            continue

        # V7.3: \u7f29\u77ed ACK \u964d\u4f4e\u5ef6\u8fdf \u2014 "\u55ef" (0.3s TTS) \u66ff\u4ee3 "\u6211\u5728" (1.5s TTS)
        _publish_chat_input_raw(text)
        _speak_ack(u"\u55ef", allow_interrupt=False)
        try:
            open("/dev/shm/chat_active", "w").close()
        except OSError:
            pass
        _mic_allowed.wait()  # \u7b49 "\u6211\u5728" \u64ad\u5b8c
        logging.info(u"[WakeOnly] wake_ack_done")
        logging.info(u"[WakeOnly] second_listen_open")
        logging.info(u"[WakeOnly] second_record_start timeout=%ds", VAD_TIMEOUT)
        # M2: 唤醒后的二次录音用 fast_start (空白期 1.5s -> 0.2s)
        status2 = _primary_record_with_vad(
            "wake_second", timeout=VAD_TIMEOUT, fast_start=True)
        logging.info(u"[WakeOnly] second_record_done status=%s", status2)
        if status2 == "SUCCESS":
            text2 = sound2text(client)
            if text2 and len(text2) >= 2:
                # V7.3: \u6307\u4ee4\u9636\u6bb5\u518d\u542b\u5524\u9192\u8bcd \u2192 \u91cd\u65b0\u5524\u9192 (\u5265\u79bb\u540e\u91cd\u8def\u7531)
                _has_wake2 = any(w in text2 for w in WAKE_WORDS)
                if _has_wake2:
                    _stripped2 = text2
                    for w in WAKE_WORDS:
                        _stripped2 = _stripped2.replace(w, "").strip(u" ,\u3002\uff0c.!\uff01\uff1f?")
                    if _stripped2 and len(_stripped2) >= 2:
                        logging.info(u"[\u5355\u8f6e] \u6307\u4ee4\u5185\u542b\u5524\u9192\u8bcd, \u5265\u79bb\u540e\u8def\u7531: %s", _stripped2[:40])
                        _route_text(_stripped2, display_text=text2)
                    else:
                        logging.info(u"[\u5355\u8f6e] \u6307\u4ee4\u53ea\u6709\u5524\u9192\u8bcd, \u89c6\u4e3a\u91cd\u5524\u9192(\u5ffd\u7565)")
                else:
                    logging.info(u"[\u5355\u8f6e] \u7528\u6237\u6307\u4ee4: %s", text2[:50])
                    _route_text(text2, display_text=text2)
            else:
                logging.info(u"[WakeOnly] second_asr_empty")
                _finish_unclear_turn("second_asr_empty")
                continue
        else:
            logging.info(u"[WakeOnly] second_no_speech_idle status=%s", status2)
            _finish_quiet_turn("second_no_speech_idle")
            continue
        try:
            os.remove("/dev/shm/chat_active")
        except OSError:
            pass
        # M11 (V7.17): 单轮顽疾修复 - 同上, 强制掐断录音 + 快速回 SLEEP
        _kill_active_recording("two_step_done")
        _wait_sm_idle(3.0)
        _dialog_exit()
        logging.info(u"[\u5355\u8f6e M11] \u5b8c\u6210, \u5f55\u97f3\u5df2\u6389\uff0c\u5fc5\u987b\u91cd\u65b0\u5589\u6559\u7ec3")


def hard_alarm_worker(client):
    # type: (object) -> None
    """T5 · L0 级硬警报独立线程 (参考 main2.py hard_alarm_worker)

    主循环检测到 violation_alert.txt 更新 → 设置 _violation_event + 填 latest text
    本线程响应 Event: SIGKILL 任何正在播的 aplay → 写 voice_interrupt → 播放警报
    - V4.6: 移除静音尊重 (对齐 main2.py，违规警报无视静音强制播报)
    - allow_interrupt=False (警报播放中不被次级请求打断)
    - 守护线程 (daemon=True)，主进程退出时自动终止
    """
    logging.info("[hard_alarm] L0 \u8b66\u62a5\u7ebf\u7a0b\u542f\u52a8")
    while True:
        if _violation_event.wait(timeout=0.5):
            _violation_event.clear()
            text = _violation_text_latest[0]
            if not text:
                continue

            # V7.8 + M3: 对话/播报中屏蔽警报
            # 规则: SpeechManager 在播 | _dialog_active 对话中 | chat_active 文件存在 | 静音态 -> 丢弃
            _skip = False
            _skip_reason = ""
            try:
                if _dialog_active_safe():
                    _skip = True
                    _skip_reason = "M3_dialog_active"
                elif _sm._current_prio[0] < 99:
                    _skip = True
                    _skip_reason = "SM_playing"
                elif os.path.exists("/dev/shm/chat_active"):
                    _skip = True
                    _skip_reason = "chat_active_file"
                elif _is_muted[0]:
                    _skip = True
                    _skip_reason = "muted"
            except Exception:
                pass
            if _skip:
                logging.info(u"[hard_alarm] 屏蔽警报 (%s): %s", _skip_reason, text)
                continue

            logging.info("[hard_alarm] \u89e6\u53d1 L0 \u8b66\u62a5 (PRIO_ALARM): %s", text)
            try:
                _sm.enqueue(PRIO_ALARM, text, allow_interrupt=False)
            except Exception as e:
                logging.error("[hard_alarm] enqueue \u5931\u8d25: %s", e)


def _deliver_to_fsm(text):
    """将用户语音文字投递到 FSM 的 chat_input 信号文件"""
    try:
        tmp = CHAT_INPUT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.rename(tmp, CHAT_INPUT_FILE)
        logging.info("投递到 FSM: %s", text)
    except Exception as e:
        logging.error("投递失败: %s", e)


# ===== M5 (V7.13, 2026-04-20): 两句硬编码闲聊直通 =====
# 用户需求: 只会问两个问题, ASR 识别到"近音"就强制显示硬编码原文 + 调 DeepSeek
# 设计: 关键词集合 OR 拼音片段任一命中 -> 返回规范化原文, 绕过 A/B 路
HARDCODE_CHATS = [
    {
        "canonical": u"请问现在几点了",
        "keywords": (u"几点", u"现在几点", u"点钟", u"点了", u"时间", u"几点了"),
        "pinyin_pats": (u"jidian", u"jidianl", u"jidian了", u"shijian", u"xianzaiji", u"dianle", u"dianzhong"),
    },
    {
        "canonical": u"我的膝盖有疼痛感怎么办",
        "keywords": (u"膝盖", u"膝", u"疼", u"痛", u"酸", u"怎么办"),
        "pinyin_pats": (u"xigai", u"xigaiteng", u"xigaitong", u"tengtong", u"tongtong",
                        u"xitong", u"xiteng", u"suantong", u"zenmeban"),
    },
]


def _m7_allow_b_route(text):
    # type: (str) -> bool
    """M7 (V7.13, 2026-04-20): 判断文本是否允许走 B 路(DeepSeek 闲聊).
    过严的长度护栏 + 与 M5 canonical 的拼音距离检查.
    防止 ASR 错听的短促闲聊输入被 LLM 自由发挥("假装理解"返回).
    """
    if not text:
        return False
    L = len(text)
    if L < 4:
        return False  # 3 字内几乎肯定是错听
    if L > 15:
        return False  # 健身场景正常闲聊 ≤ 15 字
    # 拼音邻近 M5 -> 让 M5 来处理, 不要在 B 路发散
    if _PINYIN_AVAILABLE:
        try:
            py = u"".join(_lazy_pinyin(text))
            for entry in HARDCODE_CHATS:
                cpy = u"".join(_lazy_pinyin(entry["canonical"]))
                # 子串即算相似, 或编辑距离小于 4
                if cpy in py or py in cpy:
                    return False
                # 对短文本做全量距离, 长文本做滑窗
                if min(len(py), len(cpy)) <= 8 and _edit_distance(py, cpy) <= 3:
                    return False
        except Exception:
            pass
    return True


def _try_hardcode_chat(text):
    # type: (str) -> str
    """M5: 返回命中的 canonical 原文, 未命中返回空串.
    匹配规则: 关键词任一 ∈ text OR 拼音片段任一 ∈ pinyin(text).
    """
    if not text:
        return u""
    try:
        py = u"".join(_lazy_pinyin(text)) if _PINYIN_AVAILABLE else text.lower()
    except Exception:
        py = text.lower()
    for entry in HARDCODE_CHATS:
        # 关键词直接匹配
        if any(kw in text for kw in entry["keywords"]):
            logging.info(u"[M5_hardcode] 关键词命中 -> %s", entry["canonical"])
            return entry["canonical"]
        # 拼音片段匹配
        for pat in entry["pinyin_pats"]:
            # 拼音片段里的中文字符也要先转拼音
            pat_py = u"".join(_lazy_pinyin(pat)) if _PINYIN_AVAILABLE and any(u'\u4e00' <= c <= u'\u9fff' for c in pat) else pat
            if pat_py in py:
                logging.info(u"[M5_hardcode] 拼音片段 '%s' in '%s' -> %s",
                             pat_py, py[:30], entry["canonical"])
                return entry["canonical"]
    return u""


def _publish_chat_input_raw(text):
    # type: (str) -> None
    """M4 (V7.13, 2026-04-20): 把 ASR 原文写入 chat_input.txt (带 [voice-handled] 标记).
    标记会被 streamer /api/chat_input 剥离, 被 FSM main_claw_loop 识别为"跳过"避免双 DeepSeek.
    UI 轮询 chat_input 立即显示用户气泡 -- 即便后续被 gibberish/A路/B路失败拦下, 也能看到原话."""
    if not text:
        return
    try:
        payload = text + u"\n[voice-handled]"
        tmp = CHAT_INPUT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.rename(tmp, CHAT_INPUT_FILE)
        logging.info(u"[M4] chat_input 原文已写入: %s", text[:40])
    except Exception as e:
        logging.debug(u"[M4] 写 chat_input 失败: %s", e)
    _publish_user_event(text)
    # V7.30: also publish to voice_turn.json so UI dedupes by turn_id (S1)
    _emit_turn_stage("user_input", text=text)


def _publish_chat_reply(reply):
    # type: (str) -> None
    """V5.0 \u5355\u901a\u9053\u6743\u5a01: \u539f\u5b50\u5199 /dev/shm/chat_reply.txt + \u81ea\u589e seq.
    watcher \u7ebf\u7a0b\u901a\u8fc7 seq \u6bd4\u8f83\u53d6\u4ee3 mtime \u6bd4\u8f83, \u6839\u6cbb\u540c\u79d2\u591a\u5199\u6f0f\u8bfb\u3002
    \u4e0d\u5728\u6b64\u5904\u76f4\u63a5 speak, \u4ea4 watcher \u4e00\u7edf\u6295 SpeechManager PRIO_LLM \u3002
    """
    reply = _sanitize_coach_text(reply)
    path = "/dev/shm/chat_reply.txt"
    try:
        seq = _bump_reply_seq("chat")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(reply)
        os.rename(tmp, path)
        # \u540c\u65f6\u5199 \u4f34\u968f seq \u6587\u4ef6,watcher \u4f18\u5148\u770b seq; mtime \u4f5c\u4e3a\u6b21\u7ea7\u53c2\u8003
        with open(path + ".seq", "w") as sf:
            sf.write(str(seq))
        _ts = time.time()
        os.utime(path, (_ts, _ts))
    except Exception as e:
        logging.error("_publish_chat_reply \u5931\u8d25: %s", e)
    _publish_coach_event(reply, kind="llm_reply")
    # V7.30: emit assistant_reply stage so UI knows reply belongs to current turn (S1)
    _emit_turn_stage("assistant_reply", text=reply)


# V5.0 seq \u8ba1\u6570\u5668 (\u8fdb\u7a0b\u5185\u5b58, \u542f\u52a8\u4ece 0; FSM \u7aef\u4e0d\u5fc5\u540c\u6b65,watcher \u53ef\u517c\u5bb9\u6587\u4ef6 seq \u548c mtime \u53cc\u4fe1\u53f7)
_reply_seq_map = {"llm": 0, "chat": 0}
_reply_seq_lock = threading.Lock()

def _bump_reply_seq(kind):
    # type: (str) -> int
    with _reply_seq_lock:
        _reply_seq_map[kind] = _reply_seq_map.get(kind, 0) + 1
        return _reply_seq_map[kind]


def _read_seq_file(path):
    # type: (str) -> int
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return int(f.read().strip() or "0")
    except Exception:
        pass
    return 0


def _dedup_ok(cache, text, ttl=30.0):
    # type: (list, str, float) -> bool
    """V6.2 \u9632\u91cd\u590d\u64ad\u62a5: \u540c\u4e00\u5185\u5bb9 ttl \u79d2\u5185\u53ea\u5141\u8bb8\u64ad\u4e00\u6b21.
    cache = [{"text":..., "ts":...}] (mutable single-element list)."""
    now = time.time()
    if cache and cache[0].get("text") == text and now - cache[0].get("ts", 0) < ttl:
        return False
    cache[:] = [{"text": text, "ts": now}]
    return True


def _mic_allowed_watchdog():
    """V7.26 (2026-04-21) \u9632\u6b7b\u9501 (\u5168\u91cd\u8bbe):

    \u5386\u53f2\u6559\u8bad:
      - V7.8  \u539f\u59cb 15s \u5151\u5e95
      - V7.24 15s \u2192 3s  \u2192 \u8bef\u6740\u6b63\u5e38\u957f\u53e5 TTS
      - V7.25 3s  \u2192 6s  \u2192 LLM \u957f\u56de\u590d (~8s) \u4ecd\u4f1a\u8e29\u96f7
                           \u2192 watchdog \u63d0\u524d\u91ca\u653e \u2192 \u9ea6\u514b\u98ce\u5f55\u5165 aplay \u6269\u58f0\u5668\u56de\u58f0
                           \u2192 baseline \u6c61\u67d3\u4e3a 662 \u2192 threshold 702 \u2192 \u7528\u6237\u559a "\u6559\u7ec3" rms \u2248 650 \u8fbe\u4e0d\u5230 \u2192 \u5361 30s

    V7.26 \u6839\u6cbb\u4e09\u7bc7:
      (B) \u667a\u80fd\u5224\u5b9a: aplay \u8fd8\u5728\u6b63\u5e38\u8dd1 \u2192 \u4e0d\u7b97\u5361\u6b7b, \u4e0d\u8ba1\u65f6
      (A) \u5f3a\u5236\u91ca\u653e\u524d: kill aplay \u6d88\u9664\u56de\u58f0\u6e90 + \u4f5c\u5e9f VAD baseline \u7f13\u5b58
      \u77e9: \u5151\u5e95\u9608\u503c\u56de\u8c03\u5230 12s (\u8986\u76d6 \u2248 99% LLM \u957f\u53e5 TTS; \u8fd9\u79cd\u6781\u7aef\u60c5\u51b5\u4e0b\u624d\u5151\u5e95)
    """
    _blocked_since = [0.0]
    while True:
        time.sleep(1.0)
        try:
            if _mic_allowed.is_set():
                _blocked_since[0] = 0
                continue

            # V7.26 (B): aplay \u5728\u8dd1\u5c31\u4e0d\u7b97\u5361\u6b7b \u2014 SpeechManager \u6b63\u5728\u64ad\u62a5\u662f\u5408\u7406\u963b\u585e
            try:
                _p = _sm._play_proc[0]
                if _p is not None and _p.poll() is None:
                    # aplay \u8fd8\u6d3b, \u91cd\u7f6e\u8ba1\u65f6\u5668 \u2014 \u7b49\u5b83\u6b63\u5e38\u7ed3\u675f
                    _blocked_since[0] = 0
                    continue
            except Exception:
                pass

            if _blocked_since[0] == 0:
                _blocked_since[0] = time.time()
                continue

            # \u5151\u5e95\u9608\u503c 12s \u2014 aplay \u5df2\u6b7b\u4f46 _mic_allowed \u4ecd clear \u8d85\u8fc7 12s, \u771f\u6b63\u5361\u6b7b
            if time.time() - _blocked_since[0] > 12.0:
                logging.warning(u"[mic_watchdog] \u9ea6\u98ce\u95e8\u6301\u7eed\u963b\u585e >12s \u4e14 aplay \u5df2\u6b7b, \u5f3a\u5236\u91ca\u653e")
                # V7.26 (A): kill \u6b8b\u7559 aplay (\u9632\u4e07\u4e00) + \u4f5c\u5e9f baseline \u7f13\u5b58
                try:
                    subprocess.run(["killall", "-9", "aplay"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
                except Exception:
                    pass
                try:
                    _VAD_BASELINE_CACHE["ts"] = 0.0  # \u4e0b\u6b21 record_with_vad \u5fc5\u91cd\u91c7 baseline
                    _VAD_BASELINE_CACHE["baseline"] = 0.0
                    logging.info(u"[mic_watchdog] VAD baseline \u7f13\u5b58\u5df2\u4f5c\u5e9f, \u9632\u6269\u58f0\u5668\u56de\u58f0\u6c61\u67d3")
                except Exception:
                    pass
                try:
                    _sm._play_proc[0] = None
                    _sm._current_prio[0] = 99
                    with _sm._lock:
                        for p in _sm._buckets:
                            _sm._buckets[p] = []
                except Exception:
                    pass
                _mic_allowed.set()
                _blocked_since[0] = 0
        except Exception:
            pass


def _mute_signal_watcher():
    """V7.3: \u8f6e\u8be2 /dev/shm/mute_signal.json, \u540c\u6b65 _is_muted[0]\u3002
    UI \u70b9\u89e3\u9664\u9759\u97f3\u6309\u94ae \u2192 streamer_app \u5199 mute_signal.json \u2192 \u672c watcher \u66f4\u65b0 _is_muted\u3002
    \u89e3\u51b3: UI \u89e3\u9664\u9759\u97f3\u540e\u5589\u6559\u7ec3\u65e0\u54cd\u5e94\u7684 bug\u3002"""
    path = "/dev/shm/mute_signal.json"
    last_ts = 0.0
    logging.info(u"[MuteWatcher] \u542f\u52a8, \u8f6e\u8be2 mute_signal.json")
    while True:
        time.sleep(0.3)
        try:
            if not os.path.exists(path):
                continue
            mt = os.path.getmtime(path)
            if mt == last_ts:
                continue
            last_ts = mt
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            new_mute = bool(d.get("muted", False))
            if new_mute != _is_muted[0]:
                _is_muted[0] = new_mute
                logging.info(u"[MuteWatcher] \u5916\u90e8\u540c\u6b65: _is_muted=%s", new_mute)
                if new_mute:
                    _sm.interrupt_current(reason="external_mute", clear_pending=True)
                    _kill_active_recording("external_mute")
                    try:
                        subprocess.run(["killall", "-9", "aplay"],
                                       stderr=subprocess.DEVNULL, timeout=1)
                    except Exception:
                        pass
                    _apply_playback_volume(muted=True, reason="external_mute")
                else:
                    _apply_playback_volume(muted=False, reason="external_unmute")
                    _mic_allowed.set()
                    _replay_last_tts_payload(reason="external_unmute")
            elif (not new_mute) and d.get("replay"):
                logging.info(u"[MuteWatcher] 外部请求补播最近 TTS")
                _apply_playback_volume(muted=False, reason="external_unmute_replay")
                _mic_allowed.set()
                _replay_last_tts_payload(reason="external_unmute")
        except Exception as e:
            logging.debug(u"[MuteWatcher] %s", e)


def _replay_signal_watcher():
    # type: () -> None
    """Poll explicit UI replay requests after unmute."""
    path = REPLAY_LAST_TTS_FILE
    last_ts = 0.0
    logging.info(u"[MuteReplay] 启动, 轮询 replay_last_tts.json")
    while True:
        time.sleep(0.3)
        try:
            if not os.path.exists(path):
                continue
            mt = os.path.getmtime(path)
            if mt == last_ts:
                continue
            last_ts = mt
            if _is_muted[0]:
                logging.info(u"[MuteReplay] replay signal ignored while muted")
                continue
            _apply_playback_volume(muted=False, reason="replay_signal")
            _mic_allowed.set()
            _replay_last_tts_payload(reason="replay_signal")
        except Exception as e:
            logging.debug(u"[MuteReplay] watcher failed: %s", e)


def _llm_reply_watcher():
    """V6.2 \u76d1\u542c /dev/shm/llm_reply.txt.
    - \u53cc\u4fe1\u53f7: seq > 0 \u8d70 seq \u6bd4\u8f83; seq == 0 \u964d\u7ea7 mtime
    - \u9632\u91cd\u590d: \u540c\u4e00\u5185\u5bb9 30s \u5185\u53ea\u64ad\u4e00\u904d
    - \u6355\u83b7\u5230 \u2192 enqueue PRIO_LLM
    """
    path = "/dev/shm/llm_reply.txt"
    seq_path = path + ".seq"
    last_seq = _read_seq_file(seq_path)
    last_mtime = os.path.getmtime(path) if os.path.exists(path) else 0.0
    _dedup_cache = [{}]
    logging.info(u"[LLM_Watcher] \u542f\u52a8, \u57fa\u7ebf seq=%d mtime=%.2f", last_seq, last_mtime)
    while True:
        time.sleep(0.2)
        try:
            if not os.path.exists(path):
                continue
            cur_seq = _read_seq_file(seq_path)
            cur_mtime = os.path.getmtime(path)
            changed = False
            if cur_seq > 0 and cur_seq != last_seq:
                last_seq = cur_seq
                changed = True
            elif cur_seq == 0 and cur_mtime != last_mtime:
                last_mtime = cur_mtime
                changed = True
            if not changed:
                continue
            with open(path, "r", encoding="utf-8") as f:
                txt = f.read().strip()
            if txt and _dedup_ok(_dedup_cache, txt):
                _publish_coach_event(txt, kind="auto_summary")
                # V7.23: \u9884\u5360\u9ea6\u514b\u98ce\u95e8, \u5173\u95ed watcher\u2192SpeechManager \u4e4b\u95f4\u7684\u7ade\u6001\u7a97\u53e3
                # \u6839\u6cbb\u8bed\u97f3\u957f\u5f55\u97f3 bug: \u4e0d\u7b49 SpeechManager \u5408\u6210\u5b8c TTS, \u7acb\u5373\u963b\u585e\u4e3b\u5faa\u73af L1294 _mic_allowed.wait()
                # V7.24: \u64a4\u6389 /dev/shm/voice_speaking \u6587\u4ef6 touch \u2014 SpeechManager \u4e0d\u8d70 play_audio, \u4fe1\u53f7\u65e0\u4eba\u6e05\u7406\u4f1a\u6b8b\u7559\u6b7b\u9501\u4e3b\u5faa\u73af
                _mic_allowed.clear()
                logging.info(u"[LLM_Watcher] \u65b0 llm_reply (len=%d): %s", len(txt), txt[:60])
                _speak_llm(txt)
                # V7.31: auto-trigger may have put the daemon in BUSY and
                # SIGSTOP'd arecord. Release after the LLM TTS queue drains,
                # with a bounded timeout, so voice can never stay frozen.
                try:
                    _schedule_auto_busy_release(_auto_busy_token[0],
                                                "llm_reply_done",
                                                delay_sec=0.2,
                                                wait_speech=True)
                except Exception as _release_e:
                    logging.warning(u"[AutoTrigger] release schedule failed: %s", _release_e)
                # V7.21 (2026-04-21): \u75b2\u52b3/\u624b\u52a8\u89e6\u53d1 LLM \u64ad\u62a5\u540e, \u5fc5\u987b\u6389\u65ad SLEEP \u6001\u4e0b
                # \u6b63\u5728\u8d77\u98de\u7684 record_with_vad, \u5426\u5219 TTS \u6cc4\u9732\u4f1a\u62c9\u957f VAD "\u5076\u9047\u5f55\u97f3"\u5e7b\u89c9.
                # \u5bf9\u9f50 M11 wake \u8def\u5f84\u7684\u5904\u7406, \u4fdd\u8bc1\u64ad\u5b8c\u81ea\u52a8\u56de SLEEP \u7b49"\u6559\u7ec3".
                try:
                    subprocess.run(["killall", "-9", "arecord"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
            elif txt:
                logging.info(u"[LLM_Watcher] \u91cd\u590d\u5185\u5bb9\u5e72\u901f\u4e22\u5f03: %s", txt[:40])
        except Exception as e:
            logging.debug(u"[LLM_Watcher] %s", e)


def _auto_trigger_watcher():
    """V7.30 Phase 3: watch /dev/shm/auto_trigger.json \u2014 fatigue/manual auto trigger.

    FSM writes auto_trigger.json before kicking off LLM summary. We:
      1. Transition state machine LISTEN\u2192BUSY (suspends record_with_vad)
      2. Open a new auto-Turn for UI dedupe (different from a wake turn)
      3. Suspend arecord so the upcoming TTS doesn't bleed into the mic
    The actual TTS playback is handled by _llm_reply_watcher when llm_reply.txt
    arrives. After that watcher resets _mic_allowed, this state will naturally
    transition back to LISTEN on the next dialog cycle.
    """
    path = "/dev/shm/auto_trigger.json"
    last_ts = 0.0
    logging.info(u"[AutoTrigger] watcher up")
    while True:
        time.sleep(0.5)
        try:
            if not os.path.exists(path):
                continue
            mtime = os.path.getmtime(path)
            if mtime == last_ts:
                continue
            last_ts = mtime
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            reason = payload.get("reason", "unknown")
            logging.info(u"[AutoTrigger] fired reason=%s good=%s failed=%s comp=%s",
                         reason, payload.get("good"), payload.get("failed"),
                         payload.get("comp"))
            _enter_auto_busy(reason, payload)
            try:
                os.remove(path)
            except OSError:
                pass
        except (OSError, ValueError) as e:
            logging.debug(u"[AutoTrigger] %s", e)


def _chat_reply_watcher():
    """V6.2 \u76d1\u542c /dev/shm/chat_reply.txt. \u903b\u8f91\u540c _llm_reply_watcher."""
    path = "/dev/shm/chat_reply.txt"
    seq_path = path + ".seq"
    last_seq = _read_seq_file(seq_path)
    last_mtime = os.path.getmtime(path) if os.path.exists(path) else 0.0
    _dedup_cache = [{}]
    logging.info(u"[Chat_Watcher] \u542f\u52a8, \u57fa\u7ebf seq=%d mtime=%.2f", last_seq, last_mtime)
    while True:
        time.sleep(0.2)
        try:
            if not os.path.exists(path):
                continue
            cur_seq = _read_seq_file(seq_path)
            cur_mtime = os.path.getmtime(path)
            changed = False
            if cur_seq > 0 and cur_seq != last_seq:
                last_seq = cur_seq
                changed = True
            elif cur_seq == 0 and cur_mtime != last_mtime:
                last_mtime = cur_mtime
                changed = True
            if not changed:
                continue
            with open(path, "r", encoding="utf-8") as f:
                txt = f.read().strip()
            if txt and _dedup_ok(_dedup_cache, txt):
                logging.info(u"[Chat_Watcher] \u65b0 chat_reply (len=%d): %s", len(txt), txt[:60])
                _mic_allowed.clear()
                _kill_active_recording("chat_reply_tts")
                _speak_llm(txt)
            elif txt:
                logging.info(u"[Chat_Watcher] \u91cd\u590d\u5185\u5bb9\u5e72\u901f\u4e22\u5f03: %s", txt[:40])
        except Exception as e:
            logging.debug(u"[Chat_Watcher] %s", e)


def _try_deepseek_chat(text):
    """V4.8 闲聊 fallback: 如果用户说的不是命令,直接调 DeepSeek 问答,保持 3 句话内.
    M13 (V7.21, 2026-04-20): env 缺失时从 .api_config.json 热加载 (与 BAIDU 热加载对齐),
    避免 voice_daemon 不经 start_voice_with_env.sh 启动时 DeepSeek 总是"网络有点慢"."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        try:
            _cfg_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                ".api_config.json")
            if os.path.exists(_cfg_path):
                with open(_cfg_path, "r") as _cf:
                    _cfg = json.load(_cf)
                api_key = (_cfg.get("DEEPSEEK_API_KEY") or _cfg.get("deepseek_api_key") or "").strip()
                if api_key:
                    os.environ["DEEPSEEK_API_KEY"] = api_key  # 缓存到 env, 避免每次调用重读磁盘
                    logging.info(u"[M13] DeepSeek API key 从 .api_config.json 热加载成功")
        except Exception as _hke:
            logging.debug(u"[M13] 热加载 DeepSeek API key 失败: %s", _hke)
    if not api_key or len(text) < 3:
        return None
    try:
        import urllib.request as _ur
        import time as _t
        # Build date/time context so 'now几点' can be answered
        # V7.8 \u677f\u7aef\u65f6\u533a UTC, \u624b\u52a8 +8h \u8f6c\u5317\u4eac\u65f6\u95f4 (sudoers \u9650\u5236\u65e0\u6cd5\u6539\u7cfb\u7edf\u65f6\u533a)
        _cn_ts = _t.time() + 8 * 3600
        _now = _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime(_cn_ts))
        _weekday = ["周一","周二","周三","周四","周五","周六","周日"][_t.gmtime(_cn_ts).tm_wday]
        # Read recent fitness stats if available
        _stats_ctx = ""
        try:
            with open("/dev/shm/fsm_state.json") as _f:
                _fsm = json.load(_f)
                _stats_ctx = u"(训练实况: 当前%s,标准%d次,违规%d次,疲劳%d)" % (
                    _fsm.get("exercise","squat"),
                    _fsm.get("good",0),
                    _fsm.get("failed",0),
                    int(_fsm.get("fatigue",0)))
        except Exception:
            pass
        # V4.8: \u52a8\u6001 system_prompt \u2014 \u4ece system_prompt_versions \u8bfb\u53d6\u6700\u65b0 active \u7248\u672c
        # \u5931\u8d25 (\u65e0\u5e93 / \u65e0 active / \u5f02\u5e38) \u8fd4\u56de fallback, \u4fdd\u8bc1\u95f2\u804a\u4e0d\u65ad\u3002
        _base_prompt = u"\u4f60\u662f IronBuddy \u5065\u8eab\u6559\u7ec3\u3002"
        try:
            _db_ref = _get_db()
            if _db_ref is not None:
                _base_prompt = _db_ref.get_active_system_prompt(
                    fallback=_base_prompt)
        except Exception as _pe:
            logging.debug(u"[V4.8] get_active_system_prompt failed: %s", _pe)
        system_prompt = (
            _base_prompt
            + u" \u5f53\u524d\u65f6\u95f4:" + _now + u" " + _weekday + u"\u3002"
            + _stats_ctx
            + u" \u56de\u7b54\u52a1\u5fc5\u7b80\u77ed:3 \u53e5\u8bdd\u4ee5\u5185,80 \u5b57\u4ee5\u5185,\u4e0d\u4f7f\u7528 markdown\u3002"
        )
        try:
            _rag_ctx = u""
            if adp_knowledge is not None:
                _online = adp_knowledge.search_adp_knowledge(text, limit=3)
                _rag_ctx = (_online or {}).get("context") or u""
            elif online_knowledge is not None:
                _online = online_knowledge.search_online_knowledge(text, limit=3)
                _rag_ctx = (_online or {}).get("context") or u""
            if _rag_ctx:
                system_prompt += u" 优先参考以下 ADP 专业知识库内容，回答要先给结论，并说明这是训练建议不是医疗诊断：\n" + _rag_ctx
                logging.info(u"[ADP] context injected len=%d", len(_rag_ctx))
            else:
                _manual_ctx = build_rag_context(text, limit=3, max_chars=300)
                if _manual_ctx:
                    system_prompt += u" 如问题涉及系统操作，只参考以下本地规则/手册：\n" + _manual_ctx
        except Exception as _rag_e:
            logging.debug(u"[OnlineRAG] build context failed: %s", _rag_e)
        body = json.dumps({
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.7,
            "max_tokens": 200,
            "stream": False,
        }).encode("utf-8")
        req = _ur.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key,
            })
        resp = _ur.urlopen(req, timeout=5)
        data = json.loads(resp.read().decode("utf-8"))
        reply = data["choices"][0]["message"]["content"].strip()
        # Trim to 3 sentences safety
        for sep in [u"。", u".", u"!", u"！"]:
            pass
        return reply[:150]
    except Exception as e:
        logging.warning(u"DeepSeek 闲聊失败: %s", e)
        return None


# ===== 语音调控命令 (Task 2) =====
def _extract_fatigue_limit(text):
    # type: (str) -> int
    """Extract a fatigue limit from Arabic or common Chinese numerals."""
    import re
    _CN2INT = {u'\u4e00':1, u'\u4e8c':2, u'\u4e24':2, u'\u4e09':3, u'\u56db':4,
               u'\u4e94':5, u'\u516d':6, u'\u4e03':7, u'\u516b':8, u'\u4e5d':9, u'\u96f6':0}

    def _parse_cn_num(match):
        s = match.group(0)
        total = 0
        km = re.match(u'([\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d])\u5343', s)
        if km:
            total += _CN2INT[km.group(1)] * 1000
            s = s[2:]
        bm = re.match(u'([\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d])\u767e', s)
        if bm:
            total += _CN2INT[bm.group(1)] * 100
            s = s[2:]
        if s.startswith(u'\u5341'):
            total += 10
            s = s[1:]
            if s and s[0] in _CN2INT:
                total += _CN2INT[s[0]]
                s = s[1:]
        else:
            tm = re.match(u'([\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d])\u5341', s)
            if tm:
                total += _CN2INT[tm.group(1)] * 10
                s = s[2:]
                if s and s[0] in _CN2INT:
                    total += _CN2INT[s[0]]
                    s = s[1:]
        if s and s[0] in _CN2INT:
            d = _CN2INT[s[0]]
            if km and not bm:
                total += d * 100
            elif bm and not tm and u'\u5341' not in match.group(0):
                total += d * 10
            else:
                total += d
        return str(total) if total > 0 else match.group(0)

    norm_text = re.sub(u'[\u4e00\u4e8c\u4e24\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\u767e\u5343]+',
                       _parse_cn_num, text)
    m = re.search(r'(\d{3,5})', norm_text)
    if m and any(w in norm_text for w in ["疲劳", "目标", "上限"]):
        return int(m.group(1))
    return 0


def _is_daily_plan_generate_intent(text):
    # type: (str) -> bool
    if not text:
        return False
    return any(w in text for w in [
        u"规划今日训练", u"规划今天训练", u"今日训练规划", u"今天训练规划",
        u"生成今日方案", u"生成今天方案", u"今日方案", u"今天方案",
        u"帮我规划", u"训练计划", u"训练规划",
    ]) and not _is_daily_plan_accept_intent(text)


def _is_daily_plan_accept_intent(text):
    # type: (str) -> bool
    if not text:
        return False
    return any(w in text for w in [
        u"采纳方案", u"接受方案", u"采用方案", u"就按这个", u"按这个来",
        u"开始训练", u"开始今天训练", u"开始今日训练",
    ])


def _post_streamer_json(path, payload):
    # type: (str, dict) -> dict
    if _urllib_request is None:
        return {}
    url = os.environ.get("IRONBUDDY_STREAMER_URL", "http://127.0.0.1:5000").rstrip("/") + path
    body = json.dumps(payload or {}).encode("utf-8")
    req = _urllib_request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = _urllib_request.urlopen(req, timeout=6)
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logging.warning(u"[DailyPlan] streamer request failed %s: %s", path, e)
        return {}


def _try_daily_plan_voice_command(text):
    # type: (str) -> bool
    """Narrow product-layer voice intent. Does not alter TTS/ASR playback path."""
    if _is_daily_plan_accept_intent(text):
        data = _post_streamer_json("/api/training_plan/daily/accept", {})
        reply = data.get("reply") if isinstance(data, dict) else ""
        _publish_coach_ack(reply or u"已采纳今日训练方案，开始训练", kind="daily_plan_accept", allow_interrupt=False)
        return True
    if _is_daily_plan_generate_intent(text):
        data = _post_streamer_json("/api/training_plan/daily", {
            "prompt": text,
            "speak": False,
        })
        plan = data.get("plan") if isinstance(data, dict) else {}
        summary = (plan or {}).get("summary") or u"今日训练方案已生成"
        reason = (plan or {}).get("reason") or u"请在数据页查看并采纳方案。"
        _publish_coach_ack(summary + u" " + reason + u" 你可以说采纳方案开始训练。",
                           kind="daily_plan_generate", allow_interrupt=False)
        return True
    return False


def _try_combo_voice_command(text):
    # type: (str) -> bool
    """Handle a small set of demo-friendly compound system commands."""
    limit = _extract_fatigue_limit(text)
    wants_next = any(w in text for w in [u"\u4e0b\u4e00\u7ec4", u"\u4e0b\u7ec4", u"\u7ee7\u7eed\u8bad\u7ec3", u"\u5f00\u59cb\u4e0b\u4e00\u7ec4"])
    if not (limit and wants_next):
        return False
    now = time.time()
    _write_signal("/dev/shm/fatigue_limit.json", {"limit": limit, "ts": now, "src": "voice_combo"})
    try:
        with open("/dev/shm/ui_fatigue_limit.json.tmp", "w", encoding="utf-8") as _uf:
            json.dump({"limit": limit, "ts": now, "src": "voice_combo"}, _uf)
        os.rename("/dev/shm/ui_fatigue_limit.json.tmp", "/dev/shm/ui_fatigue_limit.json")
    except Exception:
        pass
    try:
        with open("/dev/shm/next_set.request", "w") as _f:
            _f.write(str(now))
    except Exception:
        pass
    _publish_coach_ack(u"疲劳上限已改为%d，并开始下一组训练" % limit,
                       kind="combo_command", allow_interrupt=False)
    logging.info(u"命令: 组合执行 疲劳上限=%d + 下一组", limit)
    return True


def _try_voice_command(text, client=None):
    # type: (str, object) -> bool
    """检查是否是系统命令, 是则执行并返回True, 否则返回False让文字投递到FSM

    V4.5 修复: 先判"解除静音"再判"静音"。否则用户说"解除静音"会被"静音"子串命中，
    造成死锁无法解除。"解除静音"精确匹配优先。
    """
    # V4.9 同音纠错保护: 若 text 已精确包含权威命令词, 跳过 _HOMOPHONES 替换
    # (防止"解除静音"被"净音→静音"等规则意外破坏)
    _PROTECTED_WORDS = (
        u"解除静音", u"静音", u"恢复对话", u"关机", u"再见教练",
        u"深蹲", u"弯举", u"疲劳", u"飞书", u"纯视觉",
    )
    _skip_homophone = any(w in text for w in _PROTECTED_WORDS)

    # V7.0: \u7528 pypinyin \u62fc\u97f3\u6a21\u7cca\u5339\u914d\u53d6\u4ee3\u624b\u5de5\u540c\u97f3\u8bcd\u8868\u3002
    # _pinyin_fuzzy_normalize \u5728 text \u672b\u5c3e\u8ffd\u52a0\u547d\u4e2d\u7684\u786c\u7f16\u7801\u8bcd, \u4e0b\u6e38 `word in text` \u81ea\u7136\u547d\u4e2d\u3002
    # \u5176\u4e0d\u4f1a\u7834\u574f\u539f\u6587 (\u4f9d\u7136\u4fdd\u7559 ASR \u539f\u59cb\u8f93\u51fa\u4f9b\u65e5\u5fd7)\u3002
    if not _skip_homophone:
        text = _pinyin_fuzzy_normalize(text)

    # V6.2 音量调节: 支持"大一点"(+3)、"降到两格"(=2)、"调到第五档"(=5)等
    # 中文数字 → 阿拉伯数字映射
    _VOL_CN_MAP = {u"一": 1, u"两": 2, u"三": 3, u"四": 4, u"五": 5,
                   u"六": 6, u"七": 7, u"八": 8, u"九": 9, u"十": 10}
    _vol_target = None
    # 先检查指定格数: "降到两格" / "调到第三格" / "调到五档"
    if any(w in text for w in [u"降到", u"调到", u"调成", u"设成", u"设为"]):
        import re as _re
        _m = _re.search(r'(\d+)', text)
        if _m:
            _vol_target = int(_m.group(1))
        else:
            for _cn, _n in _VOL_CN_MAP.items():
                if _cn in text:
                    _vol_target = _n
                    break
        if _vol_target is not None:
            new = max(1, min(15, _vol_target))
            _set_tts_volume(new)
            _publish_coach_ack(u"音量已设置为%d档" % new, kind="volume", allow_interrupt=False)
            logging.info(u"命令: 音量指定 %d", new)
            return True
    # V7.10 "\u4e0b\u4e00\u7ec4" \u786c\u7f16\u7801: \u91cd\u7f6e FSM \u6570\u636e\u5f00\u59cb\u65b0\u7ec4\u8bad\u7ec3
    if any(w in text for w in [u"\u4e0b\u4e00\u7ec4", u"\u4e0b\u7ec4", u"\u7ee7\u7eed\u8bad\u7ec3", u"\u5f00\u59cb\u4e0b\u4e00\u7ec4"]):
        try:
            with open("/dev/shm/next_set.request", "w") as _f:
                _f.write(str(time.time()))
        except Exception:
            pass
        _publish_coach_ack(u"\u597d\u7684\uff0c\u5f00\u59cb\u4e0b\u4e00\u7ec4\u8bad\u7ec3", kind="next_set", allow_interrupt=False)
        logging.info(u"\u547d\u4ee4: \u4e0b\u4e00\u7ec4 (\u4fe1\u53f7\u5df2\u53d1)")
        return True

    # V7.5 \u67e5\u8be2\u5f53\u524d\u97f3\u91cf (\u4f18\u5148\u4e8e\u8c03\u9ad8/\u8c03\u4f4e)
    if any(w in text for w in [u"\u5f53\u524d\u97f3\u91cf", u"\u97f3\u91cf\u591a\u5c11", u"\u97f3\u91cf\u662f\u591a\u5c11", u"\u73b0\u5728\u97f3\u91cf"]):
        cur = _get_tts_volume()
        _publish_coach_ack(u"\u5f53\u524d\u97f3\u91cf\u662f%d\u6863" % cur, kind="volume", allow_interrupt=False)
        logging.info(u"\u547d\u4ee4: \u67e5\u8be2\u97f3\u91cf (%d)", cur)
        return True
    if any(w in text for w in [u"大一点", u"调高音量", u"声音大", u"更大声", u"音量加", u"大声点"]):
        cur = _get_tts_volume()
        new = min(15, cur + 3)
        _set_tts_volume(new)
        _publish_coach_ack(u"音量已调大到%d档" % new, kind="volume", allow_interrupt=False)
        logging.info(u"命令: 音量调高 %d -> %d", cur, new)
        return True
    if any(w in text for w in [u"小一点", u"调低音量", u"声音小", u"轻一点", u"音量减", u"小声点"]):
        cur = _get_tts_volume()
        new = max(1, cur - 3)
        _set_tts_volume(new)
        _publish_coach_ack(u"音量已调小到%d档" % new, kind="volume", allow_interrupt=False)
        logging.info(u"命令: 音量调低 %d -> %d", cur, new)
        return True

    # V7.3 \u5148\u5224"\u89e3\u9664\u9759\u97f3" (\u7cbe\u786e\u5173\u952e\u8bcd, \u5bb9\u9519\u6269\u5145)
    UNMUTE_WORDS = [
        u"\u89e3\u9664\u9759\u97f3", u"\u53d6\u6d88\u9759\u97f3", u"\u5173\u95ed\u9759\u97f3",
        u"\u53ef\u4ee5\u8bf4\u8bdd", u"\u4f60\u53ef\u4ee5\u8bf4\u8bdd", u"\u6062\u590d\u5bf9\u8bdd",
        u"\u6062\u590d\u8bf4\u8bdd", u"\u7ee7\u7eed\u8bf4\u8bdd", u"\u522b\u88c5\u6b7b",
        u"\u6253\u5f00\u58f0\u97f3", u"\u5f00\u58f0", u"\u6062\u590d\u58f0\u97f3",
    ]
    if any(w in text for w in UNMUTE_WORDS):
        _is_muted[0] = False
        _write_signal("/dev/shm/mute_signal.json", {"muted": False, "ts": time.time()})
        _apply_playback_volume(muted=False, reason="voice_unmute")
        _publish_coach_ack("好的，恢复正常对话", kind="unmute", allow_interrupt=False)
        logging.info("命令: 解除静音")
        return True

    # \u518d\u5224"\u9759\u97f3" (V5.0: \u79fb\u9664 "\u5b89\u9759" \u2014 \u5bb9\u6613\u5339\u914d "\u6211\u60f3\u5b89\u9759\u4e00\u4e0b" \u7b49\u81ea\u7136\u8bed\u53e5)
    MUTE_WORDS = ["闭嘴", "静音", "教练静音", "别说话", "别吵"]
    if any(w in text for w in MUTE_WORDS):
        return _execute_voice_mute_command(source="voice")

    # V6.0 \u72b6\u6001\u5feb\u901f\u67e5\u8be2\uff08\u96f6\u5ef6\u8fdf\u672c\u5730\u7b54\uff0c\u4e0d\u8d70 LLM\uff09
    # \u547d\u4e2d\u89c4\u5219: \u5305\u542b\u4e0b\u5217\u67e5\u8be2\u8bcd \u4e14 \u540c\u65f6\u5305\u542b\u52a8\u4f5c\u8bcd (\u6df1\u8e72/\u5f2f\u4e3e) \u6216 "\u591a\u5c11\u4e2a/\u51e0\u4e2a/\u6210\u7ee9/\u62a5\u6570/\u5b8c\u6210"
    # V7.0.2: \u67e5\u8be2\u5fc5\u987b\u540c\u65f6\u5305\u542b "\u591a\u5c11/\u51e0/\u5b8c\u6210\u5ea6/\u6210\u7ee9/\u62a5\u6570" \u5f02\u4e3b\u8bcd
    # \u5355\u72ec\u7684 "\u6df1\u8e72"/"\u5f2f\u4e3e" \u4e0d\u89e6\u53d1\u67e5\u8be2 \u2014 \u907f\u514d\u4e0e"\u5207\u6362\u5230\u6df1\u8e72/\u5f2f\u4e3e"\u51b2\u7a81
    # V7.7: "\u591a\u5c11"\u5355\u72ec\u4e5f\u7b97\u67e5\u8be2(\u9632 ASR \u88c1\u65ad)
    _QUERY_WORDS = (u"\u591a\u5c11\u4e2a", u"\u51e0\u4e2a", u"\u5b8c\u6210\u5ea6", u"\u6210\u7ee9",
                    u"\u62a5\u6570", u"\u505a\u4e86\u591a\u5c11", u"\u5e72\u4e86\u591a\u5c11",
                    u"\u591a\u5c11\u6b21", u"\u7ec3\u4e86\u591a\u5c11")
    # \u67e5\u8be2\u6761\u4ef6: \u5fc5\u987b\u542b _QUERY_WORDS \u4e4b\u4e00 \u4e14 \u4e0d\u542b "\u5207\u6362"
    _is_query = any(w in text for w in _QUERY_WORDS) and (u"\u5207\u6362" not in text)
    if _is_query:
        try:
            with open("/dev/shm/fsm_state.json", "r", encoding="utf-8") as f:
                d = json.load(f)
            good = int(d.get("good", 0))
            failed = int(d.get("failed", 0))
            ex = d.get("exercise", "squat")
            ex_cn = u"\u6df1\u8e72" if ex == "squat" else u"\u5f2f\u4e3e"
            _publish_coach_ack(u"\u62a5\u544a\uff0c\u5f53\u524d%s\u8fbe\u6807%d\u4e2a\uff0c\u8fdd\u89c4%d\u4e2a\uff0c\u7ee7\u7eed\u52a0\u6cb9\uff01" %
                               (ex_cn, good, failed), kind="stats_query", allow_interrupt=False)
            logging.info(u"\u547d\u4ee4: \u52a8\u4f5c\u67e5\u8be2 (%s good=%d failed=%d)", ex, good, failed)
        except Exception:
            _publish_coach_ack(u"\u76ee\u524d\u8fd8\u6ca1\u6709\u8fd0\u52a8\u6570\u636e\u54e6\uff01", kind="stats_query", allow_interrupt=False)
        return True

    # \u5207\u6362\u5230\u6df1\u8e72 \u2014 V6.1 \u4e25\u683c\u786e\u8ba4\u540e\u624d\u64ad\u62a5
    _is_squat_switch = any(w in text for w in ["切换到深蹲", "切到深蹲", "换到深蹲", "进入深蹲", "深蹲模式"]) or \
                       (u"深蹲" in text and any(w in text for w in [u"切换", u"切到", u"换到", u"进入", u"模式", u"改成"]))
    if _is_squat_switch:
        _write_signal("/dev/shm/exercise_mode.json", {"mode": "squat", "ts": time.time()})
        if _wait_mode_applied("exercise", "squat", timeout=3.0):
            _publish_coach_ack(u"\u5df2\u5207\u6362\u5230\u6df1\u8e72\u6a21\u5f0f", kind="mode_change", allow_interrupt=False)
            logging.info(u"\u547d\u4ee4: \u5207\u6362\u5230\u6df1\u8e72 (FSM \u786e\u8ba4)")
        else:
            _publish_coach_ack(u"\u5207\u6362\u6df1\u8e72\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u4e3b\u5faa\u73af", kind="mode_change_failed", allow_interrupt=False)
            logging.warning(u"\u547d\u4ee4: \u5207\u6362\u6df1\u8e72 FSM \u672a\u786e\u8ba4")
        return True

    # \u5207\u6362\u5230\u5f2f\u4e3e
    # V7.4: "\u54d1\u94c3"/"\u52a8\u4f5c"\u4e5f\u89e6\u53d1\u5f2f\u4e3e\u5207\u6362 (\u56e0\u4e3a\u9ed8\u8ba4\u662f\u6df1\u8e72,\u8bf4"\u5207\u6362\u52a8\u4f5c"=\u5207\u6362\u5f2f\u4e3e)
    _is_curl_switch = any(w in text for w in ["切换到弯举", "切到弯举", "换到弯举", "进入弯举", "弯举模式"]) or \
                      (u"\u5207\u6362" in text and (u"\u54d1\u94c3" in text or u"\u5f2f\u4e3e" in text)) or \
                      (u"\u5207\u6362" in text and u"\u52a8\u4f5c" in text and u"\u6df1\u8e72" not in text)
    if _is_curl_switch:
        _write_signal("/dev/shm/exercise_mode.json", {"mode": "curl", "ts": time.time()})
        if _wait_mode_applied("exercise", "curl", timeout=3.0):
            _publish_coach_ack(u"\u5df2\u5207\u6362\u5230\u5f2f\u4e3e\u6a21\u5f0f", kind="mode_change", allow_interrupt=False)
            logging.info(u"\u547d\u4ee4: \u5207\u6362\u5230\u5f2f\u4e3e (FSM \u786e\u8ba4)")
        else:
            _publish_coach_ack(u"\u5207\u6362\u5f2f\u4e3e\u5931\u8d25", kind="mode_change_failed", allow_interrupt=False)
            logging.warning(u"\u547d\u4ee4: \u5207\u6362\u5f2f\u4e3e FSM \u672a\u786e\u8ba4")
        return True

    # V6.1 \u7eaf\u89c6\u89c9/\u89c6\u89c9+\u4f20\u611f \u6a21\u5f0f\u5207\u6362 \u2014 \u4e25\u683c\u786e\u8ba4
    if any(w in text for w in [u"纯视觉", u"视觉模式"]):
        _write_signal("/dev/shm/inference_mode.json", {"mode": "pure_vision", "ts": time.time()})
        if _wait_mode_applied("inference", "pure_vision", timeout=3.0):
            _publish_coach_ack(u"\u5df2\u5207\u6362\u5230\u7eaf\u89c6\u89c9\u6a21\u5f0f", kind="mode_change", allow_interrupt=False)
            logging.info(u"\u547d\u4ee4: \u5207\u6362\u7eaf\u89c6\u89c9 (\u786e\u8ba4)")
        else:
            _publish_coach_ack(u"\u5207\u6362\u7eaf\u89c6\u89c9\u5931\u8d25", kind="mode_change_failed", allow_interrupt=False)
            logging.warning(u"\u547d\u4ee4: \u5207\u6362\u7eaf\u89c6\u89c9\u672a\u786e\u8ba4")
        return True
    # M11 (V7.17, 2026-04-20): 视觉+传感关键词大幅扩容, 容错 ASR 错识
    # 实测错识变体: "视觉加权战" / "视觉加杠杆" / "视觉加感" (jiachuangan -> jiaquanzhan/jiagangang/jiagan)
    # 策略: "视觉加" 后接任意内容 (默认纯视觉时,说"视觉加X"几乎必是想切传感)
    #       + 各种"传感"同音近音
    _SENSOR_KEYWORDS = [
        # 精确词
        u"视觉加传感", u"视觉传感", u"传感模式", u"传感器模式",
        u"传感器", u"视觉感知", u"视觉加感知",
        # "视觉加X" 泛匹配 (用户 demo 实录)
        u"视觉加权", u"视觉加杠", u"视觉加感", u"视觉加肌",
        u"视觉加杠杆", u"视觉加权战", u"视觉加岗", u"视觉加刚",
        # 单独"加 X" 当传感意图
        u"加传感", u"加杠杆", u"加权战", u"加肌电",
        # 混合模式口语
        u"混合模式", u"双模", u"一体化", u"融合模式", u"全模式",
    ]
    # "视觉加" 泛启发: 只要含"视觉加"且不是"视觉加纯"等 (纯视觉分支在上面已判)
    _is_sensor_cmd = any(w in text for w in _SENSOR_KEYWORDS)
    if not _is_sensor_cmd and u"视觉加" in text:
        _is_sensor_cmd = True
    if _is_sensor_cmd:
        _write_signal("/dev/shm/inference_mode.json", {"mode": "vision_sensor", "ts": time.time()})
        if _wait_mode_applied("inference", "vision_sensor", timeout=3.0):
            _publish_coach_ack(u"\u5df2\u5207\u6362\u5230\u89c6\u89c9\u52a0\u4f20\u611f\u6a21\u5f0f", kind="mode_change", allow_interrupt=False)
            logging.info(u"\u547d\u4ee4: \u5207\u6362\u89c6\u89c9+\u4f20\u611f (\u786e\u8ba4)")
        else:
            _publish_coach_ack(u"\u5207\u6362\u89c6\u89c9\u52a0\u4f20\u611f\u5931\u8d25", kind="mode_change_failed", allow_interrupt=False)
            logging.warning(u"\u547d\u4ee4: \u5207\u6362\u89c6\u89c9+\u4f20\u611f\u672a\u786e\u8ba4")
        return True

    # 飞书智能代理: 规划、总结、提醒 分流
    # M11 (V7.17): 扩容容错 — "推送"/"推到"系列 + "飞出/非书/菲书/腓书" 等飞书近音字
    # 用户实录错识: "推送到非书平台" / "推送到飞出平台" / "就是飞出去的"
    push_type = None
    speak_ack = None
    # "飞书"的近音变体 (百度 ASR 经常把 feishu 切成同音字)
    _FEISHU_SYNONYMS = (u"飞书", u"非书", u"飞出", u"非出", u"菲书", u"腓书",
                        u"废书", u"飞输", u"非出去", u"飞出去", u"肥书")
    _has_feishu = any(w in text for w in _FEISHU_SYNONYMS)
    # "推送"系列 — 用户说"推送"99% 是想发飞书, 和细分意图解耦
    _has_push = any(w in text for w in [u"推送", u"推到", u"推送到", u"发送到", u"发到", u"发给"])
    # 训练相关 + 推送 → summary
    _summary_keywords = [u"总结", u"汇报", u"战报", u"飞书报告", u"飞书总结",
                         u"生成报告", u"训练结果", u"训练总结", u"训练报告",
                         u"战况", u"成绩报告"]
    if any(w in text for w in _summary_keywords) or (_has_push and not any(w in text for w in [u"警告", u"提醒", u"规划", u"计划"])):
        push_type = "summary"
        speak_ack = u"正在查阅历史并汇总战报，推送到飞书"
    elif any(w in text for w in [u"警告", u"提醒", u"超载", u"报警"]):
        push_type = "reminder"
        speak_ack = u"正在发送系统超载警示提醒通告"
    elif any(w in text for w in [u"规划", u"计划", u"发飞书", u"发消息"]):
        push_type = "plan"
        speak_ack = u"正在调取长期记忆，生成专业规划推送飞书"
    # 仅含"飞书"近音而无具体意图 → 默认 summary
    elif _has_feishu:
        push_type = "summary"
        speak_ack = u"收到，正在推送训练总结到飞书"

    if push_type:
        _publish_coach_ack(speak_ack, kind="feishu", allow_interrupt=False)
        logging.info("命令: 飞书智能推送 (%s)", push_type)
        try:
            payload = json.dumps({"type": push_type, "prompt": text}).encode('utf-8')
            import urllib
            if hasattr(urllib, 'request'):
                req = urllib.request.Request(
                    "http://127.0.0.1:5000/api/feishu/push",
                    data=payload,
                    headers={"Content-Type": "application/json"})
                # V7.21 (2026-04-21): timeout 40→90s —— 后端最坏 DeepSeek 20+token 8+msg 15 = 43s,
                # 加上 Nexus SQLite 查询峰值可再多 10s, 40s 死线必炸 → "服务器忙碌"假失败
                resp = urllib.request.urlopen(req, timeout=90)
                result = json.loads(resp.read().decode())
                if result.get("ok"):
                    _elapsed = result.get("elapsed_s")
                    _degraded = result.get("degraded", False)
                    if _degraded:
                        _publish_coach_ack("飞书已投递简报，AI 点评暂不可用", kind="feishu_done", allow_interrupt=False)
                    else:
                        _publish_coach_ack("飞书投递已完成！", kind="feishu_done", allow_interrupt=False)
                else:
                    _publish_coach_ack("推送失败: %s" % str(result.get("error", "未知错误"))[:20], kind="feishu_failed", allow_interrupt=False)
            else:
                # Python 2 fallback
                import urllib2
                req = urllib2.Request(
                    "http://127.0.0.1:5000/api/feishu/push",
                    data=payload,
                    headers={"Content-Type": "application/json"})
                resp = urllib2.urlopen(req, timeout=90)
                _publish_coach_ack("飞书投递已完成！", kind="feishu_done", allow_interrupt=False)
        except Exception as e:
            logging.error("飞书推送异常: %s", e)
            # V7.21: 区分超时 vs 其他异常, 不再笼统说"服务器忙碌"
            _emsg = type(e).__name__
            if "timeout" in _emsg.lower() or "timed out" in str(e).lower():
                _publish_coach_ack("飞书推送仍在后台进行，请稍后查看群消息", kind="feishu_pending", allow_interrupt=False)
            else:
                _publish_coach_ack("飞书链路异常：%s" % _emsg[:15], kind="feishu_failed", allow_interrupt=False)
        return True

    # V6.1 \u8bed\u97f3\u5173\u673a \u2014 \u5fc5\u987b\u5148\u64ad\u5b8c"\u518d\u89c1"\u518d\u8c03\u505c\u673a API
    if any(w in text for w in [u"\u5173\u673a", u"\u4e0b\u7ebf", u"\u7ed3\u675f\u8bad\u7ec3", u"\u518d\u89c1\u6559\u7ec3", u"\u6559\u7ec3\u518d\u89c1"]):
        _farewell = u"\u597d\u7684\uff0c\u518d\u89c1\uff0c\u8bb0\u5f97\u4e0b\u6b21\u53ca\u65f6\u8bad\u7ec3"
        _publish_coach_ack(_farewell, kind="farewell", allow_interrupt=False)
        logging.info(u"\u547d\u4ee4: \u8bed\u97f3\u5173\u673a")
        # V6.1: \u7b49 TTS \u771f\u7684\u64ad\u5b8c\u518d\u53d1\u5173\u673a API
        # \u4f30\u6570: \u5408\u6210\u9700 1s + \u6587\u672c\u957f\u5ea6 * 0.25s + \u51b7\u5374 1s
        _expected_sec = 1.5 + len(_farewell) * 0.25 + 1.0
        _deadline = time.time() + _expected_sec + 2.0  # \u989d\u5916 2s \u5bbd\u5bb9
        while time.time() < _deadline:
            # \u68c0\u67e5 SpeechManager \u662f\u5426\u8fd8\u5728\u5fd9
            if _sm._current_prio[0] >= 99 and not _sm._has_pending():
                logging.info(u"[\u5173\u673a] TTS \u786e\u8ba4\u64ad\u5b8c")
                break
            time.sleep(0.2)
        else:
            logging.warning(u"[\u5173\u673a] TTS \u64ad\u62a5\u8d85\u65f6, \u5f3a\u5236\u5173\u673a")
        try:
            import urllib.request as _ur
            _req = _ur.Request(
                "http://127.0.0.1:5000/api/admin/stop",
                data=b"{}",
                headers={"Content-Type": "application/json"})
            _ur.urlopen(_req, timeout=5).read()
        except Exception as _e:
            logging.warning(u"\u5173\u673a API \u8c03\u7528\u5931\u8d25: %s", _e)
        return True

    # V7.12 \u901a\u7528\u4e2d\u6587\u6570\u5b57 \u2192 \u963f\u62c9\u4f2f
    limit = _extract_fatigue_limit(text)
    if limit:
        _write_signal("/dev/shm/fatigue_limit.json", {"limit": limit, "ts": time.time()})
        # V6.1 \u4e25\u683c\u786e\u8ba4: \u7b49 FSM \u6d88\u8d39 shm \u6587\u4ef6 \u6216 fsm_state.fatigue_limit \u5339\u914d
        _applied = False
        _start = time.time()
        while time.time() - _start < 3.0:
            if not os.path.exists("/dev/shm/fatigue_limit.json"):
                _applied = True
                break
            try:
                with open("/dev/shm/fsm_state.json", "r", encoding="utf-8") as _fs:
                    _fsm_d = json.load(_fs)
                if int(_fsm_d.get("fatigue_limit", 0)) == limit:
                    _applied = True
                    break
            except Exception:
                pass
            time.sleep(0.15)
        if _applied:
            # V6.2: \u540c\u65f6\u5199 ui_fatigue_limit.json, state_feed \u8bfb\u5b83 \u2192 UI bar \u7acb\u5373\u5237\u65b0
            try:
                _ui_path = "/dev/shm/ui_fatigue_limit.json"
                with open(_ui_path + ".tmp", "w", encoding="utf-8") as _uf:
                    json.dump({"limit": limit, "ts": time.time()}, _uf)
                os.rename(_ui_path + ".tmp", _ui_path)
            except Exception as _ue:
                logging.debug(u"\u5199 ui_fatigue_limit.json \u5931\u8d25: %s", _ue)
            _publish_coach_ack(u"\u75b2\u52b3\u4e0a\u9650\u5df2\u6539\u4e3a%d" % limit, kind="fatigue_limit", allow_interrupt=False)
            logging.info(u"\u547d\u4ee4: \u75b2\u52b3\u4e0a\u9650 %d (FSM \u786e\u8ba4, UI \u5df2\u5237\u65b0)", limit)
        else:
            _publish_coach_ack(u"\u75b2\u52b3\u4e0a\u9650\u5207\u6362\u5931\u8d25", kind="fatigue_limit_failed", allow_interrupt=False)
            logging.warning(u"\u547d\u4ee4: \u75b2\u52b3\u4e0a\u9650 %d \u672a\u786e\u8ba4", limit)
        return True

    return False


def _wait_mode_applied(kind, expected_mode, timeout=3.0):
    # type: (str, str, float) -> bool
    """V6.1 \u4e25\u683c\u786e\u8ba4\u6a21\u5f0f\u5207\u6362\u751f\u6548.
    FSM \u6d88\u8d39\u540e\u4f1a\u4ece /dev/shm \u5220\u6389 shm \u6587\u4ef6 + \u66f4\u65b0 fsm_state.json
    \u4efb\u4e00\u8bc1\u636e\u5373\u89c6\u4e3a\u751f\u6548.
    - kind: \"exercise\" | \"inference\"
    - expected_mode: \"squat/curl\" \u6216 \"pure_vision/vision_sensor\"
    \u8fd4\u56de True \u8868\u793a\u786e\u8ba4,False \u8868\u793a\u8d85\u65f6\u3002"""
    shm_file = "/dev/shm/%s_mode.json" % kind
    # FSM \u5185\u90e8\u628a "curl" \u518d\u6620\u5c04\u6210 "bicep_curl"
    _alt_mode = {"curl": "bicep_curl"}.get(expected_mode, expected_mode)
    start = time.time()
    while time.time() - start < timeout:
        # \u8bc1\u636e 1: shm \u6587\u4ef6\u88ab FSM \u6d88\u8d39\u5220\u6389
        if not os.path.exists(shm_file):
            logging.info(u"[_wait_mode_applied %s=%s] shm \u6587\u4ef6\u5df2\u88ab\u6d88\u8d39, \u786e\u8ba4\u751f\u6548",
                         kind, expected_mode)
            return True
        # \u8bc1\u636e 2: fsm_state.json \u91cc\u7684 exercise/inference_mode \u5339\u914d
        try:
            with open("/dev/shm/fsm_state.json", "r", encoding="utf-8") as f:
                fsm_d = json.load(f)
            if kind == "exercise":
                _cur = fsm_d.get("exercise")
                if _cur == expected_mode or _cur == _alt_mode:
                    logging.info(u"[_wait_mode_applied] fsm_state.exercise=%s \u5339\u914d", _cur)
                    return True
            elif kind == "inference" and fsm_d.get("inference_mode") == expected_mode:
                logging.info(u"[_wait_mode_applied] fsm_state.inference_mode=%s \u5339\u914d", expected_mode)
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def _write_signal(path, data):
    # type: (str, dict) -> None
    """原子写入信号文件"""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.rename(tmp, path)
    except Exception as e:
        logging.error("写信号文件失败 %s: %s", path, e)


# V7.30: realise router Action — adapter from voice/router.py output to live shm writes.
# Wire-up into _route_text deferred to manual session: this function is exercised
# by unit tests today, will replace the if-chain in _route_text after live STT validation.
def _realize_action(action, speak_fn=None):
    """Map a voice/router.py Action to side effects (shm writes + speak)."""
    if action is None or action.kind == "silent":
        return False
    if action.kind == "speak":
        if action.text and speak_fn is not None:
            speak_fn(action.text)
        return True
    # action.kind == "tool"
    name = action.tool_name
    args = action.args or {}
    now = time.time()
    if name == "set_mute":
        _write_signal("/dev/shm/mute_signal.json",
                       {"muted": bool(args.get("muted")), "ts": now})
    elif name == "stop_speaking":
        _write_signal("/dev/shm/voice_interrupt", {"ts": now})
    elif name == "switch_exercise":
        mode = args.get("action") or args.get("mode")
        if mode in ("squat", "curl"):
            _write_signal("/dev/shm/exercise_mode.json", {"mode": mode, "ts": now})
            _write_signal("/dev/shm/user_profile.json",
                           {"exercise": "bicep_curl" if mode == "curl" else "squat", "ts": now})
    elif name == "switch_vision_mode":
        mode = args.get("mode")
        if mode in ("pure_vision", "vision_sensor"):
            _write_signal("/dev/shm/inference_mode.json", {"mode": mode, "ts": now})
    elif name == "switch_inference_backend":
        backend = args.get("backend")
        if backend in ("local_npu", "cloud_gpu"):
            _write_signal("/dev/shm/vision_mode.json", {"mode": backend, "ts": now})
    elif name == "set_fatigue_limit":
        try:
            limit = int(args.get("value"))
        except (TypeError, ValueError):
            limit = 0
        if 100 <= limit <= 5000:
            _write_signal("/dev/shm/fatigue_limit.json", {"limit": limit, "ts": now})
    elif name == "push_feishu_summary":
        _write_signal("/dev/shm/auto_trigger.json",
                       {"reason": "feishu_summary", "ts": now})
    elif name == "shutdown":
        _write_signal("/dev/shm/shutdown.json", {"reason": "voice", "ts": now})
    elif name == "report_status":
        if speak_fn is not None:
            speak_fn(_format_status_report())
        return True
    else:
        logging.info(u"[ROUTER] unknown tool: %s", name)
        return False
    if action.text and speak_fn is not None:
        speak_fn(action.text)
    return True


def _format_status_report():
    """Read /dev/shm/fsm_state.json and produce a 1-sentence status string."""
    try:
        with open("/dev/shm/fsm_state.json", "r") as f:
            data = json.load(f)
        good = data.get("good", 0)
        failed = data.get("failed", 0)
        comp = data.get("comp", 0)
        fatigue = int(data.get("fatigue", 0))
        return u"已完成%d次标准、%d次违规、%d次代偿，疲劳值%d" % (good, failed, comp, fatigue)
    except (OSError, ValueError, KeyError):
        return u"暂无训练数据"


if __name__ == "__main__":
    main()
