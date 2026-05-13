#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""IronBuddy 定时推送调度器 (systemd timer 触发)

功能：
  - 每次触发（建议每日早 9 点 + 晚 9 点 + 手动）
  - 从 SQLite 读过去 24h 训练数据
  - 根据规则生成提醒文本（含 DeepSeek 可选调用）
  - 推送到飞书 webhook
  - 入库 llm_log

用法：
  python3 scripts/ironbuddy_scheduler.py            # 单次跑
  python3 scripts/ironbuddy_scheduler.py --mode=morning
  python3 scripts/ironbuddy_scheduler.py --dry-run  # 不推送只打印

systemd timer 配置（未来）：
  /etc/systemd/system/ironbuddy-scheduler.timer
  [Unit] Description=Daily IronBuddy push
  [Timer] OnCalendar=*-*-* 09,21:00
  [Install] WantedBy=timers.target
"""
from __future__ import print_function
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta

# 保证可以 import 项目模块
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.path.join(_PROJECT_ROOT, "hardware_engine"))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [SCHEDULER] - %(message)s',
)

try:
    from persistence.db import FitnessDB
except Exception as e:
    logging.error("DB import failed: %s", e)
    sys.exit(1)


# ===== 飞书推送 =====
def push_feishu(webhook_url, text):
    """推送到飞书机器人。text 可以是 markdown。"""
    if not webhook_url:
        logging.warning("飞书 webhook 未配置，跳过推送")
        return False
    try:
        import urllib.request
        payload = json.dumps({
            "msg_type": "text",
            "content": {"text": text}
        }).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=8)
        ok = resp.getcode() == 200
        logging.info("飞书推送 %s: %s", "成功" if ok else "失败", resp.getcode())
        return ok
    except Exception as e:
        logging.error("飞书推送异常: %s", e)
        return False


# ===== 文本生成规则（无 LLM 版本）=====
def build_reminder(db, mode="auto"):
    """根据数据库统计生成提醒文本。"""
    recent = db.get_recent_sessions(limit=20)
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    today_stats = db.get_range_stats(today, today) if hasattr(db, "get_range_stats") else []
    yest_stats = db.get_range_stats(yesterday, yesterday) if hasattr(db, "get_range_stats") else []

    today_done = today_stats[0] if today_stats else None
    yest_done = yest_stats[0] if yest_stats else None

    lines = []
    lines.append("【IronBuddy 训练提醒】")

    if mode == "morning":
        lines.append("早上好。")
        if yest_done and yest_done.get("total_good", 0) > 0:
            lines.append("昨天完成 %d 个标准动作，违规 %d 个。" % (
                yest_done.get("total_good", 0),
                yest_done.get("total_failed", 0),
            ))
        else:
            lines.append("昨天未训练。建议今日安排 1 组深蹲或弯举。")
        lines.append("建议训练时段：上午 10 点 / 下午 5 点。")

    elif mode == "evening":
        lines.append("晚间总结。")
        if today_done and today_done.get("total_good", 0) > 0:
            rate = 0
            total = today_done.get("total_good", 0) + today_done.get("total_failed", 0)
            if total > 0:
                rate = int(100 * today_done.get("total_good", 0) / total)
            lines.append("今日训练 %d 次（合格 %d，合格率 %d%%），疲劳累计 %.0f。" % (
                today_done.get("session_count", 0),
                today_done.get("total_good", 0),
                rate,
                today_done.get("total_fatigue", 0),
            ))
        else:
            lines.append("今日尚未训练。赶在睡前补一组 10 分钟的弯举。")

    else:  # auto
        if today_done and today_done.get("total_good", 0) > 0:
            lines.append("今日已完成 %d 次训练。" % today_done.get("session_count", 0))
        else:
            lines.append("今日未训练，距离上次训练 %d 天。" % (
                _days_since_last(recent)
            ))

    lines.append("——@IronBuddy 教练助手")
    return "\n".join(lines)


def _days_since_last(sessions):
    if not sessions:
        return 999
    try:
        last_ts = sessions[0].get("started_at")
        if not last_ts:
            return 999
        dt = datetime.strptime(last_ts[:10], "%Y-%m-%d")
        return (datetime.now() - dt).days
    except Exception:
        return 999


# ===== 主入口 =====
def main():
    parser = argparse.ArgumentParser(description="IronBuddy 定时推送")
    parser.add_argument("--mode", choices=["morning", "evening", "auto"], default="auto")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    args = parser.parse_args()

    db = FitnessDB()
    db.connect()

    webhook = db.get_config("feishu_webhook", "")
    text = build_reminder(db, mode=args.mode)

    logging.info("生成文本:\n%s", text)

    if args.dry_run:
        logging.info("[dry-run] 未推送")
        return

    pushed = push_feishu(webhook, text)

    # 入库 llm_log (trigger=scheduler)
    try:
        db.log_llm(
            trigger="scheduler/%s" % args.mode,
            prompt="mode=%s" % args.mode,
            response=text,
            tokens_in=0,
            tokens_out=0,
        )
    except Exception as e:
        logging.warning("llm_log 入库失败: %s", e)

    sys.exit(0 if pushed else 2)


if __name__ == "__main__":
    main()
