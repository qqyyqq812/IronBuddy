"""V7.30 M3 修补测试：训练侧 Symmetry 偏置已注释，推理侧 sym=1.0 对齐。"""
import os

SQUAT_TRAINER = os.path.join(
    os.path.dirname(__file__), "..", "tools", "train_gru_three_class.py"
)
BICEP_TRAINER = os.path.join(
    os.path.dirname(__file__), "..", "tools", "train_gru_three_class_bicep.py"
)


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def test_squat_trainer_no_active_symmetry_bias():
    src = _read(SQUAT_TRAINER)
    # 旧偏置行存在但必须被注释掉
    for line in src.splitlines():
        stripped = line.strip()
        if "comp[:, 5]" in stripped and "uniform" in stripped:
            assert stripped.startswith("#"), (
                "active comp[:,5] *= uniform(...) line found — should be commented"
            )


def test_squat_trainer_has_m3_marker():
    src = _read(SQUAT_TRAINER)
    assert "M3" in src and "sym=1.0" in src


def test_bicep_trainer_does_not_synthesize_symmetry():
    src = _read(BICEP_TRAINER)
    # bicep trainer 用真实录制的 bad 数据，没有合成 symmetry 偏置
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not ("comp[:, 5]" in stripped and "uniform" in stripped), (
            "bicep trainer should not synthesize symmetry bias"
        )
