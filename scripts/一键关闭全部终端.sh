#!/bin/bash
echo "============================================="
echo "    IronBuddy 终极一键停止脚本"
echo "============================================="
echo "🔴 正在停止本地 UI 与 后台监控服务..."
pkill -f "streamer_app.py"
pkill -f "openclaw_daemon.py"
echo "✅ 已全部关闭。"
