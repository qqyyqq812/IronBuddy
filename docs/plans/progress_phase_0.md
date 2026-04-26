# Phase 0 Progress Report

**Phase**：0 — 多模态 bug 修补
**Date**：2026-04-27（automated overnight session）
**Branch**：main
**Status**：Code refactor done; validation deferred [manual-pending]

---

## 完成的 Task

### ✅ Task 0.1：M1 — Ang_Vel 列推理时归一化对齐训练
- `hardware_engine/main_claw_loop.py:~1062` 加入 `window[:,0] = clip(/30, [-3,3])`
- `tests/test_main_claw_loop_normalization.py` 新增 4 个 unit test，全部 PASS
- Commit `89d2239`

### ✅ Task 0.2：M2 — 恢复 V7.15 FSM/GRU mode-gating
- 把 squat (`main_claw_loop.py:~386`) 和 curl (`~664`) 的 good/failed increment
  包到 `if self._mode_cache != "vision_sensor":` 守卫中
- pure_vision 模式仍走角度硬判定；vision_sensor 模式让 GRU 独占 good/failed/comp
- `tests/test_fsm_mode_gating.py` 用 AST 静态分析 + 文本检查（避免 cv2/torch 依赖），4 个 test 全部 PASS
- Commit `3de7749`

### ✅ Task 0.3：M3 — Symmetry 训练侧改 1.0
- `tools/train_gru_three_class.py:124-125` 注释掉 `comp[:,5] *= U(0.5, 0.75)`
- `tools/train_gru_three_class_bicep.py` 经验证不需要改动（用真实录制 bad 数据）
- `tests/test_train_symmetry_alignment.py` 新增 3 个 test，全部 PASS
- **重训 [manual-pending]**：早上跑 `python3 tools/train_gru_three_class.py --epochs 20`
- Commit `f02eabb`

### ✅ Task 0.4：M4 — 归档 deprecated V3 map files
- `tools/train_model.py` → `.archive/deprecated_v3map/` (`git mv`)
- `models/extreme_fusion_gru_squat.pt` + `_curl.pt` → `.archive/deprecated_v3map/` (mv only — `*.pt` 在 .gitignore)
- `.archive/deprecated_v3map/README.md` 写明替代关系
- `CLAUDE.md` 快速开始更新为新两个三类 trainer
- Commit `b0c41a5`

### 🟡 Task 0.5：simulator 三类验证 [manual-pending]
- 整 task 跳过（无法启动 FSM + simulator 验证组合）
- 在 design doc §10.1 写好早上手动验证步骤 + 三类期望
- Commit `a467479`

### ⏭️ Task 0.6：ESP32 微调
- 仅在 Task 0.5 失败时启用，跳过

---

## 跳过的 Step

| Task | 跳过的 Step | 原因 |
|---|---|---|
| 0.1 | Step 5 烟测推理 | 需要加载 .pt 模型 + 运行 forward，不在测试范围 |
| 0.3 | Step 3-4 重训 squat + curl | 重训命令不允许执行 |
| 0.5 | 全部 Step | 需要 FSM + simulator 联动，automated 无法验证 |

---

## 待手测项 (`grep "manual-pending" git log`)

```
f02eabb fix(M3): symmetry=1.0 in training to match inference [manual-pending]
a467479 docs(P0): add §10.1 Phase 0 validation log placeholder [manual-pending]
```

---

## 测试结果

```
tests/test_main_claw_loop_normalization.py     4 passed
tests/test_fsm_mode_gating.py                  4 passed
tests/test_train_symmetry_alignment.py         3 passed
============================== 11 passed ======================================
```

---

## 疑虑 / Risk Notes

1. **M3 fix 不重训不生效**：训练侧改了 sym 偏置，但旧模型 `extreme_fusion_gru.pt` 还是按旧分布训练的。早上必须重训才能验证 simulator 三类区分度。
2. **M2 mode-gating 测试是静态分析**：没用真实 cv2 帧驱动 FSM。测试只验证守卫语句存在；运行时正确性需要 simulator 跑通才能确认。
3. **CLAUDE.md 改动小但影响导航**：把 GRU 训练命令更新到了新 trainer，AG/CC 后续启动会读到新指令。
4. **scripts/switch_model.sh 仍引用旧路径**：未改。属于历史脚本，不在 plan 范围内，留给后续清理。

---

## 下一步

进入 Phase 1（语音状态机 + UI 协议 + VAD 边界）。
