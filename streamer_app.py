"""
IronBuddy 推流中台 v3 — 精简重写版
剔除 ASR/Microphone/Audio 全部不可用模块，专注视频推流 + FSM 状态 + DeepSeek 教练
"""
import os
import json
import time
import io
from flask import Flask, Response, request

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
            except FileNotFoundError:
                pass
            time.sleep(0.033)  # ~30fps cap

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


if __name__ == '__main__':
    print("[*] 🚀 IronBuddy 推流中台 v3 (精简版) 已上线 — 0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
