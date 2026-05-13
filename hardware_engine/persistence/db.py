"""
IronBuddy SQLite 持久化层

- Python 3.7 兼容：不使用 walrus `:=`、不使用 `X | None` 注解、不使用 match/case
- 无 pandas 依赖，仅 stdlib sqlite3
- 所有方法内部自吞异常，失败时返回安全默认值，不影响主流程
- DB 路径优先使用环境变量 IRONBUDDY_DB_PATH，否则：
    * 板端: /home/toybrick/streamer_v3/data/ironbuddy.db
    * 开发: ./data/ironbuddy.db （相对工作区）
"""

import os
import json
import sqlite3
import threading
import logging
import atexit
import time
from datetime import datetime, date, timedelta

try:
    import fcntl
except Exception:
    fcntl = None

_BOARD_DB = "/home/toybrick/streamer_v3/data/ironbuddy.db"
_DEV_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "ironbuddy.db",
)
_DEFER_WRITES_ENV = "IRONBUDDY_DB_DEFER_WRITES"


def _defer_stage_path():
    return os.environ.get(
        "IRONBUDDY_DB_STAGE_FILE", "/dev/shm/ironbuddy_db_stage.json"
    )


def _defer_stage_lock():
    return _defer_stage_path() + ".lock"


def _resolve_db_path():
    env_path = os.environ.get("IRONBUDDY_DB_PATH")
    if env_path:
        return env_path
    # 板端优先
    board_dir = os.path.dirname(_BOARD_DB)
    if os.path.isdir(board_dir):
        return _BOARD_DB
    return _DEV_DB


def _env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json_file(path, default=None):
    if default is None:
        default = {}
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _empty_deferred_payload():
    return {
        "version": 1,
        "updated_at": "",
        "sessions": {},
        "rep_events": [],
        "llm_log": [],
        "voice_sessions": [],
    }


def _normalize_deferred_payload(payload):
    if not isinstance(payload, dict):
        payload = {}
    data = _empty_deferred_payload()
    for key in data.keys():
        value = payload.get(key, data[key])
        if key == "sessions" and not isinstance(value, dict):
            value = {}
        elif key in ("rep_events", "llm_log", "voice_sessions") and not isinstance(value, list):
            value = []
        elif key in ("version", "updated_at") and value is None:
            value = data[key]
        data[key] = value
    return data


class _StageFileLock(object):
    def __enter__(self):
        self._lock_path = _defer_stage_lock()
        parent = os.path.dirname(self._lock_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        self._fh = open(self._lock_path, "a+")
        if fcntl is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self._fh

    def __exit__(self, exc_type, exc, tb):
        try:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass


_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS training_sessions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at    TEXT NOT NULL,
        ended_at      TEXT,
        exercise      TEXT NOT NULL,
        good_count    INTEGER DEFAULT 0,
        failed_count  INTEGER DEFAULT 0,
        fatigue_peak  REAL DEFAULT 0,
        duration_sec  INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rep_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER,
        ts          TEXT NOT NULL,
        is_good     INTEGER NOT NULL,
        angle_min   REAL,
        emg_target  REAL,
        emg_comp    REAL,
        FOREIGN KEY (session_id) REFERENCES training_sessions(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        trigger     TEXT,
        prompt      TEXT,
        response    TEXT,
        tokens_in   INTEGER DEFAULT 0,
        tokens_out  INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_summary (
        date           TEXT PRIMARY KEY,
        session_count  INTEGER DEFAULT 0,
        total_good     INTEGER DEFAULT 0,
        total_failed   INTEGER DEFAULT 0,
        total_fatigue  REAL DEFAULT 0,
        best_streak    INTEGER DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_config (
        key         TEXT PRIMARY KEY,
        value       TEXT,
        updated_at  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rep_session ON rep_events(session_id)",
    "CREATE INDEX IF NOT EXISTS idx_sess_started ON training_sessions(started_at)",
    "CREATE INDEX IF NOT EXISTS idx_llm_ts ON llm_log(ts)",
]

_EXTRA_COLUMNS = {
    "training_sessions": (
        ("is_demo_seed", "INTEGER DEFAULT 0"),
    ),
    "rep_events": (
        ("exercise", "TEXT"),
        ("rep_index", "INTEGER"),
        ("visual_result", "TEXT"),
        ("model_class", "TEXT"),
        ("model_confidence", "REAL"),
        ("model_similarity", "REAL"),
        ("classification_source", "TEXT"),
        ("angle_metric", "TEXT"),
        ("rom", "REAL"),
        ("emg_ok", "INTEGER DEFAULT 0"),
        ("is_demo_seed", "INTEGER DEFAULT 0"),
    ),
}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _maybe_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


class FitnessDB(object):
    """单连接 SQLite 封装，线程安全（check_same_thread=False + 锁）。"""

    def __init__(self, path=None, buffer_writes=None):
        if path is None:
            path = _resolve_db_path()
        self.path = path
        self._conn = None
        self._lock = threading.Lock()
        self.buffer_writes = (
            _env_flag(_DEFER_WRITES_ENV, False)
            if buffer_writes is None else bool(buffer_writes)
        )
        self._buffer_seq = 0
        self._buffer_registered = False

    # ---------- defer helpers ----------
    def _next_buffer_session_id(self):
        self._buffer_seq += 1
        return -int(time.time() * 1000) - self._buffer_seq

    def _with_stage_payload(self, mutator):
        with _StageFileLock():
            payload = _normalize_deferred_payload(
                _read_json_file(_defer_stage_path(), _empty_deferred_payload())
            )
            result = mutator(payload)
            payload["updated_at"] = _now()
            _atomic_write_json(_defer_stage_path(), payload)
            return result

    def _stage_session_upsert(self, session_id, exercise=None, started_at=None):
        sid = str(session_id)

        def _mutate(payload):
            sessions = payload["sessions"]
            row = sessions.get(sid) or {
                "session_id": int(session_id),
                "started_at": started_at or _now(),
                "ended_at": None,
                "exercise": exercise or "unknown",
                "good_count": 0,
                "failed_count": 0,
                "fatigue_peak": 0.0,
                "duration_sec": 0,
            }
            if exercise and not row.get("exercise"):
                row["exercise"] = exercise
            sessions[sid] = row
            return row

        return self._with_stage_payload(_mutate)

    def _update_staged_session(self, session_id, values):
        def _mutate(payload):
            sessions = payload["sessions"]
            row = sessions.get(str(session_id)) or {
                "session_id": int(session_id),
                "started_at": _now(),
                "ended_at": None,
                "exercise": "unknown",
                "good_count": 0,
                "failed_count": 0,
                "fatigue_peak": 0.0,
                "duration_sec": 0,
            }
            row.update(values or {})
            sessions[str(session_id)] = row
            return row

        return self._with_stage_payload(_mutate)

    @classmethod
    def get_deferred_status(cls):
        payload = _normalize_deferred_payload(
            _read_json_file(_defer_stage_path(), _empty_deferred_payload())
        )
        sessions = payload.get("sessions") or {}
        return {
            "enabled": _env_flag(_DEFER_WRITES_ENV, False),
            "path": _defer_stage_path(),
            "updated_at": payload.get("updated_at") or "",
            "pending_sessions": len(sessions),
            "pending_rep_events": len(payload.get("rep_events") or []),
            "pending_llm_log": len(payload.get("llm_log") or []),
            "pending_voice_sessions": len(payload.get("voice_sessions") or []),
            "pending_total": len(sessions)
            + len(payload.get("rep_events") or [])
            + len(payload.get("llm_log") or [])
            + len(payload.get("voice_sessions") or []),
        }

    def _claim_deferred_payload(self):
        def _noop(_payload):
            return None
        with _StageFileLock():
            payload = _normalize_deferred_payload(
                _read_json_file(_defer_stage_path(), _empty_deferred_payload())
            )
            has_pending = (
                payload["sessions"] or payload["rep_events"]
                or payload["llm_log"] or payload["voice_sessions"]
            )
            if not has_pending:
                return _empty_deferred_payload(), None
            stamp = "%s.%s" % (os.getpid(), int(time.time() * 1000))
            snapshot = _defer_stage_path() + ".flush." + stamp
            _atomic_write_json(snapshot, payload)
            _atomic_write_json(_defer_stage_path(), _empty_deferred_payload())
            return payload, snapshot

    def flush_deferred_writes(self, finalize_open_sessions=False):
        summary = {
            "ok": True,
            "sessions": 0,
            "rep_events": 0,
            "llm_log": 0,
            "voice_sessions": 0,
            "snapshot": None,
            "error": None,
        }
        try:
            payload, snapshot = self._claim_deferred_payload()
            summary["snapshot"] = snapshot
            if snapshot is None:
                return summary
            conn = self._ensure()
            if conn is None:
                summary["ok"] = False
                summary["error"] = "db unavailable"
                return summary
            session_map = {}
            touched_dates = set()
            with self._lock:
                cur = conn.cursor()
                sessions = payload.get("sessions") or {}
                for key in sorted(sessions.keys(), key=lambda x: int(x)):
                    row = dict(sessions[key] or {})
                    started_at = row.get("started_at") or _now()
                    ended_at = row.get("ended_at")
                    if finalize_open_sessions and not ended_at:
                        ended_at = _now()
                        row["ended_at"] = ended_at
                    if not row.get("duration_sec"):
                        try:
                            t0 = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S")
                            t1 = datetime.strptime(
                                ended_at or _now(), "%Y-%m-%d %H:%M:%S"
                            )
                            row["duration_sec"] = max(
                                0, int((t1 - t0).total_seconds())
                            )
                        except Exception:
                            row["duration_sec"] = 0
                    cur.execute(
                        "INSERT INTO training_sessions "
                        "(started_at, ended_at, exercise, good_count, failed_count, fatigue_peak, duration_sec) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            started_at,
                            ended_at,
                            str(row.get("exercise") or "unknown"),
                            int(row.get("good_count") or 0),
                            int(row.get("failed_count") or 0),
                            float(row.get("fatigue_peak") or 0.0),
                            int(row.get("duration_sec") or 0),
                        ),
                    )
                    session_map[int(key)] = cur.lastrowid
                    summary["sessions"] += 1
                    touched_dates.add(str(started_at)[:10])

                for row in payload.get("rep_events") or []:
                    staged_sid = row.get("session_id")
                    real_sid = session_map.get(int(staged_sid), staged_sid)
                    cur.execute(
                        "INSERT INTO rep_events "
                        "(session_id, ts, is_good, angle_min, emg_target, emg_comp, "
                        "exercise, rep_index, visual_result, model_class, model_confidence, "
                        "model_similarity, classification_source, angle_metric, rom, emg_ok, is_demo_seed) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                        (
                            real_sid,
                            row.get("ts") or _now(),
                            1 if row.get("is_good") else 0,
                            float(row.get("angle_min") or 0.0),
                            float(row.get("emg_target") or 0.0),
                            float(row.get("emg_comp") or 0.0),
                            row.get("exercise"),
                            row.get("rep_index"),
                            row.get("visual_result"),
                            row.get("model_class"),
                            _maybe_float(row.get("model_confidence")),
                            _maybe_float(row.get("model_similarity")),
                            row.get("classification_source"),
                            row.get("angle_metric"),
                            _maybe_float(row.get("rom")),
                            1 if row.get("emg_ok") else 0,
                        ),
                    )
                    summary["rep_events"] += 1

                for row in payload.get("llm_log") or []:
                    cur.execute(
                        "INSERT INTO llm_log (ts, trigger, prompt, response, tokens_in, tokens_out) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            row.get("ts") or _now(),
                            str(row.get("trigger") or ""),
                            str(row.get("prompt") or "")[:4000],
                            str(row.get("response") or "")[:4000],
                            int(row.get("tokens_in") or 0),
                            int(row.get("tokens_out") or 0),
                        ),
                    )
                    summary["llm_log"] += 1

                for row in payload.get("voice_sessions") or []:
                    cur.execute(
                        "INSERT INTO voice_sessions "
                        "(ts, transcript, response, summary, duration_s, trigger_src, is_demo_seed) "
                        "VALUES (?, ?, ?, ?, ?, ?, 0)",
                        (
                            row.get("ts") or datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                            str(row.get("transcript") or "")[:4000],
                            str(row.get("response") or "")[:4000],
                            (str(row.get("summary"))[:2000]) if row.get("summary") else None,
                            float(row.get("duration_s") or 0.0),
                            str(row.get("trigger_src") or "chat"),
                        ),
                    )
                    summary["voice_sessions"] += 1

                conn.commit()
            for date_str in sorted(d for d in touched_dates if d):
                self.compute_daily_summary(date_str=date_str)
            if snapshot and os.path.exists(snapshot):
                os.remove(snapshot)
            return summary
        except Exception as e:
            summary["ok"] = False
            summary["error"] = str(e)
            logging.warning("[FitnessDB] flush_deferred_writes failed: %s", e)
            return summary

    # ---------- 基础 ----------
    def connect(self):
        """建立连接并自动建表。失败返回 None，不抛异常。"""
        try:
            parent = os.path.dirname(self.path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            self._conn = sqlite3.connect(
                self.path, check_same_thread=False, timeout=5.0
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            cur = self._conn.cursor()
            for stmt in _SCHEMA:
                cur.execute(stmt)
            self._ensure_extra_columns(cur)
            self._conn.commit()
            if self.buffer_writes and not self._buffer_registered:
                atexit.register(self.flush_deferred_writes)
                self._buffer_registered = True
            return self._conn
        except Exception as e:
            logging.warning("[FitnessDB] connect failed: %s", e)
            self._conn = None
            return None

    def _ensure_extra_columns(self, cur):
        """Idempotent lightweight migration for additive columns."""
        for table, columns in _EXTRA_COLUMNS.items():
            try:
                existing = set(
                    row["name"] for row in cur.execute(
                        "PRAGMA table_info(%s)" % table
                    ).fetchall()
                )
            except Exception:
                existing = set()
            for name, definition in columns:
                if name in existing:
                    continue
                cur.execute(
                    "ALTER TABLE %s ADD COLUMN %s %s" %
                    (table, name, definition)
                )

    def _ensure(self):
        if self._conn is None:
            self.connect()
        return self._conn

    # ---------- 训练会话 ----------
    def start_session(self, exercise):
        try:
            if self.buffer_writes:
                session_id = self._next_buffer_session_id()
                self._stage_session_upsert(
                    session_id,
                    exercise=str(exercise or "unknown"),
                    started_at=_now(),
                )
                return session_id
            conn = self._ensure()
            if conn is None:
                return None
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO training_sessions (started_at, exercise) VALUES (?, ?)",
                    (_now(), exercise or "unknown"),
                )
                conn.commit()
                return cur.lastrowid
        except Exception as e:
            logging.warning("[FitnessDB] start_session failed: %s", e)
            return None

    def end_session(self, session_id, good, failed, fatigue_peak):
        if session_id is None:
            return
        try:
            if self.buffer_writes:
                row = self._stage_session_upsert(session_id)
                duration = 0
                try:
                    t0 = datetime.strptime(
                        row.get("started_at") or _now(),
                        "%Y-%m-%d %H:%M:%S",
                    )
                    duration = int((datetime.now() - t0).total_seconds())
                except Exception:
                    duration = 0
                self._update_staged_session(session_id, {
                    "ended_at": _now(),
                    "good_count": int(good or 0),
                    "failed_count": int(failed or 0),
                    "fatigue_peak": float(fatigue_peak or 0.0),
                    "duration_sec": duration,
                })
                return
            conn = self._ensure()
            if conn is None:
                return
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "SELECT started_at FROM training_sessions WHERE id=?",
                    (session_id,),
                )
                row = cur.fetchone()
                duration = 0
                if row is not None:
                    try:
                        t0 = datetime.strptime(row["started_at"], "%Y-%m-%d %H:%M:%S")
                        duration = int((datetime.now() - t0).total_seconds())
                    except Exception:
                        duration = 0
                cur.execute(
                    "UPDATE training_sessions SET ended_at=?, good_count=?, "
                    "failed_count=?, fatigue_peak=?, duration_sec=? WHERE id=?",
                    (_now(), int(good or 0), int(failed or 0),
                     float(fatigue_peak or 0.0), duration, session_id),
                )
                conn.commit()
        except Exception as e:
            logging.warning("[FitnessDB] end_session failed: %s", e)

    def update_session_counts(self, session_id, good, failed, fatigue_peak):
        """Refresh live counters without ending the active session."""
        if session_id is None:
            return
        try:
            if self.buffer_writes:
                self._stage_session_upsert(session_id)
                self._update_staged_session(session_id, {
                    "good_count": int(good or 0),
                    "failed_count": int(failed or 0),
                    "fatigue_peak": float(fatigue_peak or 0.0),
                })
                return
            conn = self._ensure()
            if conn is None:
                return
            with self._lock:
                conn.execute(
                    "UPDATE training_sessions SET good_count=?, "
                    "failed_count=?, fatigue_peak=? WHERE id=?",
                    (int(good or 0), int(failed or 0),
                     float(fatigue_peak or 0.0), session_id),
                )
                conn.commit()
        except Exception as e:
            logging.warning("[FitnessDB] update_session_counts failed: %s", e)

    def log_rep(self, session_id, is_good, angle_min, emg_target, emg_comp,
                exercise=None, rep_index=None, visual_result=None,
                model_class=None, model_confidence=None,
                model_similarity=None, classification_source=None,
                angle_metric=None, rom=None, emg_ok=False):
        try:
            if self.buffer_writes:
                self._stage_session_upsert(session_id, exercise=exercise)

                def _mutate(payload):
                    payload["rep_events"].append({
                        "session_id": session_id,
                        "ts": _now(),
                        "is_good": 1 if is_good else 0,
                        "angle_min": float(angle_min or 0.0),
                        "emg_target": float(emg_target or 0.0),
                        "emg_comp": float(emg_comp or 0.0),
                        "exercise": str(exercise) if exercise else None,
                        "rep_index": int(rep_index) if rep_index is not None else None,
                        "visual_result": str(visual_result) if visual_result else None,
                        "model_class": str(model_class) if model_class else None,
                        "model_confidence": _maybe_float(model_confidence),
                        "model_similarity": _maybe_float(model_similarity),
                        "classification_source": str(classification_source) if classification_source else None,
                        "angle_metric": str(angle_metric) if angle_metric else None,
                        "rom": _maybe_float(rom),
                        "emg_ok": 1 if emg_ok else 0,
                    })

                self._with_stage_payload(_mutate)
                return
            conn = self._ensure()
            if conn is None:
                return
            with self._lock:
                conn.execute(
                    "INSERT INTO rep_events "
                    "(session_id, ts, is_good, angle_min, emg_target, emg_comp, "
                    "exercise, rep_index, visual_result, model_class, "
                    "model_confidence, model_similarity, classification_source, "
                    "angle_metric, rom, emg_ok, is_demo_seed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (session_id, _now(), 1 if is_good else 0,
                     float(angle_min or 0.0), float(emg_target or 0.0),
                     float(emg_comp or 0.0),
                     str(exercise) if exercise else None,
                     int(rep_index) if rep_index is not None else None,
                     str(visual_result) if visual_result else None,
                     str(model_class) if model_class else None,
                     _maybe_float(model_confidence),
                     _maybe_float(model_similarity),
                     str(classification_source) if classification_source else None,
                     str(angle_metric) if angle_metric else None,
                     _maybe_float(rom),
                     1 if emg_ok else 0),
                )
                conn.commit()
        except Exception as e:
            logging.warning("[FitnessDB] log_rep failed: %s", e)

    # ---------- LLM 日志 ----------
    def log_llm(self, trigger, prompt, response, tokens_in=0, tokens_out=0):
        try:
            if self.buffer_writes:
                def _mutate(payload):
                    payload["llm_log"].append({
                        "ts": _now(),
                        "trigger": str(trigger or ""),
                        "prompt": str(prompt or "")[:4000],
                        "response": str(response or "")[:4000],
                        "tokens_in": int(tokens_in or 0),
                        "tokens_out": int(tokens_out or 0),
                    })

                self._with_stage_payload(_mutate)
                return
            conn = self._ensure()
            if conn is None:
                return
            with self._lock:
                conn.execute(
                    "INSERT INTO llm_log (ts, trigger, prompt, response, "
                    "tokens_in, tokens_out) VALUES (?, ?, ?, ?, ?, ?)",
                    (_now(), str(trigger or ""), str(prompt or "")[:4000],
                     str(response or "")[:4000],
                     int(tokens_in or 0), int(tokens_out or 0)),
                )
                conn.commit()
        except Exception as e:
            logging.warning("[FitnessDB] log_llm failed: %s", e)

    # ---------- 查询 ----------
    def get_recent_sessions(self, limit=10):
        try:
            conn = self._ensure()
            if conn is None:
                return []
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM training_sessions "
                    "ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                )
                rows = cur.fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logging.warning("[FitnessDB] get_recent_sessions failed: %s", e)
            return []

    def get_daily_summary(self, date_str):
        try:
            conn = self._ensure()
            if conn is None:
                return {}
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "SELECT * FROM daily_summary WHERE date=?", (date_str,)
                )
                row = cur.fetchone()
                if row is None:
                    return {}
                return dict(row)
        except Exception as e:
            logging.warning("[FitnessDB] get_daily_summary failed: %s", e)
            return {}

    def get_range_stats(self, days=7):
        """返回最近 N 天按日聚合列表。"""
        try:
            conn = self._ensure()
            if conn is None:
                return []
            with self._lock:
                start = (date.today() - timedelta(days=int(days) - 1)).strftime(
                    "%Y-%m-%d"
                )
                cur = conn.cursor()
                cur.execute(
                    "SELECT date(started_at) AS d, "
                    "COUNT(*) AS session_count, "
                    "COALESCE(SUM(good_count),0) AS total_good, "
                    "COALESCE(SUM(failed_count),0) AS total_failed, "
                    "COALESCE(SUM(fatigue_peak),0) AS total_fatigue "
                    "FROM training_sessions "
                    "WHERE date(started_at) >= ? "
                    "GROUP BY date(started_at) ORDER BY d ASC",
                    (start,),
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logging.warning("[FitnessDB] get_range_stats failed: %s", e)
            return []

    # ---------- 配置 ----------
    def get_config(self, key, default=None):
        try:
            conn = self._ensure()
            if conn is None:
                return default
            with self._lock:
                cur = conn.cursor()
                cur.execute("SELECT value FROM user_config WHERE key=?", (key,))
                row = cur.fetchone()
                if row is None:
                    return default
                return row["value"]
        except Exception as e:
            logging.warning("[FitnessDB] get_config failed: %s", e)
            return default

    def set_config(self, key, value):
        try:
            conn = self._ensure()
            if conn is None:
                return
            with self._lock:
                conn.execute(
                    "INSERT INTO user_config (key, value, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (str(key), str(value), _now()),
                )
                conn.commit()
        except Exception as e:
            logging.warning("[FitnessDB] set_config failed: %s", e)

    # ---------- 每日汇总 ----------
    def compute_daily_summary(self, date_str=None):
        """基于当天 training_sessions upsert 到 daily_summary。"""
        try:
            conn = self._ensure()
            if conn is None:
                return {}
            if date_str is None:
                date_str = date.today().strftime("%Y-%m-%d")
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) AS c, "
                    "COALESCE(SUM(good_count),0) AS g, "
                    "COALESCE(SUM(failed_count),0) AS f, "
                    "COALESCE(SUM(fatigue_peak),0) AS fa "
                    "FROM training_sessions WHERE date(started_at)=?",
                    (date_str,),
                )
                row = cur.fetchone()
                c = int(row["c"]) if row is not None else 0
                g = int(row["g"]) if row is not None else 0
                f = int(row["f"]) if row is not None else 0
                fa = float(row["fa"]) if row is not None else 0.0
                # best_streak = 该日连续 good 最长（简化：取最大 good_count 单场）
                cur.execute(
                    "SELECT COALESCE(MAX(good_count),0) AS bs "
                    "FROM training_sessions WHERE date(started_at)=?",
                    (date_str,),
                )
                bs_row = cur.fetchone()
                bs = int(bs_row["bs"]) if bs_row is not None else 0
                conn.execute(
                    "INSERT INTO daily_summary "
                    "(date, session_count, total_good, total_failed, "
                    "total_fatigue, best_streak) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(date) DO UPDATE SET "
                    "session_count=excluded.session_count, "
                    "total_good=excluded.total_good, "
                    "total_failed=excluded.total_failed, "
                    "total_fatigue=excluded.total_fatigue, "
                    "best_streak=excluded.best_streak",
                    (date_str, c, g, f, fa, bs),
                )
                conn.commit()
                return {
                    "date": date_str,
                    "session_count": c,
                    "total_good": g,
                    "total_failed": f,
                    "total_fatigue": fa,
                    "best_streak": bs,
                }
        except Exception as e:
            logging.warning("[FitnessDB] compute_daily_summary failed: %s", e)
            return {}

    # ---------- V4.7 扩展：后端记忆闭环所需 ----------
    def get_recent_chats(self, days=14):
        """返回最近 N 天的 llm_log 记录，用于 OpenClaw 长期记忆注入。

        返回: [{ts, trigger, prompt, response}, ...] 按 ts DESC 排序
        失败时返回空列表，不抛异常。
        """
        try:
            conn = self._ensure()
            if conn is None:
                return []
            with self._lock:
                start = (
                    datetime.now() - timedelta(days=int(days))
                ).strftime("%Y-%m-%d %H:%M:%S")
                cur = conn.cursor()
                cur.execute(
                    "SELECT ts, trigger, prompt, response FROM llm_log "
                    "WHERE ts >= ? ORDER BY ts DESC",
                    (start,),
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logging.warning("[FitnessDB] get_recent_chats failed: %s", e)
            return []

    def get_user_preferences(self):
        """返回 user_config 表中所有 key 以 'user_preference.' 开头的偏好。

        返回: {key: value} 字典（key 保留完整前缀）
        失败时返回空 dict。
        """
        try:
            conn = self._ensure()
            if conn is None:
                return {}
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "SELECT key, value FROM user_config "
                    "WHERE key LIKE 'user_preference.%'"
                )
                return {row["key"]: row["value"] for row in cur.fetchall()}
        except Exception as e:
            logging.warning("[FitnessDB] get_user_preferences failed: %s", e)
            return {}

    def set_user_preference(self, key, value):
        """写入一条偏好。自动补全 'user_preference.' 前缀（若未带）。

        使用 INSERT OR REPLACE 语义（兼容 ON CONFLICT 不支持的旧 SQLite）。
        """
        try:
            conn = self._ensure()
            if conn is None:
                return
            k = str(key or "").strip()
            if not k:
                return
            if not k.startswith("user_preference."):
                k = "user_preference." + k
            with self._lock:
                conn.execute(
                    "INSERT OR REPLACE INTO user_config (key, value, updated_at) "
                    "VALUES (?, ?, ?)",
                    (k, str(value), _now()),
                )
                conn.commit()
        except Exception as e:
            logging.warning("[FitnessDB] set_user_preference failed: %s", e)

    # ---------- V4.8 扩展：语音会话 / 偏好演化 / system_prompt 版本化 ----------
    def log_voice_session(self, trigger_src, transcript, response,
                          duration_s=0.0, summary=None):
        """写入一条 voice_sessions（闲聊或语音问答）。

        - ts 用 ISO8601（本地时间），is_demo_seed=0
        - 任何异常吞掉，返回 None；成功返回 lastrowid
        - summary 留空时由 daemon 批量回填
        """
        try:
            if self.buffer_writes:
                def _mutate(payload):
                    payload["voice_sessions"].append({
                        "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                        "transcript": str(transcript or "")[:4000],
                        "response": str(response or "")[:4000],
                        "summary": (str(summary)[:2000]) if summary else None,
                        "duration_s": float(duration_s or 0.0),
                        "trigger_src": str(trigger_src or "chat"),
                    })

                self._with_stage_payload(_mutate)
                return int(time.time() * 1000)
            conn = self._ensure()
            if conn is None:
                return None
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO voice_sessions "
                    "(ts, transcript, response, summary, duration_s, "
                    "trigger_src, is_demo_seed) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (
                        datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                        str(transcript or "")[:4000],
                        str(response or "")[:4000],
                        (str(summary)[:2000]) if summary else None,
                        float(duration_s or 0.0),
                        str(trigger_src or "chat"),
                    ),
                )
                conn.commit()
                return cur.lastrowid
        except Exception as e:
            logging.warning("[FitnessDB] log_voice_session failed: %s", e)
            return None

    def get_active_system_prompt(self, fallback=""):
        """返回当前 active=1 的 system_prompt_versions.prompt_text。

        无活动记录时返回 fallback（默认空串）。
        """
        try:
            conn = self._ensure()
            if conn is None:
                return fallback
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "SELECT prompt_text FROM system_prompt_versions "
                    "WHERE active=1 ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row is None or not row["prompt_text"]:
                    return fallback
                return row["prompt_text"]
        except Exception as e:
            logging.warning(
                "[FitnessDB] get_active_system_prompt failed: %s", e)
            return fallback

    def get_user_preferences_snapshot(self):
        """返回 user_config 全表快照为 dict。

        兼容两种 key：带 'user_preference.' 前缀与不带前缀。
        - 读取时**优先**使用不带前缀的 key；若只有带前缀的版本则剥离前缀后放入 dict。
        - 失败时返回空 dict。
        """
        try:
            conn = self._ensure()
            if conn is None:
                return {}
            with self._lock:
                cur = conn.cursor()
                cur.execute("SELECT key, value FROM user_config")
                rows = cur.fetchall()
            raw = {}
            for r in rows:
                raw[r["key"]] = r["value"]
            snap = {}
            # 先灌入带前缀的（低优先级）
            for k, v in raw.items():
                if k.startswith("user_preference."):
                    snap[k[len("user_preference."):]] = v
            # 再用不带前缀覆盖（高优先级）
            for k, v in raw.items():
                if not k.startswith("user_preference."):
                    snap[k] = v
            return snap
        except Exception as e:
            logging.warning(
                "[FitnessDB] get_user_preferences_snapshot failed: %s", e)
            return {}

    def record_preference_change(self, field, old_value, new_value,
                                 source, confidence):
        """写 preference_history 一行，同时同步 user_config。

        同步规则：
          - 若存在 `field` 的 key，UPDATE 它
          - 否则若存在 `user_preference.{field}` 的 key，UPDATE 它
          - 否则 INSERT 新的 `field`（不带前缀）
        失败吞异常，返回 None；成功返回 history id。
        """
        try:
            conn = self._ensure()
            if conn is None:
                return None
            field_s = str(field or "").strip()
            if not field_s:
                return None
            new_s = "" if new_value is None else str(new_value)
            old_s = None if old_value is None else str(old_value)
            conf = float(confidence or 0.0)
            source_s = str(source or "rule_engine")
            now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO preference_history "
                    "(ts, field, old_value, new_value, source, "
                    "confidence, is_demo_seed) "
                    "VALUES (?, ?, ?, ?, ?, ?, 0)",
                    (now_iso, field_s, old_s, new_s, source_s, conf),
                )
                history_id = cur.lastrowid
                # 同步 user_config
                prefixed = "user_preference." + field_s
                cur.execute(
                    "SELECT key FROM user_config WHERE key IN (?, ?)",
                    (field_s, prefixed),
                )
                existing = {r["key"] for r in cur.fetchall()}
                ts_short = _now()
                if field_s in existing:
                    conn.execute(
                        "UPDATE user_config SET value=?, updated_at=? "
                        "WHERE key=?",
                        (new_s, ts_short, field_s),
                    )
                elif prefixed in existing:
                    conn.execute(
                        "UPDATE user_config SET value=?, updated_at=? "
                        "WHERE key=?",
                        (new_s, ts_short, prefixed),
                    )
                else:
                    conn.execute(
                        "INSERT INTO user_config (key, value, updated_at) "
                        "VALUES (?, ?, ?)",
                        (field_s, new_s, ts_short),
                    )
                conn.commit()
                return history_id
        except Exception as e:
            logging.warning(
                "[FitnessDB] record_preference_change failed: %s", e)
            return None

    def create_system_prompt_version(self, prompt_text,
                                     based_on_summary_ids=None):
        """新建一条 system_prompt_versions，自动把旧 active=1 降级为 0。

        - based_on_summary_ids: list/tuple，序列化为 JSON 存入
        - 成功后把新 id 写入 user_config.`last_prompt_version` 和
          `user_preference.last_prompt_version`（两种 key 都更新/插入）
        - 失败返回 None
        """
        try:
            conn = self._ensure()
            if conn is None:
                return None
            if not prompt_text:
                return None
            try:
                ids_json = json.dumps(list(based_on_summary_ids or []))
            except Exception:
                ids_json = "[]"
            now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            with self._lock:
                cur = conn.cursor()
                cur.execute(
                    "UPDATE system_prompt_versions SET active=0 "
                    "WHERE active=1"
                )
                cur.execute(
                    "INSERT INTO system_prompt_versions "
                    "(ts, prompt_text, based_on_summary_ids, active, "
                    "is_demo_seed) VALUES (?, ?, ?, 1, 0)",
                    (now_iso, str(prompt_text), ids_json),
                )
                new_id = cur.lastrowid
                ts_short = _now()
                # 两种 key 都 upsert，供不同调用方兼容
                for k in ("last_prompt_version",
                          "user_preference.last_prompt_version"):
                    cur.execute(
                        "SELECT key FROM user_config WHERE key=?", (k,)
                    )
                    if cur.fetchone() is None:
                        conn.execute(
                            "INSERT INTO user_config "
                            "(key, value, updated_at) VALUES (?, ?, ?)",
                            (k, str(new_id), ts_short),
                        )
                    else:
                        conn.execute(
                            "UPDATE user_config SET value=?, updated_at=? "
                            "WHERE key=?",
                            (str(new_id), ts_short, k),
                        )
                conn.commit()
                return new_id
        except Exception as e:
            logging.warning(
                "[FitnessDB] create_system_prompt_version failed: %s", e)
            return None

    # ============================================================
    # V4.9 编辑与模型视图支持
    # ============================================================

    # voice_sessions 可编辑字段白名单 (其他字段一律拒绝写)
    _VOICE_EDITABLE = (
        "transcript", "response", "summary", "duration_s", "trigger_src",
    )

    def update_voice_session_field(self, row_id, field, value):
        """编辑 voice_sessions 单个字段。白名单校验 + 参数化查询。

        返回 True 表示受影响行 >= 1，False 表示字段不在白名单或 id 不存在。
        """
        if field not in self._VOICE_EDITABLE:
            logging.warning(
                "[FitnessDB] update_voice_session_field: field %r not allowed",
                field,
            )
            return False
        try:
            conn = self._ensure()
            cur = conn.execute(
                "UPDATE voice_sessions SET " + field + "=? WHERE id=?",
                (value, int(row_id)),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception as e:
            logging.warning(
                "[FitnessDB] update_voice_session_field(%s) failed: %s",
                field, e)
            return False

    def list_models(self, only_active=False):
        """返回 model_registry 全表（或 active=1）。"""
        try:
            conn = self._ensure()
            sql = ("SELECT id,name,exercise,path,arch,params_m,size_kb,"
                   "train_acc,val_acc,epochs,dataset,trained_at,active,notes,"
                   "is_demo_seed FROM model_registry")
            if only_active:
                sql += " WHERE active=1"
            sql += " ORDER BY exercise, id"
            rows = conn.execute(sql).fetchall()
            cols = ("id", "name", "exercise", "path", "arch", "params_m",
                    "size_kb", "train_acc", "val_acc", "epochs", "dataset",
                    "trained_at", "active", "notes", "is_demo_seed")
            return [dict(zip(cols, r)) for r in rows]
        except Exception as e:
            logging.warning("[FitnessDB] list_models failed: %s", e)
            return []

    def get_feature_embeddings(self, exercise=None):
        """返回 feature_embeddings 全部或某 exercise 的 2D 散点。"""
        try:
            conn = self._ensure()
            if exercise:
                rows = conn.execute(
                    "SELECT exercise,label,x,y FROM feature_embeddings "
                    "WHERE exercise=? ORDER BY id",
                    (exercise,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT exercise,label,x,y FROM feature_embeddings "
                    "ORDER BY exercise, id"
                ).fetchall()
            return [
                {"exercise": r[0], "label": r[1], "x": r[2], "y": r[3]}
                for r in rows
            ]
        except Exception as e:
            logging.warning("[FitnessDB] get_feature_embeddings failed: %s", e)
            return []

    def close(self):
        try:
            if self.buffer_writes:
                self.flush_deferred_writes()
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        except Exception:
            pass
