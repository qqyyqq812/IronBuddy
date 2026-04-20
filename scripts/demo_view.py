#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演示聚合脚本：一条命令代替一堆 heredoc。
用法：
  python3 scripts/demo_view.py overview    # Step 1
  python3 scripts/demo_view.py prompts     # Step 3
  python3 scripts/demo_view.py feishu      # Step 4（dry-run）
  python3 scripts/demo_view.py inject-knee # Step 5 现场加戏

必须在项目根目录 /home/qq/projects/embedded-fullstack 下执行。
"""
import os
import sys
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "ironbuddy.db")


def _conn():
    if not os.path.exists(DB_PATH):
        print("❌ 找不到数据库 %s" % DB_PATH)
        print("   请确认在项目根目录执行，或先跑 scripts/seed_demo_2026_04.py")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


def cmd_overview():
    c = _conn()
    print("=" * 60)
    print("  IronBuddy 数据库三位一体总览")
    print("=" * 60)

    ts_cnt = c.execute(
        "SELECT COUNT(*) FROM training_sessions WHERE is_demo_seed=1"
    ).fetchone()[0]
    re_cnt = c.execute(
        "SELECT COUNT(*) FROM rep_events WHERE is_demo_seed=1"
    ).fetchone()[0]
    print("\n[1] 客观训练数据  三天训练 seed=%d 场 / %d reps" % (ts_cnt, re_cnt))

    vs_cnt = c.execute("SELECT COUNT(*) FROM voice_sessions").fetchone()[0]
    knee_rows = c.execute(
        "SELECT ts, transcript FROM voice_sessions "
        "WHERE transcript LIKE '%膝盖%' ORDER BY ts"
    ).fetchall()
    print(
        "\n[2] 主观闲聊      voice_sessions 共 %d 条，%d 次提及"
        "'膝盖'：" % (vs_cnt, len(knee_rows))
    )
    for ts, tx in knee_rows:
        print("     - %s | %s" % (ts, tx[:60]))

    ph_cnt = c.execute("SELECT COUNT(*) FROM preference_history").fetchone()[0]
    ph_rows = c.execute(
        "SELECT ts, field, new_value, confidence FROM preference_history "
        "ORDER BY ts"
    ).fetchall()
    print("\n[3] 学到的偏好    preference_history 共 %d 条演化：" % ph_cnt)
    for ts, f, v, conf in ph_rows:
        print("     - %s | %s = %r (conf=%s)" % (ts, f, v, conf))

    print("\n[4] 每日总结      daily_summary 种子 3 条：")
    for d, s in c.execute(
        "SELECT date, summary FROM daily_summary "
        "WHERE is_demo_seed=1 ORDER BY date"
    ):
        print("     - %s: %s" % (d, (s or "")[:70]))
    print()


def cmd_prompts():
    c = _conn()
    print("=" * 60)
    print("  系统提示词演进史（v1 → v最新）")
    print("=" * 60)
    rows = c.execute(
        "SELECT id, active, is_demo_seed, ts, prompt_text "
        "FROM system_prompt_versions ORDER BY id"
    ).fetchall()
    for pid, active, seed, ts, text in rows:
        head = "★ ACTIVE ★" if active else "          "
        tag = "[seed]" if seed else "[live]"
        print("\n%s v%s %s  %s" % (head, pid, tag, ts))
        body = (text or "").strip()
        if len(body) > 180:
            body = body[:180] + "..."
        print("   " + body)
    print()


def cmd_feishu():
    sys.path.insert(0, os.path.join(ROOT, "hardware_engine"))
    os.environ.setdefault("IRONBUDDY_FEISHU_DRY_RUN", "1")
    try:
        from integrations.feishu_client import FeishuClient
        from persistence.db import FitnessDB
        from cognitive.openclaw_daemon import _format_daily_stats, _today_str
    except Exception as e:
        print("❌ 模块导入失败: %s" % e)
        print("   请确认项目结构完整，以及当前在项目根目录。")
        sys.exit(1)

    db = FitnessDB()
    db.connect()
    today = _today_str()
    stats = _format_daily_stats(db, today)

    c = FeishuClient()
    print("=" * 60)
    print("  飞书投递演示（dry-run 护栏开）")
    print("=" * 60)
    print("dry_run =", c.dry_run, "  (True 代表不会真发)")
    print()

    print("--- 发送文本消息 ---")
    r = c.send_text("IronBuddy 演示消息（dry-run）")
    print("返回:", r)
    print()

    print("--- 发送早报卡片 ---")
    card = FeishuClient.build_morning_card(
        today,
        stats,
        "今日建议：上肢 20min + 核心 10min，避开下肢高强度。",
    )
    r = c.send_card(card)
    print("返回:", r)
    print()


def cmd_inject_knee():
    c = _conn()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    c.execute(
        "INSERT INTO voice_sessions "
        "(ts, trigger_src, transcript, response, duration_s, is_demo_seed) "
        "VALUES (?, 'chat', '膝盖又疼了，今天是不是该歇一下？', "
        "'建议今天避开下肢，练上肢或核心。', 4.2, 0)",
        (now,),
    )
    c.commit()
    print("✓ 已插入一条闲聊: '膝盖又疼了'")
    print("  下一步: touch /dev/shm/openclaw_trigger_preference_learning")
    print("  然后盯终端 B 等 60 秒看新版 prompt 产出。")


CMDS = {
    "overview": cmd_overview,
    "prompts": cmd_prompts,
    "feishu": cmd_feishu,
    "inject-knee": cmd_inject_knee,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print("用法: python3 scripts/demo_view.py {%s}" % "|".join(CMDS))
        sys.exit(1)
    CMDS[sys.argv[1]]()


if __name__ == "__main__":
    main()
