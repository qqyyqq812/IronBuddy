import os
import sys
from rknn.api import RKNN

if __name__ == '__main__':
    # ==========================
    # 配置参数区
    # ==========================
    ONNX_MODEL = 'rtmpose_s_simcc.onnx'      # 你的 ONNX 模型名
    RKNN_MODEL = 'rtmpose_quant_int8.rknn'   # 输出的 RKNN (INT8) 模型名
    DATASET = './dataset.txt'                # 包含预热量化图片的文本文件列表
    TARGET_PLATFORM = 'rk3399pro'            # 锁定目标板子为 RK3399Pro (NPU V1)

    # 1. 创建 RKNN 对象
    rknn = RKNN(verbose=True)

    # 2. 预处理设置
    # 【总经理特殊裁决】：开启激进的 INT8 压缩以追求最大帧率
    print('--> 正在配置构建参数 (INT8 高画质妥协模式)...')
    # mean_values 和 std_values 需要根据 MMPose 的具体预处理进行对应配置
    # 常规 ImageNet/COCO 预处理通常为 mean=[123.675, 116.28, 103.53], std=[58.395, 57.12, 57.375]
    rknn.config(
        channel_mean_value='123.675 116.28 103.53 58.395',
        reorder_channel='0 1 2', # 从 OpenCV BGR 装换网络所需的 RGB
        target_platform=TARGET_PLATFORM
    )

    # 3. 导入 ONNX 模型
    print('--> 正在导入 ONNX 结构...')
    ret = rknn.load_onnx(model=ONNX_MODEL)
    if ret != 0:
        print('加载 ONNX 模型失败! 请确保该网络剔除了不支持的动态控制流。')
        sys.exit(ret)

    # 4. 交叉编译与量化为 INT8
    print('--> 正在编译与执行极尽压缩策略 (INT8)...')
    # do_quantization=True 会启用量化，前提必须有 dataset 喂图做校准
    if not os.path.exists(DATASET):
        print(f"致命警告: 找不到校准集文件 {DATASET}! ")
        print("为了 INT8 量化，请必须创建一个 dataset.txt，每行写上用于让机器找感觉图片的本地相对路径。")
        sys.exit(-1)

    ret = rknn.build(do_quantization=True, dataset=DATASET, pre_compile=True)
    if ret != 0:
        print('构建 RKNN 模型失败！注意查看上方是否发生了算子不支持报错。')
        sys.exit(ret)

    # 5. 导出模型保存
    print('--> 正在导出并保存为固封的 .rknn ...')
    ret = rknn.export_rknn(RKNN_MODEL)
    if ret != 0:
        print('导出模型失败！')
        sys.exit(ret)

    print('完成! 炼丹结束，您的 INT8 模型此时已可放入主板跑圈。')
    rknn.release()
