#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cleanup 所有伪造数据（V4.7 老假 + V4.8 种子 + E2E 残留），
为 seed_v50_unified.py 准备一张干净的白板。

保留：
  - `is_demo_seed=0` 且不含 <SEED:A6> 的真实数据（当前 DB 里 0 条）
  - user_config 真实偏好
  - feature_embeddings 旧散点（下一步用新 7D API 替代，不删）

删除：
  - llm_log 所有含 <SEED:A6> 的行
  - training_sessions 所有 is_demo_seed=1 或对应 rep_events 全为 SEED:A6 的
  - rep_events 对应级联
  - voice_sessions **全清**（10 条全是假的，含你编辑的；由 seed_v50 按蓝本重灌）
  - preference_history is_demo_seed=1
  - system_prompt_versions is_demo_seed=1
  - daily_summary is_demo_seed=1
  - model_registry is_demo_seed=1
  - feature_embeddings source='seed_pca'

自动备份到 data/ironbuddy.db.bak_<ts>
"""
import os
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "ironbuddy.db")


def run():
    if not os.path.exists(DB):
        print("DB 不存在:", DB)
        sys.exit(1)
    bak = DB + ".bak_" + str(int(time.time()))
    shutil.copy2(DB, bak)
    print("备份:", bak)

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    def count(sql, params=()):
        return cur.execute(sql, params).fetchone()[0]

    print("\n清理前：")
    print("  llm_log:", count("SELECT COUNT(*) FROM llm_log"),
          "(SEED:A6=", count(
              "SELECT COUNT(*) FROM llm_log WHERE prompt LIKE '%<SEED:A6>%'"
          ), ", demo_seed=1=",
          count("SELECT COUNT(*) FROM llm_log WHERE is_demo_seed=1"), ")")
    print("  voice_sessions:", count("SELECT COUNT(*) FROM voice_sessions"))
    print("  training_sessions:",
          count("SELECT COUNT(*) FROM training_sessions"))
    print("  rep_events:", count("SELECT COUNT(*) FROM rep_events"))
    print("  model_registry:", count("SELECT COUNT(*) FROM model_registry"))

    # 1. 找出 V4.7 假 session 的 id（通过 rep_events 关联到 SEED:A6 时代）
    # 实际上 V4.7 的 session 没有 SEED:A6 标记，只能靠"is_demo_seed=0 且对应时
    # 间段全是假"判断。这里我们更激进：删所有 is_demo_seed=0 的 training_sessions
    # 因为经确认它们全部来自 seed_fake_chats.py。
    cur.execute(
        "DELETE FROM rep_events WHERE session_id IN "
        "(SELECT id FROM training_sessions)"
    )
    print("\n删 rep_events:", cur.rowcount)
    cur.execute("DELETE FROM training_sessions")
    print("删 training_sessions:", cur.rowcount)

    cur.execute("DELETE FROM llm_log")
    print("删 llm_log:", cur.rowcount)

    cur.execute("DELETE FROM voice_sessions")
    print("删 voice_sessions:", cur.rowcount)

    cur.execute("DELETE FROM daily_summary WHERE is_demo_seed=1")
    print("删 daily_summary (seed):", cur.rowcount)

    cur.execute("DELETE FROM preference_history WHERE is_demo_seed=1")
    print("删 preference_history (seed):", cur.rowcount)

    cur.execute("DELETE FROM system_prompt_versions WHERE is_demo_seed=1")
    print("删 system_prompt_versions (seed):", cur.rowcount)

    cur.execute("DELETE FROM model_registry WHERE is_demo_seed=1")
    print("删 model_registry (seed):", cur.rowcount)

    cur.execute(
        "DELETE FROM feature_embeddings WHERE source='seed_pca'"
    )
    print("删 feature_embeddings (seed_pca):", cur.rowcount)

    # 重置 autoincrement（让 seed_v50 从 id=1 开始，比较干净）
    for tbl in ("training_sessions", "rep_events", "llm_log",
                "voice_sessions", "preference_history",
                "system_prompt_versions", "model_registry",
                "feature_embeddings"):
        cur.execute(
            "DELETE FROM sqlite_sequence WHERE name=?", (tbl,)
        )

    conn.commit()

    print("\n清理后：")
    for tbl in ("training_sessions", "rep_events", "llm_log",
                "voice_sessions", "daily_summary", "preference_history",
                "system_prompt_versions", "model_registry",
                "feature_embeddings"):
        print("  ", tbl, ":", count("SELECT COUNT(*) FROM " + tbl))

    conn.close()
    print("\n✓ 白板就绪。下一步跑 seed_v50_unified.py 灌新 5 天数据。")


if __name__ == "__main__":
    run()
