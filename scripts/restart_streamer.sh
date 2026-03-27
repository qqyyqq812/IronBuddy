#!/bin/bash
echo "Killing all python3 instances..."
pkill -9 -f python3
sleep 2

echo "Checking if video5 is free:"
lsof /dev/video5

echo "Starting streamer app..."
cd /home/toybrick/streamer
# 极客级：绕过板卡老旧依赖，使用高版 CXX11 引擎强行拉起 Vosk ASR
export LD_PRELOAD=/home/toybrick/streamer/libstdc++.so.6.0.28
nohup python3 streamer_app.py > streamer.log 2>&1 < /dev/null &
echo "Done."
exit 0
