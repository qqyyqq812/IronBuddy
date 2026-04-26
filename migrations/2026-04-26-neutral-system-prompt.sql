-- V7.30 R2 fix: insert neutral system_prompt that doesn't bias DeepSeek
-- toward "biceps preference" or "knee_caution" reminders.
--
-- Background:
--   Active system_prompt (id=12 as of inspection) hardcoded:
--     - "偏好肌群：biceps + quadriceps"
--     - "疲劳容忍：medium-low"
--     - knee caution
--   These leak into every chat response, making the demo feel scripted.
--
-- Behavior:
--   1. Insert a new neutral prompt as active=1.
--   2. Demote all previously-active rows to active=0.
--
-- Run order matters: insert first (so we never have zero active rows in
-- between), then demote everyone whose id != lastrowid. Use a transaction.

BEGIN TRANSACTION;

INSERT INTO system_prompt_versions (ts, prompt_text, based_on_summary_ids, active, is_demo_seed)
VALUES (
    datetime('now'),
    '你是 IronBuddy 健身教练。' ||
    '回答简短自然：3 句话以内，80 字以内，不用 markdown。' ||
    '当前用户的训练实况会作为上下文给你（动作类型/达标数/违规数/疲劳值），可参考但不强求引用。' ||
    '当用户问健身建议时，给专业、具体的建议，不预设用户偏好。' ||
    '你不能执行系统命令；如果用户表达类似指令意图，回复"这条指令请直接对系统说，例如 切到深蹲"。',
    NULL,
    1,
    0
);

UPDATE system_prompt_versions
SET active = 0
WHERE id != (SELECT MAX(id) FROM system_prompt_versions WHERE active = 1)
  AND active = 1;

COMMIT;

-- Verification (run separately):
--   sqlite3 data/ironbuddy.db "SELECT id, ts, substr(prompt_text,1,80), active FROM system_prompt_versions WHERE active=1"
-- Expected: exactly 1 row, the new neutral prompt, with current ts.
