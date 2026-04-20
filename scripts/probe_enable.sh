#!/bin/bash
# 一键启用视觉特征探测 UI (sentinel 版 · 板端重启后仍生效)
# 原理：
#   1. 推送 probe v2 + streamer_app + index.html 改动到板端
#   2. 在板端 /tmp/IRONBUDDY_PROBE_ENABLED sentinel 文件写一下
#   3. 重启 streamer (start_all_services.sh 已打补丁读 sentinel)
# 关闭：
#   bash scripts/probe_disable.sh  (删 sentinel + 重启 streamer, UI 自动隐藏)

set -e

BOARD_IP="${BOARD_IP:-10.18.76.224}"
BOARD_USER="${BOARD_USER:-toybrick}"
BOARD_KEY="$HOME/.ssh/id_rsa_toybrick"
BOARD_ROOT="/home/toybrick/streamer_v3"
SSH_OPTS="-i ${BOARD_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=5"

echo "==> 同步 probe v2 脚本 + streamer + index.html 改动到板端"
rsync -az --checksum -e "ssh ${SSH_OPTS}" \
  tools/vision_feature_probe_v2.py \
  "${BOARD_USER}@${BOARD_IP}:${BOARD_ROOT}/tools/"
rsync -az --checksum -e "ssh ${SSH_OPTS}" \
  streamer_app.py \
  templates/index.html \
  "${BOARD_USER}@${BOARD_IP}:${BOARD_ROOT}/"

echo "==> 写 sentinel + 幂等打补丁 + 重启 streamer"
ssh ${SSH_OPTS} "${BOARD_USER}@${BOARD_IP}" bash -s <<'EOF'
set -e
# 1) 写 sentinel (tmpfs, 重启后自动消失, 非持久 = 安全)
touch /tmp/IRONBUDDY_PROBE_ENABLED

# 2) 幂等打补丁 start_all_services.sh (板子重启后若被清掉自动补回)
F=/home/toybrick/streamer_v3/scripts/start_all_services.sh
if ! grep -q "IRONBUDDY_PROBE_ENABLED" "$F"; then
  sed -i '/^export LLM_BACKEND=direct$/a\
# --- PROBE SENTINEL (scripts/probe_enable.sh) ---\
[ -f /tmp/IRONBUDDY_PROBE_ENABLED ] && export IRONBUDDY_PROBE_ENABLED=1 && echo "[PROBE] enabled via sentinel" >> "$LOG"\
# --- END PROBE SENTINEL ---' "$F"
  echo "✓ sentinel reader 已补回 $F"
else
  echo "✓ sentinel reader 已存在"
fi

# 3) 优雅重启 streamer: kill → 等 systemd 重拉 OR 用现有脚本的 launch 定义
pkill -f '[s]treamer_app.py' 2>/dev/null || true
sleep 1.5

# 4) 用 start_all_services.sh 里的 launch 语义重新手动起一个, 让 sentinel 被 export 生效
# (systemd 可能不会自动重启, 手动保证)
cd /home/toybrick/streamer_v3
# 直接用 launch helper 的语义: setsid+nohup+export env
cat > /tmp/_streamer_probe.sh <<'INNER'
#!/bin/bash
cd /home/toybrick/streamer_v3
export PYTHONUNBUFFERED=1
[ -f /tmp/IRONBUDDY_PROBE_ENABLED ] && export IRONBUDDY_PROBE_ENABLED=1
# 加载 API keys (如果有)
if [ -f /home/toybrick/streamer_v3/.api_config.json ]; then
    eval "$(python3 -c "import json;c=json.load(open('/home/toybrick/streamer_v3/.api_config.json'));[print(f'export {k}={v!r}') for k,v in c.items() if isinstance(v,str)]")"
fi
exec python3 -u streamer_app.py
INNER
chmod +x /tmp/_streamer_probe.sh
setsid nohup /tmp/_streamer_probe.sh >/tmp/streamer.log 2>&1 < /dev/null &
disown 2>/dev/null || true
sleep 2.5

# 5) 验证
if pgrep -f '[s]treamer_app.py' >/dev/null; then
  PID=$(pgrep -f '[s]treamer_app.py' | head -1)
  # 验证环境变量确实生效
  if grep -q IRONBUDDY_PROBE_ENABLED /proc/$PID/environ 2>/dev/null; then
    echo "✓ streamer PID=$PID 带 IRONBUDDY_PROBE_ENABLED=1 启动"
  else
    echo "⚠ streamer PID=$PID 起来了但 env 未注入, 看 /tmp/streamer.log"
    tail -10 /tmp/streamer.log
    exit 1
  fi
else
  echo "✗ streamer 未起, 看 /tmp/streamer.log"
  tail -15 /tmp/streamer.log
  exit 1
fi
EOF

echo ""
echo "==> 验证 /api/probe/enabled"
RESULT=$(curl -s --noproxy '*' --max-time 5 http://${BOARD_IP}:5000/api/probe/enabled)
echo "$RESULT"
if [[ "$RESULT" == *'"enabled": true'* ]]; then
  echo ""
  echo "✅ 探测 UI 已启用. 现在做："
  echo "   1. 浏览器打开 http://${BOARD_IP}:5000/"
  echo "   2. 按 Ctrl+Shift+R 强刷"
  echo "   3. 设置页 → 最下面 🎯 视觉特征探测 → 启动探测 → 做动作"
  echo "   4. 做完 → 停止探测 → 复制表格贴给 AI"
  echo ""
  echo "📝 停止: bash scripts/probe_disable.sh"
else
  echo "✗ 接口返回 false, 检查 streamer 日志"
  exit 1
fi
