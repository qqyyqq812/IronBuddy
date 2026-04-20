#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V5.0 统一种子：5 天时间线 · voice/llm 严格成对 · 用户蓝本去 \\n。

前提：cleanup_fake_data.py 已跑（所有 training/rep/voice/llm 表空）。

时间线（5 天）：
  4/14 周一 晚 19:30 · 深蹲 45 reps · 首次推送（错字"飞速"）
  4/15 周二 晚 19:00 · 深蹲 52 reps · 正常推送（"飞书平台"）
  4/16 周四 下午     · 弯举 38 reps · 首报膝盖疼 + wake_word 自检
  4/19 周日 上午     · 深蹲 60 reps · 膝盖复发 + 疲劳预警 + 总结
  4/20 周一 上午     · 弯举 38 reps · 时间问询 + 上肢计划 + 周总结

所有种子 is_demo_seed=1。
"""
import math
import os
import random
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "ironbuddy.db")


# ============================================================
# 5 天时间线数据
# ============================================================

# 每天的训练会话
TRAINING_SESSIONS = [
    # 4/14 晚 (19:30 - 19:48)
    dict(started_at="2026-04-14 19:30:00", ended_at="2026-04-14 19:48:00",
         exercise="squat", good=33, failed=12, fatigue_peak=1380.0,
         duration_sec=1080),
    # 4/15 晚 (19:00 - 19:22)
    dict(started_at="2026-04-15 19:00:00", ended_at="2026-04-15 19:22:00",
         exercise="squat", good=41, failed=11, fatigue_peak=1460.0,
         duration_sec=1320),
    # 4/16 下午 (15:00 - 15:18)
    dict(started_at="2026-04-16 15:00:00", ended_at="2026-04-16 15:18:00",
         exercise="bicep_curl", good=30, failed=8, fatigue_peak=1120.0,
         duration_sec=1080),
    # 4/19 上午 (10:30 - 10:52)
    dict(started_at="2026-04-19 10:30:00", ended_at="2026-04-19 10:52:00",
         exercise="squat", good=48, failed=12, fatigue_peak=1560.0,
         duration_sec=1320),
    # 4/20 上午 (09:00 - 09:15)
    dict(started_at="2026-04-20 09:00:00", ended_at="2026-04-20 09:15:00",
         exercise="bicep_curl", good=30, failed=8, fatigue_peak=1180.0,
         duration_sec=900),
]


# voice_sessions 蓝本（id 1-10，转录去 \n，其他内容不变）
# 注意：id=12 蓝本 transcript 和 response 不一致（用户编辑痕迹），按用户要求保留
VOICE_ENTRIES = [
    # id 1 · 4/14 19:48 · 首次推送（错字版）
    dict(ts="2026-04-14 19:48:30", trigger_src="chat",
         transcript="我现在做了多少个深蹲，帮我推送飞速",
         response="你今天做了 45 个深蹲（标准 33 / 代偿 12），命中率 73%。"
                  "你说的应该是\"飞书\"吧？已按纠错后推送。",
         summary="首次推送尝试 · 用户说错字\"飞速\"，AI 纠错后执行。",
         duration_s=6.4),
    # id 2 · 4/15 19:22 · 正常推送
    dict(ts="2026-04-15 19:22:30", trigger_src="chat",
         transcript="我现在做了多少个深蹲，帮我推送到飞书平台",
         response="做了 52 个深蹲（标准 41 / 代偿 11），命中率 79%。"
                  "已推送到飞书群。",
         summary="正常推送 · 用户复用句式并修正了错字。",
         duration_s=7.1),
    # id 3 · 4/16 14:58 · wake_word（从蓝本 id=16 挪来，日期对齐到主时间线）
    dict(ts="2026-04-16 14:58:42", trigger_src="wake_word",
         transcript="教练",
         response="在的。",
         summary="唤醒词触发自检测试，无后续对话。",
         duration_s=4.2),
    # id 4 · 4/16 15:22 · 蓝本 id=9（去 \n，内容不变）
    dict(ts="2026-04-16 15:22:10", trigger_src="chat",
         transcript="我的膝盖疼怎么办",
         response="建议立即停止下肢训练，48 小时内冰敷每次 15-20 分钟，"
                  "避免下蹲、爬楼。若持续疼痛或肿胀，尽快咨询骨科或运动"
                  "医学医师。",
         summary="用户首次报告膝盖疼痛，需关注下肢负荷。",
         duration_s=21.0),
    # id 5 · 4/19 11:00 · 蓝本 id=10
    dict(ts="2026-04-19 11:00:55", trigger_src="chat",
         transcript="我的膝盖很疼痛，给出建议",
         response="动态拉伸更适合训前激活（如腿摆/弓步走），静态拉伸放训"
                  "后冷身（每个肌群 30-45 秒，3 组）。重点拉伸大腿后侧、"
                  "髋屈肌和胸大肌。",
         summary="用户咨询拉伸方法，给出动态/静态拉伸搭配方案。",
         duration_s=9.8),
    # id 6 · 4/19 11:45 · 蓝本 id=11
    dict(ts="2026-04-19 11:45:30", trigger_src="fatigue_alert",
         transcript="现在的疲劳值是多少",
         response="你今天深蹲后 emg_rms 平均下降 ~30%（最后 10 reps 比前 "
                  "10 reps 低 28.4%），疲劳累积明显。建议本组结束后休息 5 "
                  "分钟或切换上肢动作。",
         summary="疲劳预警触发，量化报告 EMG 下降幅度并建议轮换。",
         duration_s=20.7),
    # id 7 · 4/19 19:50 · 蓝本 id=12（transcript 与 response 不匹配是你的编辑原样）
    dict(ts="2026-04-19 19:50:18", trigger_src="chat",
         transcript="现在几点了",
         response="今天深蹲量较大（22 min/60 reps），膝盖疼痛已是本周第"
                  "二次。强烈建议明天做恢复日，仅做上肢和核心；下次深蹲前"
                  "增加 5 分钟股四头肌激活与髋外展激活。",
         summary="用户复述膝盖疼痛，AI 给出明日恢复日方案与下次预热建议。",
         duration_s=22.1),
    # id 8 · 4/20 08:50 · 蓝本 id=13
    dict(ts="2026-04-20 08:50:05", trigger_src="chat",
         transcript="现在几点了",
         response="现在是早上 8:50，距离你上次设定的训练时间还有 10 分钟。",
         summary="闲聊式问询当前时间。",
         duration_s=25.0),
    # id 9 · 4/20 09:30 · 蓝本 id=14
    dict(ts="2026-04-20 09:30:40", trigger_src="chat",
         transcript="给出今天的训练计划",
         response="今天严格避开下肢，建议二头弯举 4×12 + 三头下压 3×12 + "
                  "平板支撑 3×45s。膝盖保护今天列为最高优先。",
         summary="膝盖持续疼痛，AI 推荐上肢/核心替代方案。",
         duration_s=11.6),
    # id 10 · 4/20 12:00 · 蓝本 id=15（"推送费"是你加的错字，保留原样）
    dict(ts="2026-04-20 12:00:15", trigger_src="chat",
         transcript="帮我总结一下这周训练。推送费",
         response="本周 3 次训练共 113 reps，命中率 76.9%。两次出现膝盖"
                  "相关诉求（4/16、4/19），4/19 深蹲后 EMG 下降 30%。建议"
                  "下周保持上肢主导、临床评估膝盖。",
         summary="周总结：训练量、命中率、膝盖风险点统一汇报。",
         duration_s=9.8),
]


# llm_log 与 voice_sessions 成对 + 额外独立触发
# trigger 值用 llm_log.trigger 列
LLM_ENTRIES = []
for v in VOICE_ENTRIES:
    # 1:1 镜像 voice_sessions
    trig = "voice_chat" if v["trigger_src"] == "chat" else v["trigger_src"]
    LLM_ENTRIES.append(dict(
        ts=v["ts"], trigger=trig,
        prompt=v["transcript"], response=v["response"],
    ))
# 额外独立 llm_log（非语音来源）
LLM_ENTRIES.extend([
    # 4/19 疲劳补充推理
    dict(ts="2026-04-19 11:30:00", trigger="fatigue",
         prompt="当前疲劳池 1420/1500，是否继续训练？",
         response="建议暂停 2 分钟，避免 emg_rms 继续下降。"),
    # 4/20 周总结内部触发
    dict(ts="2026-04-20 12:05:00", trigger="summary",
         prompt="生成本周训练总结",
         response="训练量 113 reps，命中率 76.9%，膝盖事件 3 次..."),
])


# 每日总结（5 天，daemon 逻辑产出的样子）
DAILY_SUMMARIES = [
    dict(date="2026-04-14", session_count=1, total_good=33, total_failed=12,
         total_fatigue=1380, best_streak=9,
         summary="首次推送尝试（含错字纠错），深蹲 45 reps / 命中率 73%。",
         rec="推送链路已跑通，建议明日继续深蹲测试模型稳定性。"),
    dict(date="2026-04-15", session_count=1, total_good=41, total_failed=11,
         total_fatigue=1460, best_streak=12,
         summary="深蹲 52 reps / 命中率 79%，正常推送飞书成功。",
         rec="良好进步，下次可尝试 60 reps 负荷。"),
    dict(date="2026-04-16", session_count=1, total_good=30, total_failed=8,
         total_fatigue=1120, best_streak=11,
         summary="二头弯举 38 reps / 命中率 79%。用户首次主诉膝盖不适。",
         rec="明日避免深蹲，建议上肢+核心。"),
    dict(date="2026-04-19", session_count=1, total_good=48, total_failed=12,
         total_fatigue=1560, best_streak=14,
         summary="深蹲 60 reps / 命中率 80%，最后 10 reps EMG 下降 30%。"
                 "用户再次提及膝盖痛。",
         rec="建议恢复日或低强度上肢；热身延长股四头激活。"),
    dict(date="2026-04-20", session_count=1, total_good=30, total_failed=8,
         total_fatigue=1180, best_streak=13,
         summary="二头弯举 38 reps / 命中率 79%。膝盖连续第 3 天被提及。",
         rec="本周训练计划调整：避下肢 3 天。建议临床评估。"),
]


# 偏好演化（2 次真实演进）
PREFERENCE_HISTORY = [
    dict(ts="2026-04-16 23:00:00", field="target_muscle_groups",
         old_value=None, new_value="biceps",
         source="llm_inference", confidence=0.65),
    dict(ts="2026-04-19 23:00:00", field="target_muscle_groups",
         old_value="biceps", new_value="biceps,quadriceps",
         source="llm_inference", confidence=0.72),
    dict(ts="2026-04-20 23:00:00", field="knee_caution",
         old_value=None, new_value="true",
         source="llm_inference", confidence=0.88),
    dict(ts="2026-04-20 23:00:00", field="fatigue_tolerance",
         old_value="medium", new_value="medium-low",
         source="llm_inference", confidence=0.75),
]


# 系统提示词版本（v1 通用 → v2 关注膝盖 → v3 个性化）
SYSTEM_PROMPTS = [
    dict(ts="2026-04-14 00:00:00", active=0,
         based_on="[]",
         prompt_text="你是 IronBuddy 健身教练。陪伴用户完成训练，"
                     "给出鼓励与基本动作指导。保持回复简洁（≤2 句），口语化。"),
    dict(ts="2026-04-17 00:00:00", active=0,
         based_on="[3]",
         prompt_text="你是 IronBuddy 健身教练。陪伴用户完成训练，给出鼓励"
                     "与基本动作指导。**新增关注：用户近期主诉膝盖不适，涉"
                     "及下肢动作时主动询问膝盖状态并提示保护。**保持回复简"
                     "洁（≤2 句），口语化。"),
    dict(ts="2026-04-20 00:00:00", active=1,
         based_on="[3,4,5]",
         prompt_text="你是 IronBuddy 健身教练，已根据用户近一周训练数据完"
                     "成个性化适配：\n- 偏好肌群：biceps + quadriceps；\n"
                     "- 疲劳容忍：medium-low（EMG 下降 25% 即提示休息）；\n"
                     "- 健康约束：knee_caution=true，下肢动作前必须确认膝"
                     "盖状态，优先推荐替代上肢/核心方案；\n- 教练风格：鼓"
                     "励为主，量化反馈优先于鸡汤。\n回答简短不超 3 句。"),
]


# 模型注册（只保留 2 个，去掉 yolov8n_pose）
MODELS = [
    dict(
        name="extreme_fusion_gru_curl", exercise="bicep_curl",
        path="models/extreme_fusion_gru_curl.pt",
        arch="CompensationGRU 7D->similarity+3class",
        params_m=0.18, train_acc=1.00, val_acc=1.00, epochs=20,
        dataset="augmented_curl v4.7 33k rows (3 seed × 11 aug)",
        trained_at="2026-04-19T10:20:00", active=1,
        notes="⚠️ val_acc=1.0 有数据泄漏（augment 同源混入 val），"
              "板端现场 A/B 实测补偿",
    ),
    dict(
        name="extreme_fusion_gru_squat", exercise="squat",
        path="models/extreme_fusion_gru_squat.pt",
        arch="CompensationGRU 7D->similarity+3class",
        params_m=0.18, train_acc=0.96, val_acc=0.92, epochs=30,
        dataset="MIA_squat_raw + V3 golden/lazy/bad 手采",
        trained_at="2026-04-18T22:10:00", active=1,
        notes="V3 7D 稳定版，板端 NPU 推理 ~22ms",
    ),
]


# ============================================================
# 灌库
# ============================================================

def gen_rep_events(session_id, started_at, duration_sec, good, failed,
                   exercise, seed):
    """均匀分布 rep 时间戳，给合理的 angle_min / emg_target / emg_comp。"""
    rng = random.Random(seed)
    total = good + failed
    from datetime import datetime, timedelta
    start = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
    rows = []
    # 好坏打乱但整体 bad 偏向后半（模拟疲劳）
    good_ts = sorted(rng.sample(range(total), good))
    is_good_seq = [0] * total
    for t in good_ts:
        is_good_seq[t] = 1
    for i in range(total):
        ts = start + timedelta(seconds=int(duration_sec * (i + 0.5) / total))
        is_good = is_good_seq[i]
        if exercise == "squat":
            ang_min = rng.uniform(95, 110) if is_good else rng.uniform(115, 135)
        else:  # bicep_curl
            ang_min = rng.uniform(45, 60) if is_good else rng.uniform(70, 95)
        # good: target RMS 高，comp RMS 低（目标肌发力）
        # bad: target RMS 低，comp RMS 高（代偿）
        if is_good:
            target = rng.uniform(0.55, 0.85)
            comp = rng.uniform(0.20, 0.45)
        else:
            target = rng.uniform(0.25, 0.50)
            comp = rng.uniform(0.50, 0.85)
        rows.append((
            session_id,
            ts.strftime("%Y-%m-%dT%H:%M:%S"),
            is_good,
            round(ang_min, 2),
            round(target, 3),
            round(comp, 3),
            1,  # is_demo_seed
        ))
    return rows


def run():
    if not os.path.exists(DB):
        print("DB 不存在:", DB); sys.exit(1)
    bak = DB + ".bak_" + str(int(time.time()))
    shutil.copy2(DB, bak)
    print("备份:", bak)

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # 1. training_sessions
    for i, s in enumerate(TRAINING_SESSIONS):
        cur.execute(
            "INSERT INTO training_sessions (started_at, ended_at, exercise, "
            "good_count, failed_count, fatigue_peak, duration_sec, "
            "is_demo_seed) VALUES (?,?,?,?,?,?,?,1)",
            (s["started_at"], s["ended_at"], s["exercise"],
             s["good"], s["failed"], s["fatigue_peak"], s["duration_sec"]),
        )
    conn.commit()

    # 2. rep_events（按 session 生成）
    for i, s in enumerate(TRAINING_SESSIONS):
        sid = cur.execute(
            "SELECT id FROM training_sessions WHERE started_at=?",
            (s["started_at"],)
        ).fetchone()[0]
        rows = gen_rep_events(sid, s["started_at"], s["duration_sec"],
                              s["good"], s["failed"], s["exercise"],
                              seed=100 + i)
        cur.executemany(
            "INSERT INTO rep_events (session_id, ts, is_good, angle_min, "
            "emg_target, emg_comp, is_demo_seed) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
    conn.commit()

    # 3. voice_sessions（10 条）
    for v in VOICE_ENTRIES:
        cur.execute(
            "INSERT INTO voice_sessions (ts, transcript, response, summary, "
            "duration_s, trigger_src, is_demo_seed) VALUES (?,?,?,?,?,?,1)",
            (v["ts"], v["transcript"], v["response"], v["summary"],
             v["duration_s"], v["trigger_src"]),
        )
    conn.commit()

    # 4. llm_log（成对 + 额外独立）
    for l in LLM_ENTRIES:
        cur.execute(
            "INSERT INTO llm_log (ts, trigger, prompt, response, tokens_in, "
            "tokens_out, is_demo_seed) VALUES (?,?,?,?,0,0,1)",
            (l["ts"], l["trigger"], l["prompt"], l["response"]),
        )
    conn.commit()

    # 5. daily_summary
    for d in DAILY_SUMMARIES:
        cur.execute(
            "INSERT OR REPLACE INTO daily_summary (date, session_count, "
            "total_good, total_failed, total_fatigue, best_streak, "
            "summary, rec, is_demo_seed) VALUES (?,?,?,?,?,?,?,?,1)",
            (d["date"], d["session_count"], d["total_good"], d["total_failed"],
             d["total_fatigue"], d["best_streak"], d["summary"], d["rec"]),
        )
    conn.commit()

    # 6. preference_history
    for p in PREFERENCE_HISTORY:
        cur.execute(
            "INSERT INTO preference_history (ts, field, old_value, new_value, "
            "source, confidence, is_demo_seed) VALUES (?,?,?,?,?,?,1)",
            (p["ts"], p["field"], p["old_value"], p["new_value"],
             p["source"], p["confidence"]),
        )
    conn.commit()

    # 7. system_prompt_versions（最后一版 active=1）
    for sp in SYSTEM_PROMPTS:
        cur.execute(
            "INSERT INTO system_prompt_versions (ts, prompt_text, "
            "based_on_summary_ids, active, is_demo_seed) VALUES (?,?,?,?,1)",
            (sp["ts"], sp["prompt_text"], sp["based_on"], sp["active"]),
        )
    # 同步 user_config.last_prompt_version
    active_id = cur.execute(
        "SELECT id FROM system_prompt_versions WHERE active=1 "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    for k in ("last_prompt_version", "user_preference.last_prompt_version"):
        cur.execute(
            "INSERT OR REPLACE INTO user_config (key, value, updated_at) "
            "VALUES (?,?,?)",
            (k, str(active_id), "2026-04-20 23:00:00"),
        )
    conn.commit()

    # 8. model_registry
    for m in MODELS:
        p = os.path.join(ROOT, m["path"])
        size_kb = round(os.path.getsize(p) / 1024.0, 1) \
            if os.path.exists(p) else None
        cur.execute(
            "INSERT INTO model_registry (name, exercise, path, arch, "
            "params_m, size_kb, train_acc, val_acc, epochs, dataset, "
            "trained_at, active, notes, is_demo_seed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (m["name"], m["exercise"], m["path"], m["arch"], m["params_m"],
             size_kb, m["train_acc"], m["val_acc"], m["epochs"], m["dataset"],
             m["trained_at"], m["active"], m["notes"]),
        )
    conn.commit()

    # ============= 自检 =============
    print("\n--- 自检 ---")
    for tbl in ("training_sessions", "rep_events", "voice_sessions",
                "llm_log", "daily_summary", "preference_history",
                "system_prompt_versions", "model_registry"):
        n = cur.execute("SELECT COUNT(*) FROM " + tbl).fetchone()[0]
        print("  %-26s %d" % (tbl, n))

    print("\n  --- voice_sessions / llm_log 成对检查 ---")
    pairs = cur.execute(
        "SELECT v.ts, v.trigger_src, COUNT(l.id) "
        "FROM voice_sessions v LEFT JOIN llm_log l ON v.ts=l.ts "
        "GROUP BY v.id ORDER BY v.ts"
    ).fetchall()
    for p in pairs:
        print("   ", p[0], p[1], "-> llm rows:", p[2])

    print("\n  --- 时间线 ---")
    for r in cur.execute(
        "SELECT date(started_at) d, exercise, good_count, failed_count "
        "FROM training_sessions ORDER BY started_at"
    ):
        print("   ", r)

    conn.close()
    print("\n✓ V5.0 seed 落库完成。")


if __name__ == "__main__":
    run()
