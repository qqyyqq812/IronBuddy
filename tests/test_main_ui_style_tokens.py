"""Static checks for V7.36 Linear-style UI token additions.

Goal: ensure new --bg-primary / --bg-card / button hierarchy / status glow
are added WITHOUT removing the V4.7 token system that the rest of the UI
already depends on.
"""
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(PROJECT_ROOT, "templates", "index.html")


def _read():
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        return f.read()


def test_legacy_v47_tokens_kept():
    """V4.7 design tokens must not be deleted (rest of UI depends on them)."""
    src = _read()
    for token in ("--bg-deep", "--bg-glass", "--accent", "--text-muted",
                  "--shadow-md", "--space-md"):
        assert token in src, "regression: " + token + " token removed"


def test_v736_alias_tokens_added():
    """V7.36 introduces alias names matching the design doc, mapped to V4.7."""
    src = _read()
    for token in ("--bg-primary", "--bg-card", "--border-subtle",
                  "--accent-cyan", "--text-default", "--accent-orange"):
        assert token in src, "missing v7.36 alias: " + token


def test_button_layers_defined():
    src = _read()
    assert ".btn-primary" in src
    assert ".btn-secondary" in src
    assert ".btn-ghost" in src


def test_font_feature_settings_present():
    src = _read()
    assert '"cv11"' in src


def test_tab_panel_transition_present():
    src = _read()
    # Some transition must apply to .tab-panel for the 100ms feel
    idx = src.find(".tab-panel")
    assert idx != -1
    body = src[idx:idx + 800]
    assert "transition" in body or "transform" in body or "opacity" in body


def test_no_interaction_handler_changed_count():
    """Sanity: onclick count should stay at the order of ~30+ (Stage 3 is css only)."""
    src = _read()
    onclick_count = src.count("onclick=")
    assert onclick_count >= 30
