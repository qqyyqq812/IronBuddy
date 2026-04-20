#!/bin/bash
# 一键回退：删 sentinel + 重启 streamer, UI 探测面板自动隐藏
# /api/probe/* 接口仍在但返回 403 (enabled=false)

set -e

BOARD_IP="${BOARD_IP:-10.18.76.224}"
BOARD_USER="${BOARD_USER:-toybrick}"
BOARD_KEY="$HOME/.ssh/id_rsa_toybrick"
SSH_OPTS="-i ${BOARD_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=5"

ssh ${SSH_OPTS} "${BOARD_USER}@${BOARD_IP}" bash -s <<'EOF'
set -e
# 1) 杀 probe 进程
pkill -f '[v]ision_feature_probe_v2.py' 2>/dev/null || true
rm -f /dev/shm/probe_v2.pid /dev/shm/rep_features.jsonl 2>/dev/null || true

# 2) 删 sentinel
rm -f /tmp/IRONBUDDY_PROBE_ENABLED

# 3) 重启 streamer (不带 env)
pkill -f '[s]treamer_app.py' 2>/dev/null || true
sleep 1.5

cd /home/toybrick/streamer_v3
cat > /tmp/_streamer_normal.sh <<'INNER'
#!/bin/bash
cd /home/toybrick/streamer_v3
export PYTHONUNBUFFERED=1
# 注意: 不 source sentinel, probe 功能将被 /api/probe/enabled=false 屏蔽
if [ -f /home/toybrick/streamer_v3/.api_config.json ]; then
    eval "$(python3 -c "import json;c=json.load(open('/home/toybrick/streamer_v3/.api_config.json'));[print(f'export {k}={v!r}') for k,v in c.items() if isinstance(v,str)]")"
fi
exec python3 -u streamer_app.py
INNER
chmod +x /tmp/_streamer_normal.sh
setsid nohup /tmp/_streamer_normal.sh >/tmp/streamer.log 2>&1 < /dev/null &
disown 2>/dev/null || true
sleep 2

if pgrep -f '[s]treamer_app.py' >/dev/null; then
  echo "✓ streamer 已回归正常, PID=$(pgrep -f '[s]treamer_app.py')"
else
  echo "✗ 重启失败, 查 /tmp/streamer.log"
  tail -15 /tmp/streamer.log
  exit 1
fi
EOF

echo ""
echo "==> 验证 /api/probe/enabled"
RESULT=$(curl -s --noproxy '*' --max-time 5 http://${BOARD_IP}:5000/api/probe/enabled)
echo "$RESULT"
echo ""
echo "✅ 探测 UI 已关闭. 硬刷浏览器 (Ctrl+Shift+R), 🎯 视觉特征探测 组自动消失."
echo ""
echo "📝 如需完全清除代码改动 (git 回滚):"
echo "   git diff streamer_app.py templates/index.html   # 查看"
echo "   git checkout streamer_app.py templates/index.html   # 回滚"
echo "   rm tools/vision_feature_probe.py tools/vision_feature_probe_v2.py"
echo "   rm scripts/probe_enable.sh scripts/probe_disable.sh"
echo "   # 板端清 sentinel reader:"
echo "   ssh toybrick 'sed -i \"/PROBE SENTINEL/,/END PROBE SENTINEL/d\" /home/toybrick/streamer_v3/scripts/start_all_services.sh'"
