from pathlib import Path

import pytest

import tools.ironbuddy_promote_personal_bicep_gru as promote


def test_validate_candidate_accepts_compensation_gru_state(tmp_path):
    torch = pytest.importorskip("torch")
    from hardware_engine.cognitive.fusion_model import CompensationGRU

    candidate = tmp_path / "extreme_fusion_gru_bicep_personal_test.pt"
    model = CompensationGRU(input_size=7, hidden_size=16)
    torch.save(model.state_dict(), str(candidate))

    ok, detail = promote.validate_candidate(candidate)

    assert ok is True
    assert detail == "loadable"


def test_promotion_script_is_dry_run_by_default():
    src = Path(promote.__file__).read_text(encoding="utf-8")

    assert "--apply" in src
    assert "dry_run=true" in src
    assert "model_backups" in src
    assert "os.replace" in src
