# V7.30 Manual Test Checklist (T1-T20)

> Driven by `docs/plans/2026-04-26-ironbuddy-refactor-design.md` §6.
> Run **after** the [manual-pending] gates from progress reports are cleared:
>   1. M3 squat retraining (`python3 tools/train_gru_three_class.py --epochs 20`)
>   2. R2 SQL migration applied
>   3. _realize_action wire-up confirmed (or rolled back)
>
> Mark each row PASS / FAIL / SKIP. Note FAIL reason.

---

## Phase 0 — Multimodal classification (post-M1+M2+M3)

| # | Steps | Expected | Actual |
|---|---|---|---|
| T_M1 | reset → start FSM → vision_sensor + squat → simulate_emg_from_mia.py --label standard | UI statGood++ over 30s; statComp=0; statFailed=0 | |
| T_M2 | as above, --label compensating | statComp++; statGood=0; statFailed=0 | |
| T_M3 | as above, --label non_standard | statFailed++; statGood=0; statComp=0 | |
| T_M4 | switch to bicep_curl; simulate_emg_from_bicep.py --label standard | statGood++ for curl | |
| T_M5 | curl --label compensating | statComp++ | |
| T_M6 | curl --label non_standard | statFailed++ | |
| T_M7 | observe a single rep transition | exactly +1 across the 3 counters total (no double-count) | |

If any T_M* fails: stop, debug GRU classifier (maybe Symmetry retraining didn't take effect, or thresholds shifted).

---

## Phase 1 — Voice state machine + UI

| # | Steps | Expected | Actual |
|---|---|---|---|
| T1  | "教练 现在适合做深蹲吗" | DeepSeek replies (≤ 3 sentences); auto returns LISTEN | |
| T2  | speak randomly for 30s without saying "教练" | nothing routes; only SLEEP-state log entries | |
| T3  | "教练" then talk continuously 6+ seconds | hard-cap kicks in at ~6s, no overshoot | |
| T4  | rapid sequence: "教练 切深蹲" / "教练 切弯举" / "教练 切纯视觉" | UI shows 3 distinct dialog bubble pairs (turn_id rotates) | |
| T5  | trigger fatigue auto-summary; speak in background | environment noise is NOT recorded during TTS | |
| T6  | "教练 开始 MVC" while curl mode | MVC flow runs; environment noise NOT recorded | |
| T7  | within one turn, STT delivers user_input then assistant_reply | UI updates same bubble pair (no new bubbles) | |

---

## Phase 2 — DeepSeek tools + implicit ack

| # | Steps | Expected | Actual |
|---|---|---|---|
| T8  | "切到深蹲" / "切到弯举" / "切到纯视觉" sequentially | each switch confirmed via TTS ack; FSM state matches | |
| T9  | "现在适合做深蹲吗" (chat, not command) | TIER B DeepSeek reply; NOT "没听清" | |
| T10 | "我膝盖酸" | TIER B DeepSeek reply | |
| T11 | "推送多少组" | DeepSeek picks push_feishu (or similar) — NOT report_status | |
| T12 | UI POST `/api/fatigue_limit` then 50ms later voice "调到800" | only one wins (latest ts); both signals visible in /dev/shm/intent_*.json + canonical | |
| T13 | re-run T1-T7 | all still PASS post-Phase-2 changes | |

---

## Phase 3 — Auto fatigue + MVC + shoot prep

| # | Steps | Expected | Actual |
|---|---|---|---|
| T14 | trigger auto-summary; speak random words during TTS | nothing recorded (ArecordGate.suspend held) | |
| T15 | during TTS, shout "教练" | DOES NOT interrupt; after TTS done, next "教练" works normally | |
| T16 | "切到弯举" → wait for MVC tip → "开始" → 3-2-1 countdown → 3.5s peak record → "测试结束" | full MVC sequence completes | |
| T17 | during MVC tip wait, say "我膝盖酸" | DeepSeek replies; MVC tip flow not broken | |
| T18 | "请关机" | TTS ack short farewell; system shuts down | |
| T19 | "切到深蹲" | implicit ack only (single TTS line "好，切到深蹲"); no triple confirm | |
| T20 | bash scripts/prepare_subvideo_squat.sh + prepare_subvideo_curl.sh | each prints OK + state files written | |

---

## Decision after run

- All PASS → proceed to Phase 4 shooting (subvideos + main video)
- ≥ 1 FAIL in Phase 0 → block; tune model retraining
- ≥ 1 FAIL in Phase 1/2/3 → fix and re-run only the failed test
- ≥ 3 FAILs total → halt and re-evaluate refactor plan
