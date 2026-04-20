#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cleanup_demo_seed.py
=====================
一键回滚 seed_demo_2026_04.py 灌入的所有演示数据。

策略：
  - 对带 is_demo_seed 列的表：DELETE WHERE is_demo_seed=1。
  - 对新增表（voice_sessions / preference_history / system_prompt_versions）：
    既支持 is_demo_seed=1 清理，也支持「整表清空」开关 --purge-new-tables。
  - user_config 中演示期间写入的键：精确按 key 列表删除。
  - last_prompt_version 也回滚（同 key 列表）。
  - 备份再操作（与 seed 脚本相同保险）。

使用：
  python3 scripts/cleanup_demo_seed.py             # 仅删 is_demo_seed=1 + demo user_config keys
  python3 scripts/cleanup_demo_seed.py --purge-new-tables  # 同上 + 三张新表整表清空
"""

import argparse
import shutil
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "ironbuddy.db"

DEMO_USER_CONFIG_KEYS = [
    "user_preference.fatigue_tolerance",
    "user_preference.target_muscle_groups",
    "user_preference.knee_caution",
    "system.last_prompt_version",
    "fatigue_tolerance",
    "target_muscle_groups",
    "knee_caution",
    "last_prompt_version",
]

DEMO_SEED_TABLES = [
    "rep_events",
    "training_sessions",
    "llm_log",
    "daily_summary",
    "voice_sessions",
    "preference_history",
    "system_prompt_versions",
]

NEW_TABLES = [
    "voice_sessions",
    "preference_history",
    "system_prompt_versions",
]


def backup(db_path: Path) -> Path:
    ts = int(time.time())
    bak = db_path.with_suffix(db_path.suffix + f".cleanup_bak_{ts}")
    shutil.copy2(db_path, bak)
    print(f"[backup] {db_path} -> {bak}")
    return bak


def column_exists(cur, table: str, col: str) -> bool:
    cur.execute(f"PRAGMA table_info({table});")
    return any(row[1] == col for row in cur.fetchall())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--purge-new-tables",
        action="store_true",
        help="把 voice_sessions / preference_history / system_prompt_versions 整表清空"
             "（不只删 is_demo_seed=1 行）。",
    )
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise SystemExit(f"DB not found: {DB_PATH}")

    backup(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys=OFF;")
    cur = conn.cursor()
    try:
        # 1. 删 is_demo_seed=1
        for t in DEMO_SEED_TABLES:
            if not column_exists(cur, t, "is_demo_seed"):
                print(f"[skip] {t}: no is_demo_seed column")
                continue
            cur.execute(f"DELETE FROM {t} WHERE is_demo_seed=1;")
            print(f"[clean] {t}: deleted {cur.rowcount} seed rows")

        # 2. 可选：整表清空新增表
        if args.purge_new_tables:
            for t in NEW_TABLES:
                cur.execute(f"DELETE FROM {t};")
                print(f"[purge] {t}: deleted {cur.rowcount} all rows")

        # 3. 删 user_config 中的演示键
        for k in DEMO_USER_CONFIG_KEYS:
            cur.execute("DELETE FROM user_config WHERE key=?;", (k,))
            if cur.rowcount:
                print(f"[clean] user_config: removed key={k}")

        conn.commit()
        print("[cleanup] DONE")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
