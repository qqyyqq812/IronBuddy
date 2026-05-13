#!/bin/bash
set -e

echo "Starting conda environment initialization..."
export PATH="/home/qq/miniforge3/bin:$PATH"
source /home/qq/miniforge3/etc/profile.d/conda.sh

# Recreate environment safely
conda create -n rknn_env_py38 python=3.8 -y

cd /home/qq/projects/embedded-fullstack/tools/
if [ ! -d "rknn-toolkit_source" ]; then
    echo "Cloning Rockchip official repository..."
    git clone --depth 1 -b master https://github.com/rockchip-linux/rknn-toolkit.git rknn-toolkit_source
fi

echo "Activating rknn_env_py38..."
conda activate rknn_env_py38

echo "Installing core building tools..."
python -m pip install --upgrade pip setuptools wheel -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "Installing Rockchip requirements for Py3.8..."
pip install -r rknn-toolkit_source/packages/requirements-cpu-ubuntu20.04_py38.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

echo "Installing RKNN Toolkit Wheel..."
pip install rknn-toolkit_source/packages/rknn_toolkit-1.7.5-cp38-cp38-linux_x86_64.whl

echo "Executing the conversion script..."
python convert_rtmpose.py

echo "Conversion process complete."
