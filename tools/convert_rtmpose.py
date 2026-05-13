#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 该脚本必须在具备 rknn-toolkit1 (Rockchip RK3399Pro专用) 的环境内运行
# 例如通过 Rockchip 官方提供的 docker 或者专门的 Python 3.6 conda 环境

import os
import sys

try:
    from rknn.api import RKNN
except ImportError:
    print("错误：未检测到 rknn.api 包。")
    print("本转换脚本必须在安装了 rknn-toolkit 的宿主环境（PC 或 WSL）下运行。")
    sys.exit(1)

def convert_rtmpose(onnx_model_path, rknn_model_path):
    # 建立 RKNN 对象
    rknn = RKNN()

    print("[1] 正在配置模型构建参数...")
    # 配置模型参数
    # 因为 rtmpose-m 主要是全卷积，不需要特殊的 channel_mean_value（保持图片预处理传入 0~255 然后再前处理即可）
    # 或者如果你希望在 NPU 内做归一化(除以255)，可以指定 channel_mean_value='0 0 0 255' 等等。
    # 这里我们采用对等输入输出的通用设置。
    rknn.config(
        mean_values=[[123.675, 116.28, 103.53]],
        std_values=[[58.395, 57.12, 57.375]],
        target_platform='rk3399pro',
        optimization_level=0
    )

    print(f"[2] 正在加载 ONNX 模型: {onnx_model_path} ...")
    # 加载 ONNX 
    ret = rknn.load_onnx(model=onnx_model_path)
    if ret != 0:
        print("ONNX 加载失败，请检查模型文件是否损坏！")
        return

    print("[3] 正在构建与融合图引擎拓扑...")
    # 关闭量化！使用 FP16 高精度直接跑骨干网络！
    ret = rknn.build(do_quantization=False, dataset='./dataset.txt')
    if ret != 0:
        print("构建失败，可能是存在不支持的算子（但按理说截断 SimCC 后此步应该顺畅通过）")
        return

    print(f"[4] 正在导出实体 .rknn 文件到: {rknn_model_path}")
    ret = rknn.export_rknn(rknn_model_path)
    if ret != 0:
        print("导出 RKNN 文件失败！")
        return

    print(f"✅ 转换大功告成！生成的 {rknn_model_path} 可以直接推送到板子上了！")
    
    rknn.release()

if __name__ == '__main__':
    if not os.path.exists("./rtmpose_backbone.onnx"):
        print("💡 提示：运行本脚本前，请确保同目录下存在 rtmpose_backbone.onnx 模型文件。")
        sys.exit(1)
        
    convert_rtmpose("./rtmpose_backbone.onnx", "../hardware_engine/ai_sensory/vision/experimental/rtmpose_quant.rknn")
