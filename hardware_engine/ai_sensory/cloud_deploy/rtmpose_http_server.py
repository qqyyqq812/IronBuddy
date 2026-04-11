#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IronBuddy Cloud RTMPose HTTP Inference Server — ONNX Runtime GPU
Port: 6006 (AutoDL exposed HTTP port)

Uses ONNX Runtime with CUDAExecutionProvider for RTMPose-m (body7, 256x192).
No mmpose/mmcv dependency required.

Endpoints:
  POST /infer           - multipart JPEG → keypoints JSON
  POST /infer_with_viz  - multipart JPEG → keypoints + skeleton JPEG (base64)
  GET  /health          - server status + stats
"""

import asyncio
import base64
import os
import time
import threading
from contextlib import asynccontextmanager

import cv2
import numpy as np
import onnxruntime as ort

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
import uvicorn

# ── Model config ──────────────────────────────────────────────────────────────
MODEL_PATH = os.environ.get("RTMPOSE_ONNX", "/root/ironbuddy_cloud/rtmpose_m.onnx")
PORT = int(os.environ.get("RTMPOSE_PORT", "6006"))
INPUT_W, INPUT_H = 192, 256
MEAN = np.array([123.675, 116.28, 103.53], dtype=np.float32)
STD = np.array([58.395, 57.12, 57.375], dtype=np.float32)
SIMCC_SPLIT_RATIO = 2.0

# COCO-17 skeleton edges for visualization
SKELETON = [
    (0, 1), (0, 2), (1, 3), (2, 4),
    (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),
    (5, 11), (6, 12), (11, 12),
    (11, 13), (13, 15), (12, 14), (14, 16),
]
KPT_COLORS = [
    (255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0),
    (85, 255, 0), (0, 255, 0), (0, 255, 85), (0, 255, 170), (0, 255, 255),
    (0, 170, 255), (0, 85, 255), (0, 0, 255), (85, 0, 255), (170, 0, 255),
    (255, 0, 255), (255, 0, 170),
]

# ── Globals ────────────────────────────────────────────────────────────────────
_session = None
_ready = False
_init_error = None
_stats = {"frames": 0, "total_ms": 0.0}
_lock = threading.Lock()


def _init_model():
    global _session, _ready, _init_error
    try:
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        _session = ort.InferenceSession(MODEL_PATH, sess_options=opts, providers=providers)
        active = _session.get_providers()
        print(f"[Server] RTMPose ONNX loaded. Providers: {active}")
        # Warmup 3 frames
        dummy = np.zeros((1, 3, INPUT_H, INPUT_W), dtype=np.float32)
        for _ in range(3):
            _session.run(None, {"input": dummy})
        _ready = True
        print("[Server] Warmup complete. Ready for inference.")
    except Exception as e:
        _init_error = str(e)
        print(f"[Server] FATAL: {e}")


def _preprocess(img_bgr: np.ndarray):
    """Full-image top-down preprocessing: affine warp + normalize."""
    h, w = img_bgr.shape[:2]
    center = np.array([w / 2.0, h / 2.0], dtype=np.float32)
    scale = np.array([w, h], dtype=np.float32) * 1.25

    src = np.array([
        center,
        [center[0], center[1] - scale[1] * 0.5],
        [center[0] - scale[0] * 0.5, center[1]],
    ], dtype=np.float32)
    dst = np.array([
        [INPUT_W * 0.5, INPUT_H * 0.5],
        [INPUT_W * 0.5, 0],
        [0, INPUT_H * 0.5],
    ], dtype=np.float32)

    M = cv2.getAffineTransform(src, dst)
    warped = cv2.warpAffine(img_bgr, M, (INPUT_W, INPUT_H), flags=cv2.INTER_LINEAR)

    rgb = cv2.cvtColor(warped, cv2.COLOR_BGR2RGB).astype(np.float32)
    rgb = (rgb - MEAN) / STD
    blob = np.expand_dims(rgb.transpose(2, 0, 1), 0).astype(np.float32)
    return blob, {"center": center, "scale": scale, "M_fwd": M}


def _postprocess(simcc_x, simcc_y, meta):
    """Decode SimCC outputs → keypoints in original image coordinates."""
    sx, sy = simcc_x[0], simcc_y[0]  # [K, 384], [K, 512]
    K = sx.shape[0]

    # Inverse affine: input coords → original coords
    src = np.array([
        [INPUT_W * 0.5, INPUT_H * 0.5],
        [INPUT_W * 0.5, 0],
        [0, INPUT_H * 0.5],
    ], dtype=np.float32)
    dst = np.array([
        meta["center"],
        [meta["center"][0], meta["center"][1] - meta["scale"][1] * 0.5],
        [meta["center"][0] - meta["scale"][0] * 0.5, meta["center"][1]],
    ], dtype=np.float32)
    M_inv = cv2.getAffineTransform(src, dst)

    kpts = []
    for k in range(K):
        xi = int(np.argmax(sx[k]))
        yi = int(np.argmax(sy[k]))
        score = float(max(sx[k][xi], sy[k][yi]))
        x_in = xi / SIMCC_SPLIT_RATIO
        y_in = yi / SIMCC_SPLIT_RATIO
        pt = M_inv @ np.array([x_in, y_in, 1.0])
        kpts.append([round(float(pt[0]), 1), round(float(pt[1]), 1), round(score, 3)])
    return kpts


def _draw_skeleton(img, kpts, thr=0.3):
    """Draw keypoints + skeleton on image copy."""
    out = img.copy()
    for i, (x, y, s) in enumerate(kpts):
        if s < thr:
            continue
        c = KPT_COLORS[i % len(KPT_COLORS)]
        cv2.circle(out, (int(x), int(y)), 4, c, -1)
    for i, j in SKELETON:
        if i >= len(kpts) or j >= len(kpts):
            continue
        if kpts[i][2] < thr or kpts[j][2] < thr:
            continue
        cv2.line(out, (int(kpts[i][0]), int(kpts[i][1])),
                 (int(kpts[j][0]), int(kpts[j][1])), (0, 220, 0), 2)
    return out


def _infer(img_bgr, seq_id):
    t0 = time.perf_counter()
    blob, meta = _preprocess(img_bgr)
    outs = _session.run(None, {"input": blob})
    kpts = _postprocess(outs[0], outs[1], meta)
    dt = (time.perf_counter() - t0) * 1000

    with _lock:
        _stats["frames"] += 1
        _stats["total_ms"] += dt

    scores = sorted([k[2] for k in kpts], reverse=True)
    person_score = float(np.mean(scores[:10]))
    return {
        "keypoints": kpts,
        "score": round(person_score, 3),
        "latency_ms": round(dt, 2),
        "seq_id": seq_id,
    }


# ── App ────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(a):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _init_model)
    yield
    print("[Server] Shutdown.")


app = FastAPI(title="IronBuddy RTMPose Cloud", lifespan=lifespan)


@app.get("/health")
async def health():
    avg = _stats["total_ms"] / max(_stats["frames"], 1)
    return {
        "status": "ready" if _ready else ("error" if _init_error else "loading"),
        "model": "rtmpose-m-body7-onnx",
        "gpu": "CUDAExecutionProvider" in (_session.get_providers() if _session else []),
        "frames_processed": _stats["frames"],
        "avg_inference_ms": round(avg, 2),
        "error": _init_error,
    }


@app.post("/infer")
async def infer(
    frame: UploadFile = File(...),
    seq_id: int = Form(0),
):
    if not _ready:
        raise HTTPException(503, _init_error or "Model loading...")
    raw = await frame.read()
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Invalid JPEG")
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _infer, img, seq_id)
    return JSONResponse(result)


@app.post("/infer_with_viz")
async def infer_with_viz(
    frame: UploadFile = File(...),
    seq_id: int = Form(0),
):
    """Returns keypoints + skeleton-drawn JPEG as base64."""
    if not _ready:
        raise HTTPException(503, _init_error or "Model loading...")
    raw = await frame.read()
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "Invalid JPEG")

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _infer, img, seq_id)

    viz = _draw_skeleton(img, result["keypoints"])
    _, enc = cv2.imencode(".jpg", viz, [cv2.IMWRITE_JPEG_QUALITY, 80])
    result["viz_jpeg_b64"] = base64.b64encode(enc.tobytes()).decode()
    return JSONResponse(result)


if __name__ == "__main__":
    uvicorn.run("rtmpose_http_server:app", host="0.0.0.0", port=PORT,
                workers=1, log_level="info")
