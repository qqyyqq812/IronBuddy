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
import sqlite3
import threading
import logging
from datetime import datetime, date, timedelta

_BOARD_DB = "/home/toybrick/streamer_v3/data/ironbuddy.db"
_DEV_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "ironbuddy.db",
)


def _resolve_db_path():
    env_path = os.environ.get("IRONBUDDY_DB_PATH")
    if env_path:
        return env_path
    # 板端优先
    board_dir = os.path.dirname(_BOARD_DB)
    if os.path.isdir(board_dir):
        return _BOARD_DB
    return _DEV_DB


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


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class FitnessDB(object):
    """单连接 SQLite 封装，线程安全（check_same_thread=False + 锁）。"""

    def __init__(self, path=None):
        if path is None:
            path = _resolve_db_path()
        self.path = path
        self._conn = None
        self._lock = threading.Lock()

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
            self._conn.commit()
            return self._conn
        except Exception as e:
            logging.warning("[FitnessDB] connect failed: %s", e)
            self._conn = None
            return None

    def _ensure(self):
        if self._conn is None:
            self.connect()
        return self._conn

    # ---------- 训练会话 ----------
    def start_session(self, exercise):
        try:
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

    def log_rep(self, session_id, is_good, angle_min, emg_target, emg_comp):
        try:
            conn = self._ensure()
            if conn is None:
                return
            with self._lock:
                conn.execute(
                    "INSERT INTO rep_events "
                    "(session_id, ts, is_good, angle_min, emg_target, emg_comp) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, _now(), 1 if is_good else 0,
                     float(angle_min or 0.0), float(emg_target or 0.0),
                     float(emg_comp or 0.0)),
                )
                conn.commit()
        except Exception as e:
            logging.warning("[FitnessDB] log_rep failed: %s", e)

    # ---------- LLM 日志 ----------
    def log_llm(self, trigger, prompt, response, tokens_in=0, tokens_out=0):
        try:
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

    def close(self):
        try:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        except Exception:
            pass
