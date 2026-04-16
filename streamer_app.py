"""
IronBuddy 推流中台 v3 — 精简重写版
剔除 ASR/Microphone/Audio 全部不可用模块，专注视频推流 + FSM 状态 + DeepSeek 教练
V3.1: + 管理面板 (/admin)
"""
import os
import json
import time
import io
import subprocess
import glob as glob_mod
from flask import Flask, Response, request, redirect

template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templates'))
app = Flask(__name__, template_folder=template_dir)

# ===== JPEG 压缩配置 =====
SNAPSHOT_QUALITY = 65       # JPEG 质量 (1-100)，65 约 35-40KB（保持文字清晰）
_snapshot_last_mtime = 0    # 帧去重：上次文件修改时间
_snapshot_cache = b''       # 帧去重：缓存压缩结果


@app.route('/')
def index():
    """主页 — 直接读文件返回，绕过 Jinja2 模板缓存"""
    try:
        html_path = os.path.join(template_dir, 'index.html')
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        resp = Response(html_content, mimetype='text/html')
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp
    except Exception as e:
        return f"<h1>模板加载失败</h1><p>{e}</p>", 500


@app.route('/manifest.json')
def pwa_manifest():
    """PWA manifest for standalone app experience."""
    manifest = {
        "name": "IronBuddy",
        "short_name": "IronBuddy",
        "start_url": "/",
        "display": "standalone",
        "orientation": "landscape",
        "background_color": "#0a0e17",
        "theme_color": "#0a0e17",
        "icons": []
    }
    return Response(json.dumps(manifest), mimetype='application/json')


@app.route('/snapshot')
def snapshot():
    """核心管线：cv2 压缩 JPEG (97KB→~15KB) + 帧去重"""
    global _snapshot_last_mtime, _snapshot_cache
    try:
        st = os.stat("/dev/shm/result.jpg")

        # 帧去重：文件未变化则直接返回缓存
        if st.st_mtime_ns == _snapshot_last_mtime and _snapshot_cache:
            resp = Response(_snapshot_cache, mimetype='image/jpeg')
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            resp.headers['Content-Length'] = str(len(_snapshot_cache))
            return resp

        with open("/dev/shm/result.jpg", "rb") as f:
            raw = f.read()

        _snapshot_last_mtime = st.st_mtime_ns
        _snapshot_cache = raw

        resp = Response(raw, mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        resp.headers['Content-Length'] = str(len(raw))
        return resp
    except FileNotFoundError:
        return Response(b'', status=204)


# V2.2: MJPEG 流 — 浏览器原生解码，替代 JS 链式轮询
@app.route('/video_feed')
def video_feed():
    """MJPEG multipart 流，浏览器 <img> 直接订阅"""
    def gen_frames():
        last_mtime = 0
        last_yield_time = time.time()
        while True:
            try:
                st = os.stat("/dev/shm/result.jpg")
                if st.st_mtime_ns != last_mtime:
                    last_mtime = st.st_mtime_ns
                    with open("/dev/shm/result.jpg", "rb") as f:
                        raw = f.read()

                    yield (b'--frame\r\n'
                           b'Content-Type: image/jpeg\r\n'
                           b'Content-Length: ' + str(len(raw)).encode() + b'\r\n\r\n' + raw + b'\r\n')
                    last_yield_time = time.time()
            except FileNotFoundError:
                pass
            # 10秒无新帧则终止流，让浏览器触发重连
            if time.time() - last_yield_time > 10.0:
                return
            time.sleep(0.1)  # ~10fps cap (减轻CPU负担)

    resp = Response(gen_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


@app.route('/state_feed')
def state_feed():
    """FSM 深蹲状态（JSON）"""
    try:
        if os.path.exists("/dev/shm/fsm_state.json"):
            with open("/dev/shm/fsm_state.json", "r") as f:
                return Response(f.read(), mimetype='application/json')
    except Exception:
        pass
    return Response('{"state":"NO_PERSON","good":0,"failed":0,"angle":0}', mimetype='application/json')


@app.route('/llm_reply_feed')
def llm_reply_feed():
    """DeepSeek 教练回复"""
    try:
        if os.path.exists("/dev/shm/llm_reply.txt"):
            with open("/dev/shm/llm_reply.txt", "r", encoding="utf-8") as f:
                reply = f.read().strip()
            return Response(json.dumps({"reply": reply}, ensure_ascii=False), mimetype='application/json')
    except Exception:
        pass
    return Response('{"reply":""}', mimetype='application/json')


@app.route('/reset_session', methods=['POST'])
def reset_session():
    """重置 FSM 计数"""
    try:
        with open("/dev/shm/fsm_reset_signal", "w") as f:
            f.write("reset")
        return Response('{"ok":true}', mimetype='application/json')
    except Exception:
        return Response('{"ok":false}', mimetype='application/json', status=500)


@app.route('/trigger_deepseek', methods=['POST'])
def trigger_deepseek():
    """手动触发 DeepSeek 教练点评"""
    try:
        with open("/dev/shm/trigger_deepseek", "w") as f:
            f.write("trigger")
        return Response('{"ok":true}', mimetype='application/json')
    except Exception:
        return Response('{"ok":false}', mimetype='application/json', status=500)


@app.route('/api/chat', methods=['POST'])
def chat_input():
    """接收用户语音/文字消息，写入共享内存供 main_loop 读取"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        text = data.get("text", "").strip()
        if not text:
            return Response('{"ok":false,"error":"empty"}', mimetype='application/json', status=400)
        with open("/dev/shm/chat_input.txt.tmp", "w", encoding="utf-8") as f:
            f.write(text)
        os.rename("/dev/shm/chat_input.txt.tmp", "/dev/shm/chat_input.txt")
        return Response('{"ok":true}', mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}), mimetype='application/json', status=500)


@app.route('/api/chat_reply')
def chat_reply():
    """读取 DeepSeek 对话回复"""
    try:
        if os.path.exists("/dev/shm/chat_reply.txt"):
            with open("/dev/shm/chat_reply.txt", "r", encoding="utf-8") as f:
                reply = f.read().strip()
            mtime = os.path.getmtime("/dev/shm/chat_reply.txt")
            return Response(json.dumps({"reply": reply, "ts": mtime}, ensure_ascii=False), mimetype='application/json')
    except Exception:
        pass
    return Response('{"reply":"","ts":0}', mimetype='application/json')


@app.route('/api/chat_input')
def get_chat_input():
    """读取用户语音识别内容"""
    try:
        if os.path.exists("/dev/shm/chat_input.txt"):
            with open("/dev/shm/chat_input.txt", "r", encoding="utf-8") as f:
                content = f.read().strip()
            mtime = os.path.getmtime("/dev/shm/chat_input.txt")
            return Response(json.dumps({"text": content, "ts": mtime}, ensure_ascii=False), mimetype='application/json')
    except Exception:
        pass
    return Response('{"text":"","ts":0}', mimetype='application/json')

@app.route('/api/nn_inference')
def nn_inference():
    """Read neural network inference results"""
    try:
        if os.path.exists("/dev/shm/fsm_state.json"):
            with open("/dev/shm/fsm_state.json", "r") as f:
                data = json.load(f)
            return Response(json.dumps({
                "similarity": data.get("similarity", 0),
                "classification": data.get("classification", "unknown"),
                "nn_confidence": data.get("nn_confidence", 0)
            }), mimetype='application/json')
    except Exception:
        pass
    return Response('{"similarity":0,"classification":"unknown","nn_confidence":0}', mimetype='application/json')


@app.route('/api/voice_debug')
def get_voice_debug():
    try:
        if os.path.exists("/dev/shm/voice_debug.json"):
            with open("/dev/shm/voice_debug.json", "r", encoding="utf-8") as f:
                return Response(f.read(), mimetype='application/json')
    except Exception:
        pass
    return Response('{"energy":0, "threshold":150, "text":""}', mimetype='application/json')

@app.route('/api/chat_draft')
def chat_draft():
    """读取正在识别的草稿文字"""
    try:
        if os.path.exists("/dev/shm/chat_draft.txt"):
            with open("/dev/shm/chat_draft.txt", "r", encoding="utf-8") as f:
                content = f.read().strip()
            return Response(json.dumps({"text": content}, ensure_ascii=False), mimetype='application/json')
    except Exception:
        pass
    return Response('{"text":""}', mimetype='application/json')


# ===== V4: Mute / Vision Mode Toggle =====

@app.route('/api/mute', methods=['POST'])
def api_mute():
    """Write mute signal for voice daemon."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        muted = bool(data.get("muted", False))
        payload = json.dumps({"muted": muted, "ts": time.time()})
        tmp_path = "/dev/shm/mute_signal.json.tmp"
        target_path = "/dev/shm/mute_signal.json"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.rename(tmp_path, target_path)
        return Response(json.dumps({"ok": True, "muted": muted}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/switch_vision', methods=['POST'])
def api_switch_vision():
    """Write vision mode signal (cloud / local)."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        mode = data.get("mode", "cloud")
        if mode not in ("cloud", "local"):
            return Response(json.dumps({"ok": False, "error": "invalid mode"}),
                            mimetype='application/json', status=400)
        payload = json.dumps({"mode": mode, "ts": time.time()})
        tmp_path = "/dev/shm/vision_mode.json.tmp"
        target_path = "/dev/shm/vision_mode.json"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.rename(tmp_path, target_path)
        return Response(json.dumps({"ok": True, "mode": mode}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/switch_inference_mode', methods=['POST'])
def api_switch_inference_mode():
    """Switch between pure_vision (if-else only) and vision_sensor (NN + EMG)."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        mode = data.get("mode", "pure_vision")
        if mode not in ("pure_vision", "vision_sensor"):
            return Response(json.dumps({"ok": False, "error": "invalid mode"}),
                            mimetype='application/json', status=400)
        payload = json.dumps({"mode": mode, "ts": time.time()})
        tmp_path = "/dev/shm/inference_mode.json.tmp"
        target_path = "/dev/shm/inference_mode.json"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.rename(tmp_path, target_path)
        return Response(json.dumps({"ok": True, "mode": mode}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/inference_mode')
def get_inference_mode():
    """Read current inference mode (pure_vision or vision_sensor)."""
    try:
        path = "/dev/shm/inference_mode.json"
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return Response(f.read(), mimetype='application/json')
    except Exception:
        pass
    return Response('{"mode":"pure_vision","ts":0}', mimetype='application/json')


@app.route('/api/vision_mode')
def get_vision_mode():
    """Read current vision mode from signal file."""
    try:
        vm_path = "/dev/shm/vision_mode.json"
        if os.path.exists(vm_path):
            with open(vm_path, "r", encoding="utf-8") as f:
                data = f.read()
            return Response(data, mimetype='application/json')
    except Exception:
        pass
    return Response('{"mode":"local","ts":0}', mimetype='application/json')


# ===== HDMI Status API =====

@app.route('/api/hdmi_status')
def hdmi_status():
    """Check if HDMI display is active. Hardware status overrides signal file."""
    hw_connected = False
    try:
        with open("/sys/class/drm/card0-HDMI-A-1/status", "r") as f:
            hw_connected = "connected" in f.read()
    except Exception:
        pass

    # If hardware is disconnected, HDMI is definitely inactive
    if not hw_connected:
        return Response('{"active":false,"hw_connected":false}', mimetype='application/json')

    # Hardware connected — check if vision process is actually using it
    try:
        path = "/dev/shm/hdmi_status.json"
        if os.path.exists(path):
            with open(path, "r") as f:
                data = json.loads(f.read())
                data["hw_connected"] = True
                return Response(json.dumps(data), mimetype='application/json')
    except Exception:
        pass

    return Response(json.dumps({"active": False, "hw_connected": True}),
                    mimetype='application/json')


# ===== Feishu Plan Push API =====

@app.route('/api/feishu/send_plan', methods=['POST'])
def feishu_send_plan():
    """Generate a fitness plan via DeepSeek and push to Feishu."""
    api_cfg = _load_api_config()
    ds_key = api_cfg.get("deepseek_api_key", "")
    feishu_app_id = api_cfg.get("feishu_app_id", "")
    feishu_app_secret = api_cfg.get("feishu_app_secret", "")
    feishu_chat_id = api_cfg.get("feishu_chat_id", "")

    if not ds_key:
        return Response(json.dumps({"ok": False, "error": "DeepSeek API Key 未配置"}),
                        mimetype='application/json', status=400)
    if not feishu_app_id or not feishu_chat_id:
        return Response(json.dumps({"ok": False, "error": "飞书凭证未配置"}),
                        mimetype='application/json', status=400)

    # Read current training state
    fsm_data = {}
    try:
        if os.path.exists("/dev/shm/fsm_state.json"):
            with open("/dev/shm/fsm_state.json", "r") as f:
                fsm_data = json.load(f)
    except Exception:
        pass

    good = fsm_data.get("good", 0)
    failed = fsm_data.get("failed", 0)
    fatigue = fsm_data.get("fatigue", 0)
    exercise = fsm_data.get("exercise", "squat")

    # Custom prompt from request (optional)
    req_data = request.get_json(silent=True) or {}
    custom_prompt = req_data.get("prompt", "")

    # Call DeepSeek to generate plan
    import urllib.request
    import ssl
    try:
        prompt = custom_prompt if custom_prompt else (
            "你是IronBuddy智能健身教练。根据以下训练数据，生成今日健身规划：\n"
            "- 当前运动: {}\n- 标准次数: {} 违规次数: {}\n- 疲劳值: {}/1500\n"
            "请生成一份简洁的训练计划（包含热身、主训练、拉伸），控制在200字以内。"
        ).format(exercise, good, failed, fatigue)

        ds_payload = json.dumps({
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "你是专业健身教练，输出简洁的训练计划。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
        }).encode("utf-8")

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        ds_req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=ds_payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + ds_key,
            },
        )
        ds_resp = json.loads(urllib.request.urlopen(ds_req, timeout=30, context=ctx).read())
        plan_text = ds_resp["choices"][0]["message"]["content"]
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": "DeepSeek 调用失败: " + str(e)}),
                        mimetype='application/json', status=500)

    # Push to Feishu
    try:
        # Get token
        token_data = json.dumps({
            "app_id": feishu_app_id,
            "app_secret": feishu_app_secret,
        }).encode("utf-8")
        token_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=token_data,
            headers={"Content-Type": "application/json"},
        )
        token_resp = json.loads(urllib.request.urlopen(token_req, timeout=10, context=ctx).read())
        access_token = token_resp.get("tenant_access_token", "")

        # Send message
        import datetime
        feishu_text = "🏋️ IronBuddy 健身规划\n📅 {}\n\n{}".format(
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), plan_text)
        msg_data = json.dumps({
            "receive_id": feishu_chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": feishu_text}),
        }).encode("utf-8")
        msg_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=msg_data,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + access_token,
            },
        )
        msg_resp = json.loads(urllib.request.urlopen(msg_req, timeout=15, context=ctx).read())
        if msg_resp.get("code") == 0:
            return Response(json.dumps({"ok": True, "plan": plan_text}), mimetype='application/json')
        else:
            return Response(json.dumps({"ok": False, "error": "飞书发送失败", "detail": str(msg_resp)}),
                            mimetype='application/json', status=500)
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": "飞书推送失败: " + str(e), "plan": plan_text}),
                        mimetype='application/json', status=500)


# ===== V2: 肌肉激活热力图 API =====

@app.route('/api/muscle_activation')
def muscle_activation():
    """读取肌肉激活数据（由 main_claw_loop V2 管线写入）"""
    try:
        if os.path.exists("/dev/shm/muscle_activation.json"):
            with open("/dev/shm/muscle_activation.json", "r", encoding="utf-8") as f:
                data = f.read()
            resp = Response(data, mimetype='application/json')
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return resp
    except Exception:
        pass
    return Response('{"activations":{},"warnings":[],"exercise":null}', mimetype='application/json')


@app.route('/api/user_profile', methods=['POST'])
def user_profile():
    """接收用户身体参数（身高/体重/动作/器材重量），写入共享内存供主循环读取"""
    try:
        data = request.get_json(force=True)
        with open("/dev/shm/user_profile.json.tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.rename("/dev/shm/user_profile.json.tmp", "/dev/shm/user_profile.json")
        return Response(json.dumps({"status": "ok"}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"status": "error", "msg": str(e)}), mimetype='application/json'), 500


# ===== V2.5: 训练历史页面 =====

@app.route('/history')
def history_page():
    """训练历史页面"""
    try:
        html_path = os.path.join(template_dir, 'history.html')
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        resp = Response(html_content, mimetype='text/html')
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    except Exception as e:
        return f"<h1>页面加载失败</h1><p>{e}</p>", 500


@app.route('/api/training_log')
def training_log():
    """返回训练日志 JSON（由 main_claw_loop 写入板端文件）"""
    log_path = "/home/toybrick/agent_memory/training_log.json"
    try:
        if os.path.exists(log_path):
            with open(log_path, "r", encoding="utf-8") as f:
                data = f.read()
            resp = Response(data, mimetype='application/json')
            resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
            return resp
    except Exception:
        pass
    return Response('{}', mimetype='application/json')


# ===== V3.1: Admin Management Panel =====

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BOARD_TARGET = "toybrick@10.105.245.224"
BOARD_KEY_PATH = os.path.expanduser("~/.ssh/id_rsa_toybrick")
CLOUD_SSH = "root@connect.westd.seetacloud.com"
CLOUD_PORT = 14191
CLOUD_KEY_PATH = os.path.expanduser("~/.ssh/id_cloud_autodl")

# Service process signatures for pgrep
SERVICE_SIGNATURES = {
    "vision": "cloud_rtmpose_client.py",
    "streamer": "streamer_app.py",
    "fsm": "main_claw_loop.py",
    "emg": "udp_emg_server.py",
    "voice": "voice_daemon.py",
}


def _run_cmd(cmd, timeout=5):
    """Run a shell command, return (success, stdout)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout.strip()
    except Exception as e:
        return False, str(e)


def _local_cmd(cmd, timeout=5):
    """Run command locally on the board (APP runs on board itself)."""
    return _run_cmd(cmd, timeout=timeout)


def _ssh_board_cmd(cmd, timeout=5):
    """Run command on board — local first, SSH fallback."""
    # If we're ON the board, run locally (detect by hostname or always try local)
    import platform
    if 'toybrick' in platform.node() or 'debian10' in platform.node() or os.path.exists('/dev/shm/pose_data.json'):
        return _local_cmd(cmd, timeout=timeout)
    # Otherwise SSH
    if not os.path.exists(BOARD_KEY_PATH):
        return False, "SSH key not found"
    ssh = 'ssh -i {} -o StrictHostKeyChecking=no -o ConnectTimeout=3 {} "{}"'.format(
        BOARD_KEY_PATH, BOARD_TARGET, cmd
    )
    return _run_cmd(ssh, timeout=timeout)


@app.route('/admin')
def admin_page():
    """Admin panel now integrated into main page — redirect."""
    return redirect('/', code=302)


@app.route('/api/admin/overview')
def admin_overview():
    """Quick overview: board online, service count, data count"""
    board_ok, _ = _ssh_board_cmd("echo ok", timeout=3)

    data_dir = os.path.join(PROJECT_ROOT, "data")
    csv_count = len(glob_mod.glob(os.path.join(data_dir, "**", "*.csv"), recursive=True))

    model_path = os.path.join(PROJECT_ROOT, "models", "extreme_fusion_gru.pt")
    model_exists = os.path.exists(model_path)
    model_size = os.path.getsize(model_path) if model_exists else 0

    result = {
        "board_online": board_ok,
        "board_ip": "10.105.245.224",
        "csv_count": csv_count,
        "model_exists": model_exists,
        "model_size_kb": round(model_size / 1024, 1) if model_exists else 0,
    }
    return Response(json.dumps(result), mimetype='application/json')


@app.route('/api/admin/services')
def admin_services():
    """Check which services are running on the board"""
    result = {}
    board_ok, _ = _ssh_board_cmd("echo ok", timeout=3)
    if not board_ok:
        for name in SERVICE_SIGNATURES:
            result[name] = {"running": False, "pid": None, "error": "board offline"}
        return Response(json.dumps({"board_online": False, "services": result}), mimetype='application/json')

    for name, sig in SERVICE_SIGNATURES.items():
        # Bracket trick avoids pgrep self-match in shell=True mode
        safe_sig = '[' + sig[0] + ']' + sig[1:]
        ok, out = _ssh_board_cmd(
            "pgrep -f '{}' | while read p; do "
            "  st=$(ps -o state= -p $p 2>/dev/null); "
            "  [ -n \"$st\" ] && [ \"$st\" != 'Z' ] && echo $p && break; "
            "done".format(safe_sig), timeout=3)
        pid = out.strip() if ok and out.strip() else None
        result[name] = {"running": pid is not None, "pid": pid}

    return Response(json.dumps({"board_online": True, "services": result}), mimetype='application/json')


# Service launch commands (board-local, no SSH)
# Each service: (process_signature, launch_command, log_file)
_SERVICE_LAUNCHERS = {
    "vision": {
        "sig": "cloud_rtmpose_client.py",
        "cmd": "cd {root} && ENABLE_HDMI={hdmi} DISPLAY=:0 VISION_MODE=local LOCAL_POSE_MODEL=/home/toybrick/deploy_rknn_yolo/YOLOv5-Style/data/weights/pose-5s6-640-uint8.rknn PYTHONUNBUFFERED=1 python3 hardware_engine/ai_sensory/cloud_rtmpose_client.py",
        "log": "/tmp/vision_local.log",
    },
    "fsm": {
        "sig": "main_claw_loop.py",
        "cmd": "cd {root} && PYTHONUNBUFFERED=1 python3 hardware_engine/main_claw_loop.py",
        "log": "/tmp/fsm_loop.log",
    },
    "emg": {
        "sig": "udp_emg_server.py",
        "cmd": "cd {root} && PYTHONUNBUFFERED=1 python3 hardware_engine/sensor/udp_emg_server.py",
        "log": "/tmp/emg_server.log",
    },
    "voice": {
        "sig": "voice_daemon.py",
        "cmd": "cd {root} && PYTHONUNBUFFERED=1 python3 hardware_engine/voice_daemon.py",
        "log": "/tmp/voice_daemon.log",
    },
}


@app.route('/api/admin/start', methods=['POST'])
def admin_start():
    """Start individual or all services directly on the board."""
    data = request.get_json(silent=True) or {}
    target = data.get("service", "all")  # "all" or specific service name

    results = {}
    services_to_start = _SERVICE_LAUNCHERS if target == "all" else {target: _SERVICE_LAUNCHERS.get(target)}

    for name, info in services_to_start.items():
        if info is None:
            results[name] = {"ok": False, "error": "unknown service"}
            continue
        # Check if already running (bracket trick avoids shell self-match)
        safe_sig = '[' + info["sig"][0] + ']' + info["sig"][1:]
        ok, pid_out = _run_cmd("pgrep -f '{}' | head -1".format(safe_sig), timeout=3)
        if ok and pid_out.strip():
            results[name] = {"ok": True, "status": "already running", "pid": pid_out.strip()}
            continue
        # Launch in background with nohup
        # Auto-detect HDMI for vision + write vision_mode signal
        hdmi_val = "0"
        if name == "vision":
            try:
                with open("/sys/class/drm/card0-HDMI-A-1/status", "r") as hf:
                    hdmi_val = "1" if "connected" in hf.read() else "0"
            except Exception:
                pass
            # Write vision mode signal file
            _run_cmd('echo \'{"mode":"local","ts":' + str(int(time.time())) + '}\'>/dev/shm/vision_mode.json', timeout=2)
        launch = info["cmd"].format(root=PROJECT_ROOT, hdmi=hdmi_val)
        # Inject API config env vars for FSM and Voice
        if name in ("fsm", "voice"):
            api_cfg = _load_api_config()
            api_key = api_cfg.get("deepseek_api_key", "")
            llm_backend = api_cfg.get("llm_backend", "direct")
            feishu_app_id = api_cfg.get("feishu_app_id", "")
            feishu_app_secret = api_cfg.get("feishu_app_secret", "")
            feishu_chat_id = api_cfg.get("feishu_chat_id", "")
            env_prefix = ""
            if api_key:
                env_prefix += "DEEPSEEK_API_KEY='{}' LLM_BACKEND='{}' ".format(api_key, llm_backend)
            if feishu_app_id:
                env_prefix += "FEISHU_APP_ID='{}' FEISHU_APP_SECRET='{}' FEISHU_CHAT_ID='{}' ".format(
                    feishu_app_id, feishu_app_secret, feishu_chat_id)
            # Baidu speech credentials (for voice daemon)
            baidu_app_id = api_cfg.get("baidu_app_id", "")
            baidu_api_key = api_cfg.get("baidu_api_key", "")
            baidu_secret_key = api_cfg.get("baidu_secret_key", "")
            if baidu_app_id:
                env_prefix += "BAIDU_APP_ID='{}' BAIDU_API_KEY='{}' BAIDU_SECRET_KEY='{}' ".format(
                    baidu_app_id, baidu_api_key, baidu_secret_key)
            if env_prefix:
                launch = env_prefix + launch
        log = info["log"]
        # Write launch command to a temp script
        # Split env vars from command so exports work across && chains
        script_path = "/tmp/_launch_{}.sh".format(name)
        try:
            with open(script_path, "w") as sf:
                sf.write("#!/bin/bash\n")
                # Set audio path for voice/fsm (speaker on)
                if name in ("fsm", "voice"):
                    sf.write("sudo amixer -c 0 cset numid=1,iface=MIXER,name='Playback Path' 6 >/dev/null 2>&1\n")
                sf.write("export " + " ".join(
                    "{}='{}'".format(k, v) for k, v in [
                        ("PYTHONUNBUFFERED", "1"),
                    ]
                ) + "\n")
                # Extract and export env vars from launch string
                # launch = "ENV1=val1 ENV2=val2 cd /path && ... python3 ..."
                parts = launch.split()
                env_exports = []
                cmd_start = 0
                for i, p in enumerate(parts):
                    if '=' in p and not p.startswith('-') and not p.startswith('/'):
                        env_exports.append(p)
                    else:
                        cmd_start = i
                        break
                if env_exports:
                    sf.write("export " + " ".join(env_exports) + "\n")
                sf.write(" ".join(parts[cmd_start:]) + "\n")
            os.chmod(script_path, 0o755)
        except Exception as e:
            results[name] = {"ok": False, "error": "script write failed: " + str(e)}
            continue
        full_cmd = "nohup {} >{} 2>&1 &".format(script_path, log)
        ok_launch, launch_out = _run_cmd(full_cmd, timeout=8)
        # Verify it started
        import time as _t
        _t.sleep(1.5)
        safe_sig2 = '[' + info["sig"][0] + ']' + info["sig"][1:]
        ok2, pid2 = _run_cmd("pgrep -f '{}' | head -1".format(safe_sig2), timeout=3)
        results[name] = {"ok": bool(ok2 and pid2.strip()), "pid": pid2.strip() if ok2 else None}
        if not (ok2 and pid2.strip()):
            # Process died — capture error from log
            _, err_tail = _run_cmd("tail -5 {}".format(log), timeout=2)
            results[name]["error"] = err_tail

    return Response(json.dumps({"ok": True, "services": results}), mimetype='application/json')


@app.route('/api/admin/stop', methods=['POST'])
def admin_stop():
    """Stop individual or all services — aggressive process-tree kill."""
    data = request.get_json(silent=True) or {}
    target = data.get("service", "all")

    results = {}
    sigs = SERVICE_SIGNATURES if target == "all" else {target: SERVICE_SIGNATURES.get(target)}

    # Collect all PIDs first (python3 processes matching signatures)
    all_pids = []
    for name, sig in sigs.items():
        if sig is None or name == "streamer":
            continue
        # Find all matching PIDs (python3 + bash wrappers)
        ok, pid_out = _run_cmd(
            "pgrep -f '{}' | grep -v $$".format(sig), timeout=3)
        if ok and pid_out.strip():
            for p in pid_out.strip().split('\n'):
                p = p.strip()
                if p:
                    all_pids.append(p)

    # Kill all at once with SIGKILL (no mercy — TERM was unreliable)
    if all_pids:
        pid_list = ' '.join(all_pids)
        _run_cmd("kill -9 {} 2>/dev/null".format(pid_list), timeout=3)

    # Also blanket-kill by signature as safety net
    for name, sig in sigs.items():
        if sig is None or name == "streamer":
            continue
        _run_cmd("pkill -9 -f '{}' 2>/dev/null".format(sig), timeout=3)

    # Wait for processes to die
    time.sleep(1.5)

    # Clean up zombie arecord/aplay from voice daemon
    _run_cmd("killall -9 arecord aplay 2>/dev/null", timeout=2)

    # Verify and build results
    for name, sig in sigs.items():
        if sig is None:
            results[name] = {"ok": False, "error": "unknown service"}
            continue
        if name == "streamer":
            results[name] = {"ok": True, "status": "skipped (self)"}
            continue
        safe_sig = '[' + sig[0] + ']' + sig[1:] if sig else sig
        ok, pid_out = _run_cmd("pgrep -f '{}'".format(safe_sig), timeout=3)
        still_alive = ok and pid_out.strip()
        results[name] = {"ok": not still_alive, "status": "stopped" if not still_alive else "kill failed"}

    return Response(json.dumps({"ok": True, "services": results}), mimetype='application/json')


@app.route('/api/admin/training_data')
def admin_training_data():
    """List all training CSV files grouped by exercise/label"""
    data_dir = os.path.join(PROJECT_ROOT, "data")
    result = {}
    if not os.path.isdir(data_dir):
        return Response(json.dumps(result), mimetype='application/json')

    for exercise in sorted(os.listdir(data_dir)):
        ex_path = os.path.join(data_dir, exercise)
        if not os.path.isdir(ex_path):
            continue
        result[exercise] = {}
        for label in sorted(os.listdir(ex_path)):
            label_path = os.path.join(ex_path, label)
            if not os.path.isdir(label_path):
                continue
            files = []
            for f in sorted(os.listdir(label_path)):
                if f.endswith('.csv'):
                    fp = os.path.join(label_path, f)
                    st = os.stat(fp)
                    # count lines
                    with open(fp, 'r') as fh:
                        line_count = sum(1 for _ in fh)
                    files.append({
                        "name": f,
                        "size_kb": round(st.st_size / 1024, 1),
                        "lines": line_count,
                        "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                    })
            if files:
                result[exercise][label] = files

    return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')


@app.route('/api/admin/system_info')
def admin_system_info():
    """System status: GPU, board, connectivity"""
    info = {
        "board": {"online": False, "ip": "10.105.245.224"},
        "cloud_gpu": {"online": False, "info": ""},
        "openclaw": {"status": "unknown"},
    }

    # Board check
    ok, out = _ssh_board_cmd("cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null || echo 0", timeout=5)
    if ok:
        info["board"]["online"] = True
        try:
            temp_raw = int(out.strip())
            info["board"]["cpu_temp"] = round(temp_raw / 1000.0, 1) if temp_raw > 1000 else temp_raw
        except ValueError:
            info["board"]["cpu_temp"] = 0
        # board uptime
        ok2, out2 = _ssh_board_cmd("uptime -p 2>/dev/null || uptime", timeout=3)
        if ok2:
            info["board"]["uptime"] = out2

    # Cloud GPU check
    if os.path.exists(CLOUD_KEY_PATH):
        ok, out = _run_cmd(
            'ssh -p {} -i {} -o StrictHostKeyChecking=no -o ConnectTimeout=3 {} '
            '"nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader,nounits 2>/dev/null"'.format(
                CLOUD_PORT, CLOUD_KEY_PATH, CLOUD_SSH
            ),
            timeout=8
        )
        if ok and out:
            info["cloud_gpu"]["online"] = True
            info["cloud_gpu"]["info"] = out

    return Response(json.dumps(info, ensure_ascii=False), mimetype='application/json')


# ── API Config (DeepSeek key, LLM backend) ────────────────────────────────
API_CONFIG_PATH = os.path.join(PROJECT_ROOT, ".api_config.json")


def _load_api_config():
    try:
        with open(API_CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_api_config(cfg):
    tmp = API_CONFIG_PATH + ".tmp"
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
    os.rename(tmp, API_CONFIG_PATH)


@app.route('/api/admin/api_config', methods=['GET', 'POST'])
def admin_api_config():
    if request.method == 'GET':
        cfg = _load_api_config()
        # Mask the key for display (show first 6 chars)
        masked = cfg.copy()
        key = masked.get('deepseek_api_key', '')
        if key and len(key) > 6:
            masked['deepseek_api_key'] = key[:6] + '****' + key[-4:]
        return Response(json.dumps(masked), mimetype='application/json')
    else:
        data = request.get_json(silent=True) or {}
        cfg = _load_api_config()
        if 'deepseek_api_key' in data:
            cfg['deepseek_api_key'] = data['deepseek_api_key']
        if 'llm_backend' in data:
            cfg['llm_backend'] = data['llm_backend']
        _save_api_config(cfg)
        return Response(json.dumps({"ok": True}), mimetype='application/json')


@app.route('/api/admin/logs')
def admin_logs():
    """Return recent service log lines as JSON array of {timestamp, source, message}."""
    log_files = {
        'streamer': '/tmp/streamer.log',
        'vision': '/tmp/vision_local.log',
        'fsm': '/tmp/fsm_loop.log',
        'emg': '/tmp/emg_server.log',
        'voice': '/tmp/voice_daemon.log',
    }
    lines = []
    for source, path in log_files.items():
        try:
            if not os.path.exists(path):
                continue
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                # Read last 50 lines per file
                all_lines = f.readlines()
                tail = all_lines[-50:] if len(all_lines) > 50 else all_lines
                for raw in tail:
                    raw = raw.strip()
                    if not raw:
                        continue
                    # Try to extract timestamp from common formats: [2024-01-01 12:00:00] or 2024-01-01 12:00:00
                    ts = ''
                    msg = raw
                    if raw.startswith('['):
                        bracket_end = raw.find(']')
                        if bracket_end > 0:
                            ts = raw[1:bracket_end]
                            msg = raw[bracket_end + 1:].strip()
                    elif len(raw) > 19 and raw[4] == '-' and raw[10] == ' ':
                        ts = raw[:19]
                        msg = raw[19:].strip()
                    lines.append({'timestamp': ts, 'source': source, 'message': msg})
        except Exception:
            pass

    # Sort by timestamp (best effort) and limit to last 200
    lines.sort(key=lambda x: x['timestamp'])
    lines = lines[-200:]
    return Response(json.dumps(lines, ensure_ascii=False), mimetype='application/json')


@app.route('/api/admin/project_info')
def admin_project_info():
    """Project metadata: git, model, config"""
    info = {"git": {}, "model": {}, "config": {}}

    # Git info
    ok, branch = _run_cmd("cd {} && git rev-parse --abbrev-ref HEAD".format(PROJECT_ROOT))
    if ok:
        info["git"]["branch"] = branch
    ok, commit = _run_cmd("cd {} && git log --oneline -5".format(PROJECT_ROOT))
    if ok:
        info["git"]["recent_commits"] = commit.split("\n")
    ok, status = _run_cmd("cd {} && git status --short".format(PROJECT_ROOT))
    if ok:
        info["git"]["uncommitted"] = len([l for l in status.split("\n") if l.strip()])

    # Model info
    model_path = os.path.join(PROJECT_ROOT, "models", "extreme_fusion_gru.pt")
    if os.path.exists(model_path):
        st = os.stat(model_path)
        info["model"] = {
            "path": "models/extreme_fusion_gru.pt",
            "size_kb": round(st.st_size / 1024, 1),
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "architecture": "CompensationGRU 7D->similarity+3class",
        }

    # Config
    info["config"] = {
        "board_ip": "10.105.245.224",
        "cloud_url": "https://u953119-ba4a-9dcd6a47.westd.seetacloud.com:8443/infer",
        "flask_port": 5000,
        "emg_port": 8080,
        "openclaw_port": 18789,
    }

    # TB logs
    tb_dir = os.path.join(PROJECT_ROOT, "models", "tb_logs")
    if os.path.isdir(tb_dir):
        tb_runs = sorted(os.listdir(tb_dir))
        info["training_runs"] = tb_runs[-5:] if len(tb_runs) > 5 else tb_runs

    return Response(json.dumps(info, ensure_ascii=False), mimetype='application/json')


if __name__ == '__main__':
    print("[*] IronBuddy v3.1 online -- 0.0.0.0:5000")
    print("[*] Admin panel: http://localhost:5000/admin")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
