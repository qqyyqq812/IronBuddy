# Latest Commit Diff
Commit Hash: f847d9898cf860d5a8f8ac4ea57cb4eead7a1d62
Timestamp: Fri Apr 17 22:49:08 CST 2026
```diff
commit f847d9898cf860d5a8f8ac4ea57cb4eead7a1d62
Author: qqyyqq812 <2957131097@qq.com>
Date:   Fri Apr 17 22:49:08 2026 +0800

    feat: Sprint5.2 - 连续对话 + 系统级静音 + Rig 重排 + 定时推送
    
    - voice_daemon: 唤醒后进入 3 轮连续对话循环（参考 main2.py ai_copilot_worker）
    - voice_daemon: VAD_DEBUG env 启用实时 RMS 打印（默认开），VOICE_VAD_DELTA 默认 40
    - voice_daemon: "我在，请说" → "我在"（更简洁）
    - main_claw_loop: 弯举违规文案改短 "弯举不标准，请收缩到位" → "不标准"
    - streamer_app /api/mute: amixer 系统级静音（Speaker 0% mute），彻底屏蔽 L0 警报
    - templates: Rig 独立 center-panel 在 视频↔服务状态 之间（200px 宽 aside）
    - scripts/ironbuddy_scheduler.py: 飞书定时推送（早 9 晚 9 两档）
    - scripts/ironbuddy-morning/evening timer+service: systemd 定时任务模板
---
 hardware_engine/main_claw_loop.py |   2 +-
 hardware_engine/voice_daemon.py   |  48 ++++++----
 scripts/ironbuddy-evening.service |   9 ++
 scripts/ironbuddy-evening.timer   |   9 ++
 scripts/ironbuddy-morning.service |   9 ++
 scripts/ironbuddy-morning.timer   |   9 ++
 scripts/ironbuddy_scheduler.py    | 181 ++++++++++++++++++++++++++++++++++++++
 streamer_app.py                   |  13 ++-
 templates/index.html              |  78 +++++++++-------
 9 files changed, 307 insertions(+), 51 deletions(-)

diff --git a/hardware_engine/main_claw_loop.py b/hardware_engine/main_claw_loop.py
index 6b80044..3bebc7c 100644
--- a/hardware_engine/main_claw_loop.py
+++ b/hardware_engine/main_claw_loop.py
@@ -330,7 +330,7 @@ class DumbbellCurlFSM:
         try:
             tmp = "/dev/shm/violation_alert.txt.tmp"
             with open(tmp, "w", encoding="utf-8") as f:
-                f.write("弯举不标准，请收缩到位")
+                f.write("不标准")
             os.rename(tmp, "/dev/shm/violation_alert.txt")
             logging.warning("🔊 弯举违规警报已发送到 voice_daemon")
         except Exception as e:
diff --git a/hardware_engine/voice_daemon.py b/hardware_engine/voice_daemon.py
index 64c052b..5e79289 100644
--- a/hardware_engine/voice_daemon.py
+++ b/hardware_engine/voice_daemon.py
@@ -178,7 +178,8 @@ def record_with_vad(timeout=VAD_TIMEOUT):
 
     # VAD 阈值可通过 env 调节 (修复 baseline 虚高导致唤醒失败的问题)
     VAD_MIN = int(os.environ.get("VOICE_VAD_MIN", "250"))
-    VAD_DELTA = int(os.environ.get("VOICE_VAD_DELTA", "120"))
+    VAD_DELTA = int(os.environ.get("VOICE_VAD_DELTA", "40"))
+    VAD_DEBUG = os.environ.get("VOICE_VAD_DEBUG", "0") == "1"
     baseline = sum(noise_samples) / len(noise_samples) if noise_samples else 300
     threshold = max(VAD_MIN, baseline + VAD_DELTA)
     logging.info("VAD校准: baseline=%.0f threshold=%.0f (min=%d delta=%d)", baseline, threshold, VAD_MIN, VAD_DELTA)
@@ -198,11 +199,16 @@ def record_with_vad(timeout=VAD_TIMEOUT):
             arr = np.frombuffer(data, dtype=np.int16).astype(np.float32)
             rms = float(np.sqrt(np.mean(np.square(arr))))
 
+            # Debug: 实时打印 RMS vs threshold
+            if VAD_DEBUG:
+                logging.info("[VAD_DBG] rms=%.0f thresh=%.0f started=%s", rms, threshold, started)
+
             if not started:
                 if rms > threshold:
                     started = True
                     audio_frames.extend(pre_roll)
                     audio_frames.append(data)
+                    logging.info("[VAD] 触发! rms=%.0f > thresh=%.0f", rms, threshold)
                 else:
                     pre_roll.append(data)
             else:
@@ -447,28 +453,40 @@ def main():
                     _deliver_to_fsm(remaining)
                 continue
 
-            # 无后续指令 — 提示说话
-            speak(client, "我在，请说", allow_interrupt=False)
-
-            # 等待用户说具体内容
+            # 无后续指令 — 提示说话后进入连续对话模式 (参考 main2.py ai_copilot_worker)
+            speak(client, "我在", allow_interrupt=False)
             try:
                 open("/dev/shm/chat_active", "w").close()
             except OSError:
                 pass
 
-            status2 = record_with_vad(timeout=VAD_TIMEOUT)
-            if status2 == "SUCCESS":
-                text2 = sound2text(client)
-                if text2 and len(text2) >= 2:
-                    logging.info("对话内容: %s", text2)
-                    # 先尝试系统命令
-                    if not _try_voice_command(client, text2):
+            # 连续对话循环: 持续听 3 轮, 每轮空录超时则退出回待机
+            _silence_rounds = 0
+            _MAX_SILENCE_ROUNDS = 3
+            while _silence_rounds < _MAX_SILENCE_ROUNDS:
+                status2 = record_with_vad(timeout=VAD_TIMEOUT)
+                if status2 == "SUCCESS":
+                    text2 = sound2text(client)
+                    if text2 and len(text2) >= 2:
+                        _silence_rounds = 0  # 重置静默计数
+                        logging.info("[对话] 用户: %s", text2)
+                        # 退出条件
+                        if any(w in text2 for w in ["再见", "退下", "结束对话"]):
+                            speak(client, "好的，有事再叫我")
+                            break
+                        # 先尝试系统命令 (切模式/静音/疲劳上限)
+                        if _try_voice_command(client, text2):
+                            continue
+                        # 常规对话 → 通过 chat_input.txt 走 FSM + DeepSeek 回复
                         _deliver_to_fsm(text2)
+                    else:
+                        _silence_rounds += 1
                 else:
-                    speak(client, "没听清，请再说一次")
-            else:
-                speak(client, "没有听到声音")
+                    _silence_rounds += 1
+                    if _silence_rounds < _MAX_SILENCE_ROUNDS:
+                        logging.info("[对话] 静默 %d/%d，继续监听", _silence_rounds, _MAX_SILENCE_ROUNDS)
 
+            logging.info("[对话] 连续 %d 轮无声，退回待机", _MAX_SILENCE_ROUNDS)
             try:
                 os.remove("/dev/shm/chat_active")
             except OSError:
diff --git a/scripts/ironbuddy-evening.service b/scripts/ironbuddy-evening.service
new file mode 100644
index 0000000..45b56f2
--- /dev/null
+++ b/scripts/ironbuddy-evening.service
@@ -0,0 +1,9 @@
+[Unit]
+Description=IronBuddy evening summary one-shot
+After=network-online.target
+
+[Service]
+Type=oneshot
+WorkingDirectory=/home/toybrick/streamer_v3
+ExecStart=/usr/bin/python3 /home/toybrick/streamer_v3/scripts/ironbuddy_scheduler.py --mode=evening
+User=toybrick
diff --git a/scripts/ironbuddy-evening.timer b/scripts/ironbuddy-evening.timer
new file mode 100644
index 0000000..579f0ca
--- /dev/null
+++ b/scripts/ironbuddy-evening.timer
@@ -0,0 +1,9 @@
+[Unit]
+Description=IronBuddy evening summary (9pm)
+
+[Timer]
+OnCalendar=*-*-* 21:00:00
+Persistent=true
+
+[Install]
+WantedBy=timers.target
diff --git a/scripts/ironbuddy-morning.service b/scripts/ironbuddy-morning.service
new file mode 100644
index 0000000..cc49113
--- /dev/null
+++ b/scripts/ironbuddy-morning.service
@@ -0,0 +1,9 @@
+[Unit]
+Description=IronBuddy morning reminder one-shot
+After=network-online.target
+
+[Service]
+Type=oneshot
+WorkingDirectory=/home/toybrick/streamer_v3
+ExecStart=/usr/bin/python3 /home/toybrick/streamer_v3/scripts/ironbuddy_scheduler.py --mode=morning
+User=toybrick
diff --git a/scripts/ironbuddy-morning.timer b/scripts/ironbuddy-morning.timer
new file mode 100644
index 0000000..e01dfe4
--- /dev/null
+++ b/scripts/ironbuddy-morning.timer
@@ -0,0 +1,9 @@
+[Unit]
+Description=IronBuddy morning reminder (9am)
+
+[Timer]
+OnCalendar=*-*-* 09:00:00
+Persistent=true
+
+[Install]
+WantedBy=timers.target
diff --git a/scripts/ironbuddy_scheduler.py b/scripts/ironbuddy_scheduler.py
new file mode 100644
index 0000000..928e950
--- /dev/null
+++ b/scripts/ironbuddy_scheduler.py
@@ -0,0 +1,181 @@
+#!/usr/bin/env python3
+# -*- coding: utf-8 -*-
+"""IronBuddy 定时推送调度器 (systemd timer 触发)
+
+功能：
+  - 每次触发（建议每日早 9 点 + 晚 9 点 + 手动）
+  - 从 SQLite 读过去 24h 训练数据
+  - 根据规则生成提醒文本（含 DeepSeek 可选调用）
+  - 推送到飞书 webhook
+  - 入库 llm_log
+
+用法：
+  python3 scripts/ironbuddy_scheduler.py            # 单次跑
+  python3 scripts/ironbuddy_scheduler.py --mode=morning
+  python3 scripts/ironbuddy_scheduler.py --dry-run  # 不推送只打印
+
+systemd timer 配置（未来）：
+  /etc/systemd/system/ironbuddy-scheduler.timer
+  [Unit] Description=Daily IronBuddy push
+  [Timer] OnCalendar=*-*-* 09,21:00
+  [Install] WantedBy=timers.target
+"""
+from __future__ import print_function
+import argparse
+import json
+import logging
+import os
+import sys
+import time
+from datetime import datetime, timedelta
+
+# 保证可以 import 项目模块
+_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
+_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
+sys.path.insert(0, _PROJECT_ROOT)
+sys.path.insert(0, os.path.join(_PROJECT_ROOT, "hardware_engine"))
+
+logging.basicConfig(
+    level=logging.INFO,
+    format='%(asctime)s - [SCHEDULER] - %(message)s',
+)
+
+try:
+    from persistence.db import FitnessDB
+except Exception as e:
+    logging.error("DB import failed: %s", e)
+    sys.exit(1)
+
+
+# ===== 飞书推送 =====
+def push_feishu(webhook_url, text):
+    """推送到飞书机器人。text 可以是 markdown。"""
+    if not webhook_url:
+        logging.warning("飞书 webhook 未配置，跳过推送")
+        return False
+    try:
+        import urllib.request
+        payload = json.dumps({
+            "msg_type": "text",
+            "content": {"text": text}
+        }).encode("utf-8")
+        req = urllib.request.Request(
+            webhook_url,
+            data=payload,
+            headers={"Content-Type": "application/json"}
+        )
+        resp = urllib.request.urlopen(req, timeout=8)
+        ok = resp.getcode() == 200
+        logging.info("飞书推送 %s: %s", "成功" if ok else "失败", resp.getcode())
+        return ok
+    except Exception as e:
+        logging.error("飞书推送异常: %s", e)
+        return False
+
+
+# ===== 文本生成规则（无 LLM 版本）=====
+def build_reminder(db, mode="auto"):
+    """根据数据库统计生成提醒文本。"""
+    recent = db.get_recent_sessions(limit=20)
+    today = datetime.now().strftime("%Y-%m-%d")
+    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
+
+    today_stats = db.get_range_stats(today, today) if hasattr(db, "get_range_stats") else []
+    yest_stats = db.get_range_stats(yesterday, yesterday) if hasattr(db, "get_range_stats") else []
+
+    today_done = today_stats[0] if today_stats else None
+    yest_done = yest_stats[0] if yest_stats else None
+
+    lines = []
+    lines.append("【IronBuddy 训练提醒】")
+
+    if mode == "morning":
+        lines.append("早上好。")
+        if yest_done and yest_done.get("total_good", 0) > 0:
+            lines.append("昨天完成 %d 个标准动作，违规 %d 个。" % (
+                yest_done.get("total_good", 0),
+                yest_done.get("total_failed", 0),
+            ))
+        else:
+            lines.append("昨天未训练。建议今日安排 1 组深蹲或弯举。")
+        lines.append("建议训练时段：上午 10 点 / 下午 5 点。")
+
+    elif mode == "evening":
+        lines.append("晚间总结。")
+        if today_done and today_done.get("total_good", 0) > 0:
+            rate = 0
+            total = today_done.get("total_good", 0) + today_done.get("total_failed", 0)
+            if total > 0:
+                rate = int(100 * today_done.get("total_good", 0) / total)
+            lines.append("今日训练 %d 次（合格 %d，合格率 %d%%），疲劳累计 %.0f。" % (
+                today_done.get("session_count", 0),
+                today_done.get("total_good", 0),
+                rate,
+                today_done.get("total_fatigue", 0),
+            ))
+        else:
+            lines.append("今日尚未训练。赶在睡前补一组 10 分钟的弯举。")
+
+    else:  # auto
+        if today_done and today_done.get("total_good", 0) > 0:
+            lines.append("今日已完成 %d 次训练。" % today_done.get("session_count", 0))
+        else:
+            lines.append("今日未训练，距离上次训练 %d 天。" % (
+                _days_since_last(recent)
+            ))
+
+    lines.append("——@IronBuddy 教练助手")
+    return "\n".join(lines)
+
+
+def _days_since_last(sessions):
+    if not sessions:
+        return 999
+    try:
+        last_ts = sessions[0].get("started_at")
+        if not last_ts:
+            return 999
+        dt = datetime.strptime(last_ts[:10], "%Y-%m-%d")
+        return (datetime.now() - dt).days
+    except Exception:
+        return 999
+
+
+# ===== 主入口 =====
+def main():
+    parser = argparse.ArgumentParser(description="IronBuddy 定时推送")
+    parser.add_argument("--mode", choices=["morning", "evening", "auto"], default="auto")
+    parser.add_argument("--dry-run", action="store_true", help="只打印，不推送")
+    args = parser.parse_args()
+
+    db = FitnessDB()
+    db.connect()
+
+    webhook = db.get_config("feishu_webhook", "")
+    text = build_reminder(db, mode=args.mode)
+
+    logging.info("生成文本:\n%s", text)
+
+    if args.dry_run:
+        logging.info("[dry-run] 未推送")
+        return
+
+    pushed = push_feishu(webhook, text)
+
+    # 入库 llm_log (trigger=scheduler)
+    try:
+        db.log_llm(
+            trigger="scheduler/%s" % args.mode,
+            prompt="mode=%s" % args.mode,
+            response=text,
+            tokens_in=0,
+            tokens_out=0,
+        )
+    except Exception as e:
+        logging.warning("llm_log 入库失败: %s", e)
+
+    sys.exit(0 if pushed else 2)
+
+
+if __name__ == "__main__":
+    main()
diff --git a/streamer_app.py b/streamer_app.py
index a3245c3..066ea03 100644
--- a/streamer_app.py
+++ b/streamer_app.py
@@ -266,7 +266,7 @@ def chat_draft():
 
 @app.route('/api/mute', methods=['POST'])
 def api_mute():
-    """Write mute signal for voice daemon."""
+    """Mute/unmute: (1) write signal for voice daemon TTS gating, (2) hard-mute system Speaker via amixer."""
     try:
         data = request.get_json(force=True, silent=True) or {}
         muted = bool(data.get("muted", False))
@@ -276,6 +276,17 @@ def api_mute():
         with open(tmp_path, "w", encoding="utf-8") as f:
             f.write(payload)
         os.rename(tmp_path, target_path)
+        # Hard-mute: amixer control Speaker channel directly (drops L0 alarms too)
+        try:
+            import subprocess
+            if muted:
+                subprocess.run(["sudo", "-n", "amixer", "-c", "0", "sset", "Speaker", "0%", "mute"],
+                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
+            else:
+                subprocess.run(["sudo", "-n", "amixer", "-c", "0", "sset", "Speaker", "80%", "unmute"],
+                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
+        except Exception:
+            pass  # amixer may not have sudo NOPASSWD; voice daemon gating still works
         return Response(json.dumps({"ok": True, "muted": muted}), mimetype='application/json')
     except Exception as e:
         return Response(json.dumps({"ok": False, "error": str(e)}),
diff --git a/templates/index.html b/templates/index.html
index c4543f7..36b317e 100644
--- a/templates/index.html
+++ b/templates/index.html
@@ -210,6 +210,26 @@
         .left-panel::-webkit-scrollbar { width: 4px; }
         .left-panel::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }
 
+        /* Center: Kinematics Rig 独立中间列 (视频↔服务状态之间) */
+        .center-panel {
+            flex: 0 0 200px;
+            padding: 14px 8px;
+            display: flex;
+            flex-direction: column;
+            gap: 8px;
+            border-left: 1px solid var(--border);
+            background: rgba(0,0,0,0.15);
+            overflow-y: auto;
+        }
+        .center-panel .center-title {
+            font-size: 0.7em;
+            color: var(--text-muted);
+            letter-spacing: 0.12em;
+            text-transform: uppercase;
+            padding-left: 2px;
+            font-weight: 600;
+        }
+
         /* Video area */
         .video-container {
             position: relative;
@@ -1227,40 +1247,6 @@
                 </div>
             </div>
 
-            <!-- Kinematics Rig (CSS 2.5D skeleton, compact backfill from a5f954f, dual-exercise support) -->
-            <div id="kinematicsRigStage" style="position:relative; width:100%; height:170px; margin:4px 0; perspective:600px; padding-top:6px; overflow:hidden; flex-shrink:0; background:rgba(0,0,0,0.2); border:1px solid var(--border); border-radius:var(--radius);">
-                <!-- Glow halo (fatigue gradient + flash) -->
-                <div id="rigGlow" class="rig-glow" style="position:absolute; top:50%; left:50%; width:80px; height:120px; transform:translate(-50%,-50%); border-radius:50%; box-shadow:0 0 20px rgba(52,211,153,0.15); transition:box-shadow 0.2s ease-out; z-index:0; pointer-events:none;"></div>
-
-                <!-- Body root (torso + head, pivots for squat lean) -->
-                <div id="rigBody" style="position:absolute; left:50%; top:8px; width:28px; transform:translateX(-50%); z-index:2; transition:transform 0.15s cubic-bezier(0.175,0.885,0.32,1.275);">
-                    <!-- Torso -->
-                    <div style="width:22px; height:52px; background:linear-gradient(180deg,#60a5fa,#3b82f6); border-radius:11px; margin:0 auto; box-shadow:0 0 10px rgba(96,165,250,0.6); position:relative;">
-                        <!-- Head ring -->
-                        <div style="position:absolute; top:-22px; left:-2px; width:26px; height:26px; border-radius:50%; border:2px solid #60a5fa; box-shadow:inset 0 0 6px #60a5fa, 0 0 10px #60a5fa;"></div>
-                    </div>
-
-                    <!-- Thigh > Calf (squat mode) -->
-                    <div id="rigThigh" style="position:absolute; top:46px; left:5px; width:16px; height:46px; background:linear-gradient(180deg,#3b82f6,#1d4ed8); border-radius:8px; transform-origin:8px 8px; transition:transform 0.15s cubic-bezier(0.175,0.885,0.32,1.275), opacity 0.2s, box-shadow 0.3s; box-shadow:0 0 10px rgba(59,130,246,0.6); z-index:1;">
-                        <div id="rigCalf" style="position:absolute; top:38px; left:1px; width:14px; height:46px; background:linear-gradient(180deg,#1d4ed8,#1e3a8a); border-radius:7px; transform-origin:7px 7px; transition:transform 0.15s cubic-bezier(0.175,0.885,0.32,1.275); box-shadow:0 0 10px rgba(29,78,216,0.6);">
-                            <!-- Foot plate -->
-                            <div style="position:absolute; bottom:0; left:-6px; width:26px; height:4px; background:#e2e8f0; border-radius:2px; box-shadow:0 0 6px #e2e8f0;"></div>
-                        </div>
-                    </div>
-
-                    <!-- UpperArm > Forearm (bicep curl mode, opacity=0 default) -->
-                    <div id="rigUpperArm" style="position:absolute; top:10px; left:20px; width:12px; height:36px; background:linear-gradient(180deg,#f59e0b,#d97706); border-radius:6px; transform-origin:6px 6px; transition:transform 0.15s cubic-bezier(0.175,0.885,0.32,1.275), opacity 0.2s, box-shadow 0.3s; box-shadow:0 0 10px rgba(245,158,11,0.6); z-index:3; opacity:0;">
-                        <div id="rigForearm" style="position:absolute; top:30px; left:1px; width:11px; height:36px; background:linear-gradient(180deg,#d97706,#92400e); border-radius:6px; transform-origin:5px 5px; transition:transform 0.15s cubic-bezier(0.175,0.885,0.32,1.275); box-shadow:0 0 10px rgba(217,119,6,0.6);">
-                            <!-- Dumbbell -->
-                            <div style="position:absolute; bottom:-3px; left:-8px; width:26px; height:8px; background:linear-gradient(90deg,#334155,#64748b,#334155); border-radius:2px; box-shadow:0 0 6px rgba(100,116,139,0.8);"></div>
-                        </div>
-                    </div>
-                </div>
-
-                <!-- Status text (bottom overlay) -->
-                <div id="rigStatusText" style="position:absolute; bottom:3px; left:0; right:0; z-index:4; font-size:0.7em; color:var(--text-muted); text-align:center;">训练状态光效</div>
-            </div>
-
             <!-- Stats -->
             <div class="stats-row">
                 <div class="stat-card">
@@ -1324,6 +1310,30 @@
             </div>
         </div>
 
+        <!-- CENTER PANEL: Kinematics Rig (视频↔服务状态之间) -->
+        <aside class="center-panel" id="centerPanel">
+            <div class="center-title">骨架渲染</div>
+            <div id="kinematicsRigStage" style="position:relative; width:100%; height:280px; perspective:800px; padding-top:10px; overflow:hidden; background:rgba(0,0,0,0.25); border:1px solid var(--border); border-radius:var(--radius);">
+                <div id="rigGlow" class="rig-glow" style="position:absolute; top:50%; left:50%; width:110px; height:180px; transform:translate(-50%,-50%); border-radius:50%; box-shadow:0 0 20px rgba(52,211,153,0.15); transition:box-shadow 0.2s ease-out; z-index:0; pointer-events:none;"></div>
+                <div id="rigBody" style="position:absolute; left:50%; top:12px; width:36px; transform:translateX(-50%); z-index:2; transition:transform 0.15s cubic-bezier(0.175,0.885,0.32,1.275);">
+                    <div style="width:30px; height:78px; background:linear-gradient(180deg,#60a5fa,#3b82f6); border-radius:15px; margin:0 auto; box-shadow:0 0 12px rgba(96,165,250,0.6); position:relative;">
+                        <div style="position:absolute; top:-30px; left:-2px; width:34px; height:34px; border-radius:50%; border:3px solid #60a5fa; box-shadow:inset 0 0 8px #60a5fa, 0 0 12px #60a5fa;"></div>
+                    </div>
+                    <div id="rigThigh" style="position:absolute; top:68px; left:6px; width:22px; height:70px; background:linear-gradient(180deg,#3b82f6,#1d4ed8); border-radius:11px; transform-origin:11px 11px; transition:transform 0.15s cubic-bezier(0.175,0.885,0.32,1.275), opacity 0.2s, box-shadow 0.3s; box-shadow:0 0 12px rgba(59,130,246,0.6); z-index:1;">
+                        <div id="rigCalf" style="position:absolute; top:58px; left:2px; width:18px; height:70px; background:linear-gradient(180deg,#1d4ed8,#1e3a8a); border-radius:9px; transform-origin:9px 9px; transition:transform 0.15s cubic-bezier(0.175,0.885,0.32,1.275); box-shadow:0 0 12px rgba(29,78,216,0.6);">
+                            <div style="position:absolute; bottom:0; left:-8px; width:34px; height:6px; background:#e2e8f0; border-radius:3px; box-shadow:0 0 8px #e2e8f0;"></div>
+                        </div>
+                    </div>
+                    <div id="rigUpperArm" style="position:absolute; top:14px; left:26px; width:16px; height:52px; background:linear-gradient(180deg,#f59e0b,#d97706); border-radius:8px; transform-origin:8px 8px; transition:transform 0.15s cubic-bezier(0.175,0.885,0.32,1.275), opacity 0.2s, box-shadow 0.3s; box-shadow:0 0 12px rgba(245,158,11,0.6); z-index:3; opacity:0;">
+                        <div id="rigForearm" style="position:absolute; top:44px; left:1px; width:14px; height:52px; background:linear-gradient(180deg,#d97706,#92400e); border-radius:7px; transform-origin:7px 7px; transition:transform 0.15s cubic-bezier(0.175,0.885,0.32,1.275); box-shadow:0 0 12px rgba(217,119,6,0.6);">
+                            <div style="position:absolute; bottom:-4px; left:-10px; width:34px; height:10px; background:linear-gradient(90deg,#334155,#64748b,#334155); border-radius:3px; box-shadow:0 0 8px rgba(100,116,139,0.8);"></div>
+                        </div>
+                    </div>
+                </div>
+                <div id="rigStatusText" style="position:absolute; bottom:6px; left:0; right:0; z-index:4; font-size:0.75em; color:var(--text-muted); text-align:center;">训练状态光效</div>
+            </div>
+        </aside>
+
         <!-- RIGHT SIDEBAR -->
         <aside class="sidebar" id="sidebar">
             <div class="tab-bar">
```
