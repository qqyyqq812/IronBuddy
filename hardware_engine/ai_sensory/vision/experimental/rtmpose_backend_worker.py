#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ----------------------------------------------------------------------------
# 脚本：rtmpose_backend_worker.py (测试挂件)
# 归属：Agent 2 后台托管生成 / experimental 特区
# 架构：Rockchip RK3399Pro (NPU v1) + RTMPose SimCC 
# 说明：此模块独立运行，不干扰 IronBuddy 原有框架，供环境测试使用。
# ----------------------------------------------------------------------------

import cv2
import time
import numpy as np
import sys

# 兼容 RKNN V1 (RK3399Pro) 的 Python 推理包
try:
    from rknn.api import RKNN
except ImportError:
    print("WARNING: 未检测到 rknn.api，如果在非 RK3399 开发板上则无法推理！")

class RTMPoseWorker:
    def __init__(self, model_path: str = "rtmpose_quant.rknn"):
        self.model_path = model_path
        self.rknn = None
        self._init_npu()

    def _init_npu(self):
        """初始化 Rockchip NPU 并装载模型"""
        print(f"[RTMPoseWorker] 初始化 RK3399Pro NPU 环境并加载 {self.model_path} ...")
        self.rknn = RKNN()
        
        # 加载 RKNN 模型
        ret = self.rknn.load_rknn(self.model_path)
        if ret != 0:
            print("[RTMPoseWorker] 模型加载失败，请检查 .rknn 格式合法性！")
            return
            
        # 初始化运行环境
        ret = self.rknn.init_runtime()
        if ret != 0:
            print("[RTMPoseWorker] NPU 运行时初始化失败！")
            return
            
        print("[RTMPoseWorker] 环境启动成功，RKNN Runtime Ready.")

    def preprocess(self, img_bgr: np.ndarray, target_size=(256, 192)):
        """图像前处理 (依赖具体 Top-Down 传入的裁剪框)"""
        # 注意: 实际工程中这里需要是经由目标检测器输出的检测框(Bbox)截图
        img_resized = cv2.resize(img_bgr, target_size)
        img_rgb = cv2.cvtColor(img_resized, cv2.COLOR_BGR2RGB)
        return img_rgb

    def simcc_decode(self, simcc_x: np.ndarray, simcc_y: np.ndarray, simcc_split_ratio: float = 2.0) -> np.ndarray:
        """纯 Numpy 张量后处理：剥离自 Agent 2 技术验证"""
        # (1, 17, W), (1, 17, H) -> (17, W), (17, H)
        simcc_x = np.squeeze(simcc_x, axis=0) if simcc_x.ndim == 3 else simcc_x
        simcc_y = np.squeeze(simcc_y, axis=0) if simcc_y.ndim == 3 else simcc_y
        
        # Argmax 获取离散坐标
        x_locs = np.argmax(simcc_x, axis=1)
        y_locs = np.argmax(simcc_y, axis=1)
        
        # 提取置信度 (Confidence)
        scores = (np.max(simcc_x, axis=1) + np.max(simcc_y, axis=1)) / 2.0  
        
        # 将 bin index 转为相对于 input_size 的浮点坐标
        pred_x = x_locs / simcc_split_ratio
        pred_y = y_locs / simcc_split_ratio
        
        keypoints = np.stack((pred_x, pred_y, scores), axis=1)
        return keypoints

    def inference(self, img_bgr: np.ndarray):
        """单张图像端到端推理"""
        if self.rknn is None:
            raise RuntimeError("RKNN Runtime not initialized!")

        input_tensor = self.preprocess(img_bgr)
        
        t0 = time.time()
        # [推理] 丢给 NPU，等待硬汉打桩机出结果
        outputs = self.rknn.inference(inputs=[input_tensor])
        
        # 假设 outputs 即为 [simcc_x, simcc_y] 两个 1D Tensor
        if len(outputs) == 2:
            keypoints = self.simcc_decode(outputs[0], outputs[1])
        else:
            print(f"[RTMPoseWorker] 警告：模型输出通道不符合预期的 2 路分类输出，实际收到 {len(outputs)} 路。")
            keypoints = np.zeros((17, 3))

        t_cost = (time.time() - t0) * 1000
        print(f"[RTMPoseWorker] 单帧推理解读完成，张量流耗时: {t_cost:.2f} ms")
        return keypoints

    def release(self):
        if self.rknn:
            self.rknn.release()
            print("[RTMPoseWorker] NPU 硬件资源已释放。")

if __name__ == "__main__":
    print("=== RTMPose 驻波并行测试沙坑启动 ===")
    worker = RTMPoseWorker("dummy_test.rknn")
    
    # 构建一张随机噪声图模拟摄像头一帧
    dummy_img = np.random.randint(0, 255, (256, 192, 3), dtype=np.uint8)
    
    try:
        # 这个由于无模型实体，inference必然报错或跳过。此处主要校验语构合法性。
        # worker.inference(dummy_img) 
        pass
    finally:
        worker.release()
