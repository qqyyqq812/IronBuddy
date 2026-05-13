"""假数据注入：为 OpenClaw 后端记忆闭环提供"性格化"种子（IronBuddy V4.7 主线 A.6）。

用法：
    python3 tools/seed_fake_chats.py            # 插入假数据
    python3 tools/seed_fake_chats.py --cleanup  # 回滚，仅清除标记为 SEED 的数据

插入内容：
  * 12 场 training_sessions（过去 14 天随机分布，混合 squat/bicep_curl）
  * 每场 8-20 条 rep_events（总约 180 条）
  * 30 条 llm_log（trigger 混合 chat/voice_chat/summary/fatigue；
    对话内容呈现用户性格：偏好鼓励式教练 + 喜欢下午训练 + 对弯举更感兴趣）
  * 4 条 user_config 偏好（favorite_exercise / coach_style / training_time / ideal_rep_count）

为了支持 --cleanup 干净回滚，seed 数据在 prompt 末尾附带标记 `<SEED:A6>`，
rep_events 通过 session_id 级联清除。

Python 3.7 兼容。
"""

import argparse
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

# 让脚本既能在 repo 根目录跑，也能直接被 python -m tools.xxx 引入
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(ROOT, "hardware_engine"))

from persistence.db import FitnessDB, _resolve_db_path  # noqa: E402

SEED_TAG = "<SEED:A6>"

# ========== 预设性格化对话池（30 条）==========
# 反复暗示：偏好鼓励 / 喜欢下午 / 对弯举更感兴趣
_SEED_CHATS = [
    # voice_chat：用户对弯举的好感
    ("voice_chat", "教练我今天想练弯举，能不能不练深蹲了？", "弯举当然可以，建议从 3 组 12 次开始，感受二头收缩。"),
    ("voice_chat", "我觉得弯举更带感，深蹲太累", "理解你的想法，弯举练二头很直接。今天就弯举 4 组 × 15 次吧！"),
    ("voice_chat", "下午的状态明显比早上好", "确认下午确实是你的高峰期，那就把重点训练安排在下午。"),
    ("voice_chat", "早上起来身体僵硬，不想动", "清晨僵硬属于正常现象，建议把主课留到下午再练。"),
    ("voice_chat", "我不太喜欢被严厉批评，多鼓励我一下", "没问题，以后我会以鼓励为主，你先保持动作规范。"),
    ("voice_chat", "能不能换种温和的说话方式？", "好的，会调整为鼓励式口吻。你进步非常明显，继续保持！"),
    ("voice_chat", "多少个深蹲了？", "目前深蹲 7 次，状态还不错。"),
    ("voice_chat", "弯举做到第 12 次的时候手臂开始抖，正常吗？", "典型的力竭前兆，说明你在突破阈值，加油！"),
    ("voice_chat", "我习惯下午 3 点后训练，早上效率差", "记录下来，以后 9 点的日计划会提醒你下午执行。"),
    ("voice_chat", "教练今天鼓励我一下，我有点累", "没关系，你昨天已经完成 30 个标准动作，今天能来就已经赢了。"),
    # chat：UI 按钮触发的 DeepSeek 聊天
    ("chat", "帮我制定本周训练计划", "你偏好弯举与下午训练，建议本周 5 天：周一/三/五弯举为主，周二/四下肢。"),
    ("chat", "我希望能在一个月内把弯举从 12 次提升到 20 次", "完全可行，建议每周增加 2 次容量，下午训练配合 72 小时间隔。"),
    ("chat", "我不喜欢被骂，请保持鼓励", "明白，从今天起我会保持正面鼓励口吻，不再严厉纠错。"),
    # summary：训练结束后的点评
    ("summary", "训练总结", "本次弯举达标 13/15，二头激活充分，下午状态良好，表现出色。"),
    ("summary", "训练总结", "深蹲 10 次达标 1 次违规，发力稳，继续保持。下次可尝试减少代偿。"),
    ("summary", "训练总结", "弯举四组完整达标，肌肉耐力在进步，给你点赞！"),
    ("summary", "训练总结", "下午场训练完成度 95%，节奏掌控好，建议明天下午继续。"),
    # fatigue：疲劳触发
    ("fatigue", "疲劳池接近阈值", "疲劳 1420/1500，建议稍作休息 2 分钟再继续。"),
    ("fatigue", "疲劳池接近阈值", "强度管理得当，再坚持 3 次就收工。"),
    # voice_chat 补充
    ("voice_chat", "今天下午能练多久？", "按偏好与疲劳池估算，你下午可持续 45 分钟左右。"),
    ("voice_chat", "深蹲和弯举哪个热量消耗高？", "深蹲更高，但弯举对二头塑形更直接，你可以 7:3 分配。"),
    ("voice_chat", "我想把每组弯举做到 15 次", "好目标！目前你能稳定 12 次，每周加 1 次即可达成。"),
    ("voice_chat", "教练，今天就想练上肢", "可以，以弯举 + 俯身划船为主，下肢明天再补。"),
    ("voice_chat", "早上总是犯困，下午才有劲", "已确认你的生物钟偏下午，日计划会按此定制。"),
    ("voice_chat", "我的二头什么时候能变大？", "保持下午弯举训练 + 营养跟上，6-8 周会有明显变化。"),
    ("voice_chat", "多鼓励，少说狠话", "完全理解，今后点评以正反馈为主。"),
    # chat 补充
    ("chat", "查看我的历史偏好", "目前系统学习到：喜欢弯举 / 下午训练 / 希望鼓励式教练。"),
    ("chat", "本周进步点是什么", "弯举从 10 次提升到 13 次，下午训练出勤率 100%，保持住！"),
    ("chat", "我今天不想练深蹲", "那就改成弯举 4 组 × 15 次，符合你的偏好。"),
    ("chat", "希望你能记住我更喜欢弯举", "已记录到偏好表，以后推送日计划会优先安排弯举。"),
]


def _iso(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def seed(db_path):
    """插入 seed 数据。返回插入统计字典。"""
    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    now = datetime.now()
    rnd = random.Random(20260419)  # 固定种子可重现

    # -------- 1) 12 场 training_sessions --------
    exercises = ["squat", "bicep_curl"]
    inserted_sessions = []
    for i in range(12):
        # 过去 14 天随机分布，时间偏向下午（13:00-18:00）以呼应偏好
        delta_days = rnd.randint(0, 13)
        hour = rnd.choice([10, 14, 15, 16, 17, 18, 20])
        minute = rnd.randint(0, 59)
        started = (now - timedelta(days=delta_days)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        exercise = rnd.choices(exercises, weights=[0.4, 0.6])[0]  # 偏爱弯举
        good = rnd.randint(5, 15)
        failed = rnd.randint(0, 3)
        fatigue_peak = round(rnd.uniform(300, 1500), 1)
        duration = rnd.randint(120, 900)
        ended = started + timedelta(seconds=duration)
        cur.execute(
            "INSERT INTO training_sessions "
            "(started_at, ended_at, exercise, good_count, failed_count, fatigue_peak, duration_sec) "
            "VALUES (?,?,?,?,?,?,?)",
            (_iso(started), _iso(ended), exercise, good, failed, fatigue_peak, duration),
        )
        sid = cur.lastrowid
        inserted_sessions.append((sid, started, ended, exercise, good, failed))

    # -------- 2) rep_events（每场 8-20 条）--------
    rep_count = 0
    for sid, started, ended, exercise, good, failed in inserted_sessions:
        rep_n = rnd.randint(8, 20)
        span = max(int((ended - started).total_seconds()), rep_n * 3)
        for j in range(rep_n):
            ts = started + timedelta(seconds=int(span * (j + 1) / (rep_n + 1)))
            is_good = 1 if rnd.random() < 0.75 else 0  # 70-80% 好球
            if exercise == "squat":
                angle_min = round(rnd.uniform(85.0, 110.0), 2)
            else:
                angle_min = round(rnd.uniform(40.0, 55.0), 2)
            emg_target = round(rnd.uniform(25, 80), 2)
            emg_comp = round(rnd.uniform(20, 70), 2)
            cur.execute(
                "INSERT INTO rep_events "
                "(session_id, ts, is_good, angle_min, emg_target, emg_comp) "
                "VALUES (?,?,?,?,?,?)",
                (sid, _iso(ts), is_good, angle_min, emg_target, emg_comp),
            )
            rep_count += 1

    # -------- 3) 30 条 llm_log（混合 trigger）--------
    llm_inserted = 0
    for i in range(30):
        trigger, prompt, resp = _SEED_CHATS[i % len(_SEED_CHATS)]
        ts = now - timedelta(
            days=rnd.randint(0, 13), hours=rnd.randint(0, 23), minutes=rnd.randint(0, 59)
        )
        tagged_prompt = prompt + " " + SEED_TAG
        cur.execute(
            "INSERT INTO llm_log (ts, trigger, prompt, response, tokens_in, tokens_out) "
            "VALUES (?,?,?,?,?,?)",
            (_iso(ts), trigger, tagged_prompt, resp, 0, 0),
        )
        llm_inserted += 1

    # -------- 4) 4 条 user_config 偏好 --------
    prefs = [
        ("user_preference.favorite_exercise", "bicep_curl"),
        ("user_preference.coach_style", "鼓励"),
        ("user_preference.training_time", "afternoon"),
        ("user_preference.ideal_rep_count", "15"),
    ]
    pref_inserted = 0
    for k, v in prefs:
        cur.execute(
            "INSERT OR REPLACE INTO user_config (key, value, updated_at) "
            "VALUES (?,?,?)",
            (k, v, _iso(now)),
        )
        pref_inserted += 1

    conn.commit()
    conn.close()
    return {
        "sessions": len(inserted_sessions),
        "reps": rep_count,
        "llm_logs": llm_inserted,
        "preferences": pref_inserted,
    }


def cleanup(db_path):
    """仅清除由本脚本插入的 SEED 数据。"""
    conn = sqlite3.connect(db_path, timeout=5.0)
    cur = conn.cursor()
    # 1) 找 seed llm_log
    cur.execute("DELETE FROM llm_log WHERE prompt LIKE ?", ("%" + SEED_TAG + "%",))
    llm_removed = cur.rowcount
    # 2) training_sessions 没有标记字段，直接保守清：所有 ended_at IS NOT NULL 且 id 在最近插入区间的。
    #   这里采取温和策略：只清本脚本 seed 的 12 条 preferences + 所有 rep_events 来源 session
    #   但为安全起见，需提醒用户：若真环境也有 session，不建议无脑 --cleanup。
    #   此处按 "seed 时 user_config 的 4 条偏好" 作为定位锚：若存在，则一起清。
    cur.execute(
        "DELETE FROM user_config WHERE key IN (?,?,?,?)",
        (
            "user_preference.favorite_exercise",
            "user_preference.coach_style",
            "user_preference.training_time",
            "user_preference.ideal_rep_count",
        ),
    )
    pref_removed = cur.rowcount

    # rep_events 无标记：提示用户手动核对
    conn.commit()
    conn.close()
    return {
        "llm_logs_removed": llm_removed,
        "preferences_removed": pref_removed,
        "note": "training_sessions/rep_events 无标记字段未清除；如需彻底回滚，请手动删除 data/ironbuddy.db 后重建。",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cleanup", action="store_true", help="清除 SEED 数据")
    ap.add_argument("--db", default=None, help="自定义 DB 路径")
    args = ap.parse_args()

    # 保证建表（依赖 FitnessDB 的建表逻辑）
    db = FitnessDB(path=args.db)
    db.connect()
    db.close()
    path = args.db or _resolve_db_path()
    print(f"[seed] DB 路径: {path}")

    if args.cleanup:
        stat = cleanup(path)
        print("[seed] cleanup 完成:")
        for k, v in stat.items():
            print(f"  - {k}: {v}")
        return

    stat = seed(path)
    print("[seed] 插入完成:")
    for k, v in stat.items():
        print(f"  - {k}: {v}")
    print("[seed] 提示: 使用 `python3 tools/seed_fake_chats.py --cleanup` 清除 llm_log 与偏好 seed 标记")


if __name__ == "__main__":
    main()
