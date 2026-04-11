#!/bin/bash
set -e

export PATH="/home/qq/miniforge3/bin:$PATH"
source /home/qq/miniforge3/etc/profile.d/conda.sh

cd /home/qq/projects/embedded-fullstack/tools/
conda activate rknn_env_py38

echo "Fixing PyTorch dependency block..."
pip install torch==1.10.0+cpu torchvision==0.11.1+cpu -f https://download.pytorch.org/whl/cpu/torch_stable.html -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "Fixing tensorflow/mxnet..."
pip install tensorflow==2.2.0 mxnet==1.5.0 -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "Installing RKNN Toolkit Wheel..."
pip install rknn-toolkit_source/packages/rknn_toolkit-1.7.5-cp38-cp38-linux_x86_64.whl

echo "Executing the conversion script..."
python convert_rtmpose.py

echo "Conversion process complete."
