from pathlib import Path

from hardware_engine.sensor import mvc_calibration as mvc


def test_mvc_values_from_legacy_and_schema_v2_payloads():
    assert mvc.values_from_payload({"target": 780.0, "comp": 443.0}) == {
        "target": 780.0,
        "comp": 443.0,
    }
    assert mvc.values_from_payload({
        "mvc_values": {"target": 900.0, "comp": 500.0},
        "peak_mvc": {"ch0": 1.0, "ch1": 1.0},
    }) == {"target": 900.0, "comp": 500.0}
    assert mvc.values_from_payload({"peak_mvc": {"ch0": 25.0, "ch1": 3000.0}}) == {
        "target": 50.0,
        "comp": 2000.0,
    }


def test_mvc_build_payload_is_backward_compatible():
    payload = mvc.build_payload(
        780.8,
        443.1,
        user_id="user_01",
        exercise="bicep_curl",
        source="test",
        ts=1776442139.27,
    )

    assert payload["schema_version"] == 2
    assert payload["target"] == payload["mvc_values"]["target"]
    assert payload["comp"] == payload["mvc_values"]["comp"]
    assert payload["peak_mvc"] == {"ch0": 780.8, "ch1": 443.1}
    assert payload["calibration_id"].startswith("user_01:bicep_curl:")


def test_udp_server_uses_shared_mvc_helpers():
    src = Path("hardware_engine/sensor/udp_emg_server.py").read_text(encoding="utf-8")

    assert "mvc_calibration" in src
    assert "_load_mvc_values" in src
    assert "_build_mvc_payload" in src
