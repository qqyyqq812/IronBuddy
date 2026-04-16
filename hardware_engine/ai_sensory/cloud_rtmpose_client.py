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
  VISION_MODE         – "cloud" (default) or "local" for on-device YOLOv5 pose
  LOCAL_POSE_MODEL    – path to .rknn model for local mode
                        default: /home/toybrick/deploy_rknn_yolo/YOLOv5-Style/data/weights/pose-5s6-640-uint8.rknn
  LOCAL_POSE_CONF     – confidence threshold for local model (default 0.35)

Fallback
────────
If the cloud endpoint becomes unreachable for more than CLOUD_TIMEOUT_S seconds,
the client falls back to the local RKNN NPU (if available) and retries the cloud
every 5 seconds until it comes back.

Hot-switch
──────────
Write {"mode": "local"} or {"mode": "cloud"} to /dev/shm/vision_mode.json
to switch vision mode at runtime without restarting.
"""

import os
import sys
import time
import json
import math
import threading
from typing import Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

import cv2
import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Embedded MJPEG Server (port 8080, zero-copy from memory) ─────────────────
MJPEG_PORT = int(os.environ.get("MJPEG_PORT", "8080"))
ENABLE_HDMI = os.environ.get("ENABLE_HDMI", "0") == "1"
_hdmi_ok = [True]  # set to False if cv2.imshow fails

_mjpeg_frame_lock = threading.Lock()
_mjpeg_frame = [None]  # latest JPEG bytes, shared with MJPEG server


class _MJPEGHandler(BaseHTTPRequestHandler):
    """Minimal MJPEG handler — no logging, pure speed."""

    def do_GET(self):
        if self.path == '/stream' or self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            try:
                while True:
                    with _mjpeg_frame_lock:
                        jpeg = _mjpeg_frame[0]
                    if jpeg is not None:
                        self.wfile.write(b'--frame\r\n')
                        self.wfile.write(b'Content-Type: image/jpeg\r\n')
                        self.wfile.write(b'Content-Length: ' + str(len(jpeg)).encode() + b'\r\n\r\n')
                        self.wfile.write(jpeg)
                        self.wfile.write(b'\r\n')
                    time.sleep(0.05)  # ~20fps cap
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == '/snapshot':
            with _mjpeg_frame_lock:
                jpeg = _mjpeg_frame[0]
            if jpeg:
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Content-Length', str(len(jpeg)))
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(jpeg)
            else:
                self.send_response(204)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress all logging for performance


class _ThreadedHTTPServer(HTTPServer):
    """Handle each request in a new thread."""
    daemon_threads = True
    request_queue_size = 8

    def process_request(self, request, client_address):
        t = threading.Thread(target=self.process_request_thread, args=(request, client_address))
        t.daemon = True
        t.start()

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            pass
        try:
            self.shutdown_request(request)
        except Exception:
            pass


def _start_mjpeg_server():
    """Launch embedded MJPEG server on a daemon thread."""
    try:
        server = _ThreadedHTTPServer(('0.0.0.0', MJPEG_PORT), _MJPEGHandler)
        print("[MJPEG] Embedded server on :{} (zero-copy, ~20fps)".format(MJPEG_PORT))
        server.serve_forever()
    except Exception as e:
        print("[MJPEG] Server failed: {}".format(e))

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
JPEG_QUALITY = int(os.environ.get("CLOUD_JPEG_QUALITY", "50"))
REQUEST_TIMEOUT = float(os.environ.get("CLOUD_TIMEOUT_S", "5.0"))
TARGET_FPS = int(os.environ.get("CLOUD_TARGET_FPS", "15"))
FRAME_INTERVAL = 1.0 / TARGET_FPS
USE_NPU_FALLBACK = os.environ.get("CLOUD_FALLBACK_NPU", "1") != "0"
# Async mode: camera runs at full speed, cloud inference in background thread
ASYNC_CLOUD = True

# ── Local YOLOv5-Pose mode ───────────────────────────────────────────────────
VISION_MODE = os.environ.get("VISION_MODE", "cloud").lower()  # "cloud" or "local"
LOCAL_POSE_MODEL = os.environ.get(
    "LOCAL_POSE_MODEL",
    "/home/toybrick/deploy_rknn_yolo/YOLOv5-Style/data/weights/pose-5s6-640-uint8.rknn",
)
LOCAL_POSE_CONF = float(os.environ.get("LOCAL_POSE_CONF", "0.08"))
SHM_VISION_MODE = "/dev/shm/vision_mode.json"

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
    jpeg_bytes = buf.tobytes()

    # Push to embedded MJPEG server (zero-copy from memory)
    with _mjpeg_frame_lock:
        _mjpeg_frame[0] = jpeg_bytes

    # Also write to SHM file (for Flask fallback / snapshot)
    tmp = SHM_RESULT_JPG + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(jpeg_bytes)
        os.rename(tmp, SHM_RESULT_JPG)
    except Exception:
        pass

    # HDMI direct display — fullscreen (if enabled and X11 available)
    if ENABLE_HDMI and _hdmi_ok[0]:
        try:
            cv2.imshow("IronBuddy", drawn)
            cv2.waitKey(1)
        except Exception:
            _hdmi_ok[0] = False
            print("[CloudClient] HDMI display failed, disabling.")

    # Write HDMI status for web frontend to read
    _write_hdmi_status(ENABLE_HDMI and _hdmi_ok[0])


_hdmi_status_counter = [0]

def _write_hdmi_status(active):
    """Write HDMI status to SHM (throttled to once per 30 frames)."""
    _hdmi_status_counter[0] += 1
    if _hdmi_status_counter[0] % 30 != 0:
        return
    try:
        data = json.dumps({"active": bool(active), "ts": time.time()})
        tmp = "/dev/shm/hdmi_status.json.tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.rename(tmp, "/dev/shm/hdmi_status.json")
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


# ── Hot-switch vision mode reader ─────────────────────────────────────────────
def _read_vision_mode(default):
    # type: (str) -> str
    """Read current vision mode from signal file, falling back to default."""
    try:
        if os.path.exists(SHM_VISION_MODE):
            with open(SHM_VISION_MODE, "r") as f:
                data = json.load(f)
            mode = data.get("mode", default).lower()
            if mode in ("local", "cloud"):
                return mode
    except Exception:
        pass
    return default


# ── Local YOLOv5 Pose engine (lazy init) ─────────────────────────────────────
class _LocalPoseEngine(object):
    """Wrapper around LocalYoloPose with lazy initialization."""

    def __init__(self, model_path, conf_thresh):
        self._model_path = model_path
        self._conf_thresh = conf_thresh
        self._engine = None
        self._available = None  # type: Optional[bool]

    def _try_init(self):
        # type: () -> bool
        if self._available is not None:
            return self._available
        try:
            # Try both import paths (package install vs direct run)
            try:
                from hardware_engine.ai_sensory.local_yolo_pose import LocalYoloPose
            except ImportError:
                import importlib.util
                _spec = importlib.util.spec_from_file_location(
                    "local_yolo_pose",
                    os.path.join(current_dir, "local_yolo_pose.py"),
                )
                _mod = importlib.util.module_from_spec(_spec)
                _spec.loader.exec_module(_mod)
                LocalYoloPose = _mod.LocalYoloPose
            if os.path.exists(self._model_path):
                self._engine = LocalYoloPose(self._model_path, conf_thresh=self._conf_thresh)
                self._available = True
                print("[CloudClient] Local YOLOv5-Pose engine ready: {}".format(self._model_path))
            else:
                print("[CloudClient] Local model not found: {}".format(self._model_path))
                self._available = False
        except Exception as e:
            print("[CloudClient] Local YOLOv5-Pose init failed: {}".format(e))
            self._available = False
        return self._available

    def infer(self, frame):
        # type: (np.ndarray) -> list
        """Returns [[x, y, conf], ...] x 17."""
        if not self._try_init() or self._engine is None:
            return [[0.0, 0.0, 0.0]] * 17
        return self._engine.infer(frame)

    def release(self):
        if self._engine is not None:
            self._engine.release()


# ── Main client loop ───────────────────────────────────────────────────────────
def main():
    print("[CloudClient] IronBuddy Vision Client starting...")
    print("[CloudClient] Endpoint : {}".format(CLOUD_INFER_URL))
    print("[CloudClient] Timeout  : {}s".format(REQUEST_TIMEOUT))
    print("[CloudClient] NPU fall : {}".format(USE_NPU_FALLBACK))
    print("[CloudClient] Async    : {}".format(ASYNC_CLOUD))
    print("[CloudClient] VisionMode: {}".format(VISION_MODE))
    print("[CloudClient] HDMI     : {}".format(ENABLE_HDMI))

    # Test HDMI display availability (must check BEFORE cv2.imshow — Qt abort is uncatchable)
    if ENABLE_HDMI:
        display_env = os.environ.get("DISPLAY", "")
        if not display_env:
            print("[CloudClient] HDMI requested but $DISPLAY not set. Skipping HDMI.")
            _hdmi_ok[0] = False
        else:
            # Quick X11 connectivity test
            import subprocess as _sp
            ret = _sp.call(["xdpyinfo"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=3)
            if ret != 0:
                print("[CloudClient] HDMI: X11 display :{} not reachable. Skipping.".format(display_env))
                _hdmi_ok[0] = False
            else:
                print("[CloudClient] HDMI display OK (DISPLAY={})".format(display_env))
                # Create fullscreen window
                cv2.namedWindow("IronBuddy", cv2.WINDOW_NORMAL)
                cv2.setWindowProperty("IronBuddy", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
                print("[CloudClient] HDMI fullscreen window created")

    # Start embedded MJPEG server (independent of Flask)
    mjpeg_thread = threading.Thread(target=_start_mjpeg_server, daemon=True)
    mjpeg_thread.start()

    # Current active mode (can be hot-switched via signal file)
    active_mode = VISION_MODE

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
    cap.set(cv2.CAP_PROP_FPS, TARGET_FPS)          # request lower FPS from driver
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)          # cap resolution
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    simulate_mode = not cap.isOpened()
    if simulate_mode:
        print("[CloudClient] Camera not found – running in simulation mode.")

    # ── HTTP session ──────────────────────────────────────────────────────────
    session = _make_session()

    # ── NPU fallback (initialised lazily) ─────────────────────────────────────
    npu = _NPUFallback() if USE_NPU_FALLBACK else None

    # ── Local YOLOv5-Pose engine (initialised lazily) ────────────────────────
    local_pose = _LocalPoseEngine(LOCAL_POSE_MODEL, LOCAL_POSE_CONF)

    # ── Async inference state ────────────────────────────────────────────────
    _latest_kpts = [None]             # latest keypoints from ANY source
    _cloud_jpeg_slot = [None]         # JPEG bytes for cloud thread
    _local_frame_slot = [None]        # numpy frame for local thread
    _infer_lock = threading.Lock()
    _infer_alive = [True]

    def _cloud_worker():
        """Background thread: picks up latest JPEG, sends to cloud, stores result."""
        cloud_session = _make_session()
        while _infer_alive[0]:
            with _infer_lock:
                jpeg = _cloud_jpeg_slot[0]
                _cloud_jpeg_slot[0] = None
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
                        with _infer_lock:
                            _latest_kpts[0] = kpts
            except Exception:
                pass
        cloud_session.close()

    def _local_worker():
        """Background thread: local NPU inference (non-blocking to main loop)."""
        while _infer_alive[0]:
            with _infer_lock:
                raw_frame = _local_frame_slot[0]
                _local_frame_slot[0] = None
            if raw_frame is None:
                time.sleep(0.02)
                continue
            # raw_frame here is the numpy BGR frame (not JPEG bytes)
            kpts = local_pose.infer(raw_frame)
            has_valid = any(k[2] > 0.1 for k in kpts)
            if has_valid:
                with _infer_lock:
                    _latest_kpts[0] = kpts

    if ASYNC_CLOUD:
        cloud_thread = threading.Thread(target=_cloud_worker, daemon=True)
        cloud_thread.start()
        print("[CloudClient] Async cloud worker started.")

    local_thread = threading.Thread(target=_local_worker, daemon=True)
    local_thread.start()
    print("[CloudClient] Async local NPU worker started.")

    seq_id = 0
    frame_idx = 0
    encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]

    _last_frame_time = 0.0
    try:
        while True:
            t_now = time.time()

            # ── Frame rate limiter ────────────────────────────────────────────
            elapsed = t_now - _last_frame_time
            if elapsed < FRAME_INTERVAL:
                time.sleep(FRAME_INTERVAL - elapsed)
                t_now = time.time()
            _last_frame_time = t_now

            frame_idx += 1

            # ── Grab frame ────────────────────────────────────────────────────
            if simulate_mode:
                time.sleep(0.05)
                frame = np.zeros((480, 640, 3), dtype=np.uint8)
                cv2.putText(frame, "CloudClient Sim #{}".format(frame_idx), (20, 40),
                            cv2.FONT_HERSHEY_DUPLEX, 0.8, (0, 200, 255), 2)
                orig_h, orig_w = frame.shape[:2]
            else:
                ret, frame = cap.read()
                if not ret:
                    time.sleep(0.01)
                    continue
                orig_h, orig_w = frame.shape[:2]

            # ── Hot-switch vision mode (check every 30 frames) ────────────
            if frame_idx % 30 == 0:
                new_mode = _read_vision_mode(active_mode)
                if new_mode != active_mode:
                    print("[CloudClient] Vision mode switched: {} -> {}".format(active_mode, new_mode))
                    active_mode = new_mode

            # ── Encode JPEG for upload ─────────────────────────────────────
            ret_enc, buf = cv2.imencode(".jpg", frame, encode_param)
            if not ret_enc:
                continue
            jpeg_bytes = buf.tobytes()

            raw_kpts_list = None
            inference_src = "none"

            if active_mode == "local":
                # ── Async Local: submit frame to local thread, read latest ──
                with _infer_lock:
                    _local_frame_slot[0] = frame.copy()  # numpy frame for local NPU
                    raw_kpts_list = _latest_kpts[0]      # latest result (may be None)
                if raw_kpts_list is not None:
                    inference_src = "local"
            else:
                # ── Async Cloud: submit JPEG to cloud thread, read latest ──
                if ASYNC_CLOUD:
                    with _infer_lock:
                        _cloud_jpeg_slot[0] = jpeg_bytes  # cloud uses JPEG bytes
                        raw_kpts_list = _latest_kpts[0]
                else:
                    try:
                        resp = session.post(
                            CLOUD_INFER_URL,
                            files={"frame": ("frame.jpg", jpeg_bytes, "image/jpeg")},
                            data={"seq_id": str(seq_id)},
                            timeout=REQUEST_TIMEOUT,
                        )
                        if resp.status_code == 200:
                            raw_kpts_list = resp.json().get("keypoints")
                    except Exception:
                        pass

                if raw_kpts_list is not None:
                    inference_src = "cloud"

                # ── NPU fallback (cloud mode only) ────────────────────────
                if raw_kpts_list is None:
                    if npu is not None and not simulate_mode:
                        raw_kpts_list = npu.infer(frame, orig_w, orig_h)
                        inference_src = "npu-fallback"
                    else:
                        raw_kpts_list = [[0.0, 0.0, 0.0]] * 17
                        inference_src = "sim"

            # ── Fallback: if no inference result yet, use zeros ────────────
            if raw_kpts_list is None:
                raw_kpts_list = [[0.0, 0.0, 0.0]] * 17
                inference_src = "waiting"

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
                "objects": [{"score": round(person_score, 3), "kpts": smoothed_kpts}] if person_score > 0.08 else [],
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
                print("[CloudClient] frame={} mode={} src={} seq={}".format(
                    frame_idx, active_mode, inference_src, seq_id))

            seq_id += 1

    except KeyboardInterrupt:
        print("\n[CloudClient] Interrupted, shutting down.")
    finally:
        _infer_alive[0] = False
        cap.release()
        if npu:
            npu.release()
        local_pose.release()
        session.close()


if __name__ == "__main__":
    main()
