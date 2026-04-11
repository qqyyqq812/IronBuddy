#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cloud_rtmpose_client.py
───────────────────────
Board-side HTTP client for the IronBuddy cloud RTMPose inference server.

Reads camera frames, POSTs them as JPEG to the cloud HTTP endpoint, and
writes results to the shared-memory paths that main_claw_loop.py expects:

  /dev/shm/pose_data.json  – pose data (same format as rtmpose_publisher.py)
  /dev/shm/result.jpg      – annotated frame for MJPEG streaming

Environment variables
─────────────────────
  CLOUD_RTMPOSE_URL   – full URL to the /infer endpoint
                        default: http://connect.westd.seetacloud.com:6006/infer
  CLOUD_HEALTH_URL    – health-check URL (auto-derived if unset)
  CLOUD_JPEG_QUALITY  – JPEG upload quality (default 70)
  CLOUD_TIMEOUT_S     – per-request timeout in seconds (default 3.0)
  CLOUD_FALLBACK_NPU  – set to "0" to disable NPU fallback (default "1")

Fallback
────────
If the cloud endpoint becomes unreachable for more than CLOUD_TIMEOUT_S seconds,
the client falls back to the local RKNN NPU (if available) and retries the cloud
every 5 seconds until it comes back.
"""

import os
import sys
import time
import json
import math
import threading
from typing import Optional

import cv2
import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Path bootstrap ─────────────────────────────────────────────────────────────
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../../"))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# ── Config from env ────────────────────────────────────────────────────────────
CLOUD_INFER_URL = os.environ.get(
    "CLOUD_RTMPOSE_URL",
    # Default: access via WSL SSH tunnel (same hop pattern as OpenClaw)
    # WSL start_validation.sh sets this to http://{WSL_IP}:6006/infer
    "http://127.0.0.1:6006/infer",
)
CLOUD_HEALTH_URL = os.environ.get(
    "CLOUD_HEALTH_URL",
    CLOUD_INFER_URL.replace("/infer", "/health"),
)
JPEG_QUALITY = int(os.environ.get("CLOUD_JPEG_QUALITY", "70"))
REQUEST_TIMEOUT = float(os.environ.get("CLOUD_TIMEOUT_S", "5.0"))
USE_NPU_FALLBACK = os.environ.get("CLOUD_FALLBACK_NPU", "1") != "0"
# Async mode: camera runs at full speed, cloud inference in background thread
ASYNC_CLOUD = True

SHM_POSE_JSON = "/dev/shm/pose_data.json" if os.path.exists("/dev/shm") else "/tmp/pose_data.json"
SHM_RESULT_JPG = "/dev/shm/result.jpg" if os.path.exists("/dev/shm") else "/tmp/result.jpg"
SHM_EMG_JSON = "/dev/shm/muscle_activation.json" if os.path.exists("/dev/shm") else "/tmp/muscle_activation.json"


# ── HTTP session with keep-alive and conservative retries ─────────────────────
def _make_session() -> requests.Session:
    session = requests.Session()
    session.verify = False  # AutoDL self-signed cert
    # Only retry on connection errors/5xx, NOT on timeout (we handle that ourselves)
    retry = Retry(
        total=1,
        backoff_factor=0.2,
        status_forcelist=[500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=1, pool_maxsize=2)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# ── Skeleton drawing (identical to rtmpose_publisher.py) ─────────────────────
_SKELETON = [
    (15, 13), (13, 11), (16, 14), (14, 12), (11, 12),
    (5, 11), (6, 12), (5, 6), (5, 7), (6, 8), (7, 9), (8, 10),
    (1, 2), (0, 1), (0, 2), (1, 3), (2, 4), (3, 5), (4, 6),
]


def draw_skeleton(img: np.ndarray, kpts: list) -> np.ndarray:
    img = img.copy()
    for i, pt in enumerate(kpts):
        x, y, conf = pt
        if conf > 0.1:
            color = (0, 100, 255) if i in [11, 13, 15] else (0, 255, 0)
            radius = 8 if i in [11, 13, 15] else 4
            cv2.circle(img, (int(x), int(y)), radius, color, -1)
    for u, v in _SKELETON:
        if kpts[u][2] > 0.1 and kpts[v][2] > 0.1:
            pt1 = (int(kpts[u][0]), int(kpts[u][1]))
            pt2 = (int(kpts[v][0]), int(kpts[v][1]))
            cv2.line(img, pt1, pt2, (200, 100, 0), 3)
    return img


# ── SHM write helpers (atomic rename) ─────────────────────────────────────────
def _write_pose_json(payload: dict) -> None:
    tmp = SHM_POSE_JSON + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.rename(tmp, SHM_POSE_JSON)
    except Exception:
        pass


def _write_result_jpg(frame: np.ndarray, kpts: list) -> None:
    drawn = draw_skeleton(frame, kpts)
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
    ret, buf = cv2.imencode(".jpg", drawn, encode_param)
    if not ret:
        return
    tmp = SHM_RESULT_JPG + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(buf.tobytes())
        os.rename(tmp, SHM_RESULT_JPG)
    except Exception:
        pass


def _compute_angle(a, b, c):
    ba = [a[0] - b[0], a[1] - b[1]]
    bc = [c[0] - b[0], c[1] - b[1]]
    dot = ba[0]*bc[0] + ba[1]*bc[1]
    mag_ba = math.sqrt(ba[0]**2 + ba[1]**2)
    mag_bc = math.sqrt(bc[0]**2 + bc[1]**2)
    if mag_ba * mag_bc == 0:
        return 180.0
    cos_a = max(-1.0, min(1.0, dot / (mag_ba * mag_bc)))
    return math.degrees(math.acos(cos_a))


def _generate_emg_from_angle(angle, exercise="squat"):
    import random
    noise = lambda: random.uniform(-3, 3)
    if exercise == "squat":
        if angle < 140:
            d = (140 - angle) / 70.0
            return {"quadriceps": min(100, 50+d*40+noise()), "glutes": min(100, 40+d*55+noise()),
                    "calves": min(100, 15+d*15+noise()), "biceps": min(100, 10+d*20+noise())}
        return {"quadriceps": max(0, 8+noise()), "glutes": max(0, 5+noise()),
                "calves": max(0, 3+noise()), "biceps": max(0, 3+noise())}
    else:
        if angle < 140:
            d = (140 - angle) / 90.0
            return {"quadriceps": max(0, 3+noise()), "glutes": max(0, 3+noise()),
                    "calves": min(100, 60+d*35+noise()), "biceps": max(0, 5+d*10+noise())}
        return {"quadriceps": max(0, 2+noise()), "glutes": max(0, 2+noise()),
                "calves": max(0, 5+noise()), "biceps": max(0, 2+noise())}


def _write_emg(kpts, exercise="squat"):
    if os.path.exists("/dev/shm/emg_heartbeat"):
        return
    try:
        l = kpts[11][2] + kpts[13][2] + kpts[15][2]
        r = kpts[12][2] + kpts[14][2] + kpts[16][2]
        if exercise == "bicep_curl":
            a = _compute_angle(kpts[5], kpts[7], kpts[9]) if l > r else _compute_angle(kpts[6], kpts[8], kpts[10])
        else:
            a = _compute_angle(kpts[11], kpts[13], kpts[15]) if l > r else _compute_angle(kpts[12], kpts[14], kpts[16])
        acts = _generate_emg_from_angle(a, exercise)
        data = {"activations": {k: round(max(0, v), 1) for k, v in acts.items()},
                "warnings": [], "exercise": exercise, "simulated": True}
        tmp = SHM_EMG_JSON + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.rename(tmp, SHM_EMG_JSON)
    except Exception:
        pass


# ── Optional local NPU fallback ───────────────────────────────────────────────
class _NPUFallback:
    """Lazy-loads RTMPoseWorker only when actually needed."""

    def __init__(self):
        self._worker = None
        self._available: Optional[bool] = None

    def _try_init(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            from hardware_engine.ai_sensory.vision.experimental.rtmpose_backend_worker import (
                RTMPoseWorker,
            )
            model_path = os.path.join(
                current_dir, "experimental", "rtmpose_quant.rknn"
            )
            if os.path.exists(model_path):
                self._worker = RTMPoseWorker(model_path)
                self._available = True
                print("[CloudClient] NPU fallback loaded successfully.")
            else:
                print("[CloudClient] NPU model not found; fallback disabled.")
                self._available = False
        except Exception as e:
            print(f"[CloudClient] NPU fallback unavailable: {e}")
            self._available = False
        return self._available

    def infer(self, frame: np.ndarray, orig_w: int, orig_h: int) -> list:
        """Returns [[x,y,score]×17] in original frame coords, or zeros."""
        if not self._try_init() or self._worker is None:
            return [[0.0, 0.0, 0.0]] * 17
        try:
            raw_kpts = self._worker.inference(frame)
            # Map from model input space (192×256) -> original frame
            scale_x = orig_w / 192.0
            scale_y = orig_h / 256.0
            raw_kpts[:, 0] *= scale_x
            raw_kpts[:, 1] *= scale_y
            return raw_kpts.tolist()
        except Exception as e:
            print(f"[CloudClient] NPU inference error: {e}")
            return [[0.0, 0.0, 0.0]] * 17

    def release(self):
        if self._worker:
            self._worker.release()


# ── Main client loop ───────────────────────────────────────────────────────────
def main():
    print("[CloudClient] IronBuddy Cloud RTMPose Client starting...")
    print(f"[CloudClient] Endpoint : {CLOUD_INFER_URL}")
    print(f"[CloudClient] Timeout  : {REQUEST_TIMEOUT}s")
    print(f"[CloudClient] NPU fall : {USE_NPU_FALLBACK}")
    print(f"[CloudClient] Async    : {ASYNC_CLOUD}")

    # ── Load smoother (One-Euro, same params as publisher) ────────────────────
    try:
        from hardware_engine.ai_sensory.vision.filters import PoseSmoother
        smoother = PoseSmoother(mincutoff=0.8, beta=0.015, dcutoff=1.0)
    except ImportError:
        smoother = None
        print("[CloudClient] PoseSmoother not available, raw keypoints used.")

    # ── Open camera ───────────────────────────────────────────────────────────
    import glob as _glob
    video_dev_paths = _glob.glob("/dev/v4l/by-id/*index0")
    video_dev = video_dev_paths[0] if video_dev_paths else "/dev/video5"
    print(f"[CloudClient] Camera device: {video_dev}")
    cap = cv2.VideoCapture(video_dev)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    simulate_mode = not cap.isOpened()
    if simulate_mode:
        print("[CloudClient] Camera not found – running in simulation mode.")

    # ── HTTP session ──────────────────────────────────────────────────────────
    session = _make_session()

    # ── NPU fallback (initialised lazily) ─────────────────────────────────────
    npu = _NPUFallback() if USE_NPU_FALLBACK else None

    # ── Async cloud inference state ───────────────────────────────────────────
    # Shared between main thread and cloud thread
    _latest_cloud_kpts = [None]  # latest keypoints from cloud
    _cloud_frame_slot = [None]   # latest JPEG bytes for cloud to process
    _cloud_lock = threading.Lock()
    _cloud_alive = [True]

    def _cloud_worker():
        """Background thread: picks up latest frame, sends to cloud, stores result."""
        cloud_session = _make_session()
        while _cloud_alive[0]:
            with _cloud_lock:
                jpeg = _cloud_frame_slot[0]
                _cloud_frame_slot[0] = None  # consume
            if jpeg is None:
                time.sleep(0.02)
                continue
            try:
                resp = cloud_session.post(
                    CLOUD_INFER_URL,
                    files={"frame": ("frame.jpg", jpeg, "image/jpeg")},
                    data={"seq_id": "0"},
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    kpts = payload.get("keypoints")
                    if kpts:
                        with _cloud_lock:
                            _latest_cloud_kpts[0] = kpts
            except Exception:
                pass  # cloud unavailable, silently skip
        cloud_session.close()

    if ASYNC_CLOUD:
        cloud_thread = threading.Thread(target=_cloud_worker, daemon=True)
        cloud_thread.start()
        print("[CloudClient] Async cloud worker started.")

    seq_id = 0
    frame_idx = 0
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]

    try:
        while True:
            t_now = time.time()
            frame_idx += 1

            # ── Grab frame ────────────────────────────────────────────────────
            if simulate_mode:
                time.sleep(0.05)
                frame = np.zeros((480, 640, 3), dtype=np.uint8)
                cv2.putText(frame, f"CloudClient Sim #{frame_idx}", (20, 40),
                            cv2.FONT_HERSHEY_DUPLEX, 0.8, (0, 200, 255), 2)
                orig_h, orig_w = frame.shape[:2]
            else:
                ret, frame = cap.read()
                if not ret:
                    time.sleep(0.01)
                    continue
                orig_h, orig_w = frame.shape[:2]

            # ── Encode JPEG for upload ─────────────────────────────────────
            ret_enc, buf = cv2.imencode(".jpg", frame, encode_param)
            if not ret_enc:
                continue
            jpeg_bytes = buf.tobytes()

            if ASYNC_CLOUD:
                # ── Async mode: submit frame to cloud thread, use latest result ──
                with _cloud_lock:
                    _cloud_frame_slot[0] = jpeg_bytes  # always latest
                    raw_kpts_list = _latest_cloud_kpts[0]  # may be None initially
                used_cloud = raw_kpts_list is not None
            else:
                # ── Sync mode: block on each frame (original behavior) ────────
                raw_kpts_list = None
                used_cloud = False
                try:
                    resp = session.post(
                        CLOUD_INFER_URL,
                        files={"frame": ("frame.jpg", jpeg_bytes, "image/jpeg")},
                        data={"seq_id": str(seq_id)},
                        timeout=REQUEST_TIMEOUT,
                    )
                    if resp.status_code == 200:
                        raw_kpts_list = resp.json().get("keypoints")
                        used_cloud = True
                except Exception:
                    pass

            # ── NPU fallback ──────────────────────────────────────────────
            if raw_kpts_list is None:
                if npu is not None and not simulate_mode:
                    raw_kpts_list = npu.infer(frame, orig_w, orig_h)
                else:
                    raw_kpts_list = [[0.0, 0.0, 0.0]] * 17

            # ── Apply One-Euro smoother ───────────────────────────────────
            raw_kpts_np = np.array(raw_kpts_list, dtype=float)
            if smoother is not None:
                smoothed_kpts_np = smoother.process(raw_kpts_np, timestamp=t_now)
            else:
                smoothed_kpts_np = raw_kpts_np
            smoothed_kpts = smoothed_kpts_np.tolist()

            # ── Write pose_data.json ──────────────────────────────────────
            # Use real person score from keypoint confidence (not hardcoded)
            top_confs = sorted([k[2] for k in smoothed_kpts], reverse=True)
            person_score = float(np.mean(top_confs[:6])) if top_confs else 0
            out_json = {
                "timestamp": t_now,
                "frame_idx": frame_idx,
                "objects": [{"score": round(person_score, 3), "kpts": smoothed_kpts}] if person_score > 0.15 else [],
            }
            _write_pose_json(out_json)

            # ── Write result.jpg for MJPEG streaming ──────────────────────
            _write_result_jpg(frame, smoothed_kpts)

            # ── Write simulated EMG (synced with skeleton angle) ──────────
            _exercise = "squat"
            try:
                if os.path.exists("/dev/shm/user_profile.json"):
                    with open("/dev/shm/user_profile.json", "r") as uf:
                        _exercise = json.load(uf).get("exercise", "squat")
            except Exception:
                pass
            _write_emg(smoothed_kpts, _exercise)

            # ── Heartbeat ─────────────────────────────────────────────────
            if frame_idx % 60 == 0:
                src = "cloud" if used_cloud else "NPU/sim"
                print(f"[CloudClient] frame={frame_idx} src={src} seq={seq_id}")

            seq_id += 1

    except KeyboardInterrupt:
        print("\n[CloudClient] Interrupted, shutting down.")
    finally:
        _cloud_alive[0] = False
        cap.release()
        if npu:
            npu.release()
        session.close()


if __name__ == "__main__":
    main()
