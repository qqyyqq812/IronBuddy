#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local_yolo_pose.py
──────────────────
Local YOLOv5-Pose RKNN inference for IronBuddy.

Loads a YOLOv5-style pose model (.rknn) via rknnlite and runs
on-device NPU inference. Outputs 17 COCO keypoints per detected person.

Model: pose-5s6-640-uint8.rknn (4-head P3/P4/P5/P6, 640x640 input)
Output per head: [1, 171, H, W] where 171 = 3 * (5 + 1 + 17*3)

Compatible with Python 3.7+ (no walrus operator, no X|None syntax).
"""

import math
import numpy as np
import cv2

# Type hint compat for Python 3.7
try:
    from typing import List, Tuple, Optional
except ImportError:
    pass


# ── Model constants ───────────────────────────────────────────────────────────
INPUT_SIZE = 640
NUM_KPT = 17
NUM_CLASS = 1
NUM_ANCHOR = 3
# per-anchor channel count: 5 (box) + 1 (cls) + 17*3 (kpts) = 57
CHAN_PER_ANCHOR = 5 + NUM_CLASS + NUM_KPT * 3  # 57

# P3/P4/P5/P6 anchors (from the C++ source)
ANCHORS_P3P4P5P6 = [
    [19, 27, 44, 40, 38, 94],       # P3 stride=8
    [96, 68, 86, 152, 180, 137],     # P4 stride=16
    [140, 301, 303, 264, 238, 542],  # P5 stride=32
    [436, 615, 739, 380, 925, 792],  # P6 stride=64
]

# P3/P4/P5 anchors (3-output variant)
ANCHORS_P3P4P5 = [
    [10, 13, 16, 30, 33, 23],
    [30, 61, 62, 45, 59, 119],
    [116, 90, 156, 198, 373, 326],
]

# COCO skeleton for drawing (1-indexed pairs from C++ source)
SKELETON = [
    [16, 14], [14, 12], [17, 15], [15, 13], [12, 13],
    [6, 12], [7, 13], [6, 7], [6, 8], [7, 9],
    [8, 10], [9, 11], [2, 3], [1, 2], [1, 3],
    [2, 4], [3, 5], [4, 6], [5, 7],
]


def _sigmoid(x):
    # type: (np.ndarray) -> np.ndarray
    return 1.0 / (1.0 + np.exp(-np.clip(x, -50, 50)))


def _letterbox(img, target_size=INPUT_SIZE):
    # type: (np.ndarray, int) -> Tuple[np.ndarray, float, int, int]
    """Resize with aspect-ratio preservation + padding. Returns (img, scale, pad_x, pad_y)."""
    h, w = img.shape[:2]
    scale = min(float(target_size) / w, float(target_size) / h)
    new_w = int(w * scale)
    new_h = int(h * scale)
    resized = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    pad_x = (target_size - new_w) // 2
    pad_y = (target_size - new_h) // 2

    canvas = np.full((target_size, target_size, 3), 114, dtype=np.uint8)
    canvas[pad_y:pad_y + new_h, pad_x:pad_x + new_w, :] = resized
    return canvas, scale, pad_x, pad_y


def _decode_head(output, anchors, stride, conf_thresh):
    # type: (np.ndarray, list, int, float) -> list
    """
    Decode one YOLOv5-pose output head.

    output shape: [1, 171, grid_h, grid_w]  (171 = 3 * 57)
    Returns list of (score, bbox_xyxy, kpts_17x3).
    """
    _, c, grid_h, grid_w = output.shape
    grid_len = grid_h * grid_w

    # Reshape to [3, 57, grid_h, grid_w] for easier indexing
    data = output[0].reshape(NUM_ANCHOR, CHAN_PER_ANCHOR, grid_h, grid_w)

    results = []
    for b in range(NUM_ANCHOR):
        anchor_w = anchors[b * 2]
        anchor_h = anchors[b * 2 + 1]
        block = data[b]  # [57, grid_h, grid_w]

        # Object confidence
        obj_conf = _sigmoid(block[4])  # [grid_h, grid_w]
        cls_conf = _sigmoid(block[5])  # [grid_h, grid_w]
        scores = obj_conf * cls_conf

        # Find cells above threshold
        mask = scores >= conf_thresh
        if not np.any(mask):
            continue

        ys, xs = np.where(mask)
        for idx in range(len(ys)):
            i = ys[idx]
            j = xs[idx]
            score = float(scores[i, j])

            # Decode bbox
            cx = (_sigmoid(block[0, i, j]) * 2.0 - 0.5 + j) * stride
            cy = (_sigmoid(block[1, i, j]) * 2.0 - 0.5 + i) * stride
            bw = (_sigmoid(block[2, i, j]) * 2.0) ** 2 * anchor_w
            bh = (_sigmoid(block[3, i, j]) * 2.0) ** 2 * anchor_h
            x1 = cx - bw / 2.0
            y1 = cy - bh / 2.0
            x2 = cx + bw / 2.0
            y2 = cy + bh / 2.0

            # Decode 17 keypoints (xy: raw * 2 - 0.5, NO sigmoid; conf: sigmoid)
            kpts = []
            for k in range(NUM_KPT):
                kx = (block[6 + k * 3, i, j] * 2.0 - 0.5 + j) * stride
                ky = (block[6 + k * 3 + 1, i, j] * 2.0 - 0.5 + i) * stride
                kc = float(_sigmoid(block[6 + k * 3 + 2, i, j]))
                kpts.append([kx, ky, kc])

            results.append((score, [x1, y1, x2, y2], kpts))

    return results


def _nms(detections, iou_thresh=0.45):
    # type: (list, float) -> list
    """Simple NMS on list of (score, bbox_xyxy, kpts)."""
    if not detections:
        return []

    # Sort by score descending
    detections = sorted(detections, key=lambda d: d[0], reverse=True)
    keep = []

    while detections:
        best = detections.pop(0)
        keep.append(best)
        remaining = []
        bx1, by1, bx2, by2 = best[1]
        for det in detections:
            dx1, dy1, dx2, dy2 = det[1]
            # IoU
            ix1 = max(bx1, dx1)
            iy1 = max(by1, dy1)
            ix2 = min(bx2, dx2)
            iy2 = min(by2, dy2)
            inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
            area_b = (bx2 - bx1) * (by2 - by1)
            area_d = (dx2 - dx1) * (dy2 - dy1)
            union = area_b + area_d - inter
            iou = inter / union if union > 0 else 0
            if iou < iou_thresh:
                remaining.append(det)
        detections = remaining

    return keep


class LocalYoloPose(object):
    """
    YOLOv5-Pose RKNN local inference engine.

    Usage:
        engine = LocalYoloPose("/path/to/pose-5s6-640-uint8.rknn")
        kpts_17x3 = engine.infer(bgr_frame)  # returns [[x,y,conf], ...] x17
        engine.release()
    """

    def __init__(self, model_path, conf_thresh=0.35, nms_thresh=0.45):
        # type: (str, float, float) -> None
        self._model_path = model_path
        self._conf_thresh = conf_thresh
        self._nms_thresh = nms_thresh
        self._rknn = None
        self._num_outputs = 0
        self._anchors = None
        self._available = None

    def _try_init(self):
        # type: () -> bool
        if self._available is not None:
            return self._available
        try:
            from rknnlite.api import RKNNLite
            rknn = RKNNLite()
            ret = rknn.load_rknn(self._model_path)
            if ret != 0:
                print("[LocalYoloPose] Failed to load model: {}".format(self._model_path))
                self._available = False
                return False
            ret = rknn.init_runtime()
            if ret != 0:
                print("[LocalYoloPose] Failed to init runtime")
                self._available = False
                return False
            self._rknn = rknn

            # Probe output count to select anchors
            dummy = np.zeros((INPUT_SIZE, INPUT_SIZE, 3), dtype=np.uint8)
            outs = rknn.inference(inputs=[dummy])
            self._num_outputs = len(outs)
            if self._num_outputs == 4:
                self._anchors = ANCHORS_P3P4P5P6
            elif self._num_outputs == 3:
                self._anchors = ANCHORS_P3P4P5
            else:
                print("[LocalYoloPose] Unexpected output count: {}".format(self._num_outputs))
                self._available = False
                return False

            print("[LocalYoloPose] Model loaded: {} outputs, anchors selected".format(self._num_outputs))
            self._available = True
            return True

        except Exception as e:
            print("[LocalYoloPose] Init failed: {}".format(e))
            self._available = False
            return False

    def infer(self, frame):
        # type: (np.ndarray) -> list
        """
        Run inference on a BGR frame.
        Returns [[x, y, conf], ...] x 17 in original frame coordinates.
        Returns zeros if no person detected or init fails.
        """
        if not self._try_init():
            return [[0.0, 0.0, 0.0]] * NUM_KPT

        orig_h, orig_w = frame.shape[:2]

        # Letterbox
        img_lb, scale, pad_x, pad_y = _letterbox(frame, INPUT_SIZE)

        # RKNN inference (expects HWC uint8)
        try:
            outputs = self._rknn.inference(inputs=[img_lb])
        except Exception as e:
            print("[LocalYoloPose] Inference error: {}".format(e))
            return [[0.0, 0.0, 0.0]] * NUM_KPT

        # Strides for each head
        strides = [8, 16, 32, 64] if self._num_outputs == 4 else [8, 16, 32]

        # Decode all heads
        all_dets = []
        for head_idx in range(self._num_outputs):
            dets = _decode_head(
                outputs[head_idx],
                self._anchors[head_idx],
                strides[head_idx],
                self._conf_thresh,
            )
            all_dets.extend(dets)

        # NMS
        kept = _nms(all_dets, self._nms_thresh)

        if not kept:
            return [[0.0, 0.0, 0.0]] * NUM_KPT

        # Pick best detection
        best = max(kept, key=lambda d: d[0])
        _, _, kpts = best

        # Map keypoints from letterboxed coords back to original frame
        result = []
        for kx, ky, kc in kpts:
            orig_x = (kx - pad_x) / scale
            orig_y = (ky - pad_y) / scale
            # Clamp to frame bounds
            orig_x = max(0.0, min(float(orig_w), orig_x))
            orig_y = max(0.0, min(float(orig_h), orig_y))
            result.append([orig_x, orig_y, kc])

        return result

    def infer_multi(self, frame):
        # type: (np.ndarray) -> Tuple[list, list]
        """
        Run inference returning all detected persons.
        Returns (scores, kpts_list) where kpts_list is list of [[x,y,conf]x17].
        """
        if not self._try_init():
            return [], []

        orig_h, orig_w = frame.shape[:2]
        img_lb, scale, pad_x, pad_y = _letterbox(frame, INPUT_SIZE)

        try:
            outputs = self._rknn.inference(inputs=[img_lb])
        except Exception as e:
            print("[LocalYoloPose] Inference error: {}".format(e))
            return [], []

        strides = [8, 16, 32, 64] if self._num_outputs == 4 else [8, 16, 32]
        all_dets = []
        for head_idx in range(self._num_outputs):
            dets = _decode_head(
                outputs[head_idx],
                self._anchors[head_idx],
                strides[head_idx],
                self._conf_thresh,
            )
            all_dets.extend(dets)

        kept = _nms(all_dets, self._nms_thresh)
        if not kept:
            return [], []

        scores = []
        kpts_list = []
        for score, _, kpts in kept:
            mapped = []
            for kx, ky, kc in kpts:
                ox = max(0.0, min(float(orig_w), (kx - pad_x) / scale))
                oy = max(0.0, min(float(orig_h), (ky - pad_y) / scale))
                mapped.append([ox, oy, kc])
            scores.append(score)
            kpts_list.append(mapped)

        return scores, kpts_list

    def release(self):
        # type: () -> None
        if self._rknn is not None:
            try:
                self._rknn.release()
            except Exception:
                pass
            self._rknn = None
            self._available = None
