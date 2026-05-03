"""Stage 4 tests: /database default view + per-table last_ts."""
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def test_main_ui_database_link_default_all():
    src = _read(os.path.join(PROJECT_ROOT, "templates", "index.html"))
    # Main UI link should default seed=all (was seed=seed which masked live data)
    assert 'href="/database?seed=all"' in src
    assert 'href="/database?seed=seed"' not in src


def test_database_html_renders_last_ts():
    src = _read(os.path.join(PROJECT_ROOT, "templates", "database.html"))
    assert "last_ts" in src
    assert "最后写入" in src


def test_api_db_tables_returns_last_ts():
    src = _read(os.path.join(PROJECT_ROOT, "streamer_app.py"))
    # /api/db/tables endpoint must populate last_ts per table
    idx = src.find("def api_db_tables")
    assert idx != -1
    body = src[idx:idx + 3500]
    assert "last_ts" in body
    # tries common timestamp columns
    assert "started_at" in body
    assert "MAX(" in body