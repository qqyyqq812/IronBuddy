#!/bin/bash
# IronBuddy V2.2 — Vosk 中文离线 ASR 模型部署脚本
# 在板端 (RK3399ProX) 执行

set -e

MODEL_DIR="/home/toybrick/vosk-model-small-cn-0.22"
MODEL_URL="https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip"
ZIP_NAME="/tmp/vosk-model.zip"

echo "===== IronBuddy V2.2 — Vosk 模型部署 ====="

# 1. 安装 vosk Python 包
echo "[1/3] 安装 vosk..."
pip3 install --user vosk 2>/dev/null || pip install --user vosk

# 2. 下载模型（~50MB）
if [ -d "$MODEL_DIR" ]; then
    echo "[2/3] 模型已存在: $MODEL_DIR, 跳过下载"
else
    echo "[2/3] 下载 Vosk 中文小模型 (~50MB)..."
    wget -q --show-progress -O "$ZIP_NAME" "$MODEL_URL"
    echo "  解压中..."
    cd /home/toybrick && unzip -q "$ZIP_NAME"
    rm -f "$ZIP_NAME"
    echo "  ✅ 模型已部署到: $MODEL_DIR"
fi

# 3. 验证
echo "[3/3] 验证..."
python3 -c "
from vosk import Model
m = Model('$MODEL_DIR')
print('✅ Vosk 模型加载成功!')
" && echo "===== 部署完成 =====" || echo "❌ 模型加载失败"
