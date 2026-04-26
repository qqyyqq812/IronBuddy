# Deprecated V3 Map Files

These files belong to the old V3_7D 全链路地图 era where:
- `train_model.py` was the trainer
- Models lived in `models/`
- A manual `cp models/*.pt hardware_engine/*.pt` step was required

**Replaced by**:
- `tools/train_gru_three_class.py` (squat) — writes `hardware_engine/extreme_fusion_gru.pt`
- `tools/train_gru_three_class_bicep.py` (curl) — writes `hardware_engine/extreme_fusion_gru_bicep.pt`

These trainers write directly to `hardware_engine/` so no manual copy is needed.

Archived 2026-04-26 (V7.30 refactor) to prevent confusion about which weight is loaded at runtime.
