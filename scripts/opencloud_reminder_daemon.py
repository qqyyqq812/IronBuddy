#!/usr/bin/env python3
"""OpenCloud-side IronBuddy reminder daemon.

Runs outside the Toybrick board. It periodically builds a small reminder card
from the latest board snapshot if reachable, or from the last cached snapshot
when the board is offline. No board service is required for the reminder loop.

Python 3.7 compatible; stdlib-only plus project FeishuClient.
"""

from __future__ import absolute_import, print_function

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from hardware_engine.integrations.feishu_client import FeishuClient
except Exception as exc:
    FeishuClient = None
    _IMPORT_ERROR = str(exc)
else:
    _IMPORT_ERROR = ""


RUNTIME_DIR = os.path.join(PROJECT_ROOT, "data", "runtime")
SNAPSHOT_PATH = os.path.join(RUNTIME_DIR, "opencloud_last_board_snapshot.json")
STATUS_PATH = os.path.join(RUNTIME_DIR, "opencloud_reminder_status.json")
HISTORY_PATH = os.path.join(RUNTIME_DIR, "opencloud_reminder_history.jsonl")
HISTORY_MAX_LINES = 500
DEFAULT_BOARD_URL = os.environ.get("IRONBUDDY_BOARD_URL", "http://10.244.190.224:5000")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [OPENCLOUD_REMINDER] - %(levelname)s - %(message)s",
)


def _ensure_runtime_dir():
    try:
        os.makedirs(RUNTIME_DIR)
    except OSError:
        pass


def _atomic_json(path, data):
    _ensure_runtime_dir()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.rename(tmp, path)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _fetch_board_snapshot(board_url, timeout=4):
    url = board_url.rstrip("/") + "/api/fsm_state"
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def load_snapshot(board_url):
    try:
        snap = _fetch_board_snapshot(board_url)
        snap["_snapshot_source"] = "board"
        snap["_snapshot_ts"] = time.time()
        _atomic_json(SNAPSHOT_PATH, snap)
        return snap, True, ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        cached = _read_json(SNAPSHOT_PATH)
        if cached:
            cached["_snapshot_source"] = "cached"
            return cached, False, str(exc)
        return {
            "exercise": "squat",
            "good": 0,
            "failed": 0,
            "comp": 0,
            "fatigue": 0,
            "fatigue_limit": 1500,
            "_snapshot_source": "default",
            "_snapshot_ts": time.time(),
        }, False, str(exc)


def build_reminder_text(mode, snapshot, board_online):
    exercise = snapshot.get("exercise") or "squat"
    exercise_label = "弯举" if exercise in ("curl", "bicep_curl") else "深蹲"
    good = int(snapshot.get("good", 0) or 0)
    failed = int(snapshot.get("failed", 0) or 0)
    comp = int(snapshot.get("comp", 0) or 0)
    source = snapshot.get("_snapshot_source", "default")
    online_text = "板端在线" if board_online else "板端离线，使用%s快照" % source
    if mode == "morning":
        lead = "早上好，今天先用一组慢速热身打开状态。"
    elif mode == "evening":
        lead = "晚间提醒，今天如果还没训练，可以补一组轻量动作。"
    elif mode == "weekly":
        lead = "本周训练提醒，优先保证动作质量和规律性。"
    else:
        lead = "训练提醒，按当前状态选择合适强度。"
    return (
        "%s\n%s。最近动作：%s，标准 %d 次，不标准 %d 次，代偿 %d 次。"
        "如果疲劳或疼痛明显，今天以恢复和技术动作为主。"
    ) % (lead, online_text, exercise_label, good, failed, comp)


def _fetch_insights(board_url, timeout=4):
    url = board_url.rstrip("/") + "/api/openclaw/insights"
    try:
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return {}


def build_reminder_text_v2(mode, snapshot, board_online, insights):
    """4-block markdown body: 训练统计 + 高频提问 + 学习方向 + footer."""
    if mode == "morning":
        lead = "早上好。基于本周数据库，今天关注以下几点："
    elif mode == "evening":
        lead = "晚间总结。本周积累的问题已自动归总到 RAG 待补清单："
    elif mode == "weekly":
        lead = "周日战报。本周训练 + 知识反哺概览："
    else:
        lead = "训练提醒。"
    weekly = insights.get("weekly_training") or {}
    sessions = int(weekly.get("sessions", 0))
    good = int(weekly.get("good", 0))
    failed = int(weekly.get("failed", 0))
    block1 = (
        "**📊 本周训练统计**\n"
        "会话 %d 次，标准 %d 次，不标准 %d 次"
    ) % (sessions, good, failed)
    topics = insights.get("hot_voice_topics") or []
    if topics:
        topic_lines = "\n".join(
            "- " + t["text"] + " (×" + str(t["count"]) + ")"
            for t in topics
        )
        block2 = "**💬 本周高频提问 Top 3**\n" + topic_lines
    else:
        block2 = "**💬 本周高频提问 Top 3**\n暂无真实记录"
    triggers = insights.get("llm_triggers") or []
    if triggers:
        trig_lines = " · ".join(
            (t["trigger"] or "?") + " (" + str(t["count"]) + ")"
            for t in triggers[:3]
        )
        block3 = (
            "**🦞 教练学习方向**\n"
            "LLM 触发分布: " + trig_lines + "\n"
            "下一轮 RAG 知识库将就近补充上述高频提问对应的知识卡。"
        )
    else:
        block3 = "**🦞 教练学习方向**\n本周尚无 LLM 调用，等待用户对话再积累。"
    today = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    online = "板端在线" if board_online else "板端离线 (使用%s快照)" % snapshot.get("_snapshot_source", "?")
    block4 = "_" + online + " · 推送 " + today + " · OpenClaw 后台代理_"
    return lead + "\n\n" + block1 + "\n\n" + block2 + "\n\n" + block3 + "\n\n" + block4


def push_card(mode, snapshot, text, dry_run=True):
    if FeishuClient is None:
        return {"ok": False, "error": "FeishuClient import failed", "detail": _IMPORT_ERROR}
    title_map = {
        "morning": "IronBuddy 训练早报",
        "evening": "IronBuddy 晚间提醒",
        "weekly": "IronBuddy 训练周报",
        "auto": "IronBuddy 训练提醒",
    }
    client = FeishuClient(dry_run=dry_run)
    card = FeishuClient.build_training_card(
        title_map.get(mode, "IronBuddy 训练提醒"),
        text,
        stats=snapshot,
        push_type="weekly" if mode == "weekly" else "daily",
        degraded=not bool(snapshot.get("_snapshot_source") == "board"),
        footer="IronBuddy OpenCloud · " + time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    result = client.send_card(card)
    result["card"] = card
    return result


def run_once(mode, board_url, dry_run=True):
    snapshot, board_online, error = load_snapshot(board_url)
    insights = _fetch_insights(board_url) if board_online else {}
    if insights and insights.get("ok"):
        text = build_reminder_text_v2(mode, snapshot, board_online, insights)
    else:
        text = build_reminder_text(mode, snapshot, board_online)
    result = push_card(mode, snapshot, text, dry_run=dry_run)
    status = {
        "ok": bool(result.get("ok")),
        "mode": mode,
        "dry_run": bool(dry_run),
        "board_url": board_url,
        "board_online": bool(board_online),
        "board_error": error,
        "snapshot_source": snapshot.get("_snapshot_source"),
        "last_push_ts": time.time(),
        "last_push_text": text,
        "feishu_result": dict((k, v) for k, v in result.items() if k != "card"),
    }
    # V7.37: snapshot the daemon's actual schedule into status so the
    # streamer-side /api/openclaw/status can echo the truth instead of its
    # own process env (which doesn't see systemd-injected vars).
    status["schedule"] = {
        "weekly_hour": int(os.environ.get("IRONBUDDY_WEEKLY_HOUR", "20")),
        "weekly_dow": int(os.environ.get("IRONBUDDY_WEEKLY_DOW", "6")),
        "morning_hour": int(os.environ.get("IRONBUDDY_MORNING_HOUR", "9")),
        "evening_hour": int(os.environ.get("IRONBUDDY_EVENING_HOUR", "21")),
    }
    _atomic_json(STATUS_PATH, status)
    # Append to history (rotated to last HISTORY_MAX_LINES on every write)
    try:
        _ensure_runtime_dir()
        prior = []
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        prior.append(line)
        prior.append(json.dumps(status, ensure_ascii=False))
        prior = prior[-HISTORY_MAX_LINES:]
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(prior) + "\n")
    except Exception as exc:
        logging.warning("history write failed: %s", exc)
    logging.info("run_once mode=%s board_online=%s dry_run=%s ok=%s",
                 mode, board_online, dry_run, status["ok"])
    return status


def _should_fire(now_struct, mode):
    hour = now_struct.tm_hour
    if mode == "morning":
        return hour == int(os.environ.get("IRONBUDDY_MORNING_HOUR", "9"))
    if mode == "evening":
        return hour == int(os.environ.get("IRONBUDDY_EVENING_HOUR", "21"))
    if mode == "weekly":
        return now_struct.tm_wday == int(os.environ.get("IRONBUDDY_WEEKLY_DOW", "6")) and hour == int(os.environ.get("IRONBUDDY_WEEKLY_HOUR", "20"))
    return False


def _publish_schedule_only():
    """Write daemon-side schedule to a lightweight file so streamer can echo
    the truth even when no push has happened yet."""
    sched_path = os.path.join(RUNTIME_DIR, "opencloud_schedule.json")
    payload = {
        "weekly_hour": int(os.environ.get("IRONBUDDY_WEEKLY_HOUR", "20")),
        "weekly_dow": int(os.environ.get("IRONBUDDY_WEEKLY_DOW", "6")),
        "morning_hour": int(os.environ.get("IRONBUDDY_MORNING_HOUR", "9")),
        "evening_hour": int(os.environ.get("IRONBUDDY_EVENING_HOUR", "21")),
        "loop_started_at": time.time(),
        "daemon_pid": os.getpid(),
    }
    try:
        _atomic_json(sched_path, payload)
    except Exception as exc:
        logging.warning("publish_schedule failed: %s", exc)


def loop(board_url, dry_run=True, interval=60):
    last_keys = set()
    logging.info("loop start board_url=%s dry_run=%s interval=%ss", board_url, dry_run, interval)
    _publish_schedule_only()
    while True:
        now = time.localtime()
        day_key = time.strftime("%Y-%m-%d", now)
        for mode in ("morning", "evening", "weekly"):
            key = "%s:%s:%02d" % (mode, day_key, now.tm_hour)
            if _should_fire(now, mode) and key not in last_keys:
                run_once(mode, board_url, dry_run=dry_run)
                last_keys.add(key)
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="IronBuddy OpenCloud reminder daemon")
    parser.add_argument("--board-url", default=DEFAULT_BOARD_URL)
    parser.add_argument("--mode", choices=["morning", "evening", "weekly", "auto"], default="auto")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--send", action="store_true", help="actually send Feishu card")
    args = parser.parse_args()
    dry_run = bool(args.dry_run or not args.send)
    if args.loop:
        loop(args.board_url, dry_run=dry_run, interval=args.interval)
        return
    mode = args.mode if args.mode != "auto" else "morning"
    status = run_once(mode, args.board_url, dry_run=dry_run)
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
