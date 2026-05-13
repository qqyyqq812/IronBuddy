"""DB migration and rep-event logging contract for competition runs."""
import sqlite3

from hardware_engine.persistence.db import FitnessDB


REP_EVENT_COLUMNS = (
    "exercise",
    "rep_index",
    "visual_result",
    "model_class",
    "model_confidence",
    "model_similarity",
    "classification_source",
    "angle_metric",
    "rom",
    "emg_ok",
    "is_demo_seed",
)


def _columns(conn, table):
    return [row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table)]


def test_rep_event_migration_adds_incremental_columns(tmp_path):
    db = FitnessDB(str(tmp_path / "ironbuddy.db"))
    conn = db.connect()
    assert conn is not None

    columns = _columns(conn, "rep_events")
    for name in REP_EVENT_COLUMNS:
        assert name in columns


def test_log_rep_records_event_fields_and_real_min_angle(tmp_path):
    db = FitnessDB(str(tmp_path / "ironbuddy.db"))
    conn = db.connect()
    sid = db.start_session("bicep_curl")

    db.log_rep(
        sid,
        True,
        54.2,
        37.0,
        12.5,
        exercise="bicep_curl",
        rep_index=7,
        visual_result="standard",
        model_class="compensating",
        model_confidence=0.87,
        model_similarity=0.62,
        classification_source="gru",
        angle_metric="elbow_angle",
        rom=101.5,
        emg_ok=True,
    )

    row = conn.execute(
        "SELECT * FROM rep_events WHERE session_id=? ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone()
    assert row["angle_min"] == 54.2
    assert row["angle_min"] != 999.0
    assert row["exercise"] == "bicep_curl"
    assert row["rep_index"] == 7
    assert row["visual_result"] == "standard"
    assert row["model_class"] == "compensating"
    assert row["model_confidence"] == 0.87
    assert row["model_similarity"] == 0.62
    assert row["classification_source"] == "gru"
    assert row["angle_metric"] == "elbow_angle"
    assert row["rom"] == 101.5
    assert row["emg_ok"] == 1
    assert row["is_demo_seed"] == 0


def test_session_counts_can_be_updated_without_ending_session(tmp_path):
    db = FitnessDB(str(tmp_path / "ironbuddy.db"))
    conn = db.connect()
    sid = db.start_session("squat")

    db.update_session_counts(sid, good=2, failed=1, fatigue_peak=428.5)

    row = conn.execute(
        "SELECT ended_at, good_count, failed_count, fatigue_peak "
        "FROM training_sessions WHERE id=?",
        (sid,),
    ).fetchone()
    assert row["ended_at"] is None
    assert row["good_count"] == 2
    assert row["failed_count"] == 1
    assert row["fatigue_peak"] == 428.5


def test_buffered_runtime_writes_flush_on_demand(tmp_path, monkeypatch):
    stage = tmp_path / "ironbuddy_stage.json"
    monkeypatch.setenv("IRONBUDDY_DB_STAGE_FILE", str(stage))

    db = FitnessDB(str(tmp_path / "buffered.db"), buffer_writes=True)
    conn = db.connect()
    sid = db.start_session("squat")
    db.log_rep(
        sid,
        True,
        61.2,
        10.0,
        4.0,
        exercise="squat",
        rep_index=1,
        visual_result="standard",
    )
    db.update_session_counts(sid, good=1, failed=0, fatigue_peak=180.0)
    db.end_session(sid, good=1, failed=0, fatigue_peak=180.0)

    assert conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM rep_events").fetchone()[0] == 0

    flush = db.flush_deferred_writes(finalize_open_sessions=True)
    assert flush["ok"] is True
    assert flush["sessions"] == 1
    assert flush["rep_events"] == 1

    row = conn.execute(
        "SELECT exercise, good_count, failed_count, fatigue_peak, ended_at "
        "FROM training_sessions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    rep = conn.execute(
        "SELECT angle_min, rep_index, visual_result FROM rep_events "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["exercise"] == "squat"
    assert row["good_count"] == 1
    assert row["failed_count"] == 0
    assert row["fatigue_peak"] == 180.0
    assert row["ended_at"] is not None
    assert rep["angle_min"] == 61.2
    assert rep["rep_index"] == 1
    assert rep["visual_result"] == "standard"


def test_migration_is_idempotent_on_existing_legacy_db(tmp_path):
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE rep_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id INTEGER, ts TEXT NOT NULL, is_good INTEGER NOT NULL, "
        "angle_min REAL, emg_target REAL, emg_comp REAL)"
    )
    conn.execute(
        "CREATE TABLE training_sessions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, "
        "ended_at TEXT, exercise TEXT NOT NULL, good_count INTEGER DEFAULT 0, "
        "failed_count INTEGER DEFAULT 0, fatigue_peak REAL DEFAULT 0, "
        "duration_sec INTEGER DEFAULT 0)"
    )
    conn.commit()
    conn.close()

    db = FitnessDB(path)
    first = db.connect()
    db.connect()
    columns = _columns(first, "rep_events")
    for name in REP_EVENT_COLUMNS:
        assert name in columns
