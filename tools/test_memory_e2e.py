"""记忆闭环只读验证脚本（IronBuddy V4.7 主线 A.7）。

严禁联网，严禁启动服务，严禁真正调用 LLM。只做只读校验与触发文件路径打印。

用法:
    python3 tools/test_memory_e2e.py

输出:
  1) 数据库 schema（5 张表）；
  2) 各表行数统计；
  3) 最新 5 条 llm_log 的 trigger + prompt 前 60 字；
  4) 全部 user_config 条目；
  5) OpenClaw Daemon 的 3 个手动触发文件路径供用户自行 touch。
"""

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(ROOT, "hardware_engine"))

from persistence.db import _resolve_db_path  # noqa: E402


def _hr(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def main():
    db_path = _resolve_db_path()
    print(f"[e2e] DB 路径: {db_path}")
    if not os.path.exists(db_path):
        print(f"[e2e] 数据库不存在，请先跑 `python3 tools/seed_fake_chats.py`")
        return 1

    conn = sqlite3.connect(db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 1) schema
    _hr("Schema（5 张表）")
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    for r in cur.fetchall():
        print(f"  - {r['name']}")

    # 2) 行数
    _hr("各表行数")
    for t in ("training_sessions", "rep_events", "llm_log", "daily_summary", "user_config"):
        try:
            cur.execute(f"SELECT COUNT(*) AS c FROM {t}")
            print(f"  - {t:20s}: {cur.fetchone()['c']}")
        except Exception as e:
            print(f"  - {t:20s}: (查询失败 {e})")

    # 3) 最新 5 条 llm_log
    _hr("最新 5 条 llm_log")
    cur.execute(
        "SELECT ts, trigger, prompt FROM llm_log ORDER BY id DESC LIMIT 5"
    )
    for r in cur.fetchall():
        p = (r["prompt"] or "")[:60]
        print(f"  [{r['ts']}] {r['trigger']:15s} | {p}")

    # 4) user_config
    _hr("全部 user_config")
    cur.execute("SELECT key, value, updated_at FROM user_config ORDER BY key")
    for r in cur.fetchall():
        print(f"  - {r['key']:40s} = {r['value']}  (updated={r['updated_at']})")

    conn.close()

    # 5) 手动触发文件
    _hr("OpenClaw Daemon 手动触发命令（用户自行执行）")
    print("  touch /dev/shm/openclaw_trigger_daily_plan")
    print("  touch /dev/shm/openclaw_trigger_weekly_report")
    print("  touch /dev/shm/openclaw_trigger_preference_learning")
    print(
        "\n[e2e] 本脚本仅读 DB，未启动服务，未调用 LLM。闭环验证请启动 "
        "scripts/start_openclaw_daemon.sh 后 touch 上述文件。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
