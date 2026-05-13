# CC 的改动记录

## 2026-04-18 (硬件通讯警报解除)
**致 Claude Code:**
1. 之前的硬件阵亡判定已被 100% 证伪！RK3399ProX 的板载麦克风通道 (`hw:0,0`) 在进程释放后测试完美。
2. 你需要立刻撤销之前在环境系统或任何相关脚本里打的针对 USB外挂麦克风 的 `export VOICE_FORCE_MIC=plughw:2,0` 降级补丁。
3. 把所有的默认注意力放回原装的 Main Mic (`hw:0,0`)。
## 2026-04-16

- [15:04:10] src/models/pose_detector.py

- [15:04:02] src/test.py

## 2026-04-20 (DB 演示种子灌入)

- 执行 `scripts/migrate_2026_04_20.sql` + `scripts/seed_demo_2026_04.py`：新增 voice_sessions / preference_history / system_prompt_versions 三表，并补 is_demo_seed / summary / rec / prompt_version_id 列。
- 灌入 175 行演示种子（training_sessions ×3、rep_events ×143、llm_log ×10、voice_sessions ×8、daily_summary ×3、preference_history ×5、system_prompt_versions ×3）。所有种子行带 `is_demo_seed=1` 标记。
- 一键回滚：`python scripts/cleanup_demo_seed.py`（如需连同三张新表整表清空，加 `--purge-new-tables`）。
- 备份：`data/ironbuddy.db.bak_<unix_ts>`。

