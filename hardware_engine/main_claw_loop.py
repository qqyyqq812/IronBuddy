import asyncio
import os
import sys
import json
import time
import math
import logging
import fcntl
import numpy as np
import torch
from cognitive.openclaw_bridge import OpenClawBridge
from cognitive.deepseek_direct import DeepSeekDirect
from ai_sensory.asr_worker import ASRWorker
from sensor.microphone import MicrophoneController
from cognitive.fusion_model import CompensationGRU, load_model, _compute_derived_features
try:
    from hardware_engine.fatigue_model import compute_fatigue, append_feature_snapshot
except Exception:
    try:
        from fatigue_model import compute_fatigue, append_feature_snapshot
    except Exception:
        compute_fatigue = None
        append_feature_snapshot = None

os.environ.setdefault("TZ", "Asia/Shanghai")
try:
    time.tzset()
except Exception:
    pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [MAIN LOOP] - %(message)s')

DEFAULT_RUNTIME_FATIGUE_LIMIT = 600

# ===== Sprint5: SQLite 持久化懒加载 =====
_DB = [None]
_DB_SESSION = [None]
def _db():
    if _DB[0] is not None: return _DB[0]
    try:
        from persistence.db import FitnessDB
        d = FitnessDB(); d.connect(); _DB[0] = d; return d
    except Exception as e:
        logging.warning("[DB] init failed: %s", e); return None

# ===== Agent 3 GRU 推理引擎 =====
_GRU_MODEL = None  # type: CompensationGRU or None
_GRU_WINDOW_SIZE = 30
# 滚动特征缓冲区: 每行是 7D 特征向量 (归一化前)
_gru_feature_buf = []  # list of 7D feature vectors
_GRU_7D_COLUMNS = [
    "Ang_Vel", "Angle", "Ang_Accel", "Target_RMS", "Comp_RMS",
    "Symmetry_Score", "Phase_Progress",
]
_GRU_7D_BUFFER_FILE = "/dev/shm/gru_7d_buffer.json"
_GRU_LAST_WINDOW_FILE = "/dev/shm/gru_last_window.json"
_LANE_B_RUNTIME_PREPROCESS_FILES = (
    "/dev/shm/lane_b_runtime_preprocess.json",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "sensor", "lane_b_runtime_preprocess.json"),
)
_gru_feature_rows = []  # list of dicts aligned with _gru_feature_buf
_gru_last_buffer_write_ts = 0.0
# 推理跳帧计数器 (每 N 帧推理一次，节省 CPU)
_GRU_INFER_EVERY = 3
_gru_frame_ctr   = 0
# 上一帧 ang_vel (用于在主循环里计算 ang_accel)
_gru_prev_ang_vel: float = 0.0
# P0.2: 本 rep 在 _gru_feature_buf 中起始索引；rep_start 边沿被检测时由主循环刷新
_gru_rep_start_idx = 0
# P0.2: rep_in_progress 边沿检测 (False->True 时刷新 _gru_rep_start_idx)
_gru_prev_rep_in_progress = False
# P0.5: GRU 置信度低于此值视为 collapsed/uncertain, fallback (~1/3 均匀分布=0.33)
_GRU_MIN_CONFIDENCE = 0.45
# P0.5: EMG 归一化后双通道均低于此值视为无肌肉激活, fallback
_EMG_ZERO_THRESHOLD = 0.05


def _clip_pct(value):
    try:
        value = float(value)
    except Exception:
        value = 0.0
    return max(0.0, min(100.0, value))


def _read_lane_b_runtime_preprocess():
    for path in _LANE_B_RUNTIME_PREPROCESS_FILES:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def _scale_lane_b_raw_rms_for_gru(data):
    meta = _read_lane_b_runtime_preprocess()
    if meta.get("default_training_view") != "raw_rms_robust100":
        return None
    robust = meta.get("raw_rms_robust100") if isinstance(meta.get("raw_rms_robust100"), dict) else {}
    try:
        target_ref = max(20.0, float(robust.get("target_ref") or 0.0))
        comp_ref = max(20.0, float(robust.get("comp_ref") or 0.0))
    except Exception:
        return None
    if target_ref <= 20.0 and comp_ref <= 20.0:
        return None
    target_rms = _safe_float(data.get("target_rms"), 0.0)
    comp_rms = _safe_float(data.get("comp_rms"), 0.0)
    return (
        _clip_pct(target_rms / target_ref * 100.0),
        _clip_pct(comp_rms / comp_ref * 100.0),
        meta,
    )


def _fatigue_features_from_rep(exercise, rep_event, result, target_emg=0.0,
                               comp_emg=0.0, angle_velocity=0.0,
                               angle_acceleration=0.0, current_set=1,
                               previous_set_fatigue=0.0,
                               recent_fatigue_peak=0.0, emg_meta=None,
                               target_fatigue=1500, phase=None):
    rep_event = rep_event if isinstance(rep_event, dict) else {}
    emg_meta = emg_meta if isinstance(emg_meta, dict) else {}
    target_rms = emg_meta.get("target_rms", target_emg)
    comp_rms = emg_meta.get("compensation_rms", emg_meta.get("comp_rms", comp_emg))
    dt_s = _safe_float(rep_event.get("ended_ts"), time.time()) - _safe_float(
        rep_event.get("started_ts"), time.time())
    if dt_s <= 0.0:
        dt_s = 1.0
    phase_value = phase or rep_event.get("phase") or ("UP" if result == "standard" else "UNKNOWN")
    return {
        "exercise": exercise,
        "rep_count": rep_event.get("rep_index", 1),
        "rom": rep_event.get("rom", 0.0),
        "min_angle": rep_event.get("min_angle", 999.0),
        "angle_velocity": angle_velocity,
        "angle_acceleration": angle_acceleration,
        "result": result,
        "compensation_count": rep_event.get("compensation_count", 0),
        "target_rms": target_rms,
        "compensation_rms": comp_rms,
        "target_mvc": emg_meta.get("target_mvc", 100.0),
        "comp_mvc": emg_meta.get("comp_mvc", 100.0),
        "activation_pct": emg_meta.get("activation_pct", target_emg),
        "mvc_pct": emg_meta.get("mvc_pct", 0.0),
        "iemg": emg_meta.get("iemg", 0.0),
        "emg_simulated": bool(emg_meta.get("simulated", False)),
        "emg_valid": bool(emg_meta.get("valid", True)),
        "pose_valid": bool(rep_event.get("pose_valid", True)),
        "is_training": True,
        "signal_mode": emg_meta.get("signal_mode", ""),
        "phase": phase_value,
        "dt_s": dt_s,
        "integration_mode": "rep",
        "d_target": emg_meta.get("d_target", 7.0),
        "target_fatigue": target_fatigue,
        "current_set": current_set,
        "previous_set_fatigue": previous_set_fatigue,
        "recent_fatigue_peak": recent_fatigue_peak,
    }


def _apply_fatigue_model_to_fsm(fsm, features):
    if compute_fatigue is None:
        old_increment = 1500.0 / 7.0
        fsm.total_fatigue_volume += old_increment
        fallback = {
            "fatigue_score": round(float(getattr(fsm, "total_fatigue_volume", 0.0)), 3),
            "fatigue_increment": round(old_increment, 3),
            "fatigue_components": {
                "visual": [],
                "emg": [{"name": "emg_status", "value": 0.0, "status": "missing"}],
                "context": [],
            },
            "fatigue_model_version": "legacy_fixed_increment",
            "features": dict(features or {}),
            "ts": time.time(),
        }
        fsm._last_fatigue_model = fallback
        return fallback
    previous = float(getattr(fsm, "total_fatigue_volume", 0.0) or 0.0)
    result = compute_fatigue(features, previous_score=previous)
    fsm.total_fatigue_volume = float(result.get("fatigue_score") or previous)
    fsm._last_fatigue_model = result
    if append_feature_snapshot is not None:
        try:
            append_feature_snapshot(result)
        except Exception as exc:
            logging.debug("[fatigue_model] snapshot append failed: %s", exc)
    return result


def _read_muscle_activation_meta(exercise):
    try:
        with open("/dev/shm/muscle_activation.json", "r") as mf:
            data = json.load(mf)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    acts = data.get("activations") if isinstance(data.get("activations"), dict) else {}
    if exercise == "bicep_curl":
        target_pct = acts.get("biceps", data.get("target_pct", 0.0))
        comp_pct = acts.get("glutes", data.get("comp_pct", 0.0))
    else:
        target_pct = acts.get("glutes", data.get("target_pct", 0.0))
        comp_pct = acts.get("biceps", data.get("comp_pct", 0.0))
    debug_meta = _read_emg_debug_meta()
    stream_meta = _read_emg_stream_meta()
    fallback = debug_meta or stream_meta
    if fallback and bool(data.get("simulated") or data.get("sensor_simulated")):
        data = dict(data)
        data.update(fallback)
        target_pct = fallback.get("target_pct", target_pct)
        comp_pct = fallback.get("comp_pct", comp_pct)
    return {
        "target_rms": data.get("target_rms", target_pct),
        "compensation_rms": data.get("comp_rms", comp_pct),
        "target_mvc": data.get("target_mvc", 100.0),
        "comp_mvc": data.get("comp_mvc", 100.0),
        "activation_pct": target_pct,
        "comp_pct": comp_pct,
        "simulated": bool(data.get("simulated") or data.get("sensor_simulated")),
        "valid": bool(data.get("valid", True)),
        "signal_mode": data.get("signal_mode", ""),
    }


def _read_emg_debug_meta(max_age_s=3.0):
    try:
        with open("/dev/shm/emg_debug_snapshot.json", "r") as fh:
            data = json.load(fh)
        if time.time() - _safe_float(data.get("ts"), 0.0) > max_age_s:
            return {}
        rms = data.get("rms") if isinstance(data.get("rms"), list) else []
        mvc = data.get("mvc") if isinstance(data.get("mvc"), list) else []
        pct = data.get("pct") if isinstance(data.get("pct"), list) else []
        return {
            "target_rms": rms[0] if len(rms) > 0 else data.get("target_rms", 0.0),
            "comp_rms": rms[1] if len(rms) > 1 else data.get("comp_rms", 0.0),
            "target_mvc": mvc[0] if len(mvc) > 0 else data.get("target_mvc", 100.0),
            "comp_mvc": mvc[1] if len(mvc) > 1 else data.get("comp_mvc", 100.0),
            "target_pct": pct[0] if len(pct) > 0 else data.get("target_pct", 0.0),
            "comp_pct": pct[1] if len(pct) > 1 else data.get("comp_pct", 0.0),
            "simulated": False,
            "valid": bool(data.get("connected", True)),
            "signal_mode": "real_udp_debug",
        }
    except Exception:
        return {}


def _read_emg_stream_meta(max_age_s=3.0):
    try:
        with open("/dev/shm/emg_stream_buffer.json", "r") as fh:
            data = json.load(fh)
        samples = data.get("samples") if isinstance(data.get("samples"), list) else []
        if not samples:
            return {}
        row = samples[-1]
        if not isinstance(row, list) or len(row) < 9:
            return {}
        if time.time() - _safe_float(row[0], 0.0) > max_age_s:
            return {}
        return {
            "target_rms": row[5],
            "comp_rms": row[6],
            "target_mvc": 400.0,
            "comp_mvc": 400.0,
            "target_pct": row[7],
            "comp_pct": row[8],
            "simulated": False,
            "valid": bool(data.get("connected", True)),
            "signal_mode": "real_udp_stream",
        }
    except Exception:
        return {}


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _build_rep_event(exercise, rep_index, angle_metric, min_angle, rom,
                     visual_result, finalize_reason, started_ts, ended_ts):
    return {
        "exercise": exercise,
        "rep_index": int(rep_index or 0),
        "angle_metric": angle_metric,
        "min_angle": round(_safe_float(min_angle), 3),
        "rom": round(max(0.0, _safe_float(rom)), 3),
        "visual_result": visual_result,
        "final_result": visual_result,
        "finalize_reason": finalize_reason,
        "started_ts": round(_safe_float(started_ts), 3),
        "ended_ts": round(_safe_float(ended_ts), 3),
    }


def _fresh_json_ts(path, max_age_s):
    try:
        if not os.path.exists(path):
            return False
        age_from_mtime = time.time() - os.path.getmtime(path)
        if age_from_mtime > max_age_s:
            return False
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return False
        if raw.startswith("{"):
            data = json.loads(raw)
            ts = _safe_float(data.get("ts"), 0.0)
            if ts > 0:
                return (time.time() - ts) <= max_age_s
        return True
    except Exception:
        return False


def _emg_signal_ok(max_age_s=3.0):
    heartbeat_ok = _fresh_json_ts("/dev/shm/emg_heartbeat", max_age_s)
    raw_ok = (
        _fresh_json_ts("/dev/shm/emg_raw_waveform.json", max_age_s) or
        _fresh_json_ts("/dev/shm/emg_debug_snapshot.json", max_age_s)
    )
    return bool(heartbeat_ok and raw_ok)


def _apply_rep_classification(fsm, classification, rep_index):
    # P0.3: rep 级双增防护 —— 同一 rep_index 只能计一次, 主循环路径与 FSM 角度路径互斥
    if getattr(fsm, "_counted_rep_index", -1) == rep_index:
        return
    fsm._counted_rep_index = rep_index
    if classification == "standard":
        fsm.good_squats = getattr(fsm, 'good_squats', 0) + 1
    elif classification == "compensating":
        try:
            fsm.trigger_buzzer_alert(kind="代偿")
        except Exception:
            pass
        if rep_index != getattr(fsm, "_compensation_last_rep", -1):
            fsm._compensation_count = getattr(fsm, "_compensation_count", 0) + 1
            fsm._compensation_last_rep = rep_index
            logging.info("📊 [M8] 代偿计数 -> %d (rep=%d)",
                         fsm._compensation_count, rep_index)
    else:
        fsm.failed_squats = getattr(fsm, 'failed_squats', 0) + 1
        try:
            fsm.trigger_buzzer_alert(kind="不标准")
        except Exception:
            pass


def _build_rep_window(rep_start_idx):
    """P0.2: 截取本 rep 内的特征切片, 不足 _GRU_WINDOW_SIZE 时右侧补最后一帧.

    返回 (window_np_or_None, pad_count). 若 pad_count > _GRU_WINDOW_SIZE // 2
    或本 rep 完全没有帧, 返回 (None, pad_count) 让上层 fallback.
    """
    buf_len = len(_gru_feature_buf)
    start_idx = max(0, int(rep_start_idx))
    if start_idx >= buf_len:
        return None, _GRU_WINDOW_SIZE
    slice_rows = _gru_feature_buf[start_idx:]
    if not slice_rows:
        return None, _GRU_WINDOW_SIZE
    if len(slice_rows) >= _GRU_WINDOW_SIZE:
        # 取本 rep 最后 30 帧 (rep 尾部信息最丰富)
        window = np.array(slice_rows[-_GRU_WINDOW_SIZE:], dtype=np.float32)
        return window, 0
    pad_count = _GRU_WINDOW_SIZE - len(slice_rows)
    last = slice_rows[-1]
    padded = list(slice_rows) + [list(last) for _ in range(pad_count)]
    window = np.array(padded, dtype=np.float32)
    return window, pad_count


# 按 exercise 选择权重文件名 (弯举使用独立权重, 避免覆盖深蹲)
_GRU_WEIGHT_BY_EXERCISE = {
    "squat":      "extreme_fusion_gru.pt",
    "bicep_curl": "extreme_fusion_gru_bicep.pt",
}

def _load_gru_model(exercise="squat"):
    """尝试加载对应 exercise 的 GRU 权重 (7D), 失败回退 4D.

    优先首选 hardware_engine/<name>.pt, 其次 cognitive/<name>.pt, 最后通用 extreme_fusion_gru.pt.
    """
    _dir = os.path.dirname(os.path.abspath(__file__))
    model_name = _GRU_WEIGHT_BY_EXERCISE.get(exercise, "extreme_fusion_gru.pt")
    candidates = [
        os.path.join(_dir, model_name),
        os.path.join(_dir, "cognitive", model_name),
        # 通用兜底 (旧共用权重)
        os.path.join(_dir, "extreme_fusion_gru.pt"),
        os.path.join(_dir, "cognitive", "extreme_fusion_gru.pt"),
    ]
    tried = set()
    for path in candidates:
        if path in tried or not os.path.exists(path):
            continue
        tried.add(path)
        try:
            model = load_model(path, input_size=7)
            size_kb = os.path.getsize(path) / 1024
            logging.info(f"[GRU] Loaded {path} for exercise={exercise} ({size_kb:.1f} KB)")
            return model
        except Exception as e:
            logging.warning(f"[GRU] load_model failed for {path}: {e}")
            try:
                model = load_model(path, input_size=4)
                logging.info(f"[GRU] Loaded 4D-compat model from {path}")
                return model
            except Exception as e2:
                logging.warning(f"[GRU] 4D fallback also failed: {e2}")
    logging.warning(f"[GRU] No model file found for exercise={exercise} — inference disabled.")
    return None

_GRU_MODEL = _load_gru_model("squat")

# V2.5: 加载教练人格
_SOUL_TEXT = ""
try:
    _soul_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cognitive', 'SOUL.md')
    if os.path.exists(_soul_path):
        with open(_soul_path, 'r', encoding='utf-8') as sf:
            _SOUL_TEXT = sf.read().strip()
        logging.info(f"✅ 教练人格 SOUL.md 已加载 ({len(_SOUL_TEXT)} chars)")
except Exception as e:
    logging.warning(f"SOUL.md 加载失败: {e}")


CHAT_EVENTS_FILE = "/dev/shm/chat_events.jsonl"
CHAT_EVENTS_SEQ_FILE = "/dev/shm/chat_events.seq"
ANGLE_DEBUG_FILE = "/dev/shm/angle_debug.json"
TRAINING_PLAN_FILE = "/dev/shm/ironbuddy_training_plan.json"
TRAINING_SESSION_FILE = "/dev/shm/ironbuddy_training_session.json"
INTENT_TTL_DEFAULT = 30.0


def _read_chat_event_seq():
    try:
        if os.path.exists(CHAT_EVENTS_SEQ_FILE):
            with open(CHAT_EVENTS_SEQ_FILE, "r") as f:
                return int((f.read() or "0").strip() or "0")
    except Exception:
        pass
    return 0


def _append_chat_event(role, text, kind="auto_status", stage="assistant_reply"):
    if not text:
        return 0
    try:
        lock_f = open(CHAT_EVENTS_FILE + ".lock", "a")
        try:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            seq = _read_chat_event_seq() + 1
            payload = {
                "seq": seq,
                "ts": time.time(),
                "turn_id": "",
                "role": role,
                "kind": kind,
                "stage": stage,
                "text": str(text).strip(),
            }
            with open(CHAT_EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            tmp = CHAT_EVENTS_SEQ_FILE + ".tmp"
            with open(tmp, "w") as sf:
                sf.write(str(seq))
            os.rename(tmp, CHAT_EVENTS_SEQ_FILE)
            return seq
        finally:
            try:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
                lock_f.close()
            except Exception:
                pass
    except Exception as e:
        logging.debug("chat event append failed: %s", e)
    return 0


def _atomic_write_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.rename(tmp, path)
    except Exception as e:
        logging.debug("atomic json write failed %s: %s", path, e)


def _read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_next_set_request(path="/dev/shm/next_set.request"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if raw.startswith("{"):
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        return {"ts": _safe_float(raw, time.time()), "src": "legacy_next_set"}
    except Exception:
        return {}


def _record_completed_training_set(fsm, request_data, next_set):
    session = _read_json_file(TRAINING_SESSION_FILE)
    sets = session.get("sets")
    if not isinstance(sets, list):
        sets = []
    try:
        completed_set = int(request_data.get("completed_set") or 0)
    except Exception:
        completed_set = 0
    if completed_set < 1:
        completed_set = len([s for s in sets if isinstance(s, dict)]) + 1
    comp = int(getattr(fsm, "_compensation_count", 0) or 0)
    good = int(getattr(fsm, "good_squats", 0) or 0)
    failed = int(getattr(fsm, "failed_squats", 0) or 0)
    snapshot = {
        "set_index": completed_set,
        "exercise": request_data.get("exercise") or session.get("exercise") or "squat",
        "good": good,
        "failed": failed,
        "comp": comp,
        "total_reps": good + failed + comp,
        "fatigue": round(float(getattr(fsm, "total_fatigue_volume", 0.0) or 0.0), 1),
        "ended_ts": time.time(),
        "src": "main_claw_next_set",
    }
    replaced = False
    new_sets = []
    for item in sets:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("set_index") or 0)
        except Exception:
            idx = 0
        if idx == completed_set:
            new_sets.append(snapshot)
            replaced = True
        else:
            new_sets.append(item)
    if not replaced:
        new_sets.append(snapshot)
    new_sets.sort(key=lambda item: int(item.get("set_index") or 0))
    total_good = sum(int(item.get("good") or 0) for item in new_sets)
    total_failed = sum(int(item.get("failed") or 0) for item in new_sets)
    total_comp = sum(int(item.get("comp") or 0) for item in new_sets)
    session.update({
        "schema_version": 1,
        "exercise": snapshot["exercise"],
        "sets": new_sets,
        "current_set": int(next_set or completed_set),
        "plan_active": True,
        "total_good": total_good,
        "total_failed": total_failed,
        "total_comp": total_comp,
        "total_reps": total_good + total_failed + total_comp,
        "updated_ts": time.time(),
    })
    if not session.get("started_ts"):
        session["started_ts"] = snapshot["ended_ts"]
    _atomic_write_json(TRAINING_SESSION_FILE, session)
    return snapshot


def _request_auto_next_training_set(exercise, current_limit):
    """Server-side fallback for fatigue-target plans.

    The browser normally calls `/api/training_plan/next_set`, but acceptance
    should not depend on a foreground tab. When fatigue reaches the active set
    target, the main loop advances the persisted plan and drops the same
    next-set request consumed by the existing reset path.
    """
    if os.path.exists("/dev/shm/next_set.request"):
        return False
    plan = _read_json_file(TRAINING_PLAN_FILE)
    sets = plan.get("sets")
    if not isinstance(sets, list) or not sets:
        return False
    try:
        current = int(plan.get("current_set") or 1)
    except Exception:
        current = 1
    if current < 1:
        current = 1
    if current >= len(sets):
        return False
    next_set = current + 1
    try:
        next_target = int((sets[next_set - 1] or {}).get("target_fatigue") or current_limit or DEFAULT_RUNTIME_FATIGUE_LIMIT)
    except Exception:
        next_target = int(current_limit or DEFAULT_RUNTIME_FATIGUE_LIMIT)
    plan["current_set"] = next_set
    plan["updated_ts"] = time.time()
    plan["src"] = "main_loop_auto_next_set"
    _atomic_write_json(TRAINING_PLAN_FILE, plan)
    payload = {
        "limit": next_target,
        "src": "fatigue_auto_next_set",
        "ts": time.time(),
    }
    _atomic_write_json("/dev/shm/fatigue_limit.json", payload)
    _atomic_write_json("/dev/shm/ui_fatigue_limit.json", payload)
    _atomic_write_json("/dev/shm/next_set.request", {
        "ts": time.time(),
        "exercise": exercise or plan.get("exercise") or "squat",
        "completed_set": current,
        "next_set": next_set,
        "next_target_fatigue": next_target,
        "src": "fatigue_auto_next_set",
    })
    _append_chat_event(
        "coach",
        "第%d组达到目标，已自动进入第%d组，目标疲劳%d。"
        % (current, next_set, next_target),
        kind="auto_next_set",
    )
    logging.info(
        "fatigue_auto_next_set requested completed=%s next=%s target=%s",
        current, next_set, next_target,
    )
    return True


def _read_fresh_json(path, default_ttl=INTENT_TTL_DEFAULT):
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = float(data.get("ts") or 0.0)
        ttl = float(data.get("ttl_s") or default_ttl)
        if ts > 0 and ttl > 0 and (time.time() - ts) > ttl:
            logging.info("忽略过期意图 %s age=%.1fs ttl=%.1fs", path, time.time() - ts, ttl)
            return None
        return data
    except Exception as e:
        logging.debug("read fresh json failed %s: %s", path, e)
        return None


def _write_angle_debug(exercise, state, raw_angle, smooth_angle, decision_angle,
                       kpt_conf, fps, source, extra=None):
    data = {
        "exercise": exercise,
        "state": state,
        "raw_angle": round(float(raw_angle), 1),
        "smooth_angle": round(float(smooth_angle), 1),
        "decision_angle": round(float(decision_angle), 1),
        "kpt_conf": round(float(kpt_conf or 0.0), 3),
        "fps": round(float(fps or 0.0), 1),
        "source": source or "unknown",
        "ts": time.time(),
    }
    if extra:
        data.update(extra)
    _atomic_write_json(ANGLE_DEBUG_FILE, data)


def _window_to_plain_list(window):
    try:
        if hasattr(window, "tolist"):
            return window.tolist()
    except Exception:
        pass
    out = []
    for row in window or []:
        try:
            out.append([float(v) for v in row])
        except Exception:
            continue
    return out


def _normalize_gru_7d_window(raw_window):
    """Return the exact normalized feature matrix used by current runtime."""
    try:
        arr = np.array(raw_window, dtype=np.float32)
        if arr.size == 0:
            return []
        if len(arr.shape) == 1:
            arr = arr.reshape((1, arr.shape[0]))
        arr = np.array(arr, dtype=np.float32, copy=True)
        arr[:, 0] = np.clip(arr[:, 0] / 30.0, -3.0, 3.0)
        arr[:, 1] /= 180.0
        arr[:, 3] /= 100.0
        arr[:, 4] /= 100.0
        arr[:, 2] = np.clip(arr[:, 2] / 10.0, -1.0, 1.0)
        return arr.tolist()
    except Exception as exc:
        logging.debug("[GRU 7D] normalize window failed: %s", exc)
        return []


def _trim_gru_7d_rows(limit=200):
    try:
        while len(_gru_feature_rows) > len(_gru_feature_buf):
            _gru_feature_rows.pop(0)
        while len(_gru_feature_rows) > limit:
            _gru_feature_rows.pop(0)
    except Exception:
        pass


def _append_gru_7d_sample(feature_vec, exercise, inference_mode, fsm):
    """Append a raw 7D sample and periodically expose it for Sensor Lab."""
    global _gru_last_buffer_write_ts
    try:
        values = [float(v) for v in feature_vec]
        now_ts = time.time()
        sample = {
            "ts": now_ts,
            "exercise": exercise,
            "inference_mode": inference_mode,
            "columns": list(_GRU_7D_COLUMNS),
            "values": values,
            "features": dict(zip(_GRU_7D_COLUMNS, values)),
            "fsm_state": getattr(fsm, "state", ""),
            "rep_count": int(getattr(fsm, "_total_reps_count", 0) or 0),
        }
        _gru_feature_rows.append(sample)
        _trim_gru_7d_rows()
        if now_ts - _gru_last_buffer_write_ts < 0.20:
            return
        _gru_last_buffer_write_ts = now_ts
        payload = {
            "ok": True,
            "ts": now_ts,
            "columns": list(_GRU_7D_COLUMNS),
            "samples": list(_gru_feature_rows[-200:]),
            "sample_count": len(_gru_feature_rows),
            "window_size": _GRU_WINDOW_SIZE,
            "exercise": exercise,
            "inference_mode": inference_mode,
            "source": "main_claw_loop_gru_feature_buf",
        }
        _atomic_write_json(_GRU_7D_BUFFER_FILE, payload)
    except Exception as exc:
        logging.debug("[GRU 7D] append sample failed: %s", exc)


def _write_gru_7d_window(rep_index, exercise, inference_mode, classification_source,
                         raw_window, normalized_window=None, nn_result=None,
                         emg_ok=False, visual_result=None, final_class=None):
    """Expose the exact recent 7D window used at a rep boundary."""
    try:
        raw_list = _window_to_plain_list(raw_window)
        norm_list = (
            _window_to_plain_list(normalized_window)
            if normalized_window is not None else
            _normalize_gru_7d_window(raw_list)
        )
        rows = list(_gru_feature_rows[-len(raw_list):]) if raw_list else []
        payload = {
            "ok": True,
            "ts": time.time(),
            "rep_index": int(rep_index or 0),
            "exercise": exercise,
            "inference_mode": inference_mode,
            "classification_source": classification_source,
            "emg_ok": bool(emg_ok),
            "visual_result": visual_result,
            "final_class": final_class,
            "columns": list(_GRU_7D_COLUMNS),
            "raw_window": raw_list,
            "normalized_window": norm_list,
            "sample_rows": rows,
            "window_size": _GRU_WINDOW_SIZE,
            "window_rows": len(raw_list),
            "model_result": nn_result if isinstance(nn_result, dict) else {},
            "source": "main_claw_loop_rep_boundary",
        }
        _atomic_write_json(_GRU_LAST_WINDOW_FILE, payload)
    except Exception as exc:
        logging.debug("[GRU 7D] write last window failed: %s", exc)


def _round_point(pt):
    try:
        return [round(float(pt[0]), 1), round(float(pt[1]), 1)]
    except Exception:
        return []


def _angle_drop_is_suspect(prev_angle, raw_angle, kpt_conf, dist_px, dt=None):
    """Reject one-frame pose collapses before they pollute smoothing/min angle."""
    try:
        prev = float(prev_angle)
        raw = float(raw_angle)
        conf = float(kpt_conf or 0.0)
        dist = float(dist_px or 0.0)
        dt_val = float(dt) if dt is not None else 0.0
    except Exception:
        return False
    drop = prev - raw
    if drop < 32.0:
        return False
    if raw <= 70.0 and prev >= 88.0:
        return conf < 0.20 or dist < 80.0 or (dt_val > 0.0 and drop / max(dt_val, 0.001) > 520.0)
    return raw <= 55.0 and drop >= 45.0


def _squat_depth_frame_is_credible(raw_angle, kpt_conf, dist_px):
    """Only let credible deep frames update squat rep minima."""
    try:
        raw = float(raw_angle)
        conf = float(kpt_conf or 0.0)
        dist = float(dist_px or 0.0)
    except Exception:
        return False
    if raw >= 90.0:
        return True
    if dist < 55.0:
        return False
    if raw < 75.0 and (conf < 0.20 or dist < 80.0):
        return False
    if conf < 0.08 and dist < 80.0:
        return False
    return True


def _virtual_angle_candidate(prev_angle, velocity, dt, current_angle, kpt_conf,
                             source="squat"):
    """Return a conservative virtual bottom/peak only when frame evidence is credible."""
    try:
        prev = float(prev_angle)
        vel = float(velocity)
        dt_val = float(dt)
        current = float(current_angle)
        conf = float(kpt_conf or 0.0)
    except Exception:
        return None, "bad_input"
    if conf < 0.25:
        return None, "low_conf"
    if not (0.10 < dt_val < 0.20):
        return None, "dt_out"
    if vel >= -60.0 or vel < -360.0:
        return None, "vel_out"
    predicted = prev + vel * (dt_val * 0.5)
    if predicted < 0:
        return None, "negative"
    if source == "curl":
        floor = 25.0
        max_drop = 26.0
    else:
        floor = 55.0
        max_drop = 28.0
    predicted = max(floor, predicted)
    if current - predicted > max_drop:
        return None, "too_deep"
    return predicted, "virtual"


def _pose_fps(pose_data):
    try:
        ts = float(pose_data.get("timestamp") or 0.0)
        if ts <= 0:
            return 0.0
        age = max(0.001, time.time() - ts)
        if age <= 1.0:
            return 1.0 / age
    except Exception:
        pass
    return 0.0


POSE_FRAME_MAX_AGE_S = 0.75


class _PoseFrameSkipped(Exception):
    pass


def _pose_frame_key(pose_data, fallback_mtime=0.0):
    try:
        frame_idx = pose_data.get("frame_idx")
        ts = float(pose_data.get("timestamp") or 0.0)
        if frame_idx is not None:
            return ("idx", int(frame_idx), round(ts, 4))
        if ts > 0.0:
            return ("ts", round(ts, 4))
    except Exception:
        pass
    try:
        return ("mtime", round(float(fallback_mtime or 0.0), 4))
    except Exception:
        return ("unknown", 0.0)


def _pose_frame_is_fresh(pose_data, fallback_mtime=0.0, max_age_s=POSE_FRAME_MAX_AGE_S):
    now = time.time()
    try:
        ts = float(pose_data.get("timestamp") or 0.0)
        if ts > 0.0:
            age = now - ts
            return -2.0 <= age <= float(max_age_s)
    except Exception:
        pass
    try:
        mt = float(fallback_mtime or 0.0)
        if mt > 0.0:
            return (now - mt) <= float(max_age_s)
    except Exception:
        pass
    return True


def _build_fixed_auto_summary(exercise, inference_mode, good_count, failed_count,
                              comp_count, fatigue, fatigue_limit):
    ex_cn = "哑铃弯举" if exercise == "bicep_curl" else "深蹲"
    total = int(good_count) + int(failed_count)
    if total <= 0:
        bad_desc = "暂无完整动作"
    else:
        bad_rate = int(round(float(failed_count) * 100.0 / max(1, total)))
        if failed_count == 0:
            level = "没有明显不标准"
        elif bad_rate < 25:
            level = "轻微不稳定"
        elif bad_rate < 50:
            level = "需要注意"
        else:
            level = "问题比较明显"
        bad_desc = "不标准程度%s，约%d%%" % (level, bad_rate)
    base = "本组%s：标准%d次，不标准%d次，%s" % (
        ex_cn, int(good_count), int(failed_count), bad_desc)
    if inference_mode == "vision_sensor":
        base += "，代偿%d次" % int(comp_count)
    if fatigue_limit:
        pct = int(round(float(fatigue) * 100.0 / max(1.0, float(fatigue_limit))))
        base += "，疲劳约%d%%" % max(0, min(999, pct))
    if failed_count == 0 and comp_count == 0:
        ending = "，这一组很稳，继续保持。"
    else:
        ending = "，下一组放慢节奏，把动作做完整。"
    return base + ending


class SquatStateMachine:
    ANGLE_STANDARD = 90     # V7.18 (2026-04-20): rep 最低点 < 90° → 标准；否则 → 不标准（二元归类，不留悬空）
    TREND_WINDOW = 8       # 趋势检测滑窗大小
    IDLE_RANGE = 20        # 角度波动小于此值 = 静止
    IDLE_FRAMES = 25       # 连续多少帧稳定才切入 IDLE（~3s）

    def __init__(self):
        self.state = "NO_PERSON"
        self.good_squats = 0
        self.failed_squats = 0
        self.last_active_time = time.time()
        self._last_buzzer_time = 0
        self._angle_history = []
        self._min_angle_in_rep = 999
        self._idle_counter = 0
        self._last_count_time = 0
        self.total_fatigue_volume = 0  # <--- V3新增：双轨疲劳积分池
        # V7.13 底部外插补偿: 即使帧率波动也不漏捕底部角度
        self._last_valid_ts = 0.0
        self._last_valid_angle_sq = None
        self._last_ang_vel_sq = 0.0  # deg/s, 负值=下落
        # M8 (V7.14, 2026-04-20): 代偿计数器 + 防重复
        # GRU 分类 "compensating" 时递增; 同一 rep(_cur_reps) 只计一次
        self._compensation_count = 0
        self._compensation_last_rep = -1
        # V7.15: FSM 独立 rep 边界计数 (无关模式). vision_sensor 模式下 good/failed 由 GRU 分类决定
        self._total_reps_count = 0
        # V7.15: inference_mode 缓存 (避免每帧读盘)
        self._mode_cache = "pure_vision"
        self._mode_last_ts = 0.0
        # V7.16: rep-level debounce 三件套 (复用 self._last_count_time 作为冷却闸门)
        self._descending_start_ts = 0.0   # 进入 DESCENDING 的时戳
        self._falling_frames = 0          # 连续 falling 趋势计数 (入场门控)
        self._rising_frames = 0           # 连续 rising 趋势计数 (离场门控)
        # V7.17 (2026-04-20): BOTTOM/ASCENDING 可见化 —— 用户验收拍片需要完整"蹲到底→上升"动作反馈
        self._bottom_frames = 0           # 连续处于底部稳定带的帧数
        self._BOTTOM_WINDOW = 4           # 连续 4 帧稳定 = 蹲到底 (~0.3s @ 13fps)
        self._BOTTOM_EPS = 5.0            # 度 — 底部稳定带宽度
        self._last_min_source = "none"
        self._last_drop_reject = {}
        self._rep_in_progress = False
        self._rep_started_ts = 0.0
        self._rep_last_valid_ts = 0.0
        self._rep_last_valid_angle = 180.0
        self._last_rep_result = ""
        self._last_finalize_reason = ""
        self._last_rep_min_angle = None
        self._last_rep_mode = ""
        self._last_rep_count = 0
        self._last_rep_event = None
        self._rep_start_angle = None
        self._REP_LOSS_GRACE_S = 0.8
        self._pending_gru_angle_result = None
        self._last_fatigue_model = {}

    def calculate_angle(self, a, b, c):
        try:
            ba = [a[0] - b[0], a[1] - b[1]]
            bc = [c[0] - b[0], c[1] - b[1]]
            dot_prod = ba[0]*bc[0] + ba[1]*bc[1]
            mag_ba = math.sqrt(ba[0]**2 + ba[1]**2)
            mag_bc = math.sqrt(bc[0]**2 + bc[1]**2)
            if mag_ba * mag_bc == 0:
                return 180.0
            cos_angle = dot_prod / (mag_ba * mag_bc)
            cos_angle = max(min(cos_angle, 1.0), -1.0)
            return math.degrees(math.acos(cos_angle))
        except Exception:
            return 180.0

    def trigger_buzzer_alert(self, kind="不标准"):
        """V7.6: 支持两种警报 —— "不标准" (幅度不够) / "代偿" (GRU 检测到代偿)"""
        now = time.time()
        if now - self._last_buzzer_time < 3.0:
            return
        self._last_buzzer_time = now
        try:
            tmp = "/dev/shm/violation_alert.txt.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(kind)
            os.rename(tmp, "/dev/shm/violation_alert.txt")
            logging.warning("🔊 警报已发送: %s", kind)
        except Exception as e:
            logging.error("警报写入失败: %s", e)

    def _read_emg(self):
        try:
            with open("/dev/shm/muscle_activation.json", "r") as f:
                d = json.load(f)
                return d.get("activations", {})
        except Exception:
            return {}

    _last_nn_result = None  # class-level cache for latest GRU result

    def sync_to_frontend(self, current_angle=180.0, nn_result=None):
        if nn_result is not None:
            SquatStateMachine._last_nn_result = nn_result
        try:
            emg_feats = self._read_emg()
            state_data = {
                "state": self.state,
                "good": self.good_squats,
                "failed": self.failed_squats,
                "comp": getattr(self, "_compensation_count", 0),   # V7.15: 暴露代偿计数
                "angle": round(current_angle, 1),
                "fatigue": round(self.total_fatigue_volume, 1),
                "chat_active": os.path.exists("/dev/shm/chat_active"),
                "exercise": "squat",
                "rep_in_progress": bool(self._rep_in_progress),
                "rep_min_angle": (round(float(self._min_angle_in_rep), 1)
                                  if self._rep_in_progress and self._min_angle_in_rep < 999
                                  else None),
                "last_rep_result": self._last_rep_result,
                "last_finalize_reason": self._last_finalize_reason,
                "last_drop_reason": self._last_drop_reject.get("drop_reason", ""),
                "last_rep_min_angle": (round(float(self._last_rep_min_angle), 1)
                                       if self._last_rep_min_angle is not None else None),
                "last_rep_mode": self._last_rep_mode,
                "last_rep_count": self._last_rep_count,
                "last_rep_event": self._last_rep_event,
                "total_reps": self._total_reps_count,
                "fatigue_increment": (self._last_fatigue_model or {}).get("fatigue_increment", 0.0),
                "fatigue_components": (self._last_fatigue_model or {}).get("fatigue_components", {}),
                "fatigue_model_version": (self._last_fatigue_model or {}).get("fatigue_model_version", ""),
                "emg_activations": [
                    emg_feats.get("quadriceps", 0),
                    emg_feats.get("glutes", 0),
                    emg_feats.get("calves", 0),
                    emg_feats.get("biceps", 0)
                ]
            }
            # NN 推理结果 — 只在 rep 内有缓存时注入；rep 完成后由主循环清空避免重播旧分类 (P0.1)
            cached = SquatStateMachine._last_nn_result
            if cached and self.state != "NO_PERSON":
                state_data["similarity"]     = cached.get("similarity", 0.0)
                state_data["classification"] = cached.get("classification", "unknown")
                state_data["nn_confidence"]  = cached.get("confidence", 0.0)
                state_data["nn_phase"]       = cached.get("phase", "unknown")
            else:
                # P0.1: 缓存被清空 -> 显式输出空字段, 让前端读到 "--"
                state_data["similarity"]     = None
                state_data["classification"] = ""
                state_data["nn_confidence"]  = None
                state_data["nn_phase"]       = ""

            with open("/dev/shm/fsm_state.json.tmp", "w", encoding="utf-8") as rf:
                json.dump(state_data, rf)
            os.rename("/dev/shm/fsm_state.json.tmp", "/dev/shm/fsm_state.json")
        except Exception:
            pass

    def _rep_debug_fields(self):
        return {
            "rep_in_progress": bool(self._rep_in_progress),
            "rep_min_angle": (round(float(self._min_angle_in_rep), 1)
                              if self._rep_in_progress and self._min_angle_in_rep < 999
                              else None),
            "last_rep_result": self._last_rep_result,
            "last_finalize_reason": self._last_finalize_reason,
            "last_drop_reason": self._last_drop_reject.get("drop_reason", ""),
            "last_rep_min_angle": (round(float(self._last_rep_min_angle), 1)
                                   if self._last_rep_min_angle is not None else None),
            "last_rep_mode": self._last_rep_mode,
            "last_rep_count": self._last_rep_count,
            "last_rep_event": self._last_rep_event,
        }

    def _refresh_mode_cache(self):
        now_for_mode = time.time()
        if now_for_mode - self._mode_last_ts <= 0.1:
            return
        try:
            if os.path.exists("/dev/shm/inference_mode.json"):
                with open("/dev/shm/inference_mode.json", "r") as _mf:
                    m = json.load(_mf).get("mode", "pure_vision")
                    if m in ("pure_vision", "vision_sensor"):
                        self._mode_cache = m
        except Exception:
            pass
        self._mode_last_ts = now_for_mode

    def _begin_pending_rep(self, angle, source="smooth_frame"):
        self._rep_in_progress = True
        self._rep_started_ts = time.time()
        self._rep_last_valid_ts = self._rep_started_ts
        self._rep_last_valid_angle = float(angle)
        self._rep_start_angle = float(angle)
        self._min_angle_in_rep = float(angle)
        self._last_min_source = source
        self._last_finalize_reason = ""
        self._last_rep_result = ""

    def _update_pending_min(self, angle, source="smooth_frame"):
        if not self._rep_in_progress:
            return
        try:
            val = float(angle)
        except Exception:
            return
        self._rep_last_valid_ts = time.time()
        self._rep_last_valid_angle = val
        if val < self._min_angle_in_rep:
            self._min_angle_in_rep = val
            self._last_min_source = source

    def _reset_rep_debounce(self):
        self._falling_frames = 0
        self._rising_frames = 0
        self._descending_start_ts = 0.0
        self._bottom_frames = 0

    def _finalize_pending_rep(self, reason, current_angle=None):
        if not self._rep_in_progress:
            return False
        try:
            bottom = float(self._min_angle_in_rep)
        except Exception:
            bottom = 999.0
        if bottom >= 999.0:
            if current_angle is not None:
                bottom = float(current_angle)
            else:
                bottom = float(self._rep_last_valid_angle or 180.0)

        ended_ts = time.time()
        self._total_reps_count += 1
        self._refresh_mode_cache()

        result = "standard" if bottom < self.ANGLE_STANDARD else "non_standard"
        top_angle = max(
            _safe_float(self._rep_start_angle, bottom),
            _safe_float(current_angle, bottom),
            _safe_float(self._rep_last_valid_angle, bottom),
        )
        rom = max(0.0, top_angle - bottom)
        self._last_rep_result = result
        self._last_finalize_reason = reason
        self._last_rep_min_angle = bottom
        self._last_rep_mode = self._mode_cache
        self._last_rep_count = self._total_reps_count
        self._last_rep_event = _build_rep_event(
            "squat",
            self._total_reps_count,
            "knee_angle",
            bottom,
            rom,
            result,
            reason,
            self._rep_started_ts,
            ended_ts,
        )
        fatigue_result = _apply_fatigue_model_to_fsm(
            self,
            _fatigue_features_from_rep(
                "squat",
                self._last_rep_event,
                result,
                angle_velocity=getattr(self, "_last_ang_vel_sq", 0.0),
                angle_acceleration=0.0,
                emg_meta=_read_muscle_activation_meta("squat"),
            ),
        )
        self._last_rep_event["fatigue_increment"] = fatigue_result.get("fatigue_increment")
        self._last_rep_event["fatigue_model_version"] = fatigue_result.get("fatigue_model_version")

        # vision_sensor 正常由 GRU 在 main loop 里接管分类；若模型不可用，
        # 用角度二元标准兜底，避免录制时出现“有 rep 但无结果”。
        allow_angle_count = self._mode_cache != "vision_sensor"
        if allow_angle_count:
            if result == "standard":
                self.good_squats += 1
                logging.info("🟢 标准（%s）rep#%d 最低%.0f° < %d° reason=%s 疲劳%.1f",
                             self._mode_cache, self._total_reps_count, bottom,
                             self.ANGLE_STANDARD, reason, self.total_fatigue_volume)
            else:
                self.failed_squats += 1
                self.trigger_buzzer_alert()
                logging.warning("🟡 不标准（%s）rep#%d 最低%.0f° >= %d° reason=%s 累计违规%d",
                                self._mode_cache, self._total_reps_count, bottom,
                                self.ANGLE_STANDARD, reason, self.failed_squats)
        else:
            self._pending_gru_angle_result = result
            logging.info("🧠 vision_sensor rep#%d 已结账，等待 GRU 分类 reason=%s 最低%.0f°",
                         self._total_reps_count, reason, bottom)

        self.state = "STAND"
        self._rep_in_progress = False
        self._rep_started_ts = 0.0
        self._rep_start_angle = None
        self._rep_last_valid_ts = 0.0
        self._rep_last_valid_angle = 180.0
        self._min_angle_in_rep = 999
        self._last_min_source = "none"
        self._last_count_time = time.time()
        self._reset_rep_debounce()
        self._last_valid_angle_sq = None
        self._last_ang_vel_sq = 0.0
        return True

    def _handle_pending_bad_frame(self, reason, current_angle=180.0, set_no_person=False):
        self._last_drop_reject = {"drop_reason": reason, "ts": round(float(time.time()), 3)}
        if not self._rep_in_progress:
            if set_no_person:
                self.state = "NO_PERSON"
            self.sync_to_frontend(current_angle)
            return True
        base_ts = self._rep_last_valid_ts or self._rep_started_ts or time.time()
        if (time.time() - base_ts) >= self._REP_LOSS_GRACE_S:
            self._finalize_pending_rep(reason, current_angle=current_angle)
            if set_no_person:
                self.state = "NO_PERSON"
            self.sync_to_frontend(current_angle)
            return True
        self.sync_to_frontend(self._rep_last_valid_angle or current_angle)
        return True

    def _get_trend(self):
        if len(self._angle_history) < 6:
            return "stable"
        recent = self._angle_history[-6:]
        deltas = [recent[i+1] - recent[i] for i in range(len(recent)-1)]
        avg_delta = sum(deltas) / len(deltas)
        if avg_delta < -2.5: 
            return "falling"
        elif avg_delta > 2.5:
            return "rising"
        return "stable"

    def update(self, pose_data):
        try:
            objects = pose_data.get("objects", [])
            if not objects:
                self._handle_pending_bad_frame("no_person", 180.0, set_no_person=True)
                return None

            obj = objects[0]
            if obj.get("score", 0) < 0.05:
                self._handle_pending_bad_frame("low_object_score", 180.0, set_no_person=True)
                return None

            kpts = obj.get("kpts", [])
            if len(kpts) < 17:
                self._handle_pending_bad_frame("short_keypoints", self._rep_last_valid_angle or 180.0)
                return None

            # 关键点置信度过滤: 低于阈值的坐标不可信, 跳过此帧
            MIN_KPT_CONF = 0.05
            l_score = kpts[11][2] + kpts[13][2] + kpts[15][2]
            r_score = kpts[12][2] + kpts[14][2] + kpts[16][2]
            best_score = max(l_score, r_score)

            # 三个关键点(髋/膝/踝)的平均置信度 < 阈值 → 骨架不可信
            if best_score / 3.0 < MIN_KPT_CONF:
                # 置信度太低，不更新状态
                self._handle_pending_bad_frame("low_kpt_conf", self._rep_last_valid_angle or 180.0)
                return None

            if l_score > r_score:
                hip   = [kpts[11][0], kpts[11][1]]
                knee  = [kpts[13][0], kpts[13][1]]
                ankle = [kpts[15][0], kpts[15][1]]
                side = "left"
            else:
                hip   = [kpts[12][0], kpts[12][1]]
                knee  = [kpts[14][0], kpts[14][1]]
                ankle = [kpts[16][0], kpts[16][1]]
                side = "right"
            raw_angle = self.calculate_angle(hip, knee, ankle)
            pose_fps = _pose_fps(pose_data)
            pose_source = pose_data.get("source") or pose_data.get("mode") or "pose"
            kpt_conf = best_score / 3.0
            now_for_frame = time.time()
            dt_since_valid = (now_for_frame - self._last_valid_ts) if self._last_valid_ts else 0.0

            standing_recovery = raw_angle > 175 and self._rep_in_progress

            # 角度合理性过滤 (Task 4): 量化模型噪声产生不可能的角度
            if raw_angle < 20 or (raw_angle > 175 and not self._rep_in_progress):
                logging.debug("角度异常丢弃: %.1f° (合理范围 20-175)", raw_angle)
                _write_angle_debug("squat", self.state, raw_angle,
                                   self._last_valid_angle_sq or 180.0,
                                   self._min_angle_in_rep if self._min_angle_in_rep < 999 else 180.0,
                                   kpt_conf, pose_fps, pose_source,
                                   {"side": side,
                                    "hip": _round_point(hip),
                                    "knee": _round_point(knee),
                                    "ankle": _round_point(ankle),
                                    "rejected": True,
                                    "drop_reason": "angle_range",
                                    "threshold": self.ANGLE_STANDARD,
                                    **self._rep_debug_fields()})
                if self._rep_in_progress:
                    self._handle_pending_bad_frame("angle_range", self._rep_last_valid_angle or 180.0)
                return None

            # 关键点间距检查: 髋-踝太近 = 关键点重叠不可信
            dist_ha = math.hypot(hip[0] - ankle[0], hip[1] - ankle[1])
            if dist_ha < 30:
                logging.debug("关键点间距过小: %.1f px, 丢弃此帧", dist_ha)
                _write_angle_debug("squat", self.state, raw_angle,
                                   self._last_valid_angle_sq or raw_angle,
                                   self._min_angle_in_rep if self._min_angle_in_rep < 999 else raw_angle,
                                   kpt_conf, pose_fps, pose_source,
                                   {"side": side,
                                    "hip": _round_point(hip),
                                    "knee": _round_point(knee),
                                    "ankle": _round_point(ankle),
                                    "dist_ha": round(float(dist_ha), 1),
                                    "rejected": True,
                                    "drop_reason": "keypoint_distance",
                                    "threshold": self.ANGLE_STANDARD,
                                    **self._rep_debug_fields()})
                self._handle_pending_bad_frame("keypoint_distance", self._rep_last_valid_angle or raw_angle)
                return None
            depth_frame_credible = _squat_depth_frame_is_credible(raw_angle, kpt_conf, dist_ha)
            if not depth_frame_credible:
                self._last_drop_reject = {
                    "dropped_raw_angle": round(float(raw_angle), 1),
                    "prev_angle": (round(float(self._last_valid_angle_sq), 1)
                                   if self._last_valid_angle_sq is not None else None),
                    "drop_reason": "uncertain_depth_frame",
                    "dist_ha": round(float(dist_ha), 1),
                    "kpt_conf": round(float(kpt_conf), 3),
                }
                _write_angle_debug("squat", self.state, raw_angle,
                                   self._last_valid_angle_sq or raw_angle,
                                   self._min_angle_in_rep if self._min_angle_in_rep < 999 else raw_angle,
                                   kpt_conf, pose_fps, pose_source,
                                   dict(self._last_drop_reject,
                                        side=side,
                                        hip=_round_point(hip),
                                        knee=_round_point(knee),
                                        ankle=_round_point(ankle),
                                        rejected=True,
                                        min_source=self._last_min_source,
                                        threshold=self.ANGLE_STANDARD,
                                        **self._rep_debug_fields()))
                logging.info("丢弃深蹲低可信最低角: raw=%.1f conf=%.2f dist=%.1f",
                             raw_angle, kpt_conf, dist_ha)
                self._handle_pending_bad_frame("uncertain_depth_frame",
                                               self._rep_last_valid_angle or raw_angle)
                return None
            if self._last_valid_angle_sq is not None and _angle_drop_is_suspect(
                    self._last_valid_angle_sq, raw_angle, kpt_conf, dist_ha, dt_since_valid):
                self._last_drop_reject = {
                    "dropped_raw_angle": round(float(raw_angle), 1),
                    "prev_angle": round(float(self._last_valid_angle_sq), 1),
                    "drop_reason": "sudden_pose_collapse",
                    "dt": round(float(dt_since_valid), 3),
                    "dist_ha": round(float(dist_ha), 1),
                    "kpt_conf": round(float(kpt_conf), 3),
                }
                _write_angle_debug("squat", self.state, raw_angle,
                                   self._last_valid_angle_sq,
                                   self._min_angle_in_rep if self._min_angle_in_rep < 999 else self._last_valid_angle_sq,
                                   kpt_conf, pose_fps, pose_source,
                                   dict(self._last_drop_reject,
                                        side=side,
                                        hip=_round_point(hip),
                                        knee=_round_point(knee),
                                        ankle=_round_point(ankle),
                                        rejected=True,
                                        min_source=self._last_min_source,
                                        threshold=self.ANGLE_STANDARD,
                                        **self._rep_debug_fields()))
                logging.info("丢弃深蹲角度跳降: prev=%.1f raw=%.1f conf=%.2f dist=%.1f",
                             self._last_valid_angle_sq, raw_angle, kpt_conf, dist_ha)
                self._handle_pending_bad_frame("sudden_pose_collapse", self._rep_last_valid_angle or raw_angle)
                return None

            if standing_recovery:
                _write_angle_debug("squat", self.state, raw_angle,
                                   self._last_valid_angle_sq or 180.0,
                                   self._min_angle_in_rep if self._min_angle_in_rep < 999 else 180.0,
                                   kpt_conf, pose_fps, pose_source,
                                   {"side": side,
                                    "hip": _round_point(hip),
                                    "knee": _round_point(knee),
                                    "ankle": _round_point(ankle),
                                    "dist_ha": round(float(dist_ha), 1),
                                    "rejected": False,
                                    "drop_reason": "",
                                    "finalize_hint": "standing_recovery",
                                    "threshold": self.ANGLE_STANDARD,
                                    **self._rep_debug_fields()})
                self._finalize_pending_rep("standing_recovery", current_angle=180.0)
                self.sync_to_frontend(180.0)
                return 180.0

            self._angle_history.append(raw_angle)
            if len(self._angle_history) > 16:
                self._angle_history.pop(0)
            smooth_n = min(5, len(self._angle_history))
            angle = sum(self._angle_history[-smooth_n:]) / smooth_n
            _write_angle_debug("squat", self.state, raw_angle, angle,
                               min(self._min_angle_in_rep, angle),
                               kpt_conf, pose_fps, pose_source,
                               {"threshold": self.ANGLE_STANDARD,
                                "side": side,
                                "hip": _round_point(hip),
                                "knee": _round_point(knee),
                                "ankle": _round_point(ankle),
                                "dist_ha": round(float(dist_ha), 1),
                                "dt": round(float(dt_since_valid), 3),
                                "ang_vel": round(float(self._last_ang_vel_sq), 1),
                                "min_source": self._last_min_source,
                                "rejected": False,
                                **self._rep_debug_fields()})

            trend = self._get_trend()

            # ===== 状态流转 (V7.17: 五级可见 —— NO_PERSON/STAND/DESCENDING/BOTTOM/ASCENDING) =====
            if self.state in ["NO_PERSON", "IDLE", "STAND"]:
                # V7.16: 维护连续 falling 帧计数 (其他趋势重置)
                if trend == "falling":
                    self._falling_frames += 1
                elif trend == "rising":
                    self._falling_frames = 0

                # V7.16: 入场必须满足 ALL 三项 —— 角度阈值 + 2 连续 falling + 0.8s 冷却
                _cooldown_ok = (time.time() - self._last_count_time) >= 0.8
                if angle < 140 and self._falling_frames >= 2 and _cooldown_ok:
                    self.state = "DESCENDING"
                    self._begin_pending_rep(angle, "smooth_frame")
                    self._descending_start_ts = time.time()
                    self._rising_frames = 0
                    self._bottom_frames = 0
                    self.last_active_time = time.time()
                else:
                    self.state = "STAND"

            elif self.state == "DESCENDING":
                self.last_active_time = time.time()
                # V7.16: 维护连续 rising 帧计数 (在 DESCENDING 中统计起身信号)
                if trend == "rising":
                    self._rising_frames += 1
                elif trend == "falling":
                    self._rising_frames = 0
                # V7.13 底部外插: 若两帧间隙 > 80ms 且之前在下落, 认为错过了真实底部
                # 用 angle_prev + ang_vel_prev * dt/2 估算中间点的最深角度
                now_ts = time.time()
                virtual_bottom = None
                virtual_reason = "no_prev"
                if self._last_valid_angle_sq is not None:
                    dt = now_ts - self._last_valid_ts
                    virtual_bottom, virtual_reason = _virtual_angle_candidate(
                        self._last_valid_angle_sq, self._last_ang_vel_sq, dt, angle, kpt_conf, "squat")
                if virtual_bottom is not None:
                    if virtual_bottom < self._min_angle_in_rep and virtual_bottom <= angle:
                        self._update_pending_min(virtual_bottom, "virtual")
                    self._update_pending_min(angle, "smooth_frame")
                else:
                    self._update_pending_min(angle, "smooth_frame")

                # V7.17: 底部稳定带 —— 角度在 min + 5° 内连续 N 帧 ⇒ BOTTOM（蹲到底）
                if angle <= self._min_angle_in_rep + self._BOTTOM_EPS:
                    self._bottom_frames += 1
                else:
                    self._bottom_frames = 0

                # V7.17: 快速 rep（无明显 hold）—— 已离底 > 10° 且连续 rising ⇒ 直入 ASCENDING
                if angle > self._min_angle_in_rep + 10.0 and self._rising_frames >= 2:
                    self.state = "ASCENDING"
                # V7.17: 标准 rep —— 底部稳定带达标 ⇒ BOTTOM
                elif self._bottom_frames >= self._BOTTOM_WINDOW:
                    self.state = "BOTTOM"
                    self._rising_frames = 0

            elif self.state == "BOTTOM":
                # V7.17: 蹲到底稳定态 —— 继续下落则回 DESCENDING 更新 min；连续 rising ⇒ ASCENDING
                self.last_active_time = time.time()
                self._update_pending_min(angle, "smooth_frame")
                if trend == "rising":
                    self._rising_frames += 1
                elif trend == "falling":
                    self._rising_frames = 0
                    # 还在继续探底 — 回 DESCENDING 刷 min
                    if angle < self._min_angle_in_rep - 1.5:
                        self.state = "DESCENDING"
                        self._bottom_frames = 0
                if self._rising_frames >= 2 and angle > self._min_angle_in_rep + 5.0:
                    self.state = "ASCENDING"

            elif self.state == "ASCENDING":
                # V7.17: 上升段 —— 原 DESCENDING 内的结账逻辑搬到此处
                self.last_active_time = time.time()
                # V7.18: ASCENDING 也追踪最低点 (防抖 2 帧 rising 判定可能错过一次反弹)
                self._update_pending_min(angle, "smooth_frame")
                if trend == "rising":
                    self._rising_frames += 1
                elif trend == "falling":
                    self._rising_frames = 0

                # V7.19 (2026-04-20): 结账门槛放宽 —— 用户姿态偏低起身不到 150° 时 rep 永远不结账
                # 新门槛：离底 ≥ 25° + 连续 rising ≥ 2 帧 + dur ≥ 0.4s
                _dur_ok = (time.time() - self._descending_start_ts) >= 0.4
                _rose_enough = angle > (self._min_angle_in_rep + 25.0)
                if _rose_enough and self._rising_frames >= 2 and _dur_ok:
                    self._finalize_pending_rep("normal_recovery", current_angle=angle)

            # V7.13: 每帧末尾刷新角速度追踪 (用于下一帧的外插基准)
            _now_update = time.time()
            if self._last_valid_angle_sq is not None:
                _dt_upd = _now_update - self._last_valid_ts
                if _dt_upd > 1e-3:
                    self._last_ang_vel_sq = (angle - self._last_valid_angle_sq) / _dt_upd
            self._last_valid_angle_sq = angle
            self._last_valid_ts = _now_update

            self.sync_to_frontend(angle)
            return angle
        except Exception as e:
            logging.error(f"FSM 异常: {e}")
            self.state = "NO_PERSON"
            self.sync_to_frontend()
            return None


class DumbbellCurlFSM:
    ANGLE_STANDARD = 80    # 本次 rep 最低肘角 <=80° 视为视觉标准弯举（2026-05-05 现场标定）
    CURL_MIN_ROM = 35.0
    CURL_ENTRY_ANGLE = 135.0
    
    def __init__(self):
        self.state = "NO_PERSON"
        self._good_reps = 0
        self._failed_reps = 0
        self.last_active_time = time.time()
        self._last_buzzer_time = 0
        self._angle_history = []
        self._min_angle_in_rep = 999
        self._last_count_time = 0
        self.total_fatigue_volume = 0
        # V7.13 顶峰外插补偿: 即使帧率波动也不漏捕最小角度 (手臂收紧峰值)
        self._last_valid_ts = 0.0
        self._last_valid_angle_cu = None
        self._last_ang_vel_cu = 0.0  # deg/s, 负值=肘关节正在闭合
        # M8 (V7.14): 弯举动作也要有代偿计数, prompt 统一
        self._compensation_count = 0
        self._compensation_last_rep = -1
        # V7.15: FSM 独立 rep 边界计数 (无关模式)
        self._total_reps_count = 0
        self._mode_cache = "pure_vision"
        self._mode_last_ts = 0.0
        # V7.16: rep-level debounce 三件套 (与 SquatStateMachine 对称)
        self._curling_start_ts = 0.0      # 进入 CURLING 的时戳
        self._closing_frames = 0          # 连续肘关节闭合 (falling) 帧 — 入场门控
        self._opening_frames = 0          # 连续肘关节张开 (rising) 帧 — 离场门控
        self._rep_in_progress = False
        self._rep_started_ts = 0.0
        self._rep_start_angle = None
        self._rep_last_valid_ts = 0.0
        self._rep_last_valid_angle = 180.0
        self._last_rep_result = ""
        self._last_finalize_reason = ""
        self._last_drop_reject = {}
        self._last_rep_min_angle = None
        self._last_rep_mode = ""
        self._last_rep_count = 0
        self._last_rep_event = None
        self._pending_gru_angle_result = None
        self._active_side = None
        self._last_tracking_side = None
        self._side_angle_history = {"left": [], "right": []}
        self._side_closing_frames = {"left": 0, "right": 0}
        self._last_fatigue_model = {}

    # V7.16: 与 SquatStateMachine._get_trend 同逻辑，curl 的 falling=收紧、rising=伸展
    def _get_trend(self):
        if len(self._angle_history) < 6:
            return "stable"
        recent = self._angle_history[-6:]
        deltas = [recent[i+1] - recent[i] for i in range(len(recent)-1)]
        avg_delta = sum(deltas) / len(deltas)
        if avg_delta < -2.5:
            return "falling"
        elif avg_delta > 2.5:
            return "rising"
        return "stable"

    def _get_side_trend(self, side):
        history = self._side_angle_history.get(side, [])
        if len(history) < 6:
            return "stable"
        recent = history[-6:]
        deltas = [recent[i+1] - recent[i] for i in range(len(recent)-1)]
        avg_delta = sum(deltas) / len(deltas)
        if avg_delta < -2.5:
            return "falling"
        elif avg_delta > 2.5:
            return "rising"
        return "stable"

    @property
    def good_squats(self): return self._good_reps
    @good_squats.setter
    def good_squats(self, val): self._good_reps = val
    
    @property
    def failed_squats(self): return self._failed_reps
    @failed_squats.setter
    def failed_squats(self, val): self._failed_reps = val

    def calculate_angle(self, a, b, c):
        try:
            ba = [a[0] - b[0], a[1] - b[1]]
            bc = [c[0] - b[0], c[1] - b[1]]
            dot_prod = ba[0]*bc[0] + ba[1]*bc[1]
            mag_ba = math.sqrt(ba[0]**2 + ba[1]**2)
            mag_bc = math.sqrt(bc[0]**2 + bc[1]**2)
            if mag_ba * mag_bc == 0:
                return 180.0
            cos_angle = dot_prod / (mag_ba * mag_bc)
            cos_angle = max(min(cos_angle, 1.0), -1.0)
            return math.degrees(math.acos(cos_angle))
        except Exception:
            return 180.0

    def _curl_side_metrics(self, kpts, side):
        if side == "left":
            idx = (5, 7, 9)
        else:
            idx = (6, 8, 10)
        shoulder = [kpts[idx[0]][0], kpts[idx[0]][1]]
        elbow = [kpts[idx[1]][0], kpts[idx[1]][1]]
        wrist = [kpts[idx[2]][0], kpts[idx[2]][1]]
        conf = (kpts[idx[0]][2] + kpts[idx[1]][2] + kpts[idx[2]][2]) / 3.0
        angle = self.calculate_angle(shoulder, elbow, wrist)
        upper_len = math.hypot(shoulder[0] - elbow[0], shoulder[1] - elbow[1])
        forearm_len = math.hypot(wrist[0] - elbow[0], wrist[1] - elbow[1])
        dist_sw = math.hypot(shoulder[0] - wrist[0], shoulder[1] - wrist[1])
        valid = (
            conf >= 0.05 and
            10.0 <= angle <= 175.0 and
            upper_len >= 12.0 and
            forearm_len >= 12.0 and
            dist_sw >= 20.0
        )
        return {
            "side": side,
            "shoulder": shoulder,
            "elbow": elbow,
            "wrist": wrist,
            "conf": conf,
            "angle": angle,
            "upper_len": upper_len,
            "forearm_len": forearm_len,
            "dist_sw": dist_sw,
            "valid": bool(valid),
        }

    def _select_curl_side(self, left, right):
        sides = [m for m in (left, right) if m.get("valid")]
        if not sides:
            return None, "no_valid_arm"
        if self._active_side:
            for m in sides:
                if m.get("side") == self._active_side:
                    return m, "locked_" + self._active_side
        if len(sides) == 1:
            return sides[0], "single_valid_" + sides[0].get("side", "")

        lower = left if left["angle"] <= right["angle"] else right
        higher = right if lower is left else left
        if (
            lower["angle"] <= self.CURL_ENTRY_ANGLE and
            (higher["angle"] - lower["angle"]) >= 12.0 and
            lower["conf"] >= 0.05
        ):
            return lower, "lower_elbow_angle_" + lower["side"]
        if abs(left["conf"] - right["conf"]) >= 0.10:
            chosen = left if left["conf"] > right["conf"] else right
            return chosen, "higher_conf_" + chosen["side"]
        chosen = left if left["angle"] <= right["angle"] else right
        return chosen, "tie_lower_angle_" + chosen["side"]

    def trigger_buzzer_alert(self, kind="不标准"):
        now = time.time()
        if now - self._last_buzzer_time < 3.0:
            return
        self._last_buzzer_time = now
        try:
            tmp = "/dev/shm/violation_alert.txt.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(kind)
            os.rename(tmp, "/dev/shm/violation_alert.txt")
            logging.warning("🔊 弯举警报已发送: %s", kind)
        except Exception as e:
            logging.error("违规警报写入失败: %s", e)

    def _read_emg(self):
        try:
            with open("/dev/shm/muscle_activation.json", "r") as f:
                d = json.load(f)
                return d.get("activations", {})
        except Exception:
            return {}

    def _refresh_mode_cache(self):
        now_for_mode = time.time()
        if now_for_mode - self._mode_last_ts <= 0.1:
            return
        try:
            if os.path.exists("/dev/shm/inference_mode.json"):
                with open("/dev/shm/inference_mode.json", "r") as _mf:
                    m = json.load(_mf).get("mode", "pure_vision")
                    if m in ("pure_vision", "vision_sensor"):
                        self._mode_cache = m
        except Exception:
            pass
        self._mode_last_ts = now_for_mode

    def _begin_curl_rep(self, angle):
        val = float(angle)
        self._rep_in_progress = True
        self._rep_started_ts = time.time()
        self._rep_start_angle = val
        self._rep_last_valid_ts = self._rep_started_ts
        self._rep_last_valid_angle = val
        self._min_angle_in_rep = val
        self._curling_start_ts = self._rep_started_ts
        self._opening_frames = 0
        self._last_finalize_reason = ""
        self._last_rep_result = ""

    def _reset_curl_debounce(self):
        self._closing_frames = 0
        self._opening_frames = 0
        self._curling_start_ts = 0.0

    def _reset_curl_motion_history(self):
        self._angle_history = []
        self._reset_curl_debounce()
        self._last_valid_angle_cu = None
        self._last_valid_ts = 0.0
        self._last_ang_vel_cu = 0.0
        self._active_side = None
        self._last_tracking_side = None
        self._side_angle_history = {"left": [], "right": []}
        self._side_closing_frames = {"left": 0, "right": 0}

    def _cancel_partial_curl_rep(self, reason):
        if self._rep_in_progress:
            self._last_drop_reject = {"drop_reason": reason}
        self._rep_in_progress = False
        self._rep_started_ts = 0.0
        self._rep_start_angle = None
        self._rep_last_valid_ts = 0.0
        self._rep_last_valid_angle = 180.0
        self._min_angle_in_rep = 999
        self._pending_gru_angle_result = None

    def _finalize_curl_rep(self, reason, current_angle=None):
        if not self._rep_in_progress:
            return False
        ended_ts = time.time()
        self._last_rep_min_angle = self._min_angle_in_rep
        bottom = _safe_float(self._last_rep_min_angle, 999.0)
        if bottom >= 999.0:
            bottom = _safe_float(current_angle, self._rep_last_valid_angle)
            self._last_rep_min_angle = bottom
        top_angle = max(
            _safe_float(self._rep_start_angle, bottom),
            _safe_float(current_angle, bottom),
            _safe_float(self._rep_last_valid_angle, bottom),
        )
        rom = max(0.0, top_angle - bottom)

        self._total_reps_count += 1
        self._refresh_mode_cache()

        result = "standard" if bottom <= self.ANGLE_STANDARD else "non_standard"
        self._last_rep_result = result
        self._last_finalize_reason = reason
        self._last_rep_mode = self._mode_cache
        self._last_rep_count = self._total_reps_count
        self._last_rep_event = _build_rep_event(
            "bicep_curl",
            self._total_reps_count,
            "elbow_angle",
            bottom,
            rom,
            result,
            reason,
            self._rep_started_ts,
            ended_ts,
        )
        fatigue_result = _apply_fatigue_model_to_fsm(
            self,
            _fatigue_features_from_rep(
                "bicep_curl",
                self._last_rep_event,
                result,
                angle_velocity=getattr(self, "_last_ang_vel_cu", 0.0),
                angle_acceleration=0.0,
                emg_meta=_read_muscle_activation_meta("bicep_curl"),
            ),
        )
        self._last_rep_event["fatigue_increment"] = fatigue_result.get("fatigue_increment")
        self._last_rep_event["fatigue_model_version"] = fatigue_result.get("fatigue_model_version")

        # vision_sensor 模式下，主循环会在 rep_event 上触发一次 GRU/兜底分类。
        if self._mode_cache != "vision_sensor":
            if result == "standard":
                self._good_reps += 1
                logging.info("🟢 弯举标准（%s）rep#%d 最低%.0f° ROM%.0f° 疲劳%.1f",
                             self._mode_cache, self._total_reps_count,
                             bottom, rom, self.total_fatigue_volume)
            else:
                self._failed_reps += 1
                self.trigger_buzzer_alert()
                logging.warning("🟡 弯举不标准（%s）rep#%d 最低%.0f° ROM%.0f° 违规%d",
                                self._mode_cache, self._total_reps_count,
                                bottom, rom, self._failed_reps)
        else:
            self._pending_gru_angle_result = result
            logging.info("🧠 弯举 vision_sensor rep#%d 已结账，等待 GRU 分类 reason=%s 最低%.0f° ROM%.0f°",
                         self._total_reps_count, reason, bottom, rom)

        self.state = "STAND"
        self._rep_in_progress = False
        self._rep_started_ts = 0.0
        self._rep_start_angle = None
        self._rep_last_valid_ts = 0.0
        self._rep_last_valid_angle = 180.0
        self._min_angle_in_rep = 999
        self._last_count_time = time.time()
        self._reset_curl_debounce()
        self._last_valid_angle_cu = None
        self._last_ang_vel_cu = 0.0
        self._active_side = None
        return True

    _last_nn_result = None

    def sync_to_frontend(self, current_angle=180.0, nn_result=None):
        if nn_result is not None:
            DumbbellCurlFSM._last_nn_result = nn_result
        try:
            emg_feats = self._read_emg()
            state_data = {
                "state": self.state,
                "good": self._good_reps,
                "failed": self._failed_reps,
                "comp": getattr(self, "_compensation_count", 0),   # V7.15
                "angle": round(current_angle, 1),
                "fatigue": round(self.total_fatigue_volume, 1),
                "chat_active": os.path.exists("/dev/shm/chat_active"),
                "exercise": "bicep_curl",
                "rep_in_progress": bool(self._rep_in_progress),
                "rep_min_angle": (round(float(self._min_angle_in_rep), 1)
                                  if self._rep_in_progress and self._min_angle_in_rep < 999
                                  else None),
                "last_rep_result": self._last_rep_result,
                "last_finalize_reason": self._last_finalize_reason,
                "last_drop_reason": self._last_drop_reject.get("drop_reason", ""),
                "last_rep_min_angle": (round(float(self._last_rep_min_angle), 1)
                                       if self._last_rep_min_angle is not None else None),
                "last_rep_mode": self._last_rep_mode,
                "last_rep_count": self._last_rep_count,
                "last_rep_event": self._last_rep_event,
                "total_reps": self._total_reps_count,
                "emg_activations": [
                    emg_feats.get("biceps", 0),
                    emg_feats.get("forearm", 0),
                    emg_feats.get("shoulder", emg_feats.get("deltoid", 0)),
                    emg_feats.get("triceps", 0)
                ]
            }
            # P0.1: rep 完成后类变量被清空, 不再重播上一次 GRU 结果
            cached = DumbbellCurlFSM._last_nn_result
            if cached and self.state != "NO_PERSON":
                state_data["similarity"]     = cached.get("similarity", 0.0)
                state_data["classification"] = cached.get("classification", "unknown")
                state_data["nn_confidence"]  = cached.get("confidence", 0.0)
                state_data["nn_phase"]       = cached.get("phase", "unknown")
            else:
                state_data["similarity"]     = None
                state_data["classification"] = ""
                state_data["nn_confidence"]  = None
                state_data["nn_phase"]       = ""

            with open("/dev/shm/fsm_state.json.tmp", "w", encoding="utf-8") as rf:
                json.dump(state_data, rf)
            os.rename("/dev/shm/fsm_state.json.tmp", "/dev/shm/fsm_state.json")
        except Exception:
            pass

    def update(self, pose_data):
        try:
            objects = pose_data.get("objects", [])
            if not objects:
                self.state = "NO_PERSON"
                self._cancel_partial_curl_rep("no_person")
                self._reset_curl_motion_history()
                self.sync_to_frontend()
                return None

            obj = objects[0]
            if obj.get("score", 0) < 0.05:
                self.state = "NO_PERSON"
                self._cancel_partial_curl_rep("low_person_score")
                self._reset_curl_motion_history()
                self.sync_to_frontend()
                return None

            kpts = obj.get("kpts", [])
            if len(kpts) < 17:
                return None

            left_arm = self._curl_side_metrics(kpts, "left")
            right_arm = self._curl_side_metrics(kpts, "right")
            selected_arm, select_reason = self._select_curl_side(left_arm, right_arm)
            if selected_arm is None:
                self._cancel_partial_curl_rep("no_valid_arm")
                self._reset_curl_motion_history()
                return None

            shoulder = selected_arm["shoulder"]
            elbow = selected_arm["elbow"]
            wrist = selected_arm["wrist"]
            raw_angle = selected_arm["angle"]
            selected_side = selected_arm["side"]
            pose_fps = _pose_fps(pose_data)
            pose_source = pose_data.get("source") or pose_data.get("mode") or "pose"

            # 角度合理性过滤 (Task 4)
            if raw_angle < 10 or raw_angle > 175:
                logging.debug("弯举角度异常丢弃: %.1f°", raw_angle)
                self._reset_curl_motion_history()
                return None

            # 关键点间距检查
            dist_sw = math.hypot(shoulder[0] - wrist[0], shoulder[1] - wrist[1])
            if dist_sw < 20:
                logging.debug("弯举关键点间距过小: %.1f px", dist_sw)
                self._reset_curl_motion_history()
                return None

            self._last_tracking_side = selected_side
            for side_name, arm in (("left", left_arm), ("right", right_arm)):
                history = self._side_angle_history.setdefault(side_name, [])
                if arm.get("valid"):
                    history.append(float(arm["angle"]))
                    if len(history) > 16:
                        history.pop(0)
                else:
                    history[:] = []
                    self._side_closing_frames[side_name] = 0

            self._angle_history.append(raw_angle)
            if len(self._angle_history) > 16:
                self._angle_history.pop(0)
            smooth_n = min(5, len(self._angle_history))
            angle = sum(self._angle_history[-smooth_n:]) / smooth_n
            side_history = self._side_angle_history.get(selected_side, [])
            side_smooth_n = min(5, len(side_history))
            side_angle = (
                sum(side_history[-side_smooth_n:]) / side_smooth_n
                if side_smooth_n else raw_angle
            )
            side_trend = self._get_side_trend(selected_side)
            angle_extra = {
                "threshold": self.ANGLE_STANDARD,
                "selected_side": selected_arm["side"],
                "selection_reason": select_reason,
                "left_angle": round(float(left_arm["angle"]), 1),
                "right_angle": round(float(right_arm["angle"]), 1),
                "left_conf": round(float(left_arm["conf"]), 3),
                "right_conf": round(float(right_arm["conf"]), 3),
                "left_valid": bool(left_arm["valid"]),
                "right_valid": bool(right_arm["valid"]),
                "shoulder": _round_point(shoulder),
                "elbow": _round_point(elbow),
                "wrist": _round_point(wrist),
                "left_shoulder": _round_point(left_arm["shoulder"]),
                "left_elbow": _round_point(left_arm["elbow"]),
                "left_wrist": _round_point(left_arm["wrist"]),
                "right_shoulder": _round_point(right_arm["shoulder"]),
                "right_elbow": _round_point(right_arm["elbow"]),
                "right_wrist": _round_point(right_arm["wrist"]),
                "dist_sw": round(float(selected_arm["dist_sw"]), 1),
                "upper_len": round(float(selected_arm["upper_len"]), 1),
                "forearm_len": round(float(selected_arm["forearm_len"]), 1),
                "trend": self._get_trend(),
                "side_trend": side_trend,
                "side_angle": round(float(side_angle), 1),
                "closing_frames": int(self._closing_frames),
                "side_closing_frames": int(self._side_closing_frames.get(selected_side, 0)),
                "opening_frames": int(self._opening_frames),
                "rep_in_progress": bool(self._rep_in_progress),
                "active_side": self._active_side or "",
            }
            _write_angle_debug("bicep_curl", self.state, raw_angle, angle,
                               min(self._min_angle_in_rep, angle),
                               selected_arm["conf"], pose_fps, pose_source,
                               angle_extra)

            # V7.16: 四级防抖状态流转（与 squat 对称）
            trend = self._get_trend()
            if self.state in ["NO_PERSON", "IDLE", "STAND", "EXTENDING"]:
                if side_trend == "falling":
                    self._side_closing_frames[selected_side] = (
                        self._side_closing_frames.get(selected_side, 0) + 1
                    )
                elif side_trend == "rising":
                    self._side_closing_frames[selected_side] = 0
                self._closing_frames = self._side_closing_frames.get(selected_side, 0)
                _cooldown_ok = (time.time() - self._last_count_time) >= 0.8
                if (
                    side_angle <= self.CURL_ENTRY_ANGLE and
                    self._side_closing_frames.get(selected_side, 0) >= 2 and
                    _cooldown_ok
                ):
                    self.state = "CURLING"
                    self._active_side = selected_arm["side"]
                    self._begin_curl_rep(side_angle)
                    self.last_active_time = time.time()
                else:
                    self.state = "STAND"

            elif self.state == "CURLING":
                self.last_active_time = time.time()
                if trend == "rising":
                    self._opening_frames += 1
                elif trend == "falling":
                    self._opening_frames = 0
                # V7.13 顶峰外插: 若两帧间隙 > 80ms 且之前在收紧, 认为错过了真实顶峰
                now_ts = time.time()
                virtual_peak = None
                if self._last_valid_angle_cu is not None:
                    dt = now_ts - self._last_valid_ts
                    if 0.08 < dt < 0.25 and self._last_ang_vel_cu < -8.0:
                        predicted = self._last_valid_angle_cu + self._last_ang_vel_cu * (dt * 0.5)
                        # 物理下限钳位: 肘关节最小可闭合角 ~25°
                        virtual_peak = max(25.0, predicted)
                if virtual_peak is not None:
                    self._min_angle_in_rep = min(self._min_angle_in_rep, virtual_peak, angle)
                else:
                    self._min_angle_in_rep = min(self._min_angle_in_rep, angle)
                self._rep_last_valid_ts = time.time()
                self._rep_last_valid_angle = angle

                # V7.19 (2026-04-20): 结账放宽 —— 离顶 ≥ 25° + 2 连续 opening + dur ≥ 0.4s
                _dur_ok = (time.time() - self._curling_start_ts) >= 0.4
                _rose_enough = angle > (self._min_angle_in_rep + 25.0)
                if _rose_enough and self._opening_frames >= 2 and _dur_ok:
                    self._finalize_curl_rep("normal_recovery", current_angle=angle)

            # V7.13: 每帧末尾刷新角速度追踪 (用于下一帧的外插基准)
            _now_update = time.time()
            if self._last_valid_angle_cu is not None:
                _dt_upd = _now_update - self._last_valid_ts
                if _dt_upd > 1e-3:
                    self._last_ang_vel_cu = (angle - self._last_valid_angle_cu) / _dt_upd
            self._last_valid_angle_cu = angle
            self._last_valid_ts = _now_update

            self.sync_to_frontend(angle)
            return angle
        except Exception as e:
            logging.error(f"FSM 异常: {e}")
            self.state = "NO_PERSON"
            self.sync_to_frontend()
            return None


async def _deepseek_fire_and_forget(bridge, prompt, good_count, failed_count):
    for attempt in range(3):
        try:
            logging.info(f"📤 [后台] 发送战报给 DeepSeek (尝试 {attempt+1}/3)...")
            start_time = time.time()
            reply = await bridge.ask(prompt, timeout=15)  # V7.10 60s->15s
            elapsed = time.time() - start_time

            if "Timeout" in reply or "Gateway" in reply or "rejected" in reply:
                logging.warning(f"⚠️ [后台] 尝试 {attempt+1} 返回错误: {reply}")
                if attempt < 2:
                    await asyncio.sleep(3)
                    continue

            # Strip <think>...</think> reasoning block (same as chat path)
            if "</think>" in reply:
                reply = reply.split("</think>")[-1].strip()
            logging.info(f"💡 [后台] DeepSeek 响应 ({elapsed:.2f}s): {reply}")

            try:
                with open("/dev/shm/llm_reply.txt.tmp", "w", encoding="utf-8") as rf:
                    rf.write(reply)
                os.rename("/dev/shm/llm_reply.txt.tmp", "/dev/shm/llm_reply.txt")
                # V5.0: 写 seq 递增,voice_daemon 的 _llm_reply_watcher 靠 seq 捕获同秒多写
                try:
                    _seq_path = "/dev/shm/llm_reply.txt.seq"
                    _prev = 0
                    if os.path.exists(_seq_path):
                        with open(_seq_path, "r") as _sf:
                            _prev = int((_sf.read() or "0").strip() or "0")
                    with open(_seq_path + ".tmp", "w") as _sf:
                        _sf.write(str(_prev + 1))
                    os.rename(_seq_path + ".tmp", _seq_path)
                except Exception:
                    pass
            except Exception as e:
                logging.error(f"下发回复至内存盘失败: {e}")

            # 飞书推送已改为手动/语音触发，不再自动推送每次训练点评
            return reply
        except Exception as e:
            logging.error(f"❌ [后台] 尝试 {attempt+1} 异常: {e}")
            if attempt < 2:
                await asyncio.sleep(3)
    logging.error("❌ [后台] DeepSeek 3 次重试全部失败")
    return ""


_V_BANNER = "V7.18.2 (2026-04-20)"  # 版本标识 — 启动 banner 用

async def main():
    # 必须在函数顶部声明 global, 因为下方 909/920 行会读 _GRU_MODEL,
    # 982 行才赋值; Python 3 要求 global 声明先于任何读写
    global _GRU_MODEL
    # P0.2: 动作切换与 rep 起始索引同步 (主循环底部 exercise switch 处再次重置)
    global _gru_rep_start_idx, _gru_prev_rep_in_progress
    logging.info("🚀 启动 IronBuddy V3 双轨融合状态机中枢...")
    logging.info("═════════════════════════════════════════════════════════")
    logging.info(f"🎯 FSM {_V_BANNER}  深蹲阈值={SquatStateMachine.ANGLE_STANDARD}°  弯举阈值={DumbbellCurlFSM.ANGLE_STANDARD}°")
    logging.info(f"   rep 结账门槛: 离底≥25° + 连续 rising≥2 帧 + dur≥0.4s (V7.19 放宽)")
    logging.info("═════════════════════════════════════════════════════════")

    for f in ["/dev/shm/llm_reply.txt", "/dev/shm/chat_input.txt", "/dev/shm/chat_reply.txt"]:
        try:
            os.remove(f)
        except OSError:
            pass

    # ===== M10 (V7.16, 2026-04-20): 启动初始化 - 清理所有残留信号文件 =====
    # 背景: 残留 /dev/shm/user_profile.json 导致 "语音切 curl 后 50ms 被拉回 squat" bug.
    # 该文件每帧被 main_claw 读取, fallback=squat, 无 mtime 去重 -> 循环覆盖.
    # 配合清理 inference_mode.json 让重启默认走纯视觉 (与用户验收要求一致).
    _M10_CLEANUP = [
        "/dev/shm/user_profile.json",       # UI exercise 选择 (主要污染源)
        "/dev/shm/exercise_mode.json",      # 语音 exercise 指令
        "/dev/shm/inference_mode.json",     # 视觉模式 (清后 -> 默认 pure_vision)
        "/dev/shm/fatigue_limit.json",      # 疲劳上限
        "/dev/shm/ui_fatigue_limit.json",   # UI 疲劳上限镜像
        "/dev/shm/next_set.request",        # 下一组请求
        "/dev/shm/fatigue_reset.request",   # 清零请求
        "/dev/shm/mvc_calibrate.request",   # MVC 请求
        "/dev/shm/mvc_calibrate.result",    # MVC 结果
        "/dev/shm/trigger_deepseek",        # 手动 DeepSeek 触发
        "/dev/shm/fsm_reset_signal",        # FSM 重置信号
        "/dev/shm/voice_interrupt",         # 语音打断
        "/dev/shm/chat_active",             # 对话激活标志
        "/dev/shm/violation_alert.txt",     # 残留违规警报
    ]
    _cleaned = 0
    for f in _M10_CLEANUP:
        try:
            if os.path.exists(f):
                os.remove(f)
                _cleaned += 1
        except OSError:
            pass
    logging.info(f"🧹 [M10] 启动清理完成: 移除 {_cleaned} 个残留 shm 信号 (默认 pure_vision + squat)")

    # LLM 后端切换: LLM_BACKEND=direct 使用 DeepSeek 直连, 否则走 OpenClaw Gateway
    llm_backend = os.environ.get("LLM_BACKEND", "direct").lower()
    bridge = None
    connected = False
    if llm_backend == "direct":
        logging.info("LLM 后端: DeepSeek Direct (绕过 Gateway)")
        try:
            bridge = DeepSeekDirect(soul_text=_SOUL_TEXT[:500] if _SOUL_TEXT else "")
            connected = await bridge.connect()
        except Exception as _e:
            logging.warning("DeepSeek Direct 初始化失败: %s", _e)
            connected = False
        if not connected:
            # 直连失败，尝试回退到 OpenClaw
            logging.warning("DeepSeek Direct 不可用，尝试 OpenClaw Gateway")
            try:
                gateway_url = os.environ.get("OPENCLAW_URL", "ws://127.0.0.1:18789")
                bridge = OpenClawBridge(gateway_url=gateway_url)
                connected = await bridge.connect()
            except Exception as _e:
                logging.warning("OpenClaw Gateway 也不可用: %s", _e)
                connected = False
    else:
        logging.info("LLM 后端: OpenClaw Gateway")
        try:
            gateway_url = os.environ.get("OPENCLAW_URL", "ws://127.0.0.1:18789")
            bridge = OpenClawBridge(gateway_url=gateway_url)
            connected = await bridge.connect()
        except Exception as _e:
            logging.warning("OpenClaw Gateway 连接失败: %s", _e)
            connected = False

    if not connected:
        logging.warning("⚠️ 所有 LLM 后端均不可用，FSM 将以纯视觉模式运行（无 AI 对话）")
        bridge = None
    
    current_exercise = "squat"
    _last_applied_modes = {"inference": "pure_vision", "exercise": "squat"}  # V6.1
    # V7.11 \u8de8\u7ec4\u603b\u8ba1: \u4e0b\u4e00\u7ec4\u91cd\u7f6e fsm \u65f6 \u4f1a\u5148\u628a\u672c\u7ec4\u6570\u636e merge \u5230\u8fd9\u91cc
    _session_totals = {"good": 0, "failed": 0, "comp": 0}
    fsm = SquatStateMachine()
    # Sprint5: 开启首个训练 session
    try:
        _d = _db()
        if _d is not None: _DB_SESSION[0] = _d.start_session(current_exercise)
    except Exception as _e: logging.warning("[DB] session init skipped: %s", _e)
    _last_deepseek_time = time.time()
    _ds_lock = [False]
    _this_set_triggered = [False]  # V7.11: \u6bcf\u7ec4\u53ea\u89e6\u53d1\u4e00\u6b21 API \u603b\u7ed3, "\u4e0b\u4e00\u7ec4" \u624d\u91cd\u7f6e
    _this_set_triggered_notice = [False]
    _fatigue_limit = [DEFAULT_RUNTIME_FATIGUE_LIMIT]  # 可通过语音/UI/训练计划调整

    async def _ds_wrapper(b, p, g, f, trigger_reason="fatigue"):
        reply_text = ""
        try:
            reply_text = await _deepseek_fire_and_forget(b, p, g, f) or ""
        except Exception as exc:
            logging.error("[_ds_wrapper] 失败: %s", exc)
        finally:
            # Sprint5: LLM 调用完成后落库（带真实回复）
            try:
                _d = _db()
                if _d is not None:
                    _d.log_llm(trigger_reason, p, reply_text or "(empty)", 0, 0)
            except Exception as _e:
                logging.warning("[DB] log_llm 失败: %s", _e)
            _ds_lock[0] = False
            # V7.22: 解除禁麦门禁
            try:
                if os.path.exists("/dev/shm/llm_inflight"):
                    os.remove("/dev/shm/llm_inflight")
            except Exception:
                pass
            logging.info("[_ds_wrapper] 已释放 _ds_lock")

    _chat_lock = [False]
    _chat_mtime = [0]

    async def _chat_handler(bridge_ref, user_text):
        try:
            user_text = user_text.strip()
            if not user_text or len(user_text) < 2: return
            # V7.5: voice_daemon 的 B 路闲聊已经自己调 DeepSeek, 尾部标 [voice-handled], FSM 跳过防双调
            if "[voice-handled]" in user_text:
                logging.info(f"[voice-handled] 跳过 (voice_daemon 已处理)")
                return
            # V7.2: 静音态下不响应 chat_input
            try:
                if os.path.exists("/dev/shm/mute_signal.json"):
                    with open("/dev/shm/mute_signal.json", "r") as _mf:
                        if bool(json.load(_mf).get("muted", False)):
                            logging.info(f"[静音] 忽略 chat_input: {user_text[:30]}")
                            return
            except Exception:
                pass
            logging.info(f"🎤 [对话] 收到用户消息: {user_text}")
            
            # V4.5: 疲劳上限实时读取 _fatigue_limit（用户可通过语音/UI 改为 1900 等任意值）
            _fl_now = _fatigue_limit[0]
            _fl_pct = round(fsm.total_fatigue_volume / _fl_now * 100) if _fl_now > 0 else 0
            # V7.8: 板端时区 UTC, 手动 +8h 转北京时间
            _cn_ts = time.time() + 8 * 3600
            _now_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime(_cn_ts))
            _weekday_cn = ["一", "二", "三", "四", "五", "六", "日"][time.gmtime(_cn_ts).tm_wday]
            prompt = (
                f"现在是 {_now_str} 星期{_weekday_cn}。"
                f"{_SOUL_TEXT[:500] + chr(10) + chr(10) if _SOUL_TEXT else ''}"
                f"当前数据: 标准深蹲 {fsm.good_squats} 次, 违规 {fsm.failed_squats} 次, "
                f"疲劳 {fsm.total_fatigue_volume:.0f}/{_fl_now}（{_fl_pct}%）。"
                f"汇报疲劳时**必须使用当前真实上限 {_fl_now}**，不要说 1500 等旧数字。"
                f"结合数据客观回答, 用正式专业教练语气, 40字内。不说'你小子'、'老铁'、'行啊'等口语俚语。"
                f"不要 <think> 标签。"
                f"用户说: {user_text}"
            )
            reply = await bridge_ref.ask(prompt, timeout=60)
            if "</think>" in reply:
                reply = reply.split("</think>")[-1].strip()
            logging.info(f"💬 [对话] DeepSeek 回复: {reply}")
            with open("/dev/shm/chat_reply.txt.tmp", "w", encoding="utf-8") as rf:
                rf.write(reply)
            os.rename("/dev/shm/chat_reply.txt.tmp", "/dev/shm/chat_reply.txt")
            # V5.0: 写 seq 递增,voice_daemon 的 _chat_reply_watcher 靠 seq 捕获同秒多写
            try:
                _seq_path = "/dev/shm/chat_reply.txt.seq"
                _prev = 0
                if os.path.exists(_seq_path):
                    with open(_seq_path, "r") as _sf:
                        _prev = int((_sf.read() or "0").strip() or "0")
                with open(_seq_path + ".tmp", "w") as _sf:
                    _sf.write(str(_prev + 1))
                os.rename(_seq_path + ".tmp", _seq_path)
            except Exception:
                pass
            # V4.7：语音对话也要落库（供后端 OpenClaw 偏好学习使用）
            try:
                _d = _db()
                if _d is not None:
                    _d.log_llm("voice_chat", prompt, reply or "(empty)", 0, 0)
            except Exception as _e:
                logging.warning("[DB] log_llm voice_chat 失败: %s", _e)
        except Exception as e:
            pass
        finally:
            _chat_lock[0] = False

    # V7.13: 轮询 pose_data.json 的间隔由环境变量 FSM_POLL_INTERVAL 控制
    # 默认 0.03s (33Hz) 匹配上游 25fps 视觉帧率, 比旧 0.05s 快 40%
    _fsm_poll_interval = float(os.environ.get("FSM_POLL_INTERVAL", "0.03"))
    _last_pose_key = [None]
    _last_pose_skip_log = [0.0]
    try:
        while True:
            await asyncio.sleep(_fsm_poll_interval)

            try:
                _pose_path = "/dev/shm/pose_data.json"
                _pose_mtime = os.path.getmtime(_pose_path)
                with open("/dev/shm/pose_data.json", "r") as f:
                    pose_data = json.load(f)
                if not _pose_frame_is_fresh(pose_data, _pose_mtime):
                    now_skip = time.time()
                    if now_skip - _last_pose_skip_log[0] > 2.0:
                        logging.info("跳过过期视觉帧 age=%.3fs frame_idx=%s",
                                     now_skip - float(pose_data.get("timestamp") or _pose_mtime),
                                     pose_data.get("frame_idx"))
                        _last_pose_skip_log[0] = now_skip
                    raise _PoseFrameSkipped()
                _pose_key = _pose_frame_key(pose_data, _pose_mtime)
                if _pose_key == _last_pose_key[0]:
                    raise _PoseFrameSkipped()
                _last_pose_key[0] = _pose_key
                    
                angle = fsm.update(pose_data)
                
                # ===== Agent 3 探针：7D Late-Fusion 特征拦截 + GRU 推理 =====
                if angle is not None:
                    target_emg, comp_emg = 0.0, 0.0
                    try:
                        with open("/dev/shm/muscle_activation.json", "r") as mf:
                            m_data = json.load(mf)
                            acts = m_data.get("activations", {})
                            # exercise 感知 key 路由 (对齐 udp_emg_server.py:313-326)
                            # 弯举: udp_emg 把 target_pct 写到 biceps, comp_pct 写到 glutes
                            # 深蹲: udp_emg 把 target_pct 写到 glutes,  comp_pct 写到 biceps
                            if current_exercise == "bicep_curl":
                                target_emg = acts.get("biceps", 0.0)
                                comp_emg   = acts.get("glutes", 0.0)
                                scaled = _scale_lane_b_raw_rms_for_gru(m_data)
                                if scaled is not None:
                                    target_emg, comp_emg, _pre_meta = scaled
                            else:
                                target_emg = acts.get("glutes", 0.0)
                                comp_emg   = acts.get("biceps", 0.0)
                    except:
                        pass

                    ang_vel = 0.0
                    if len(fsm._angle_history) >= 2:
                        ang_vel = fsm._angle_history[-1] - fsm._angle_history[-2]

                    # --- CSV 录制 (兼容旧格式 4D + 新格式 7D) ---
                    if os.path.exists("/dev/shm/record_mode"):
                        try:
                            with open("/dev/shm/record_mode", "r") as rf:
                                mode = rf.read().strip()
                            if mode in ["golden", "lazy", "bad"]:
                                csv_file = f"train_squat_{mode}.csv"
                                exists = os.path.exists(csv_file)
                                with open(csv_file, "a") as csvf:
                                    if not exists:
                                        csvf.write("Timestamp,Ang_Vel,Angle,Target_RMS,Comp_RMS\n")
                                    csvf.write(f"{time.time():.3f},{ang_vel:.2f},{angle:.2f},{target_emg:.2f},{comp_emg:.2f}\n")
                        except:
                            pass

                    # --- GRU 推理 ---
                    global _gru_feature_buf, _gru_frame_ctr, _gru_prev_ang_vel
                    global _gru_rep_start_idx, _gru_prev_rep_in_progress
                    ang_accel = ang_vel - _gru_prev_ang_vel
                    _gru_prev_ang_vel = ang_vel

                    # Phase progress: rough estimate from angle history
                    _ah = fsm._angle_history
                    if len(_ah) >= 2:
                        a_min = min(_ah)
                        a_max = max(_ah)
                        phase_prog = float(np.clip(
                            1.0 - (angle - a_min) / max(a_max - a_min, 1.0),
                            0.0, 1.0
                        ))
                    else:
                        phase_prog = 0.0

                    _feature_vec = [
                        ang_vel, angle, ang_accel,
                        target_emg, comp_emg,
                        1.0,         # Symmetry_Score placeholder
                        phase_prog,
                    ]
                    _gru_feature_buf.append(_feature_vec)
                    if len(_gru_feature_buf) > 200:  # keep ~10s buffer
                        _gru_feature_buf.pop(0)
                        # 整段 list 左移一格, 修正本 rep 起始索引
                        _gru_rep_start_idx = max(0, _gru_rep_start_idx - 1)

                    # P0.2: rep_in_progress 上升沿 -> 记录本 rep 在 buf 中的起始索引
                    _cur_rep_in_progress = bool(getattr(fsm, "_rep_in_progress", False))
                    if _cur_rep_in_progress and not _gru_prev_rep_in_progress:
                        # 本帧 (刚追加过) 就是 rep 内第一帧
                        _gru_rep_start_idx = max(0, len(_gru_feature_buf) - 1)
                    _gru_prev_rep_in_progress = _cur_rep_in_progress

                    # Read inference mode (pure_vision = skip GRU, vision_sensor = run GRU)
                    _inference_mode = "pure_vision"
                    try:
                        _im_path = "/dev/shm/inference_mode.json"
                        if os.path.exists(_im_path):
                            with open(_im_path, "r") as _imf:
                                _inference_mode = json.load(_imf).get("mode", "pure_vision")
                    except Exception:
                        pass
                    _append_gru_7d_sample(_feature_vec, current_exercise, _inference_mode, fsm)

                    nn_result = None
                    _rep_event = getattr(fsm, "_last_rep_event", None)
                    _cur_reps = getattr(fsm, '_total_reps_count', 0)
                    _event_rep = int(_rep_event.get("rep_index", _cur_reps)) if _rep_event else _cur_reps
                    if not hasattr(fsm, '_prev_total_reps'):
                        fsm._prev_total_reps = 0

                    # 统一 rep 事件是 DB 和 GRU 的唯一触发源：每个 rep 只处理一次。
                    if _rep_event and _event_rep > fsm._prev_total_reps:
                        visual_result = _rep_event.get("visual_result", "non_standard")
                        final_class = visual_result if visual_result in ("standard", "non_standard") else "non_standard"
                        model_class = None
                        model_confidence = None
                        model_similarity = None
                        classification_source = "visual"
                        emg_ok = _emg_signal_ok()
                        raw_window_for_log = _gru_feature_buf[-min(len(_gru_feature_buf), _GRU_WINDOW_SIZE):]
                        normalized_window_for_log = _normalize_gru_7d_window(raw_window_for_log)

                        if _inference_mode == "vision_sensor":
                            # P0.2: 用本 rep 切片构造推理窗口, 不再用末尾 30 帧跨 rep
                            window_for_infer, _pad_count = _build_rep_window(_gru_rep_start_idx)
                            if not emg_ok:
                                classification_source = "visual_fallback_no_emg"
                            elif _GRU_MODEL is None:
                                classification_source = "visual_fallback_no_model"
                            elif window_for_infer is None or _pad_count > (_GRU_WINDOW_SIZE // 2):
                                classification_source = "visual_fallback_no_window"
                                logging.info("[GRU] fallback reason=no_window pad=%d rep_start_idx=%d buf_len=%d",
                                             _pad_count, _gru_rep_start_idx, len(_gru_feature_buf))
                            else:
                                try:
                                    raw_window_for_log = np.array(window_for_infer, dtype=np.float32, copy=True)
                                    window = np.array(window_for_infer, dtype=np.float32, copy=True)
                                    # V7.30 修补 M1：Ang_Vel 列推理归一化对齐训练 (train_gru_three_class.py:138)
                                    window[:, 0] = np.clip(window[:, 0] / 30.0, -3.0, 3.0)
                                    window[:, 1] /= 180.0
                                    window[:, 3] /= 100.0
                                    window[:, 4] /= 100.0
                                    window[:, 2]  = np.clip(window[:, 2] / 10.0, -1.0, 1.0)
                                    normalized_window_for_log = np.array(window, dtype=np.float32, copy=True)
                                    # P0.5 预筛: 双 EMG 通道几乎为零 -> 跳过 GRU
                                    _mean_t = float(np.mean(window[:, 3]))
                                    _mean_c = float(np.mean(window[:, 4]))
                                    if _mean_t < _EMG_ZERO_THRESHOLD and _mean_c < _EMG_ZERO_THRESHOLD:
                                        classification_source = "visual_fallback_no_emg"
                                        logging.info("[GRU] fallback reason=emg_zero mean_T=%.3f mean_C=%.3f",
                                                     _mean_t, _mean_c)
                                    else:
                                        nn_result = _GRU_MODEL.infer(window)
                                        # P0.4: _low_effort 收紧 + env 关闭开关, 仅 standard 时强转 non_standard
                                        _low_effort_disabled = os.environ.get(
                                            "IRONBUDDY_DISABLE_LOW_EFFORT_GUARD", "0") == "1"
                                        _low_effort = (not _low_effort_disabled and
                                                       _mean_t < _EMG_ZERO_THRESHOLD and
                                                       _mean_c < _EMG_ZERO_THRESHOLD)
                                        if _low_effort and nn_result.get("classification") == "standard":
                                            nn_result["classification"] = "non_standard"
                                            nn_result["similarity"] = min(nn_result.get("similarity", 1.0), 0.3)
                                            nn_result["confidence"] = max(nn_result.get("confidence", 0.0), 0.95)
                                        # P0.5 后筛: GRU 置信度过低 -> collapsed/uncertain, fallback
                                        _max_prob = float(nn_result.get("confidence", 0.0) or 0.0)
                                        if _max_prob < _GRU_MIN_CONFIDENCE:
                                            classification_source = "visual_fallback_low_conf"
                                            logging.info("[GRU] fallback reason=low_conf max_prob=%.3f "
                                                         "mean_T=%.3f mean_C=%.3f",
                                                         _max_prob, _mean_t, _mean_c)
                                            nn_result = None
                                        else:
                                            model_class = nn_result.get("classification", "unknown")
                                            model_similarity = nn_result.get("similarity")
                                            model_confidence = nn_result.get("confidence")
                                            final_class = model_class if model_class in (
                                                "standard", "compensating", "non_standard"
                                            ) else final_class
                                            classification_source = "gru"
                                            cls_cn = {"standard":"标准","compensating":"代偿","non_standard":"错误"}
                                            logging.info(f"🧠 [GRU] 第{_event_rep}个动作判定: "
                                                         f"相似度={float(model_similarity or 0.0):.3f} "
                                                         f"分类={cls_cn.get(final_class, final_class)} "
                                                         f"置信度={float(model_confidence or 0.0):.3f}")
                                except Exception as _e:
                                    classification_source = "visual_fallback_model_error"
                                    logging.debug(f"[GRU] infer error: {_e}")

                            _apply_rep_classification(fsm, final_class, _event_rep)
                            fsm._pending_gru_angle_result = None
                            fsm._last_rep_result = final_class
                            _write_gru_7d_window(
                                _event_rep,
                                current_exercise,
                                _inference_mode,
                                classification_source,
                                raw_window_for_log,
                                normalized_window_for_log,
                                nn_result=nn_result,
                                emg_ok=emg_ok,
                                visual_result=visual_result,
                                final_class=final_class,
                            )

                        if isinstance(_rep_event, dict):
                            _rep_event["final_result"] = final_class
                            _rep_event["classification_source"] = classification_source

                        try:
                            _d = _db()
                            if _d is not None:
                                _d.log_rep(
                                    _DB_SESSION[0],
                                    final_class == "standard",
                                    _rep_event.get("min_angle", 0.0),
                                    target_emg,
                                    comp_emg,
                                    exercise=_rep_event.get("exercise", current_exercise),
                                    rep_index=_event_rep,
                                    visual_result=visual_result,
                                    model_class=model_class,
                                    model_confidence=model_confidence,
                                    model_similarity=model_similarity,
                                    classification_source=classification_source,
                                    angle_metric=_rep_event.get("angle_metric", ""),
                                    rom=_rep_event.get("rom", 0.0),
                                    emg_ok=emg_ok,
                                )
                                _session_failed = (
                                    _session_totals["failed"] +
                                    getattr(fsm, 'failed_squats', 0) +
                                    _session_totals["comp"] +
                                    getattr(fsm, '_compensation_count', 0)
                                )
                                _d.update_session_counts(
                                    _DB_SESSION[0],
                                    _session_totals["good"] + getattr(fsm, 'good_squats', 0),
                                    _session_failed,
                                    getattr(fsm, 'total_fatigue_volume', 0.0),
                                )
                        except Exception as _e:
                            logging.warning("[DB] log_rep: %s", _e)
                        fsm._prev_total_reps = _event_rep

                        # P0.1: rep 完成 + DB 落盘后清空类级别缓存, 防止 sync_to_frontend 重播
                        SquatStateMachine._last_nn_result = None
                        DumbbellCurlFSM._last_nn_result = None
                        # P0.2: rep 处理完毕, 截断 buf 仅保留最近 60 帧, 重置下一 rep 起始索引
                        if len(_gru_feature_buf) > 60:
                            del _gru_feature_buf[:-60]
                        _gru_rep_start_idx = len(_gru_feature_buf)

                    fsm.sync_to_frontend(angle, nn_result=nn_result)
                # =================================================

            except _PoseFrameSkipped:
                pass
            except (FileNotFoundError, json.JSONDecodeError):
                pass
                
            # 动作类型热切换 (前端 user_profile + 语音 exercise_mode)
            try:
                target_exercise = None
                # 语音指令信号文件 (优先)
                if os.path.exists("/dev/shm/exercise_mode.json"):
                    em_data = _read_fresh_json("/dev/shm/exercise_mode.json", default_ttl=30.0)
                    if em_data:
                        mode = em_data.get("mode", "")
                        if mode == "squat":
                            target_exercise = "squat"
                        elif mode == "curl":
                            target_exercise = "bicep_curl"
                    os.remove("/dev/shm/exercise_mode.json")
                # 前端 user_profile
                if target_exercise is None and os.path.exists("/dev/shm/user_profile.json"):
                    p_data = _read_fresh_json("/dev/shm/user_profile.json", default_ttl=8.0)
                    if p_data and p_data.get("src") == "sensor_lab":
                        logging.info("忽略 Sensor Lab user_profile 对主训练动作的持续覆盖")
                        p_data = None
                    if p_data:
                        target_exercise = p_data.get("exercise", "squat")
                if target_exercise and target_exercise != current_exercise:
                    logging.info(f"🔄 动作模式切换: {current_exercise} -> {target_exercise}")
                    current_exercise = target_exercise
                    if current_exercise == "bicep_curl":
                        fsm = DumbbellCurlFSM()
                    else:
                        fsm = SquatStateMachine()
                    # 重载对应 exercise 的 GRU 权重 + 清滑窗, 防首个 rep 串入上一 exercise 的特征
                    # (global 声明已移至 main() 顶部)
                    _GRU_MODEL = _load_gru_model(current_exercise)
                    _gru_feature_buf.clear()
                    # P0.2: 切换动作必同步重置 rep 起始索引与边沿标记
                    _gru_rep_start_idx = 0
                    _gru_prev_rep_in_progress = False
                    # P0.1: 同步清空两类 FSM 的 NN 缓存, 避免新动作首帧重播旧分类
                    SquatStateMachine._last_nn_result = None
                    DumbbellCurlFSM._last_nn_result = None
                    _gru_feature_rows.clear()
                    fsm.sync_to_frontend()
            except Exception:
                pass

            # 语音疲劳上限调整
            try:
                if os.path.exists("/dev/shm/fatigue_limit.json"):
                    with open("/dev/shm/fatigue_limit.json", "r", encoding="utf-8") as fl:
                        fl_data = json.load(fl)
                        new_limit = fl_data.get("limit", DEFAULT_RUNTIME_FATIGUE_LIMIT)
                        logging.info(f"🎯 收到语音指令：疲劳上限改为 {new_limit}")
                        # 更新全局疲劳阈值 (在 DeepSeek trigger 判定中使用)
                        _fatigue_limit[0] = new_limit
                    os.remove("/dev/shm/fatigue_limit.json")
            except Exception:
                pass

            # V6.1: \u628a\u6a21\u5f0f+\u9608\u503c\u5199\u5165 fsm_state.json \u8865\u5145\u5b57\u6bb5 (\u4f9b voice_daemon/UI \u786e\u8ba4)
            try:
                # V7.11 \u8de8\u7ec4\u603b\u8ba1 (\u5f53\u524d\u603b\u8ba1 + \u672c\u7ec4\u5b9e\u65f6): UI \u5e95\u90e8\u72b6\u6001\u680f\u5c55\u793a
                _cur_comp = getattr(fsm, "_compensation_count", 0)
                _ext = {
                    "fatigue_limit": int(_fatigue_limit[0]),
                    "inference_mode": (os.path.exists("/dev/shm/inference_mode.json")
                                       and json.load(open("/dev/shm/inference_mode.json")).get("mode")
                                       or _last_applied_modes.get("inference", "pure_vision")),
                    "exercise": current_exercise,
                    "total_good": _session_totals["good"] + fsm.good_squats,
                    "total_failed": _session_totals["failed"] + fsm.failed_squats,
                    "total_comp": _session_totals["comp"] + _cur_comp,
                }
                _last_applied_modes["inference"] = _ext["inference_mode"]
                if os.path.exists("/dev/shm/fsm_state.json"):
                    try:
                        with open("/dev/shm/fsm_state.json", "r", encoding="utf-8") as _fs:
                            _cur = json.load(_fs)
                        _cur.update(_ext)
                        with open("/dev/shm/fsm_state.json.tmp", "w", encoding="utf-8") as _rf:
                            json.dump(_cur, _rf)
                        os.rename("/dev/shm/fsm_state.json.tmp", "/dev/shm/fsm_state.json")
                    except Exception:
                        pass
            except Exception:
                pass

            # V4.8: 疲劳满自动清零 (UI 或语音触发)
            try:
                if os.path.exists("/dev/shm/fatigue_reset.request"):
                    os.remove("/dev/shm/fatigue_reset.request")
                    fsm.total_fatigue_volume = 0.0
                    logging.info("疲劳积分已清零 (触发源: UI/语音 fatigue_reset.request)")
            except Exception:
                pass

            # V7.10 \u201c\u4e0b\u4e00\u7ec4\u201d: \u603b\u7ed3\u540e\u4fdd\u7559\u6570\u636e, \u8bed\u97f3\u8bf4\u201c\u4e0b\u4e00\u7ec4\u201d\u624d\u91cd\u7f6e
            try:
                if os.path.exists("/dev/shm/next_set.request"):
                    _next_req = _read_next_set_request("/dev/shm/next_set.request")
                    os.remove("/dev/shm/next_set.request")
                    _next_set = int(_next_req.get("next_set") or 0)
                    if _next_set < 1:
                        _next_set = int(_read_json_file(TRAINING_SESSION_FILE).get("current_set") or 1)
                    _completed_snapshot = _record_completed_training_set(fsm, _next_req, _next_set)
                    # V7.11: \u672c\u7ec4\u6570\u636e merge \u5230\u8de8\u7ec4\u603b\u8ba1 (\u4f9b\u5e95\u90e8\u72b6\u6001\u680f + OpenClaw \u62c9\u53d6)
                    _session_totals["good"] += fsm.good_squats
                    _session_totals["failed"] += fsm.failed_squats
                    _session_totals["comp"] += getattr(fsm, "_compensation_count", 0)
                    try:
                        _d = _db()
                        if _d is not None:
                            _d.update_session_counts(
                                _DB_SESSION[0],
                                _session_totals["good"],
                                _session_totals["failed"] + _session_totals["comp"],
                                getattr(fsm, 'total_fatigue_volume', 0.0),
                            )
                    except Exception as _db_e:
                        logging.warning("[DB] next_set update_session_counts: %s", _db_e)
                    logging.info(f"\u2728 \u4e0b\u4e00\u7ec4 \u2014 \u672c\u7ec4: \u6807\u51c6{fsm.good_squats} \u8fdd\u89c4{fsm.failed_squats} | set={_completed_snapshot.get('set_index')} \u603b\u8ba1 {_session_totals}")
                    if current_exercise == "bicep_curl":
                        fsm = DumbbellCurlFSM()
                    else:
                        fsm = SquatStateMachine()
                    fsm.sync_to_frontend()
                    _ds_lock[0] = False
                    _this_set_triggered[0] = False
                    _this_set_triggered_notice[0] = False
            except Exception as _e:
                logging.warning(f"[next_set] \u91cd\u7f6e\u5931\u8d25: {_e}")

            # 前端重置信号
            if os.path.exists("/dev/shm/fsm_reset_signal"):
                try: os.remove("/dev/shm/fsm_reset_signal")
                except OSError: pass
                # Sprint5: 结算上个 session + 开启新 session
                try:
                    _d = _db()
                    if _d is not None:
                        _reset_good = _session_totals["good"] + fsm.good_squats
                        _reset_failed = (
                            _session_totals["failed"] + fsm.failed_squats +
                            _session_totals["comp"] + getattr(fsm, "_compensation_count", 0)
                        )
                        _d.end_session(_DB_SESSION[0], _reset_good, _reset_failed, fsm.total_fatigue_volume)
                        _DB_SESSION[0] = _d.start_session(current_exercise)
                        _session_totals["good"] = 0
                        _session_totals["failed"] = 0
                        _session_totals["comp"] = 0
                except Exception as _e: logging.warning("[DB] reset cycle: %s", _e)
                # 最残暴的重置：直接将整个 FSM 脑叶切除再造，一劳永逸
                if current_exercise == "bicep_curl":
                    fsm = DumbbellCurlFSM()
                else:
                    fsm = SquatStateMachine()
                fsm.sync_to_frontend()
                _ds_lock[0] = False
                _this_set_triggered[0] = False
                _this_set_triggered_notice[0] = False
                
                try: os.remove("/dev/shm/llm_reply.txt")
                except OSError: pass
                logging.info("🔄 收到前端重置信号，全轨数据已强制初始化零！")
                continue

            # DeepSeek trigger: manual OR fatigue auto-trigger at limit
            manual_trigger = os.path.exists("/dev/shm/trigger_deepseek")
            # V7.11: \u6bcf\u7ec4\u53ea\u89e6\u53d1\u4e00\u6b21 (_this_set_triggered \u7531"\u4e0b\u4e00\u7ec4"\u91cd\u7f6e)
            fatigue_trigger = (fsm.total_fatigue_volume >= _fatigue_limit[0]
                               and not _ds_lock[0]
                               and not _this_set_triggered[0])

            # Cooldown: prevent rapid-fire auto-triggers after reset
            if fatigue_trigger and (time.time() - _last_deepseek_time) < 30:
                fatigue_trigger = False

            if (fsm.total_fatigue_volume >= _fatigue_limit[0]
                    and _this_set_triggered[0]
                    and not _this_set_triggered_notice[0]):
                _append_chat_event("coach", "本组已总结，下一组后再次触发", kind="auto_summary_skipped")
                _this_set_triggered_notice[0] = True

            if manual_trigger:
                try: os.remove("/dev/shm/trigger_deepseek")
                except OSError: pass

            has_data = fsm.good_squats > 0 or fsm.failed_squats > 0
            is_chatting = os.path.exists("/dev/shm/chat_active")
            # V7.2: 静音态下不触发 FSM 自动 DeepSeek (防止 UI 出现意外推送)
            is_muted = False
            try:
                if os.path.exists("/dev/shm/mute_signal.json"):
                    with open("/dev/shm/mute_signal.json", "r") as _mf:
                        is_muted = bool(json.load(_mf).get("muted", False))
            except Exception:
                pass

            should_trigger = (manual_trigger or fatigue_trigger) and has_data and not _ds_lock[0] and not is_chatting and not is_muted

            if should_trigger:
                _append_chat_event("coach", "达到疲劳上限，正在生成本组总结", kind="auto_summary_pending")
                # V7.30 Phase 3: emit /dev/shm/auto_trigger.json so voice_daemon
                # state machine can transition LISTEN→BUSY before TTS starts
                # (otherwise mic might catch the playback and re-route as input).
                try:
                    _auto_trigger_payload = {
                        "reason": "fatigue_max" if fatigue_trigger else "manual",
                        "good": fsm.good_squats,
                        "failed": fsm.failed_squats,
                        "comp": getattr(fsm, '_compensation_count', 0),
                        "fatigue": round(fsm.total_fatigue_volume, 1),
                        "ts": time.time(),
                    }
                    _auto_tmp = "/dev/shm/auto_trigger.json.tmp"
                    with open(_auto_tmp, "w", encoding="utf-8") as _af:
                        json.dump(_auto_trigger_payload, _af)
                    os.rename(_auto_tmp, "/dev/shm/auto_trigger.json")
                except OSError as _ate:
                    logging.warning("auto_trigger.json write failed: %s", _ate)
                # M12 (V7.20, 2026-04-20): 防止 LLM 不通时 _ds_lock 永久死锁
                # 上一个 bug: connected=False 时仍设 _ds_lock=True + _this_set_triggered=True,
                # 但 _ds_wrapper (唯一解锁者) 不被调用, 整个 session 再也无法触发任何总结.
                # 修复: 前置判定 LLM 可用性, 不可用走本地兜底文案直写 llm_reply.txt, 不上锁.
                good_count = fsm.good_squats
                failed_count = fsm.failed_squats
                reason = "疲劳满值自动" if fatigue_trigger else "手动按键"
                comp_count = getattr(fsm, '_compensation_count', 0)
                summary_text = _build_fixed_auto_summary(
                    current_exercise,
                    _last_applied_modes.get("inference", "pure_vision"),
                    good_count,
                    failed_count,
                    comp_count,
                    fsm.total_fatigue_volume,
                    _fatigue_limit[0],
                )
                _ds_lock[0] = True
                if fatigue_trigger:
                    _this_set_triggered[0] = True
                _last_deepseek_time = time.time()
                logging.info(f"⏳ 固定模板结组 ({reason}) - {summary_text}")
                try:
                    with open("/dev/shm/llm_reply.txt.tmp", "w", encoding="utf-8") as rf:
                        rf.write(summary_text)
                    os.rename("/dev/shm/llm_reply.txt.tmp", "/dev/shm/llm_reply.txt")
                    _seq_path = "/dev/shm/llm_reply.txt.seq"
                    _prev = 0
                    try:
                        if os.path.exists(_seq_path):
                            with open(_seq_path, "r") as _sf:
                                _prev = int((_sf.read() or "0").strip() or "0")
                    except Exception:
                        _prev = 0
                    with open(_seq_path + ".tmp", "w") as _sf:
                        _sf.write(str(_prev + 1))
                    os.rename(_seq_path + ".tmp", _seq_path)
                except Exception as _fbe:
                    logging.error(f"[fixed_summary] llm_reply 写入失败: {_fbe}")
                if fatigue_trigger:
                    try:
                        _request_auto_next_training_set(current_exercise, _fatigue_limit[0])
                    except Exception as _auto_next_e:
                        logging.warning("[fatigue_auto_next_set] skipped: %s", _auto_next_e)

            # ===== 语音对话轮询 =====
            if connected and bridge is not None and not _chat_lock[0] and os.path.exists("/dev/shm/chat_input.txt"):
                try:
                    mtime = os.path.getmtime("/dev/shm/chat_input.txt")
                    if mtime != _chat_mtime[0]:
                        _chat_mtime[0] = mtime
                        with open("/dev/shm/chat_input.txt", "r", encoding="utf-8") as cf:
                            user_text = cf.read().strip()
                        if user_text:
                            _chat_lock[0] = True
                            asyncio.create_task(_chat_handler(bridge, user_text))
                except Exception as e:
                    pass

    except KeyboardInterrupt:
        logging.info("手动中断")
    finally:
        # Sprint5: 主循环退出时结算当前 session
        try:
            _d = _db()
            if _d is not None and _DB_SESSION[0] is not None:
                _d.end_session(
                    _DB_SESSION[0],
                    _session_totals["good"] + getattr(fsm, 'good_squats', 0),
                    (_session_totals["failed"] + getattr(fsm, 'failed_squats', 0) +
                     _session_totals["comp"] + getattr(fsm, "_compensation_count", 0)),
                    getattr(fsm, 'total_fatigue_volume', 0.0),
                )
                logging.info("[DB] 主循环退出，session=%s 已结算", _DB_SESSION[0])
        except Exception as _e:
            logging.warning("[DB] 退出 end_session 失败: %s", _e)
        logging.info("🧹 退出 Main Loop。")

if __name__ == "__main__":
    asyncio.run(main())
