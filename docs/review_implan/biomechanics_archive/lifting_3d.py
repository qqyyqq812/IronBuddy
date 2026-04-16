#!/usr/bin/env python3
"""
IronBuddy V2 — 2D→3D 姿态提升模块
基于 VideoPose3D (Facebook Research) 预训练权重，ONNX Runtime CPU 推理。

用法:
    from biomechanics.lifting_3d import Lifting3D
    lifter = Lifting3D("/path/to/videopose3d_243f_causal.onnx")
    kpts_3d = lifter.update(kpts_2d)  # kpts_2d: (17, 2) normalized
"""
import numpy as np
import time
import logging
from collections import deque

logging.basicConfig(level=logging.INFO, format='%(asctime)s - [LIFT3D] - %(message)s')


class Lifting3D:
    """VideoPose3D causal lifting: 2D关键点序列 → 3D关键点"""

    def __init__(self, model_path, num_frames=243):
        """
        Args:
            model_path: ONNX 模型路径
            num_frames: receptive field 帧数 (与导出时一致, 默认 243)
        """
        self.num_frames = num_frames
        self.buffer = deque(maxlen=num_frames)
        self._session = None
        self._model_path = model_path
        self._input_name = None
        self._warmup_logged = False
        self._perf_times = deque(maxlen=50)

        # 延迟加载 (板端启动时减少阻塞)
        self._loaded = False

    def _lazy_load(self):
        """按需加载 ONNX Runtime session"""
        if self._loaded:
            return True
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 2  # RK3399 双核 A72
            opts.inter_op_num_threads = 1
            self._session = ort.InferenceSession(
                self._model_path, sess_options=opts,
                providers=['CPUExecutionProvider']
            )
            self._input_name = self._session.get_inputs()[0].name
            self._loaded = True
            logging.info(f"ONNX 模型加载成功: {self._model_path}")
            logging.info(f"Receptive field: {self.num_frames} frames")
            return True
        except Exception as e:
            logging.error(f"ONNX 模型加载失败: {e}")
            return False

    def update(self, kpts_2d):
        """
        推入一帧 2D 关键点, 返回 3D 坐标 (或 None 如果帧不足)

        Args:
            kpts_2d: np.ndarray, shape (17, 2), 归一化坐标 (像素坐标 / 图像尺寸 - 0.5)

        Returns:
            np.ndarray shape (17, 3) 或 None (帧不足 / 加载失败)
        """
        if not self._lazy_load():
            return None

        self.buffer.append(kpts_2d.astype(np.float32))

        if len(self.buffer) < self.num_frames:
            if not self._warmup_logged and len(self.buffer) % 50 == 0:
                logging.info(f"3D Lifting 缓冲中: {len(self.buffer)}/{self.num_frames}")
            return None

        if not self._warmup_logged:
            self._warmup_logged = True
            logging.info(f"3D Lifting 缓冲已满, 开始推理")

        # 构建输入: (1, num_frames, 17, 2)
        input_arr = np.array(list(self.buffer), dtype=np.float32)[np.newaxis]

        t0 = time.perf_counter()
        outputs = self._session.run(None, {self._input_name: input_arr})
        dt = time.perf_counter() - t0
        self._perf_times.append(dt * 1000)

        # 输出: (1, 1, 17, 3) → (17, 3)
        return outputs[0][0, 0]

    @property
    def avg_inference_ms(self):
        if not self._perf_times:
            return 0
        return sum(self._perf_times) / len(self._perf_times)

    @property
    def is_ready(self):
        return len(self.buffer) >= self.num_frames


# ========== 独立测试入口 ==========
if __name__ == "__main__":
    import sys
    import os

    model_path = sys.argv[1] if len(sys.argv) > 1 else "/home/toybrick/biomechanics/checkpoints/videopose3d_243f_causal.onnx"

    if not os.path.exists(model_path):
        print(f"模型文件不存在: {model_path}")
        sys.exit(1)

    lifter = Lifting3D(model_path)
    print(f"开始性能测试, receptive field = {lifter.num_frames} 帧")

    # 模拟填充缓冲区
    for i in range(lifter.num_frames):
        fake_2d = np.random.randn(17, 2).astype(np.float32) * 0.3
        result = lifter.update(fake_2d)
        if result is not None:
            print(f"  缓冲满, 首次推理完成")

    # 连续推理测试
    times = []
    for i in range(30):
        fake_2d = np.random.randn(17, 2).astype(np.float32) * 0.3
        t0 = time.perf_counter()
        result = lifter.update(fake_2d)
        dt = (time.perf_counter() - t0) * 1000
        times.append(dt)

    avg = np.mean(times[5:])  # 跳过前 5 次 warmup
    print(f"\n=== 板端 CPU 性能 ===")
    print(f"  平均推理: {avg:.1f} ms")
    print(f"  最大推理: {max(times[5:]):.1f} ms")
    print(f"  最小推理: {min(times[5:]):.1f} ms")
    print(f"  输出维度: {result.shape}")
    print(f"  输出样本: {result[0]}")  # 第一个关键点的 3D 坐标
