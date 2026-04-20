#!/bin/bash
PROJECT_DIR="/home/qq/projects/embedded-fullstack"
cd $PROJECT_DIR

echo "============================================="
echo "    IronBuddy 终极一键启动与推流脚本"
echo "============================================="

echo "[1/4] 将最新核心代码极速覆盖推送给板端 (RK3399Prox)..."
# 推送主文件与配置
rsync -a -v -z -e "ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no" streamer_app.py models/extreme_fusion_gru_curl.pt .api_config.json toybrick@10.18.76.224:/home/toybrick/streamer_v3/
# 推送 UI 与 逻辑核心
rsync -a -v -z -e "ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no" templates/ toybrick@10.18.76.224:/home/toybrick/streamer_v3/templates/
rsync -a -v -z -e "ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no" scripts/ toybrick@10.18.76.224:/home/toybrick/streamer_v3/scripts/
rsync -a -v -z -e "ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no" hardware_engine/ toybrick@10.18.76.224:/home/toybrick/streamer_v3/hardware_engine/
# 板端重启加载新模型
bash scripts/switch_model.sh curl > /dev/null 2>&1

echo "[2/4] 清理遗留老进程..."
pkill -f "streamer_app.py" 2>/dev/null
pkill -f "openclaw_daemon.py" 2>/dev/null
sleep 1

echo "[3/4] 启动飞书后台监控精灵 (OpenClaw Daemon)..."
bash scripts/start_openclaw_daemon.sh

echo "[4/4] 启动本地大屏 UI前台..."
nohup python3 streamer_app.py > /dev/null 2>&1 &
sleep 2

echo "================================================="
echo "🟢 全链路一键启动完成！"
echo "👉 请打开浏览器访问: http://localhost:5000"
echo "================================================="
