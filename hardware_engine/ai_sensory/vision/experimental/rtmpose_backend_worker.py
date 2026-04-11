#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# 脚本：rtmpose_backend_worker.py (异构测试版)
# 归属：Agent 2 后台托管生成 / experimental 特区
# 架构：Rockchip RK3399Pro NPU (骨干网络) + ARM CPU ONNXRuntime (SimCC注意力头)
# 说明：此模块解决了 RKNN API 1.6 对 BatchMatMulV2 的量化兼容故障，
#      将 SimCC 一维解码放到了 CPU 执行！
# ----------------------------------------------------------------------------

import cv2
import time
import numpy as np
import os
import sys

# 兼容 RKNN V1 (RK3399Pro) 的 Python 推理包
try:
    from rknnlite.api import RKNNLite as RKNN
except ImportError:
    try:
        from rknn.api import RKNN
    except ImportError:
        print("WARNING: 未检测到 rknn.api 或 rknnlite.api！")
        RKNN = None

try:
    import onnxruntime as ort
except ImportError:
    print("WARNING: 未检测到 onnxruntime，CPU 异构推理可能受限！")

class RTMPoseWorker:
    def __init__(self, model_path: str = "rtmpose_quant.rknn"):
        self.model_path = model_path
        self.rknn = None
        self.ort_session = None
        
        # 加载 NPU 半身
        self._init_npu()
        
        # 加载 CPU 半身
        self._init_cpu_head()

    def _init_npu(self):
        """初始化 Rockchip NPU 并装载骨干模型"""
        print(f"[RTMPoseWorker] 初始化 RK3399Pro NPU 环境并加载 {self.model_path} ...")
        self.rknn = RKNN()
        
        ret = self.rknn.load_rknn(self.model_path)
        if ret != 0:
            print("[RTMPoseWorker] 模型加载失败，请检查 .rknn 格式合法性！")
            return
            
        ret = self.rknn.init_runtime()
        if ret != 0:
            print("[RTMPoseWorker] NPU 运行时初始化失败！")
            return
            
        print("[RTMPoseWorker] 环境启动成功，RKNN Runtime Ready.")

    def _init_cpu_head(self):
        """初始化基于 ONNXRuntime 的解析头"""
        base_dir = os.path.dirname(os.path.abspath(__file__))
        head_path = os.path.join(base_dir, "rtmpose_head.onnx")
        print(f"[RTMPoseWorker] 初始化 CPU 异构解析器并加载 {head_path} ...")
        try:
            self.ort_session = ort.InferenceSession(head_path, providers=['CPUExecutionProvider'])
            print("[RTMPoseWorker] CPU ONNXRuntime 启动成功！")
        except Exception as e:
            print(f"[RTMPoseWorker] CPU 头加载失败: {e}")
            self.ort_session = None

    def preprocess(self, img_bgr: np.ndarray, target_size=(192, 256)):
        """图像前处理 (依靠具体 Top-Down 传入的裁剪框)"""
        img_resized = cv2.resize(img_bgr, target_size)
        img_rgb = cv2.cvtColor(img_resized, cv2.COLOR_BGR2RGB).astype(np.float32)
        
        # 手动执行 ImageNet 标准化 (因为开启取消量化的 FP16/FP32 纯净版模型，RKNN 底层不包裹归一化层)
        mean = np.array([123.675, 116.28, 103.53], dtype=np.float32)
        std = np.array([58.395, 57.12, 57.375], dtype=np.float32)
        img_norm = (img_rgb - mean) / std
        
        # 严格遵守 NPU (pass_through) 脱壳编译要求，强制送入 NCHW 规格防错乱
        img_nchw = np.transpose(img_norm, (2, 0, 1))
        img_input = np.expand_dims(img_nchw, axis=0)
        
        return img_input

    def simcc_decode(self, simcc_x: np.ndarray, simcc_y: np.ndarray, simcc_split_ratio: float = 2.0) -> np.ndarray:
        """纯 Numpy 张量后处理：获取关键点空间坐标"""
        simcc_x = np.squeeze(simcc_x, axis=0) if simcc_x.ndim == 3 else simcc_x
        simcc_y = np.squeeze(simcc_y, axis=0) if simcc_y.ndim == 3 else simcc_y
        
        # 移除灾难性的二次 Softmax 滤波（解决极大值崩落导致无法画骨架线的问题）
        x_locs = np.argmax(simcc_x, axis=1)
        y_locs = np.argmax(simcc_y, axis=1)
        
        scores = (np.max(simcc_x, axis=1) + np.max(simcc_y, axis=1)) / 2.0  
        
        pred_x = x_locs / simcc_split_ratio
        pred_y = y_locs / simcc_split_ratio
        
        keypoints = np.stack((pred_x, pred_y, scores), axis=1)
        return keypoints

    def inference(self, img_bgr: np.ndarray):
        """混合端到端推理 (NPU -> CPU)"""
        if self.rknn is None:
            raise RuntimeError("RKNN Runtime not initialized!")

        input_tensor = self.preprocess(img_bgr)
        
        t0 = time.time()
        
        # 1. [前向卷积骨干] NPU 执行
        rknn_outputs = self.rknn.inference(inputs=[input_tensor])
        
        if not rknn_outputs or len(rknn_outputs) == 0:
            print("[RTMPoseWorker] NPU 吐出空块，可能死机。")
            return np.zeros((17, 3))
            
        features = rknn_outputs[0]
        t_npu = (time.time() - t0) * 1000
        
        # 2. [SimCC 空间注意力解码] CPU 执行
        if self.ort_session is not None:
            ort_inputs = {self.ort_session.get_inputs()[0].name: features.astype(np.float32)}
            simcc_x, simcc_y = self.ort_session.run(None, ort_inputs)
            keypoints = self.simcc_decode(simcc_x, simcc_y)
        else:
            print("[RTMPoseWorker] ORT Session 为空，跳过解析头。")
            keypoints = np.zeros((17, 3))

        t_cost = (time.time() - t0) * 1000
        t_cpu = t_cost - t_npu
        # print(f"[RTMPoseWorker] 推理完成 | 总计: {t_cost:.2f}ms (NPU: {t_npu:.2f}ms, CPU: {t_cpu:.2f}ms)")
        return keypoints

    def release(self):
        if self.rknn:
            self.rknn.release()
            print("[RTMPoseWorker] 混合计算节点已释放。")
