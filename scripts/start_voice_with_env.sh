#!/bin/bash
# Voice daemon env wrapper: reads .api_config.json (both UPPERCASE and lowercase keys),
# sets ALSA mixer paths, then exec's voice_daemon.py.
# Re-resolves config on every service restart so Settings UI changes take effect.
sudo -n amixer -c 0 cset numid=2,iface=MIXER,name='Capture MIC Path' 1 >/dev/null 2>&1 || true
sudo -n amixer -c 0 cset numid=1,iface=MIXER,name='Playback Path' 2 >/dev/null 2>&1 || true

cd /home/toybrick/streamer_v3
eval $(python3 -c "
import json
with open('.api_config.json') as f:
    c = json.load(f)
def pick(*keys):
    for k in keys:
        v = c.get(k)
        if v:
            return v
    return ''
print('export BAIDU_APP_ID=' + pick('BAIDU_APP_ID', 'baidu_app_id'))
print('export BAIDU_API_KEY=' + pick('BAIDU_API_KEY', 'baidu_api_key'))
print('export BAIDU_SECRET_KEY=' + pick('BAIDU_SECRET_KEY', 'baidu_secret_key'))
print('export DEEPSEEK_API_KEY=' + pick('DEEPSEEK_API_KEY', 'deepseek_api_key'))
print('export FEISHU_APP_ID=' + pick('FEISHU_APP_ID', 'feishu_app_id'))
print('export FEISHU_APP_SECRET=' + pick('FEISHU_APP_SECRET', 'feishu_app_secret'))
print('export FEISHU_CHAT_ID=' + pick('FEISHU_CHAT_ID', 'feishu_chat_id'))
print('export FEISHU_WEBHOOK=' + pick('FEISHU_WEBHOOK', 'feishu_webhook'))
")
export VOICE_FORCE_MIC="${VOICE_FORCE_MIC:-plughw:0,0}"
export VOICE_VAD_MIN="${VOICE_VAD_MIN:-250}"
export VOICE_VAD_DELTA="${VOICE_VAD_DELTA:-40}"
export VOICE_VAD_DEBUG="${VOICE_VAD_DEBUG:-1}"
exec python3 -u hardware_engine/voice_daemon.py
