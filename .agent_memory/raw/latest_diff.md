# Latest Commit Diff
Commit Hash: 6d145204c44aad20f6e6e84b4f11dc747ce86e83
Timestamp: Tue Apr 21 00:13:38 CST 2026
```diff
commit 6d145204c44aad20f6e6e84b4f11dc747ce86e83
Author: qqyyqq812 <2957131097@qq.com>
Date:   Tue Apr 21 00:13:38 2026 +0800

    docs(voice): fill in M13 commit hash e594abe
---
 ...41\345\235\227\346\235\203\345\250\201\346\214\207\345\215\227.md" | 4 ++--
 1 file changed, 2 insertions(+), 2 deletions(-)

diff --git "a/docs/\351\252\214\346\224\266\350\241\250/\350\257\255\351\237\263\346\250\241\345\235\227\346\235\203\345\250\201\346\214\207\345\215\227.md" "b/docs/\351\252\214\346\224\266\350\241\250/\350\257\255\351\237\263\346\250\241\345\235\227\346\235\203\345\250\201\346\214\207\345\215\227.md"
index 5886469..d6554ce 100644
--- "a/docs/\351\252\214\346\224\266\350\241\250/\350\257\255\351\237\263\346\250\241\345\235\227\346\235\203\345\250\201\346\214\207\345\215\227.md"
+++ "b/docs/\351\252\214\346\224\266\350\241\250/\350\257\255\351\237\263\346\250\241\345\235\227\346\235\203\345\250\201\346\214\207\345\215\227.md"
@@ -272,7 +272,7 @@ FSM 写 violation_alert.txt  ──►  _violation_event ──►
 **commit 序列**（按时间顺序，每个独立可回退）：
 
 ```
-(new) [M13] voice: _try_deepseek_chat reads .api_config.json when env missing
+e594abe [M13] voice: _try_deepseek_chat reads .api_config.json when env missing
 e6789e7 [M12] fsm: fix _ds_lock deadlock + local fallback when LLM unavailable
 11a4a7e [M11] voice: keyword fuzz (vision+sensor, feishu) + single-turn kill-arecord
 e4fa21c [M10] fsm+voice: startup cleanup purges residual shm signals
@@ -745,7 +745,7 @@ cp hardware_engine/main_claw_loop.py.v7.12.bak hardware_engine/main_claw_loop.py
 | M10 | e4fa21c | `git revert e4fa21c` | 重启残留污染回归，弯举切换被回拉 |
 | M11 | 11a4a7e | `git revert 11a4a7e` | 视觉+传感/飞书近音识别变窄；单轮长录音再现 |
 | M12 | e6789e7 | `git revert e6789e7` | 疲劳满再触发死锁回归、LLM 不通时无语音播报 |
-| M13 | (commit 见 §8) | `git revert <hash>` | voice_daemon 闲聊总回 "网络有点慢" 回归 |
+| M13 | e594abe | `git revert e594abe` | voice_daemon 闲聊总回 "网络有点慢" 回归 |
 
 ---
 
```
