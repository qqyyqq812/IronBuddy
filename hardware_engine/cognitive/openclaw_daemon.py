"""OpenClaw 后端常驻 Daemon（IronBuddy V4.7 主线 A.3）。

职责：
  - 每日 09:00 推送"早安 + 今日计划"（调 build_daily_plan_prompt）；
  - 周日 20:00 推送"本周训练周报"（调 build_weekly_report_prompt）；
  - 每日 23:00 跑偏好学习，解析 JSON 后更新 user_config（build_preference_learning_prompt）。

触发方式：
  1) 时间触发：主循环每 60s 检查一次系统时间，命中整点时刻立即执行；
     触发点可由环境变量覆盖：
       DAILY_PLAN_HOUR        默认 9
       WEEKLY_REPORT_DOW      默认 6（周日=6，遵循 Python datetime.weekday：Mon=0..Sun=6）
       WEEKLY_REPORT_HOUR     默认 20
       PREFERENCE_HOUR        默认 23
  2) 手动触发文件：外部 touch 以下任一文件即可立刻执行对应任务，处理完后自动删除：
       /dev/shm/openclaw_trigger_daily_plan
       /dev/shm/openclaw_trigger_weekly_report
       /dev/shm/openclaw_trigger_preference_learning

Python 3.7 兼容：不使用 `X | None`、`match/case`、`:=`。
默认不自动启动；用户通过 scripts/start_openclaw_daemon.sh 手动拉起。
"""

import os
import sys
import json
import re
import asyncio
import logging
from datetime import datetime, date, timedelta

# 允许直接 `python3 openclaw_daemon.py` 运行时找到 persistence / cognitive
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cognitive.openclaw_bridge import OpenClawBridge  # noqa: E402
from cognitive.cognitive_nexus import CognitiveNexus  # noqa: E402
from persistence.db import FitnessDB  # noqa: E402

# 飞书推送客户端（dry_run 默认开，演示前不会真发）
try:
    from integrations.feishu_client import FeishuClient  # noqa: E402
    _FEISHU_AVAILABLE = True
except Exception as _fs_exc:  # pragma: no cover - 无网络 / 未配置都不该阻塞 daemon
    FeishuClient = None  # type: ignore
    _FEISHU_AVAILABLE = False
    logging.warning("[OPENCLAW_DAEMON] FeishuClient 导入失败，飞书推送将跳过: %s", _fs_exc)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [OPENCLAW_DAEMON] - %(levelname)s - %(message)s",
)

# 触发文件路径（/dev/shm 跨进程 IPC）
_TRIGGER_DAILY = "/dev/shm/openclaw_trigger_daily_plan"
_TRIGGER_WEEKLY = "/dev/shm/openclaw_trigger_weekly_report"
_TRIGGER_PREF = "/dev/shm/openclaw_trigger_preference_learning"

# 每个任务每日只跑一次（防止同一小时内 60s 轮询重复触发）
_last_fired = {
    "daily_plan": None,      # 存 date 字符串
    "weekly_report": None,
    "preference_learning": None,
}


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except Exception:
        return default


def _today_str():
    return datetime.now().strftime("%Y-%m-%d")


def _feishu_client():
    """返回一个 FeishuClient 单例；不可用时返回 None。"""
    if not _FEISHU_AVAILABLE or FeishuClient is None:
        return None
    try:
        return FeishuClient()
    except Exception as exc:
        logging.warning("[feishu] 客户端初始化失败: %s", exc)
        return None


def _format_daily_stats(db, today):
    """从 FitnessDB 拼早报统计文本（前日数据 + 近 3 日聚合）。"""
    try:
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        summary = db.get_daily_summary(yesterday) or {}
        if summary:
            reps = summary.get("total_good", 0) or summary.get("good_count", 0) or 0
            failed = summary.get("total_failed", 0) or summary.get("failed_count", 0) or 0
            total = int(reps) + int(failed)
            acc = (int(reps) / total * 100.0) if total > 0 else 0.0
            y_line = "昨日 %s reps · 命中率 %.1f%%" % (reps, acc)
        else:
            y_line = "昨日无训练记录（演示模式）"

        stats3 = db.get_range_stats(days=3) or []
        if stats3:
            s_reps = sum(int(r.get("total_good", 0) or 0) for r in stats3)
            s_fail = sum(int(r.get("total_failed", 0) or 0) for r in stats3)
            d_line = "近 3 天：%d reps · %d 失败动作 · %d 次训练" % (
                s_reps, s_fail, sum(int(r.get("session_count", 0) or 0) for r in stats3),
            )
        else:
            d_line = "近 3 天：暂无聚合数据"
        return y_line + "\n" + d_line
    except Exception as exc:
        logging.warning("[feishu] _format_daily_stats 失败: %s", exc)
        return "（数据读取失败，展示用）"


def _format_weekly(db):
    """从 FitnessDB 拼周报 lines + highlights。"""
    try:
        stats = db.get_range_stats(days=7) or []
        lines = []
        total_reps = 0
        total_fail = 0
        best_day = None
        best_acc = -1.0
        for row in stats:
            d = row.get("d", "?")
            good = int(row.get("total_good", 0) or 0)
            fail = int(row.get("total_failed", 0) or 0)
            total = good + fail
            acc = (good / total * 100.0) if total > 0 else 0.0
            lines.append("%s：%d reps · 命中率 %.1f%%" % (d, good, acc))
            total_reps += good
            total_fail += fail
            if acc > best_acc and total > 0:
                best_acc = acc
                best_day = d

        highlights = {
            "总 reps": str(total_reps),
            "失败动作": str(total_fail),
            "最佳命中率日": (
                "%s（%.1f%%）" % (best_day, best_acc)
                if best_day else "—"
            ),
        }
        return lines, highlights
    except Exception as exc:
        logging.warning("[feishu] _format_weekly 失败: %s", exc)
        return ["（周数据读取失败，展示用）"], {"状态": "dry-run"}


async def _connect_bridge():
    """连接 OpenClaw Gateway；3 次重试失败则返回 None（静默）。"""
    gw = os.environ.get("OPENCLAW_URL", "ws://127.0.0.1:18789")
    bridge = OpenClawBridge(gateway_url=gw)
    for i in range(3):
        ok = await bridge.connect()
        if ok:
            return bridge
        logging.warning(f"Gateway 第 {i + 1}/3 次连接失败，1s 后重试")
        await asyncio.sleep(1)
    logging.error("Gateway 连接失败 3 次，本任务放弃（不抛异常）")
    return None


async def _run_daily_plan(nexus, db):
    """早 9 点：日计划推送。"""
    try:
        prompts = nexus.build_daily_plan_prompt()
        bridge = await _connect_bridge()
        if bridge is None:
            return
        logging.info("[daily_plan] prompt 已拼接，开始调用 OpenClaw")
        reply = await bridge.ask_with_memory(
            prompts["user"], memory_context=prompts["system"], timeout=120
        )
        if not reply:
            logging.warning("[daily_plan] 回复为空，跳过推送")
            return
        ok = await bridge.deliver(reply, channel="feishu")
        logging.info(f"[daily_plan] 飞书推送结果: {ok}")
        db.log_llm("daily_plan", prompts["user"], reply, 0, 0)
    except Exception as e:
        logging.error(f"[daily_plan] 执行异常: {e}")

    # === 追加：FeishuClient 早报卡片（dry-run 默认开，不影响上面的 bridge.deliver）===
    try:
        client = _feishu_client()
        if client is not None:
            today = _today_str()
            stats_text = _format_daily_stats(db, today)
            plan_text = reply if 'reply' in locals() and reply else "（今日未生成 LLM 计划，展示模板）"
            card = FeishuClient.build_morning_card(today, stats_text, plan_text)
            res = client.send_card(card)
            logging.info("[daily_plan][feishu_card] dry_run=%s result=%s",
                         client.dry_run, res)
    except Exception as exc:
        logging.warning("[daily_plan][feishu_card] 追加投递异常（已吞）: %s", exc)


async def _run_weekly_report(nexus, db):
    """周日 20 点：周报推送。"""
    try:
        prompts = nexus.build_weekly_report_prompt()
        bridge = await _connect_bridge()
        if bridge is None:
            return
        logging.info("[weekly_report] prompt 已拼接，开始调用 OpenClaw")
        reply = await bridge.ask_with_memory(
            prompts["user"], memory_context=prompts["system"], timeout=180
        )
        if not reply:
            logging.warning("[weekly_report] 回复为空，跳过推送")
            return
        ok = await bridge.deliver(reply, channel="feishu")
        logging.info(f"[weekly_report] 飞书推送结果: {ok}")
        db.log_llm("weekly_report", prompts["user"], reply, 0, 0)
    except Exception as e:
        logging.error(f"[weekly_report] 执行异常: {e}")

    # === 追加：FeishuClient 周报卡片（dry-run 默认开）===
    try:
        client = _feishu_client()
        if client is not None:
            now = datetime.now()
            iso = now.isocalendar()  # (year, week, weekday) 兼容 py37
            week_label = "%s W%02d" % (iso[0], iso[1])
            lines, highlights = _format_weekly(db)
            card = FeishuClient.build_weekly_card(week_label, lines, highlights)
            res = client.send_card(card)
            logging.info("[weekly_report][feishu_card] dry_run=%s result=%s",
                         client.dry_run, res)
    except Exception as exc:
        logging.warning("[weekly_report][feishu_card] 追加投递异常（已吞）: %s", exc)


# ---------- V4.8 偏好学习：规则引擎 + daily_summary 回填 ----------
_KNEE_KEYWORDS = ("膝盖", "膝", "knee")
_SHOULDER_KEYWORDS = ("肩膀", "肩痛", "shoulder")
_BACK_KEYWORDS = ("腰", "背", "back", "lumbar")


def _fetch_today_rows(db, today_str):
    """返回 (sessions_today, rep_events_today, voices_today)。

    直接走底层 sqlite 连接（单锁串行；daemon 是 asyncio 单协程，无并发风险）。
    失败都吞到空列表，不影响后续。
    """
    sessions, reps, voices = [], [], []
    try:
        conn = db._ensure()  # noqa: SLF001
        if conn is None:
            return sessions, reps, voices
        with db._lock:  # noqa: SLF001
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM training_sessions "
                "WHERE date(started_at)=? ORDER BY started_at ASC",
                (today_str,),
            )
            sessions = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT * FROM rep_events "
                "WHERE date(ts)=? ORDER BY ts ASC",
                (today_str,),
            )
            reps = [dict(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT * FROM voice_sessions "
                "WHERE date(ts)=? AND COALESCE(trigger_src,'chat')='chat' "
                "ORDER BY ts ASC",
                (today_str,),
            )
            voices = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logging.warning("[preference_learning] fetch today rows failed: %s", e)
    return sessions, reps, voices


def _emg_trend_down(db, days=3, threshold=0.20):
    """返回最近 N 天（含今天）rep_events 平均 emg_target 是否累计下降超过 threshold。

    简化：取每日均值，做 (first - last)/first 评估下降幅度。
    数据不足返回 False。
    """
    try:
        conn = db._ensure()  # noqa: SLF001
        if conn is None:
            return False, 0.0
        start = (date.today() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        with db._lock:  # noqa: SLF001
            cur = conn.cursor()
            cur.execute(
                "SELECT date(ts) AS d, AVG(emg_target) AS m "
                "FROM rep_events WHERE date(ts)>=? AND emg_target IS NOT NULL "
                "GROUP BY date(ts) ORDER BY d ASC",
                (start,),
            )
            rows = [dict(r) for r in cur.fetchall()]
        if len(rows) < 2:
            return False, 0.0
        first = rows[0]["m"] or 0.0
        last = rows[-1]["m"] or 0.0
        if first <= 0:
            return False, 0.0
        drop = (first - last) / first
        return drop >= threshold, drop
    except Exception as e:
        logging.warning("[preference_learning] emg trend failed: %s", e)
        return False, 0.0


def _count_keyword_hits(voices, keywords):
    n = 0
    for v in voices:
        blob = (v.get("transcript") or "") + " " + (v.get("response") or "")
        for kw in keywords:
            if kw in blob:
                n += 1
                break  # 一条会话只计一次
    return n


def _extract_muscle_groups(sessions):
    """从 training_sessions.exercise 提取肌群。简单映射表。"""
    mapping = {
        "squat": "quadriceps",
        "deep_squat": "quadriceps",
        "深蹲": "quadriceps",
        "bicep_curl": "biceps",
        "弯举": "biceps",
        "push_up": "chest",
        "shoulder_press": "shoulders",
    }
    out = set()
    for s in sessions:
        ex = (s.get("exercise") or "").lower()
        ex_cn = s.get("exercise") or ""
        mg = mapping.get(ex) or mapping.get(ex_cn)
        if mg:
            out.add(mg)
    return sorted(out)


def _compute_rule_based_preferences(db, today_str):
    """规则引擎：基于当日 rep_events + voice_sessions + 近 3 天 EMG 产出偏好。

    返回 dict[field] = (new_value: str, confidence: float, rationale: str)
    """
    sessions, reps, voices = _fetch_today_rows(db, today_str)
    prefs = {}

    # 1) 膝盖关注：chat 中 >= 2 次提到 -> knee_caution=true
    knee_hits = _count_keyword_hits(voices, _KNEE_KEYWORDS)
    if knee_hits >= 2:
        prefs["knee_caution"] = (
            "true", 0.88,
            "当日闲聊 %d 次提及膝盖" % knee_hits,
        )

    # 2) 疲劳容忍：近 3 天 rep_events emg_target 均值下降 > 20%
    down, drop = _emg_trend_down(db, days=3, threshold=0.20)
    if down:
        prefs["fatigue_tolerance"] = (
            "medium-low", 0.75,
            "近 3 天 EMG 平均下降 %.1f%%" % (drop * 100),
        )

    # 3) target_muscle_groups：当日 session exercise 多样
    mgs = _extract_muscle_groups(sessions)
    if len(mgs) >= 2:
        prefs["target_muscle_groups"] = (
            ",".join(mgs), 0.72,
            "当日训练覆盖 %d 个肌群" % len(mgs),
        )
    elif len(mgs) == 1:
        prefs["target_muscle_groups"] = (
            mgs[0], 0.60, "当日仅训练 %s" % mgs[0],
        )

    # 4) favorite_exercise：当日 session count 最多的动作
    if sessions:
        from collections import Counter
        c = Counter((s.get("exercise") or "unknown") for s in sessions)
        top = c.most_common(1)[0]
        if top[1] >= 1:
            prefs["favorite_exercise"] = (
                top[0], 0.55,
                "当日 %s 次数最多 (%d)" % (top[0], top[1]),
            )

    return prefs, sessions, reps, voices


def _generate_daily_summary_text(sessions, reps, voices):
    """规则汇总：good/failed 聚合 + voice 关键词抽取，返回 (summary, rec)。"""
    good = sum(int(s.get("good_count") or 0) for s in sessions)
    failed = sum(int(s.get("failed_count") or 0) for s in sessions)
    total = good + failed
    hit_rate = (good / total) if total > 0 else 0.0
    exs = sorted({(s.get("exercise") or "unknown") for s in sessions})
    parts = []
    if sessions:
        parts.append(
            "完成 %d 次训练(%s)，命中率 %d%%（good %d / failed %d）" % (
                len(sessions), "/".join(exs), int(hit_rate * 100), good, failed,
            )
        )
    else:
        parts.append("今日无训练记录")
    # 语音侧补一句关键词
    knee_hits = _count_keyword_hits(voices, _KNEE_KEYWORDS)
    if knee_hits:
        parts.append("闲聊中 %d 次提及膝盖" % knee_hits)
    shoulder_hits = _count_keyword_hits(voices, _SHOULDER_KEYWORDS)
    if shoulder_hits:
        parts.append("闲聊中 %d 次提及肩部" % shoulder_hits)
    summary = "；".join(parts)

    rec_bits = []
    if hit_rate < 0.6 and total >= 10:
        rec_bits.append("命中率偏低，建议降低强度巩固动作")
    if knee_hits >= 2:
        rec_bits.append("膝盖多次提及，次日避免下肢高强度")
    if not rec_bits:
        rec_bits.append("保持节奏，按计划推进")
    rec = "；".join(rec_bits)
    return summary[:400], rec[:200]


def _upsert_daily_summary(db, today_str, sessions, reps, voices):
    """把当日 summary/rec 写入 daily_summary；确保 session_count 等字段同步。

    使用 INSERT OR REPLACE（保留 is_demo_seed 列的默认 0）。
    """
    summary, rec = _generate_daily_summary_text(sessions, reps, voices)
    good = sum(int(s.get("good_count") or 0) for s in sessions)
    failed = sum(int(s.get("failed_count") or 0) for s in sessions)
    fatigue = sum(float(s.get("fatigue_peak") or 0.0) for s in sessions)
    best = max([int(s.get("good_count") or 0) for s in sessions] or [0])
    try:
        conn = db._ensure()  # noqa: SLF001
        if conn is None:
            return None, summary, rec
        with db._lock:  # noqa: SLF001
            conn.execute(
                "INSERT INTO daily_summary "
                "(date, session_count, total_good, total_failed, "
                "total_fatigue, best_streak, summary, rec, is_demo_seed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0) "
                "ON CONFLICT(date) DO UPDATE SET "
                "session_count=excluded.session_count, "
                "total_good=excluded.total_good, "
                "total_failed=excluded.total_failed, "
                "total_fatigue=excluded.total_fatigue, "
                "best_streak=excluded.best_streak, "
                "summary=excluded.summary, rec=excluded.rec",
                (today_str, len(sessions), good, failed, fatigue,
                 best, summary, rec),
            )
            conn.commit()
            cur = conn.cursor()
            cur.execute(
                "SELECT rowid AS id FROM daily_summary WHERE date=?",
                (today_str,),
            )
            row = cur.fetchone()
            day_id = dict(row)["id"] if row else None
        return day_id, summary, rec
    except Exception as e:
        logging.warning("[preference_learning] upsert daily_summary failed: %s", e)
        return None, summary, rec


def _compose_prompt_text(snapshot):
    """用偏好快照拼装新的 system_prompt_text。与 voice_daemon 的 fallback 兼容。"""
    mgs = snapshot.get("target_muscle_groups") or "全身"
    ft = snapshot.get("fatigue_tolerance") or "medium"
    knee = (snapshot.get("knee_caution") or "").lower() == "true"
    coach = snapshot.get("coach_style") or "鼓励"
    fav = snapshot.get("favorite_exercise") or "综合训练"
    lines = [
        "你是 IronBuddy 健身教练，已根据用户最新训练数据完成个性化适配：",
        "- 偏好肌群: %s" % mgs,
        "- 疲劳容忍: %s" % ft,
        "- 首选动作: %s" % fav,
        "- 教练风格: %s" % coach,
    ]
    if knee:
        lines.append(
            "- 健康约束: knee_caution=true，下肢动作前先确认膝盖状态"
        )
    lines.append("回答 ≤3 句，关键数据用 EMG/角度/reps 数字佐证。")
    return "\n".join(lines)


async def _run_preference_learning(nexus, db):
    """每日 22:30 / 23:00：偏好学习任务。

    演示模式（OPENCLAW_PREFLEARN_MODE=rule 或未设置）：
      - 纯规则引擎，不调 LLM；产出偏好 → preference_history + user_config
      - 生成 daily_summary (summary + rec)
      - 创建新 system_prompt_versions 行（active=1）

    可选 LLM 模式（OPENCLAW_PREFLEARN_MODE=llm）：
      - 保留原 OpenClaw LLM JSON 流程（不在演示中默认启用）
    """
    mode = os.environ.get("OPENCLAW_PREFLEARN_MODE", "rule").strip().lower()
    if mode == "llm":
        await _run_preference_learning_via_llm(nexus, db)
        return

    today_str = date.today().strftime("%Y-%m-%d")
    logging.info("[preference_learning] 规则引擎模式启动 (date=%s)", today_str)
    try:
        # 1) 规则引擎产偏好
        prefs, sessions, reps, voices = _compute_rule_based_preferences(
            db, today_str)

        # 2) 当前偏好快照（比对 old_value）
        snap_before = db.get_user_preferences_snapshot()

        # 3) 逐字段写变化
        changed_fields = []
        for field, (new_val, conf, rationale) in prefs.items():
            old_val = snap_before.get(field)
            if old_val == new_val:
                continue
            hid = db.record_preference_change(
                field=field, old_value=old_val, new_value=new_val,
                source="rule_engine", confidence=conf,
            )
            if hid is not None:
                changed_fields.append(field)
                logging.info(
                    "[preference_learning] 偏好变化: %s %r -> %r (conf=%.2f, %s, hid=%d)",
                    field, old_val, new_val, conf, rationale, hid,
                )

        # 4) 写当日 daily_summary（即便没变化也刷新）
        day_id, summary_txt, rec_txt = _upsert_daily_summary(
            db, today_str, sessions, reps, voices)
        logging.info(
            "[preference_learning] daily_summary 写入: day_id=%s summary=%s | rec=%s",
            day_id, summary_txt[:80], rec_txt[:60],
        )

        # 5) 生成新 system_prompt_versions（使用更新后的快照）
        snap_after = db.get_user_preferences_snapshot()
        prompt_text = _compose_prompt_text(snap_after)
        version_id = db.create_system_prompt_version(
            prompt_text=prompt_text,
            based_on_summary_ids=[day_id] if day_id else [],
        )
        logging.info(
            "[preference_learning] new prompt version=%s changed_fields=%s",
            version_id, changed_fields,
        )

        # 6) llm_log 记一条便于追溯（trigger=preference_learning_rule）
        try:
            db.log_llm(
                trigger="preference_learning_rule",
                prompt="rule_engine(date=%s)" % today_str,
                response=json.dumps(
                    {
                        "changed_fields": changed_fields,
                        "prompt_version_id": version_id,
                        "summary": summary_txt[:120],
                    },
                    ensure_ascii=False,
                )[:2000],
                tokens_in=0, tokens_out=0,
            )
        except Exception:
            pass
    except Exception as e:
        logging.error("[preference_learning] 规则引擎执行异常: %s", e)


async def _run_preference_learning_via_llm(nexus, db):
    """LLM 模式（非默认）：保留原 OpenClaw JSON 流，便于板端切换。"""
    try:
        prompts = nexus.build_preference_learning_prompt()
        bridge = await _connect_bridge()
        if bridge is None:
            return
        logging.info("[preference_learning:llm] prompt 已拼接，开始调用 OpenClaw")
        reply = await bridge.ask_with_memory(
            prompts["user"], memory_context=prompts["system"], timeout=120
        )
        if not reply:
            logging.warning("[preference_learning:llm] 回复为空，跳过")
            return
        db.log_llm("preference_learning", prompts["user"], reply, 0, 0)

        text = reply.strip()
        if "```" in text:
            parts = text.split("```")
            for seg in parts:
                seg = seg.strip()
                if seg.startswith("json"):
                    seg = seg[4:].strip()
                if seg.startswith("{"):
                    text = seg
                    break
        if not text.startswith("{"):
            i = text.find("{")
            j = text.rfind("}")
            if i >= 0 and j > i:
                text = text[i: j + 1]
        try:
            data = json.loads(text)
        except Exception as pe:
            logging.warning(
                "[preference_learning:llm] JSON 解析失败: %s; 原文前200: %s",
                pe, reply[:200],
            )
            return

        updated = 0
        for key in ("favorite_exercise", "coach_style", "training_time"):
            val = data.get(key)
            if val:
                db.set_user_preference(key, str(val))
                updated += 1
        insights = data.get("insights")
        if isinstance(insights, list) and insights:
            db.set_user_preference(
                "insights_latest", "; ".join(str(x) for x in insights[:5])
            )
            updated += 1
        logging.info(
            "[preference_learning:llm] 偏好表更新字段数: %d", updated,
        )
    except Exception as e:
        logging.error("[preference_learning:llm] 执行异常: %s", e)


async def _maybe_process_trigger_files(nexus, db):
    """处理 /dev/shm/openclaw_trigger_* 手动触发文件。"""
    if os.path.exists(_TRIGGER_DAILY):
        logging.info("[trigger] 检测到 daily_plan 手动触发文件")
        try:
            os.remove(_TRIGGER_DAILY)
        except Exception:
            pass
        await _run_daily_plan(nexus, db)

    if os.path.exists(_TRIGGER_WEEKLY):
        logging.info("[trigger] 检测到 weekly_report 手动触发文件")
        try:
            os.remove(_TRIGGER_WEEKLY)
        except Exception:
            pass
        await _run_weekly_report(nexus, db)

    if os.path.exists(_TRIGGER_PREF):
        logging.info("[trigger] 检测到 preference_learning 手动触发文件")
        try:
            os.remove(_TRIGGER_PREF)
        except Exception:
            pass
        await _run_preference_learning(nexus, db)


async def main():
    logging.info("OpenClaw Daemon 启动")
    daily_hour = _env_int("DAILY_PLAN_HOUR", 9)
    weekly_dow = _env_int("WEEKLY_REPORT_DOW", 6)  # 周日=6
    weekly_hour = _env_int("WEEKLY_REPORT_HOUR", 20)
    pref_hour = _env_int("PREFERENCE_HOUR", 23)
    logging.info(
        f"触发点: daily={daily_hour:02d}:00 / weekly=dow{weekly_dow}@{weekly_hour:02d}:00 / pref={pref_hour:02d}:00"
    )

    nexus = CognitiveNexus()
    db = FitnessDB()
    db.connect()

    while True:
        try:
            # 1) 手动触发优先处理
            await _maybe_process_trigger_files(nexus, db)

            # 2) 定时触发：每分钟比对整点
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            # 只在"分钟=0"附近触发，避免被 60s 漂移跳过。允许 0~4 分钟窗口。
            if now.minute < 5:
                if now.hour == daily_hour and _last_fired["daily_plan"] != today:
                    _last_fired["daily_plan"] = today
                    logging.info("[cron] 到达 daily_plan 触发点")
                    await _run_daily_plan(nexus, db)

                if (
                    now.weekday() == weekly_dow
                    and now.hour == weekly_hour
                    and _last_fired["weekly_report"] != today
                ):
                    _last_fired["weekly_report"] = today
                    logging.info("[cron] 到达 weekly_report 触发点")
                    await _run_weekly_report(nexus, db)

                if now.hour == pref_hour and _last_fired["preference_learning"] != today:
                    _last_fired["preference_learning"] = today
                    logging.info("[cron] 到达 preference_learning 触发点")
                    await _run_preference_learning(nexus, db)
        except Exception as e:
            logging.error(f"主循环异常（继续）: {e}")

        await asyncio.sleep(60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("OpenClaw Daemon 收到中断，退出")
