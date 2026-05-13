"""Stage 4 tests: /database default view + per-table last_ts."""
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def test_main_ui_database_link_default_live():
    src = _read(os.path.join(PROJECT_ROOT, "templates", "index.html"))
    # Main UI link should not expose internal seed/fake filtering.
    assert 'href="/database"' in src
    assert 'href="/database?seed=live"' not in src
    assert 'href="/database?seed=seed"' not in src


def test_database_html_renders_last_ts():
    src = _read(os.path.join(PROJECT_ROOT, "templates", "database.html"))
    assert "last_ts" in src
    assert "最后写入" in src


def test_database_defaults_to_overview_and_grouped_raw_tabs():
    src = _read(os.path.join(PROJECT_ROOT, "templates", "database.html"))
    assert "IronBuddy 后台数据库" in src
    assert "{ key: '__overview'" in src
    assert "{ key: '__training'" in src
    assert "{ key: '__conversation'" in src
    assert "{ key: '__model'" in src
    assert "{ key: '__system'" in src
    assert "function renderSubtabs" in src
    assert "fetch('/api/db/overview'" in src
    # Raw table names should not be top-level tab keys anymore.
    tabs_block = src[src.find("const TABS = ["):src.find("const RAW_TABS")]
    assert "{ key: 'training_sessions'" not in tabs_block
    assert "{ key: 'voice_sessions'" not in tabs_block


def test_api_db_overview_endpoint_exists():
    src = _read(os.path.join(PROJECT_ROOT, "streamer_app.py"))
    assert "@app.route('/api/db/overview'" in src
    assert "def api_db_overview" in src
    assert "_build_data_overview" in src


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


def test_database_defaults_to_real_data_filter():
    src = _read(os.path.join(PROJECT_ROOT, "templates", "database.html"))
    assert "DEFAULT_SEED_FILTER" not in src
    assert "id=\"fSeed\"" not in src
    assert "仅演示种子" not in src
    assert "伪造" not in src
    assert "种子" not in src

    streamer = _read(os.path.join(PROJECT_ROOT, "streamer_app.py"))
    idx = streamer.find("def api_db_query")
    assert idx != -1
    body = streamer[idx:idx + 1800]
    assert "request.args.get('seed'" not in body
    assert "seed_col'] + '=0'" in body


def test_database_page_hides_maintenance_and_fake_cleanup_surfaces():
    html = _read(os.path.join(PROJECT_ROOT, "templates", "database.html"))
    assert "toggleMaint" not in html
    assert "__maintenance" not in html
    assert "db/maintenance" not in html
    assert "purge_fake_only" not in html

    src = _read(os.path.join(PROJECT_ROOT, "streamer_app.py"))
    assert "_MAINTENANCE_ACTIONS" not in src
    assert "@app.route('/api/db/maintenance" not in src
    assert "purge_fake_only" not in src
    assert "seed_v50" not in src
    assert "seed_models" not in src
    assert "cleanup_fake_data.py" not in src


def test_daily_plan_paths_persist_real_dialogue_events():
    src = _read(os.path.join(PROJECT_ROOT, "streamer_app.py"))
    assert "def _log_real_voice_session" in src
    assert "def _log_real_llm_event" in src
    publish = src[src.find("def _publish_plan_reply"):src.find("@app.route('/api/training_plan/daily'", src.find("def _publish_plan_reply"))]
    assert "_log_real_voice_session" in publish
    assert "_log_real_llm_event" in publish
    accept = src[src.find("def api_daily_training_plan_accept"):src.find("def _operator_run_dir")]
    assert "daily_plan_accept" in accept
    assert "_log_real_voice_session" in accept
    assert "_log_real_llm_event" in accept
