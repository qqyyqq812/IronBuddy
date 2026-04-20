#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EMG 模拟器共享 capture 模块 (V7.16)

职责:
  1. 订阅 FSM 的 rep 计数 (读 /dev/shm/fsm_state.json 的 good+failed+comp)
  2. 检测前端下发的 /dev/shm/test_capture.session / .stop 信号
  3. 缓冲每个 sim tick 的原始数据 + 按 rep 切分为子段
  4. 点击"停止并保存"后 flush 到 data/test_capture/{exercise}/{label}/{ts}_{sid}/

被 simulate_emg_from_mia.py / simulate_emg_from_bicep.py 共享. 不对外导出类,
只暴露函数式 API 以便在模拟器的 100Hz 轮询循环中一行调用.

Python 3.7 兼容: 无 `X | None` / `:=` / `match`, 类型注解走 typing.Optional.

---------- 信号文件协议 ----------

/dev/shm/test_capture.session   (前端→模拟器)
  启动请求. JSON:
    {"enabled": true,
     "session_id": 42,
     "exercise": "squat" | "bicep_curl",
     "label": "standard" | "compensating" | "non_standard",
     "out_dir": "/abs/path/data/test_capture/squat/standard/20260420_210000_42",
     "started_ts": 1698765432.1}

/dev/shm/test_capture.stop      (前端→模拟器)
  JSON: {"discard": false}  -- 若 discard=true 则丢弃缓冲不落盘

/dev/shm/test_capture.result    (模拟器→前端)
  flush 完成. JSON:
    {"ok": true,
     "rep_count": 5,
     "raw_rows": 12050,
     "duration_s": 24.1,
     "out_dir": "...",
     "discarded": false}

---------- 调用点 (模拟器内) ----------

1. 在每个 tick 开头:
     capture_poll(state)         # 100Hz 检测启停信号
2. 发完 UDP 后:
     capture_record_frame(
         state, ts, sim_phase, sim_angle, sim_target_pct, sim_comp_pct,
         sim_udp_target, sim_udp_comp,
         simulator_src="tools/simulate_emg_from_mia.py")
3. 无需在关闭时显式 flush, stop 信号到达时自动处理.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

SHM_FSM_STATE = "/dev/shm/fsm_state.json"
SHM_SESSION = "/dev/shm/test_capture.session"
SHM_STOP = "/dev/shm/test_capture.stop"
SHM_RESULT = "/dev/shm/test_capture.result"
SHM_ACK = "/dev/shm/test_capture.session.ack"

POLL_INTERVAL_SEC = 0.01  # 100 Hz
RAW_ROW_CAP = 200_000  # 20 分钟 @ 500Hz 安全余量
CAP_FILE_PREFIX = "raw"  # 触顶时 raw.csv / raw_1.csv / ...


# ==========================================================================
# State 容器 (dict, 模拟器直接在自己的 state dict 里嵌套使用)
# ==========================================================================
def make_state() -> Dict[str, Any]:
    """生成 capture 子状态字典, 嵌入模拟器 state 里."""
    return {
        "enabled": False,
        "session_id": None,
        "exercise": None,
        "label": None,
        "out_dir": None,
        "started_ts": 0.0,
        "simulator_src": None,
        "last_poll": 0.0,
        # rep 订阅
        "last_total_reps": 0,
        "cur_rep_idx": 0,
        "cur_rep_frames": [],   # 当前 rep 累积的 raw rows
        # 缓冲
        "raw_rows": [],         # 全场 raw 行
        "rep_summaries": [],    # 按 rep 汇总
        # 统计辅助
        "fsm_state_snapshot_start": None,
        "chunk_idx": 0,         # 触顶强制 flush 分段编号
    }


# ==========================================================================
# FSM 订阅: 读 fsm_state.json 的 good+failed+comp 总和作为 rep 边界
# ==========================================================================
def read_fsm_snapshot() -> Optional[Dict[str, Any]]:
    """读 /dev/shm/fsm_state.json 返回完整 snapshot. 读失败返回 None."""
    try:
        with open(SHM_FSM_STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, ValueError):
        return None


def get_active_label(cap: Dict[str, Any]) -> Optional[str]:
    """若采集进行中, 返回 UI 下拉选择的 label; 否则 None.
    模拟器用它在运行时动态切换波形类别, 无需 kill 重启."""
    if cap.get("enabled"):
        lb = cap.get("label")
        if lb in ("standard", "compensating", "non_standard"):
            return lb
    return None


def read_fsm_total_reps(snapshot: Optional[Dict[str, Any]] = None) -> int:
    """
    读 FSM 的 rep 总数 = good + failed + comp.
    失败返回 -1 (模拟器 fallback 逻辑可检测).
    """
    snap = snapshot if snapshot is not None else read_fsm_snapshot()
    if not snap:
        return -1
    try:
        g = int(snap.get("good", 0))
        f = int(snap.get("failed", 0))
        c = int(snap.get("comp", 0))
        return g + f + c
    except (TypeError, ValueError):
        return -1


# ==========================================================================
# 信号文件轮询 (模拟器每个 tick 调一次)
# ==========================================================================
def capture_poll(cap: Dict[str, Any]) -> None:
    """
    100 Hz 检测 session / stop 信号. 调用方每 tick 传入 cap (make_state 的结果).
    内部自己节流 (超过 POLL_INTERVAL_SEC 才真正去 os.stat), 模拟器每个 tick 放心调.
    """
    now = time.time()
    if now - cap["last_poll"] < POLL_INTERVAL_SEC:
        return
    cap["last_poll"] = now

    # ---- 先看 stop ----
    if os.path.exists(SHM_STOP):
        try:
            with open(SHM_STOP, "r", encoding="utf-8") as f:
                txt = f.read().strip()
            payload = json.loads(txt) if txt else {}
        except (IOError, ValueError):
            payload = {}
        discard = bool(payload.get("discard", False))
        if cap["enabled"]:
            _finalize_and_flush(cap, discard=discard)
        # 无论状态如何都清掉信号
        _safe_remove(SHM_STOP)
        return

    # ---- 再看 session (仅当未启用时接受新 session) ----
    if os.path.exists(SHM_SESSION):
        try:
            with open(SHM_SESSION, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (IOError, ValueError) as e:
            logging.warning("[CAP] session 读取失败: %s", e)
            _safe_remove(SHM_SESSION)
            return
        if not payload.get("enabled"):
            # 手动禁用
            if cap["enabled"]:
                _finalize_and_flush(cap, discard=False)
            _safe_remove(SHM_SESSION)
            return
        if not cap["enabled"]:
            _start_session(cap, payload)


def _start_session(cap: Dict[str, Any], payload: Dict[str, Any]) -> None:
    sid = payload.get("session_id")
    out_dir = payload.get("out_dir")
    if not (isinstance(sid, int) and isinstance(out_dir, str)):
        logging.warning("[CAP] session 信号字段不全, 忽略: %s", payload)
        return
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        logging.warning("[CAP] mkdir 失败 %s: %s", out_dir, e)
        return
    cap["enabled"] = True
    cap["session_id"] = sid
    cap["exercise"] = payload.get("exercise", "unknown")
    cap["label"] = payload.get("label", "unknown")
    cap["out_dir"] = out_dir
    cap["started_ts"] = float(payload.get("started_ts", time.time()))
    cap["last_total_reps"] = max(0, read_fsm_total_reps())
    cap["cur_rep_idx"] = 0
    cap["cur_rep_frames"] = []
    cap["raw_rows"] = []
    cap["rep_summaries"] = []
    cap["fsm_state_snapshot_start"] = read_fsm_snapshot()
    cap["chunk_idx"] = 0
    # ack
    try:
        with open(SHM_ACK, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except IOError:
        pass
    logging.info("[CAP] 🎬 开始采集 session=%s exercise=%s label=%s → %s",
                 sid, cap["exercise"], cap["label"], out_dir)


# ==========================================================================
# 帧记录 + rep 边界检测
# ==========================================================================
def capture_record_frame(cap: Dict[str, Any],
                         ts: float,
                         sim_phase: float,
                         sim_angle: float,
                         sim_target_pct: float,
                         sim_comp_pct: float,
                         sim_udp_target: float,
                         sim_udp_comp: float,
                         simulator_src: str) -> None:
    """
    记录一帧数据. 顺带检测 rep 边界 (FSM 总数自增时 flush 当前 rep).
    capture 关闭时直接 return, 性能代价近零.
    """
    if not cap["enabled"]:
        return

    if cap["simulator_src"] is None:
        cap["simulator_src"] = simulator_src

    snap = read_fsm_snapshot() or {}
    fsm_state = str(snap.get("state", ""))
    fsm_angle = snap.get("angle")
    fsm_good = int(snap.get("good", 0))
    fsm_failed = int(snap.get("failed", 0))
    fsm_comp = int(snap.get("comp", 0))
    fsm_cls = str(snap.get("classification", ""))
    total_reps = fsm_good + fsm_failed + fsm_comp

    # rep 边界: 总数涨了一格即 flush 当前 rep
    if total_reps > cap["last_total_reps"]:
        _finalize_current_rep(cap, end_ts=ts, end_snap=snap)
        cap["last_total_reps"] = total_reps

    row = {
        "ts": round(ts, 4),
        "sim_phase": round(sim_phase, 4),
        "sim_angle": round(sim_angle, 2),
        "sim_target_pct": round(sim_target_pct, 2),
        "sim_comp_pct": round(sim_comp_pct, 2),
        "sim_udp_target": round(sim_udp_target, 2),
        "sim_udp_comp": round(sim_udp_comp, 2),
        "fsm_state": fsm_state,
        "fsm_angle": round(float(fsm_angle), 2) if isinstance(fsm_angle, (int, float)) else "",
        "fsm_good": fsm_good,
        "fsm_failed": fsm_failed,
        "fsm_comp": fsm_comp,
        "fsm_classification": fsm_cls,
        "rep_idx": cap["cur_rep_idx"] + 1,
    }
    cap["raw_rows"].append(row)
    cap["cur_rep_frames"].append(row)

    # 触顶保护: 超过 RAW_ROW_CAP 强制 flush 分段
    if len(cap["raw_rows"]) >= RAW_ROW_CAP:
        logging.warning("[CAP] raw rows 触顶 %d → 强制 flush 分段", RAW_ROW_CAP)
        _flush_raw_chunk(cap)


def _finalize_current_rep(cap: Dict[str, Any],
                          end_ts: float,
                          end_snap: Optional[Dict[str, Any]]) -> None:
    """当前 rep 结束时, 把 cur_rep_frames 汇总成一条 rep summary."""
    frames = cap["cur_rep_frames"]
    if not frames:
        cap["cur_rep_idx"] += 1
        return
    rep_idx = cap["cur_rep_idx"] + 1
    cap["cur_rep_idx"] = rep_idx

    sim_angles = [r["sim_angle"] for r in frames if isinstance(r["sim_angle"], (int, float))]
    target_pcts = [r["sim_target_pct"] for r in frames]
    comp_pcts = [r["sim_comp_pct"] for r in frames]
    start_ts = frames[0]["ts"]
    end_ts_actual = frames[-1]["ts"]

    def _avg(xs: List[float]) -> float:
        return (sum(xs) / len(xs)) if xs else 0.0

    def _peak(xs: List[float]) -> float:
        return max(xs) if xs else 0.0

    fsm_cls = ""
    if end_snap:
        fsm_cls = str(end_snap.get("classification", ""))
    if not fsm_cls and frames:
        fsm_cls = frames[-1].get("fsm_classification", "")

    summary = {
        "rep_idx": rep_idx,
        "start_ts": round(start_ts, 4),
        "end_ts": round(end_ts_actual, 4),
        "duration_s": round(end_ts_actual - start_ts, 3),
        "min_angle": round(min(sim_angles), 2) if sim_angles else 0.0,
        "max_angle": round(max(sim_angles), 2) if sim_angles else 0.0,
        "target_rms_avg": round(_avg(target_pcts), 2),
        "target_rms_peak": round(_peak(target_pcts), 2),
        "comp_rms_avg": round(_avg(comp_pcts), 2),
        "comp_rms_peak": round(_peak(comp_pcts), 2),
        "fsm_classification": fsm_cls,
        "frames": len(frames),
    }
    cap["rep_summaries"].append(summary)
    cap["cur_rep_frames"] = []
    logging.info("[CAP] rep #%d 汇总 min=%.1f° max=%.1f° target_peak=%.1f%% cls=%s",
                 rep_idx, summary["min_angle"], summary["max_angle"],
                 summary["target_rms_peak"], fsm_cls or "-")


# ==========================================================================
# 落盘
# ==========================================================================
_RAW_CSV_COLS = [
    "ts", "sim_phase", "sim_angle", "sim_target_pct", "sim_comp_pct",
    "sim_udp_target", "sim_udp_comp",
    "fsm_state", "fsm_angle", "fsm_good", "fsm_failed", "fsm_comp",
    "fsm_classification", "rep_idx",
]

_REPS_CSV_COLS = [
    "rep_idx", "start_ts", "end_ts", "duration_s",
    "min_angle", "max_angle",
    "target_rms_avg", "target_rms_peak",
    "comp_rms_avg", "comp_rms_peak",
    "fsm_classification", "frames",
]


def _flush_raw_chunk(cap: Dict[str, Any]) -> None:
    """触顶强制 flush 当前 raw_rows 到分段 CSV, 然后清空."""
    if not cap["raw_rows"] or not cap["out_dir"]:
        return
    cap["chunk_idx"] += 1
    fname = "{}_{}.csv".format(CAP_FILE_PREFIX, cap["chunk_idx"])
    fpath = os.path.join(cap["out_dir"], fname)
    try:
        _write_csv(fpath, _RAW_CSV_COLS, cap["raw_rows"])
        logging.info("[CAP] 已分段 flush %d 行 → %s", len(cap["raw_rows"]), fname)
    except IOError as e:
        logging.warning("[CAP] 分段 flush 失败 %s: %s", fpath, e)
    cap["raw_rows"] = []


def _write_csv(path: str, cols: List[str], rows: List[Dict[str, Any]]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.rename(tmp, path)


def _git_rev() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return out.decode("ascii").strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError, OSError):
        return ""


def _finalize_and_flush(cap: Dict[str, Any], discard: bool = False) -> None:
    """
    停止采集并按 discard 决定是否落盘. 结果写 /dev/shm/test_capture.result.
    无论成败都把 cap 重置为 disabled.
    """
    # 收尾当前未完成的 rep (只有在 frames 非空时才入账)
    if cap["cur_rep_frames"] and not discard:
        last_ts = cap["cur_rep_frames"][-1]["ts"]
        _finalize_current_rep(cap, end_ts=last_ts, end_snap=read_fsm_snapshot())

    result: Dict[str, Any] = {
        "ok": True,
        "discarded": discard,
        "session_id": cap["session_id"],
        "exercise": cap["exercise"],
        "label": cap["label"],
        "out_dir": cap["out_dir"],
    }

    if discard or not cap["out_dir"]:
        result["rep_count"] = 0
        result["raw_rows"] = 0
        result["duration_s"] = 0.0
        logging.info("[CAP] 🗑️  采集已丢弃 (discard=True) session=%s", cap["session_id"])
    else:
        raw_rows = cap["raw_rows"]
        reps = cap["rep_summaries"]
        duration_s = 0.0
        if raw_rows:
            duration_s = raw_rows[-1]["ts"] - raw_rows[0]["ts"]

        # 写 raw.csv (或 raw_final.csv 若已有分段)
        raw_name = "raw.csv" if cap["chunk_idx"] == 0 else "raw_final.csv"
        try:
            _write_csv(os.path.join(cap["out_dir"], raw_name), _RAW_CSV_COLS, raw_rows)
        except IOError as e:
            logging.error("[CAP] 写 %s 失败: %s", raw_name, e)
            result["ok"] = False
            result["error"] = "write_raw_failed: {}".format(e)

        # 写 reps.csv
        try:
            _write_csv(os.path.join(cap["out_dir"], "reps.csv"), _REPS_CSV_COLS, reps)
        except IOError as e:
            logging.error("[CAP] 写 reps.csv 失败: %s", e)
            result["ok"] = False
            result["error"] = "write_reps_failed: {}".format(e)

        # 写 summary.json
        end_snap = read_fsm_snapshot()
        target_avgs = [r["target_rms_avg"] for r in reps]
        comp_avgs = [r["comp_rms_avg"] for r in reps]
        summary_json = {
            "session_id": cap["session_id"],
            "exercise": cap["exercise"],
            "label": cap["label"],
            "started_ts": cap["started_ts"],
            "ended_ts": time.time(),
            "duration_s": round(duration_s, 2),
            "rep_count_fsm": len(reps),
            "raw_frame_count": len(raw_rows) + cap["chunk_idx"] * RAW_ROW_CAP,
            "chunks": cap["chunk_idx"] + (1 if raw_rows else 0),
            "avg_target_rms": round(sum(target_avgs) / len(target_avgs), 2) if target_avgs else 0.0,
            "avg_comp_rms": round(sum(comp_avgs) / len(comp_avgs), 2) if comp_avgs else 0.0,
            "simulator_src_file": cap["simulator_src"],
            "host": socket.gethostname(),
            "git_rev": _git_rev(),
            "fsm_state_snapshot_start": cap["fsm_state_snapshot_start"],
            "fsm_state_snapshot_end": end_snap,
        }
        try:
            _write_csv  # silence linter
            with open(os.path.join(cap["out_dir"], "summary.json"), "w",
                      encoding="utf-8") as f:
                json.dump(summary_json, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logging.error("[CAP] 写 summary.json 失败: %s", e)
            result["ok"] = False
            result["error"] = "write_summary_failed: {}".format(e)

        result["rep_count"] = len(reps)
        result["raw_rows"] = len(raw_rows) + cap["chunk_idx"] * RAW_ROW_CAP
        result["duration_s"] = round(duration_s, 2)
        logging.info("[CAP] ✅ 采集完成: %d reps / %d raw rows / %.1fs → %s",
                     len(reps), result["raw_rows"], duration_s, cap["out_dir"])

    # 写 result 信号
    try:
        with open(SHM_RESULT + ".tmp", "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False)
        os.rename(SHM_RESULT + ".tmp", SHM_RESULT)
    except IOError as e:
        logging.warning("[CAP] 写 result 信号失败: %s", e)

    # 清理 session 信号
    _safe_remove(SHM_SESSION)
    _safe_remove(SHM_ACK)

    # 重置 cap
    new = make_state()
    cap.clear()
    cap.update(new)


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
