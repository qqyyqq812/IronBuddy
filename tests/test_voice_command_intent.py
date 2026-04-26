"""V7.30 R1: '做' standalone removed from _EXPLICIT_CMD_MARKERS.

Static-source check + behavioral check via importable subset of voice_daemon.
The full module imports libasound — we work around with text inspection.
"""
import os
import re

VOICE_DAEMON = os.path.join(
    os.path.dirname(__file__), "..", "hardware_engine", "voice_daemon.py"
)


def _src():
    with open(VOICE_DAEMON, "r", encoding="utf-8") as f:
        return f.read()


def _extract_markers():
    src = _src()
    m = re.search(r"_EXPLICIT_CMD_MARKERS\s*=\s*\(([^)]+)\)", src, re.DOTALL)
    assert m, "_EXPLICIT_CMD_MARKERS assignment not found"
    body = m.group(1)
    raw = re.findall(r'u"([^"]+)"', body)
    # Source stores CJK as \u escapes (legacy editor); decode to real chars.
    return [s.encode("utf-8").decode("unicode_escape") for s in raw]


def test_explicit_cmd_markers_no_standalone_zuo():
    markers = _extract_markers()
    assert u"做" not in markers, "standalone '做' must be removed (R1)"


def test_explicit_cmd_markers_has_xiangzuo_compound():
    markers = _extract_markers()
    assert u"想做" in markers


def test_explicit_cmd_markers_keeps_qiehuan_qiedao():
    markers = _extract_markers()
    assert u"切换" in markers
    assert u"切到" in markers
    assert u"换到" in markers


def test_explicit_cmd_markers_keeps_kaishi_moshi():
    markers = _extract_markers()
    assert u"开始" in markers
    assert u"模式" in markers
