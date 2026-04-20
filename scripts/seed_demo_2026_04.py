#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
seed_demo_2026_04.py
=====================
IronBuddy 演示种子数据灌入脚本（2026-04-20）。

功能：
  1. 备份现有数据库到 data/ironbuddy.db.bak_<unix_ts>。
  2. 执行 migrate_2026_04_20.sql 中的 CREATE TABLE（幂等）。
  3. 探测并补齐 is_demo_seed / summary / rec / prompt_version_id 列（避免重复 ALTER）。
  4. 清掉旧种子（is_demo_seed=1）后重新灌入：
       - 3 条 training_sessions
       - 143 条 rep_events
       - 10 条 llm_log（4/16 ×3 + 4/19 ×4 + 4/20 ×3）
       - 8  条 voice_sessions（7 条对应 chat 类 llm_log + 1 条 wake_word 测试）
       - 3  条 daily_summary
       - 5  条 preference_history
       - 3  条 system_prompt_versions（v3 active=1）
  5. 同步 user_config：last_prompt_version=3 等键。

约束：
  * 所有种子行 is_demo_seed=1（system_prompt_versions / preference_history / voice_sessions 同样标 1）。
  * 仅依赖 stdlib（sqlite3 / random / datetime / shutil / pathlib）。
  * 幂等：重复运行先 DELETE 再 INSERT。
  * cleanup 由 cleanup_demo_seed.py 一键回滚。
"""

import os
import shutil
import sqlite3
import random
import time
import json
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "ironbuddy.db"
SQL_PATH = ROOT / "scripts" / "migrate_2026_04_20.sql"

random.seed(20260420)  # 可复现

# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------

def backup(db_path: Path) -> Path:
    ts = int(time.time())
    bak = db_path.with_suffix(db_path.suffix + f".bak_{ts}")
    shutil.copy2(db_path, bak)
    print(f"[backup] {db_path} -> {bak}")
    return bak


def column_exists(cur, table: str, col: str) -> bool:
    cur.execute(f"PRAGMA table_info({table});")
    return any(row[1] == col for row in cur.fetchall())


def ensure_column(cur, table: str, col: str, decl: str):
    if not column_exists(cur, table, col):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl};")
        print(f"[alter] {table} ADD COLUMN {col} {decl}")
    else:
        print(f"[alter] {table}.{col} already exists, skip")


# ----------------------------------------------------------------------
# Schema 升级
# ----------------------------------------------------------------------

def run_migration(conn: sqlite3.Connection):
    cur = conn.cursor()
    sql = SQL_PATH.read_text(encoding="utf-8")
    cur.executescript(sql)

    # 给现有表补 is_demo_seed
    for t in ("training_sessions", "rep_events", "llm_log", "daily_summary"):
        ensure_column(cur, t, "is_demo_seed", "INTEGER DEFAULT 0")

    # daily_summary 业务列：summary / rec
    ensure_column(cur, "daily_summary", "summary", "TEXT")
    ensure_column(cur, "daily_summary", "rec", "TEXT")

    # llm_log 关联 prompt 版本
    ensure_column(cur, "llm_log", "prompt_version_id", "INTEGER")

    conn.commit()


# ----------------------------------------------------------------------
# 清理旧种子（幂等保障）
# ----------------------------------------------------------------------

def wipe_old_seed(conn: sqlite3.Connection):
    cur = conn.cursor()
    # 先清子表（rep_events 通过 session_id 关联，但用 is_demo_seed 直接清更稳）
    targets = [
        "rep_events", "training_sessions", "llm_log", "daily_summary",
        "voice_sessions", "preference_history", "system_prompt_versions",
    ]
    for t in targets:
        try:
            cur.execute(f"DELETE FROM {t} WHERE is_demo_seed=1;")
            print(f"[wipe] {t}: deleted {cur.rowcount} old seed rows")
        except sqlite3.OperationalError as e:
            print(f"[wipe] {t}: skip ({e})")
    conn.commit()


# ----------------------------------------------------------------------
# 种子数据生成器
# ----------------------------------------------------------------------

# 三场训练参数：started_at / duration_min / exercise / good / bad / fatigue_peak
TRAINING_SPECS = [
    {
        "started_at": "2026-04-16 09:15:00",
        "duration_min": 18,
        "exercise": "bicep_curl",
        "good": 32,
        "bad": 13,
        "fatigue_peak": 1420.0,
    },
    {
        "started_at": "2026-04-19 10:30:00",
        "duration_min": 22,
        "exercise": "squat",
        "good": 48,
        "bad": 12,
        "fatigue_peak": 1560.0,
    },
    {
        "started_at": "2026-04-20 09:00:00",
        "duration_min": 15,
        "exercise": "bicep_curl",
        "good": 30,
        "bad": 8,
        "fatigue_peak": 1380.0,
    },
]

# bicep_curl 角度（度）：good 区间 45~155；squat 区间 95~175
ANGLE_RANGES = {
    "bicep_curl": (45.0, 155.0),
    "squat": (95.0, 175.0),
}


def gen_session_rows(spec):
    """returns dict ready for insert into training_sessions."""
    started = datetime.fromisoformat(spec["started_at"])
    ended = started + timedelta(minutes=spec["duration_min"])
    return {
        "started_at": spec["started_at"],
        "ended_at": ended.isoformat(sep=" "),
        "exercise": spec["exercise"],
        "good_count": spec["good"],
        "failed_count": spec["bad"],
        "fatigue_peak": spec["fatigue_peak"],
        "duration_sec": spec["duration_min"] * 60,
        "is_demo_seed": 1,
    }


def gen_rep_events(session_id: int, spec: dict):
    """生成 reps：good + bad，按时间均匀分布，emg 在 0.3-0.9。"""
    started = datetime.fromisoformat(spec["started_at"])
    total = spec["good"] + spec["bad"]
    duration_s = spec["duration_min"] * 60
    a_lo, a_hi = ANGLE_RANGES[spec["exercise"]]
    rows = []
    # 生成 quality 序列：good 集中在前段，bad 集中在后段（更真实）
    quality_seq = [1] * spec["good"] + [0] * spec["bad"]
    # 后半段插入更多 bad：先按 70/30 分前后两段交错
    qs_front = quality_seq[: int(total * 0.6)]
    qs_back = quality_seq[int(total * 0.6) :]
    random.shuffle(qs_front)
    random.shuffle(qs_back)
    quality_seq = qs_front + qs_back

    # 时间间隔：均匀 18~25s，最后再缩放贴合 duration_s
    raw_intervals = [random.uniform(18, 25) for _ in range(total)]
    scale = duration_s / sum(raw_intervals)
    intervals = [iv * scale for iv in raw_intervals]

    cursor_t = started
    for i, is_good in enumerate(quality_seq):
        cursor_t = cursor_t + timedelta(seconds=intervals[i])
        if is_good:
            angle_min = round(random.uniform(a_lo, a_lo + 15), 1)
            emg_target = round(random.uniform(0.55, 0.85), 3)
            emg_comp = round(random.uniform(0.30, 0.55), 3)
        else:
            # bad：要么 ROM 不足要么动作过快 → angle 偏大（未到底）或 emg 失衡
            angle_min = round(random.uniform(a_lo + 25, a_hi - 20), 1)
            emg_target = round(random.uniform(0.30, 0.55), 3)
            emg_comp = round(random.uniform(0.50, 0.90), 3)
        rows.append((
            session_id,
            cursor_t.isoformat(sep=" ", timespec="seconds"),
            int(is_good),
            angle_min,
            emg_target,
            emg_comp,
            1,  # is_demo_seed
        ))
    return rows


# ----------------------------------------------------------------------
# llm_log 种子（10 条）
# ----------------------------------------------------------------------

LLM_LOG_SEEDS = [
    # 4/16
    {
        "ts": "2026-04-16 15:22:10",
        "trigger": "chat",
        "prompt": "我的膝盖疼怎么办？",
        "response": "建议立即停止下肢训练，48 小时内冰敷每次 15-20 分钟，避免下蹲、爬楼。若持续疼痛或肿胀，尽快咨询骨科或运动医学医师。",
        "tokens_in": 18,
        "tokens_out": 86,
        "voice": True,
        "voice_summary": "用户首次报告膝盖疼痛，给出 RICE 原则与就医建议。",
    },
    {
        "ts": "2026-04-16 16:05:43",
        "trigger": "debug",
        "prompt": "FSM 为啥一直卡在 IDLE 状态？",
        "response": "检查 wake_word 触发流：1) VAD 阈值是否过高；2) `chat_session_active` 标志是否在上次结束时正确清零；3) 看 logs/openclaw_fsm.log 末尾 30 行有无 transition 拒绝。",
        "tokens_in": 24,
        "tokens_out": 102,
        "voice": False,
    },
    {
        "ts": "2026-04-16 16:30:12",
        "trigger": "debug",
        "prompt": "VAD 阈值调到多少合理？",
        "response": "在安静环境下推荐 0.55-0.65；嘈杂环境（健身房）建议 0.70+ 并配合 noise_gate=2。可观察 vad_score 直方图微调。",
        "tokens_in": 16,
        "tokens_out": 64,
        "voice": False,
    },
    # 4/19
    {
        "ts": "2026-04-19 11:00:55",
        "trigger": "chat",
        "prompt": "怎样进行高效的拉伸动作？",
        "response": "动态拉伸更适合训前激活（如腿摆/弓步走），静态拉伸放训后冷身（每个肌群 30-45 秒，3 组）。重点拉伸大腿后侧、髋屈肌和胸大肌。",
        "tokens_in": 14,
        "tokens_out": 88,
        "voice": True,
        "voice_summary": "用户咨询拉伸方法，给出动态/静态拉伸搭配方案。",
    },
    {
        "ts": "2026-04-19 11:45:30",
        "trigger": "fatigue_alert",
        "prompt": "现在的疲劳值是多少？",
        "response": "你今天深蹲后 emg_rms 平均下降 ~30%（最后 10 reps 比前 10 reps 低 28.4%），疲劳累积明显。建议本组结束后休息 5 分钟或切换上肢动作。",
        "tokens_in": 12,
        "tokens_out": 92,
        "voice": True,
        "voice_summary": "疲劳预警触发，量化报告 EMG 下降幅度并建议轮换。",
    },
    {
        "ts": "2026-04-19 14:20:22",
        "trigger": "debug",
        "prompt": "rep 计数怎么又丢了？",
        "response": "排查 angle_extrema 检测：本次最后 5 reps 角度峰值未越过 down_threshold=110°（实测 113-117°）。建议把 down_threshold 从 110 调到 118，或检查关键点稳定性。",
        "tokens_in": 14,
        "tokens_out": 95,
        "voice": False,
    },
    {
        "ts": "2026-04-19 19:50:18",
        "trigger": "chat",
        "prompt": "深蹲完膝盖又疼了。",
        "response": "今天深蹲量较大（22 min/60 reps），膝盖疼痛已是本周第二次。强烈建议明天做恢复日，仅做上肢和核心；下次深蹲前增加 5 分钟股四头肌激活与髋外展激活。",
        "tokens_in": 16,
        "tokens_out": 110,
        "voice": True,
        "voice_summary": "用户复述膝盖疼痛，AI 给出明日恢复日方案与下次预热建议。",
    },
    # 4/20
    {
        "ts": "2026-04-20 08:50:05",
        "trigger": "chat",
        "prompt": "现在几点了？",
        "response": "现在是早上 8:50，距离你上次设定的训练时间还有 10 分钟。",
        "tokens_in": 8,
        "tokens_out": 24,
        "voice": True,
        "voice_summary": "闲聊式问询当前时间。",
    },
    {
        "ts": "2026-04-20 09:30:40",
        "trigger": "chat",
        "prompt": "膝盖还是疼，今天该练什么？",
        "response": "今天严格避开下肢，建议二头弯举 4×12 + 三头下压 3×12 + 平板支撑 3×45s。膝盖保护今天列为最高优先。",
        "tokens_in": 16,
        "tokens_out": 78,
        "voice": True,
        "voice_summary": "膝盖持续疼痛，AI 推荐上肢/核心替代方案。",
    },
    {
        "ts": "2026-04-20 12:00:15",
        "trigger": "chat",
        "prompt": "帮我总结一下这周训练。",
        "response": "本周 3 次训练共 113 reps，命中率 76.9%。两次出现膝盖相关诉求（4/16、4/19），4/19 深蹲后 EMG 下降 30%。建议下周保持上肢主导、临床评估膝盖。",
        "tokens_in": 12,
        "tokens_out": 124,
        "voice": True,
        "voice_summary": "周总结：训练量、命中率、膝盖风险点统一汇报。",
    },
]

# 额外 wake_word 测试（凑够 voice_sessions=8）
EXTRA_WAKE_WORD = {
    "ts": "2026-04-18 19:42:33",
    "transcript": "嘿小铁。",
    "response": "在的。",
    "summary": "唤醒词触发自检测试，无后续对话。",
    "duration_s": 4.2,
    "trigger_src": "wake_word",
}


# ----------------------------------------------------------------------
# daily_summary（3 条）
# ----------------------------------------------------------------------

DAILY_SUMMARY_SEEDS = [
    {
        "date": "2026-04-16",
        "session_count": 1,
        "total_good": 32,
        "total_failed": 13,
        "total_fatigue": 1420.0,
        "best_streak": 9,
        "summary": "完成 1 次二头弯举训练（18min/45reps）。命中率 71%，次优 reps 集中在后半程，疑似握距不稳。用户首次主诉膝盖不适。",
        "rec": "明日避免深蹲，建议上肢+核心。",
    },
    {
        "date": "2026-04-19",
        "session_count": 1,
        "total_good": 48,
        "total_failed": 12,
        "total_fatigue": 1560.0,
        "best_streak": 14,
        "summary": "深蹲训练 22min/60reps，命中率 80%，最后 10 reps EMG 明显下降，疲劳累积。用户再次提及膝盖痛。",
        "rec": "建议恢复日或低强度上肢；热身时延长股四头肌激活。",
    },
    {
        "date": "2026-04-20",
        "session_count": 1,
        "total_good": 30,
        "total_failed": 8,
        "total_fatigue": 1380.0,
        "best_streak": 11,
        "summary": "二头弯举 15min/38reps，命中率 79%。连续第 3 天出现膝盖相关诉求，建议临床评估。",
        "rec": "本周训练计划调整：避下肢 3 天。",
    },
]


# ----------------------------------------------------------------------
# preference_history（5 条）
# ----------------------------------------------------------------------

PREFERENCE_HISTORY_SEEDS = [
    ("2026-04-16 23:00:00", "target_muscle_groups", None,      "biceps",            "llm_inference", 0.65),
    ("2026-04-19 23:00:00", "target_muscle_groups", "biceps",  "biceps,quadriceps", "llm_inference", 0.72),
    ("2026-04-19 23:00:00", "fatigue_tolerance",    "medium",  "medium",            "llm_inference", 0.80),
    ("2026-04-20 23:00:00", "knee_caution",          None,      "true",              "llm_inference", 0.88),
    ("2026-04-20 23:00:00", "fatigue_tolerance",    "medium",  "medium-low",        "llm_inference", 0.75),
]


# ----------------------------------------------------------------------
# system_prompt_versions（3 版）
# ----------------------------------------------------------------------

PROMPT_V1 = (
    "你是 IronBuddy 健身教练。陪伴用户完成训练，给出鼓励与基本动作指导。"
    "保持回复简洁（≤2 句），口语化。"
)

PROMPT_V2 = (
    "你是 IronBuddy 健身教练。陪伴用户完成训练，给出鼓励与基本动作指导。"
    "**新增关注：用户近期主诉膝盖不适，涉及下肢动作时主动询问膝盖状态并提示保护。**"
    "保持回复简洁（≤2 句），口语化。"
)

PROMPT_V3 = (
    "你是 IronBuddy 健身教练，已根据用户近一周训练数据完成个性化适配：\n"
    "- 偏好肌群：biceps + quadriceps；\n"
    "- 疲劳容忍：medium-low（EMG 下降 25% 即提示休息）；\n"
    "- 健康约束：knee_caution=true，下肢动作前必须确认膝盖状态，优先推荐替代上肢/核心方案；\n"
    "- 教练风格：鼓励为主，量化反馈优先于鸡汤。\n"
    "回复 ≤3 句，关键数据用 EMG/角度/reps 数字佐证。"
)

PROMPT_VERSIONS = [
    ("2026-04-16 00:00:00", PROMPT_V1, None,     0),
    ("2026-04-17 00:00:00", PROMPT_V2, "[1]",    0),
    ("2026-04-20 00:00:00", PROMPT_V3, "[1,2,3]", 1),
]


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------

def main():
    if not DB_PATH.exists():
        raise SystemExit(f"DB not found: {DB_PATH}")

    backup(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=OFF;")
    try:
        # 1. 迁移
        run_migration(conn)
        # 2. 清旧种子
        wipe_old_seed(conn)

        cur = conn.cursor()

        # 3. training_sessions + rep_events
        session_ids = []
        rep_total = 0
        for spec in TRAINING_SPECS:
            row = gen_session_rows(spec)
            cur.execute(
                """
                INSERT INTO training_sessions
                  (started_at, ended_at, exercise, good_count, failed_count,
                   fatigue_peak, duration_sec, is_demo_seed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (row["started_at"], row["ended_at"], row["exercise"],
                 row["good_count"], row["failed_count"], row["fatigue_peak"],
                 row["duration_sec"], row["is_demo_seed"]),
            )
            sid = cur.lastrowid
            session_ids.append(sid)
            reps = gen_rep_events(sid, spec)
            cur.executemany(
                """
                INSERT INTO rep_events
                  (session_id, ts, is_good, angle_min, emg_target, emg_comp, is_demo_seed)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                reps,
            )
            rep_total += len(reps)
            print(f"[seed] training_session id={sid} ({spec['exercise']}, {len(reps)} reps)")

        print(f"[seed] training_sessions=3, rep_events total={rep_total}")

        # 4. llm_log（先全部 insert，prompt_version_id 占位 NULL，稍后 UPDATE）
        llm_ids = []
        for entry in LLM_LOG_SEEDS:
            cur.execute(
                """
                INSERT INTO llm_log
                  (ts, trigger, prompt, response, tokens_in, tokens_out,
                   is_demo_seed, prompt_version_id)
                VALUES (?, ?, ?, ?, ?, ?, 1, NULL)
                """,
                (entry["ts"], entry["trigger"], entry["prompt"], entry["response"],
                 entry["tokens_in"], entry["tokens_out"]),
            )
            llm_ids.append(cur.lastrowid)
        print(f"[seed] llm_log inserted={len(llm_ids)}")

        # 5. voice_sessions：来自 LLM_LOG_SEEDS 中 voice=True 的 + 1 条 wake_word
        voice_count = 0
        for entry in LLM_LOG_SEEDS:
            if not entry.get("voice"):
                continue
            ts_dt = datetime.fromisoformat(entry["ts"])
            duration = round(random.uniform(8.0, 25.0), 1)
            cur.execute(
                """
                INSERT INTO voice_sessions
                  (ts, transcript, response, summary, duration_s, trigger_src, is_demo_seed)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (entry["ts"], entry["prompt"], entry["response"],
                 entry["voice_summary"], duration, entry["trigger"]),
            )
            voice_count += 1
        # extra wake_word
        cur.execute(
            """
            INSERT INTO voice_sessions
              (ts, transcript, response, summary, duration_s, trigger_src, is_demo_seed)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (EXTRA_WAKE_WORD["ts"], EXTRA_WAKE_WORD["transcript"], EXTRA_WAKE_WORD["response"],
             EXTRA_WAKE_WORD["summary"], EXTRA_WAKE_WORD["duration_s"], EXTRA_WAKE_WORD["trigger_src"]),
        )
        voice_count += 1
        print(f"[seed] voice_sessions inserted={voice_count}")

        # 6. daily_summary
        for d in DAILY_SUMMARY_SEEDS:
            cur.execute(
                """
                INSERT OR REPLACE INTO daily_summary
                  (date, session_count, total_good, total_failed, total_fatigue,
                   best_streak, summary, rec, is_demo_seed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (d["date"], d["session_count"], d["total_good"], d["total_failed"],
                 d["total_fatigue"], d["best_streak"], d["summary"], d["rec"]),
            )
        print(f"[seed] daily_summary inserted={len(DAILY_SUMMARY_SEEDS)}")

        # 7. preference_history
        for ts, field, old, new, src, conf in PREFERENCE_HISTORY_SEEDS:
            cur.execute(
                """
                INSERT INTO preference_history
                  (ts, field, old_value, new_value, source, confidence, is_demo_seed)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (ts, field, old, new, src, conf),
            )
        print(f"[seed] preference_history inserted={len(PREFERENCE_HISTORY_SEEDS)}")

        # 8. system_prompt_versions（先把现有 active 全部清 0，避免重复 active）
        cur.execute("UPDATE system_prompt_versions SET active=0;")
        prompt_ids = []
        for ts, text, basis, active in PROMPT_VERSIONS:
            cur.execute(
                """
                INSERT INTO system_prompt_versions
                  (ts, prompt_text, based_on_summary_ids, active, is_demo_seed)
                VALUES (?, ?, ?, ?, 1)
                """,
                (ts, text, basis, active),
            )
            prompt_ids.append(cur.lastrowid)
        print(f"[seed] system_prompt_versions inserted={len(prompt_ids)} (ids={prompt_ids})")

        v1_id, v2_id, v3_id = prompt_ids

        # 9. UPDATE llm_log 关联 prompt_version_id：4/16→v1, 4/19→v2, 4/20→v3
        cur.execute(
            "UPDATE llm_log SET prompt_version_id=? WHERE is_demo_seed=1 AND ts LIKE '2026-04-16%';",
            (v1_id,),
        )
        cur.execute(
            "UPDATE llm_log SET prompt_version_id=? WHERE is_demo_seed=1 AND ts LIKE '2026-04-19%';",
            (v2_id,),
        )
        cur.execute(
            "UPDATE llm_log SET prompt_version_id=? WHERE is_demo_seed=1 AND ts LIKE '2026-04-20%';",
            (v3_id,),
        )

        # 10. user_config 扩展 + last_prompt_version
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        new_config = [
            ("user_preference.fatigue_tolerance",   "medium",                     now),
            ("user_preference.target_muscle_groups", "biceps,quadriceps",         now),
            ("user_preference.knee_caution",         "true",                      now),
            ("system.last_prompt_version",           str(v3_id),                  now),
        ]
        for k, v, t in new_config:
            cur.execute(
                "INSERT OR REPLACE INTO user_config (key, value, updated_at) VALUES (?, ?, ?);",
                (k, v, t),
            )
        # 任务原文写的 key 也兼容写一份（不带 user_preference. 前缀）
        for k, v in [
            ("fatigue_tolerance", "medium"),
            ("target_muscle_groups", "biceps,quadriceps"),
            ("knee_caution", "true"),
            ("last_prompt_version", str(v3_id)),
        ]:
            cur.execute(
                "INSERT OR REPLACE INTO user_config (key, value, updated_at) VALUES (?, ?, ?);",
                (k, v, now),
            )

        conn.commit()
        print("[seed] DONE")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
