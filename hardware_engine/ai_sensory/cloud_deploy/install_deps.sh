#!/usr/bin/env bash
# install_deps.sh – run this ONCE on the AutoDL cloud server.
# Uses ONNX Runtime GPU (no mmpose/mmcv needed).

set -e
export PATH=/root/miniconda3/bin:$PATH

echo "=== IronBuddy Cloud Dependency Installer (ONNX Runtime) ==="

TORCH_VER=$(python -c "import torch; print(torch.__version__)")
CUDA_VER=$(python -c "import torch; print(torch.version.cuda)")
echo "Detected: torch=$TORCH_VER  cuda=$CUDA_VER"

echo "--- Installing server dependencies ---"
pip install --quiet fastapi uvicorn python-multipart onnxruntime-gpu opencv-python-headless

echo "--- Verifying ONNX Runtime GPU ---"
python -c "
import onnxruntime as ort
providers = ort.get_available_providers()
print(f'ONNX Runtime providers: {providers}')
assert 'CUDAExecutionProvider' in providers, 'CUDA not available!'
print('GPU inference ready.')
"

echo "--- Downloading RTMPose-m ONNX model ---"
DEST=/root/ironbuddy_cloud
mkdir -p $DEST
cd $DEST

if [ ! -f "rtmpose_m.onnx" ]; then
    MODEL_URL="https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/rtmpose-m_simcc-body7_pt-body7_420e-256x192-e48f03d0_20230504.zip"
    echo "Downloading RTMPose-m ONNX..."
    wget -q --show-progress -O rtmpose_m.zip "$MODEL_URL"
    unzip -o rtmpose_m.zip
    cp 20230831/rtmpose_onnx/rtmpose-m_*/end2end.onnx rtmpose_m.onnx
    rm -rf 20230831 rtmpose_m.zip
    echo "Model saved to $DEST/rtmpose_m.onnx"
else
    echo "Model already exists: $DEST/rtmpose_m.onnx"
fi

echo "--- Verifying model load ---"
python -c "
import onnxruntime as ort, numpy as np
s = ort.InferenceSession('rtmpose_m.onnx', providers=['CUDAExecutionProvider'])
dummy = np.zeros((1,3,256,192), dtype=np.float32)
s.run(None, {'input': dummy})
print('Model loaded and verified on GPU!')
"

echo "=== All dependencies installed successfully ==="
