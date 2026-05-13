#!/bin/bash
# -----------------------------------------------------------------------------
# 脚本：fetch_materials.sh (策略 A：自动化拉取开源预训练权重与组建校准集)
# 说明：此脚本会去 OpenMMLab 官方服务器下载 RTMPose-s 的 ONNX 原型，并从 COCO 图库
#      下载 3 张真实人类照片组装为 dataset.txt (用于 INT8 PTQ 校准)。
# -----------------------------------------------------------------------------

set -e
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "==== 1. 开始拉取 OpenMMLab 官方 RTMPose-s ONNX 权重 ===="
# 采用 rtmpose-s 规格，平衡速度与精度，附带 simcc
wget -q --show-progress -O rtmpose_s.zip https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/rtmpose-s_simcc-body7_pt-body7_420e-256x192-23f4625d_20230504.zip

echo "==== 2. 解压剥离 .onnx 模型 ===="
unzip -o rtmpose_s.zip -d ./tmp_model
# 寻找里面的 onnx 文件并复制到当前目录
find ./tmp_model -name "*.onnx" -exec mv {} ./rtmpose_s_simcc.onnx \;
rm -rf ./tmp_model rtmpose_s.zip
echo "✅ 模型已就绪: rtmpose_s_simcc.onnx"

echo "==== 3. 开始构建 INT8 量化感知校准集 (Calibration Dataset) ===="
mkdir -p ./calib_images

# 从开源 COCO 数据集临时抽取三张包含人体的照片作为校准集
echo "下载验证图 1..."
wget -q -O ./calib_images/person1.jpg http://images.cocodataset.org/val2017/000000397133.jpg
echo "下载验证图 2..."
wget -q -O ./calib_images/person2.jpg http://images.cocodataset.org/val2017/000000037777.jpg
echo "下载验证图 3..."
wget -q -O ./calib_images/person3.jpg http://images.cocodataset.org/val2017/000000252219.jpg

# 生成量化配置文件 dataset.txt
echo "./calib_images/person1.jpg" > dataset.txt
echo "./calib_images/person2.jpg" >> dataset.txt
echo "./calib_images/person3.jpg" >> dataset.txt
echo "✅ 校准集已就绪: dataset.txt"

echo "=========================================================="
echo "🎯 原材料筹备完毕！"
echo "您现在可以启动 Docker 并在里面执行 python3 convert_rtmpose.py 炼丹了！"
echo "=========================================================="
