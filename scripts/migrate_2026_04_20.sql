-- IronBuddy DB 迁移：2026-04-20
-- 目的：新增 voice_sessions / preference_history / system_prompt_versions 三表，
--      并给现有表补 is_demo_seed 标记列与少量业务列（summary / rec / prompt_version_id）。
-- 使用：本 SQL 仅含 CREATE 语句；ALTER 部分由 migrate_2026_04_20.py 前置探测后执行。
-- 约束：幂等（IF NOT EXISTS），WAL 模式保持不变。

PRAGMA journal_mode=WAL;

-- ============ 新增：语音闲聊会话 ============
CREATE TABLE IF NOT EXISTS voice_sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,                 -- ISO8601
  transcript TEXT NOT NULL,         -- 用户原话（来自闲聊 STT）
  response TEXT,                    -- LLM 回复
  summary TEXT,                     -- 由 OpenClaw 每日 22:30 生成的一句话摘要
  duration_s REAL,
  trigger_src TEXT,                 -- 'chat' / 'wake_word' / 'fatigue_alert'
  is_demo_seed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_voice_ts ON voice_sessions(ts);

-- ============ 新增：偏好演化历史 ============
CREATE TABLE IF NOT EXISTS preference_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  field TEXT NOT NULL,              -- fatigue_tolerance / target_muscle_groups / ...
  old_value TEXT,
  new_value TEXT NOT NULL,
  source TEXT,                      -- 'llm_inference' / 'manual' / 'seed'
  confidence REAL,                  -- 0~1
  is_demo_seed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pref_ts ON preference_history(ts);

-- ============ 新增：系统提示词版本 ============
CREATE TABLE IF NOT EXISTS system_prompt_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  prompt_text TEXT NOT NULL,
  based_on_summary_ids TEXT,        -- JSON array 例 "[1,2,3]"
  active INTEGER DEFAULT 0,         -- 同时只能一个 active=1
  is_demo_seed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_prompt_active ON system_prompt_versions(active);

-- ALTER 部分：is_demo_seed / summary / rec / prompt_version_id
-- 由 Python 侧 PRAGMA table_info 检测后再决定是否执行，避免重复 ALTER 报错。
