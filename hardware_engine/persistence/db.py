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
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        except Exception:
            pass
