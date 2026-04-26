"""
IronBuddy 推流中台 v3 — 精简重写版
剔除 ASR/Microphone/Audio 全部不可用模块，专注视频推流 + FSM 状态 + DeepSeek 教练
V3.1: + 管理面板 (/admin)
"""
import os
import json
import time
import io
import logging  # V7.21 (2026-04-21): 补 import —— 原代码未导入, feishu_smart_push 降级路径炸 NameError → 500
import subprocess
import threading
import traceback
import glob as glob_mod
import requests
from flask import Flask, Response, request, redirect

template_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), 'templates'))
app = Flask(__name__, template_folder=template_dir)

# V7.21: 全局异常兜底 —— 任何未捕获异常都返回 JSON, 绝不落到 Flask 原生 HTML 500
@app.errorhandler(Exception)
def _json_error_handler(e):
    logging.error("[Unhandled] %s: %s\n%s", type(e).__name__, e, traceback.format_exc())
    body = json.dumps({"ok": False, "error": type(e).__name__, "detail": str(e)[:300]}, ensure_ascii=False)
    return Response(body, status=500, mimetype='application/json')

# V7.21: 飞书推送互斥锁 —— 防止并发/重复点击把后端挤爆
_FEISHU_PUSH_LOCK = threading.Lock()
_FEISHU_PUSH_STARTED_AT = [0.0]

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
    """FSM 深蹲状态（JSON）— 附加式合并 mute / fatigue_limit 字段（T2 voice UI）"""
    base = {"state": "NO_PERSON", "good": 0, "failed": 0, "angle": 0}
    try:
        if os.path.exists("/dev/shm/fsm_state.json"):
            with open("/dev/shm/fsm_state.json", "r") as f:
                base = json.loads(f.read())
    except Exception:
        pass
    # Merge mute state from voice daemon
    try:
        if os.path.exists("/dev/shm/mute_signal.json"):
            with open("/dev/shm/mute_signal.json", "r") as f:
                mute_data = json.loads(f.read())
                base["muted"] = bool(mute_data.get("muted", False))
    except Exception:
        pass
    if "muted" not in base:
        base["muted"] = False
    # V7.6: fatigue_limit 来源优先级 FSM (最权威) > ui_fatigue_limit.json > 1500
    if "fatigue_limit" not in base:
        try:
            if os.path.exists("/dev/shm/ui_fatigue_limit.json"):
                with open("/dev/shm/ui_fatigue_limit.json", "r") as f:
                    base["fatigue_limit"] = int(json.loads(f.read()).get("limit", 1500))
            else:
                base["fatigue_limit"] = 1500
        except Exception:
            base["fatigue_limit"] = 1500
    return Response(json.dumps(base, ensure_ascii=False), mimetype='application/json')


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


def _read_voice_turn():
    """V7.30 S1: read /dev/shm/voice_turn.json and return (turn_id, stage) or ('', '')."""
    try:
        if os.path.exists("/dev/shm/voice_turn.json"):
            with open("/dev/shm/voice_turn.json", "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("turn_id", ""), data.get("stage", "")
    except Exception:
        pass
    return "", ""


@app.route('/api/chat_reply')
def chat_reply():
    """读取 DeepSeek 对话回复 (V7.30: 附带 turn_id 让前端去重气泡)"""
    try:
        if os.path.exists("/dev/shm/chat_reply.txt"):
            with open("/dev/shm/chat_reply.txt", "r", encoding="utf-8") as f:
                reply = f.read().strip()
            mtime = os.path.getmtime("/dev/shm/chat_reply.txt")
            turn_id, stage = _read_voice_turn()
            return Response(json.dumps(
                {"reply": reply, "ts": mtime, "turn_id": turn_id, "stage": stage},
                ensure_ascii=False), mimetype='application/json')
    except Exception:
        pass
    return Response('{"reply":"","ts":0,"turn_id":"","stage":""}', mimetype='application/json')


@app.route('/api/chat_input')
def get_chat_input():
    """读取用户语音识别内容 (V7.5: 去除 [voice-handled] 内部标记; V7.30: 附 turn_id)"""
    try:
        if os.path.exists("/dev/shm/chat_input.txt"):
            with open("/dev/shm/chat_input.txt", "r", encoding="utf-8") as f:
                content = f.read().strip()
            # V7.5: 剥离 FSM 路由控制标记
            content = content.replace("[voice-handled]", "").strip()
            mtime = os.path.getmtime("/dev/shm/chat_input.txt")
            turn_id, stage = _read_voice_turn()
            return Response(json.dumps(
                {"text": content, "ts": mtime, "turn_id": turn_id, "stage": stage},
                ensure_ascii=False), mimetype='application/json')
    except Exception:
        pass
    return Response('{"text":"","ts":0,"turn_id":"","stage":""}', mimetype='application/json')


@app.route('/api/voice_turn')
def get_voice_turn():
    """V7.30: expose current voice turn metadata for UI bubble dedupe (S1)."""
    try:
        if os.path.exists("/dev/shm/voice_turn.json"):
            with open("/dev/shm/voice_turn.json", "r", encoding="utf-8") as f:
                return Response(f.read(), mimetype='application/json')
    except Exception:
        pass
    return Response('{"turn_id":"","stage":"","ts":0}', mimetype='application/json')

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
    """Mute/unmute: (1) write signal for voice daemon TTS gating, (2) hard-mute system Speaker via amixer."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        muted = bool(data.get("muted", False))
        payload = json.dumps({"muted": muted, "ts": time.time()})
        tmp_path = "/dev/shm/mute_signal.json.tmp"
        target_path = "/dev/shm/mute_signal.json"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.rename(tmp_path, target_path)
        # Hard-mute: amixer control Speaker channel directly (drops L0 alarms too)
        try:
            import subprocess
            if muted:
                subprocess.run(["sudo", "-n", "amixer", "-c", "0", "sset", "Speaker", "0%", "mute"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            else:
                subprocess.run(["sudo", "-n", "amixer", "-c", "0", "sset", "Speaker", "80%", "unmute"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
        except Exception:
            pass  # amixer may not have sudo NOPASSWD; voice daemon gating still works
        return Response(json.dumps({"ok": True, "muted": muted}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/fatigue_limit', methods=['POST'])
def api_fatigue_limit():
    """Set fatigue limit (T2): write actuation signal for FSM + persist display value for UI."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        limit = int(data.get("limit", 1500))
        if limit < 100 or limit > 10000:
            return Response(json.dumps({"ok": False, "error": "limit out of range (100-10000)"}),
                            mimetype='application/json', status=400)
        ts = time.time()
        # 1) Actuation signal: FSM consumes + deletes
        act_payload = json.dumps({"limit": limit, "ts": ts})
        act_tmp = "/dev/shm/fatigue_limit.json.tmp"
        act_target = "/dev/shm/fatigue_limit.json"
        with open(act_tmp, "w", encoding="utf-8") as f:
            f.write(act_payload)
        os.rename(act_tmp, act_target)
        # 2) Display signal: UI reads for current-value display (FSM doesn't touch this one)
        ui_tmp = "/dev/shm/ui_fatigue_limit.json.tmp"
        ui_target = "/dev/shm/ui_fatigue_limit.json"
        with open(ui_tmp, "w", encoding="utf-8") as f:
            f.write(act_payload)
        os.rename(ui_tmp, ui_target)
        return Response(json.dumps({"ok": True, "limit": limit}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/exercise_mode', methods=['POST'])
def api_exercise_mode():
    """Switch exercise mode (T2): write signal for FSM to hot-swap squat↔curl."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        mode = data.get("mode", "squat")
        if mode not in ("squat", "curl", "bicep_curl"):
            return Response(json.dumps({"ok": False, "error": "invalid mode"}),
                            mimetype='application/json', status=400)
        # Normalize: FSM/voice daemon use "squat" or "curl"
        norm_mode = "curl" if mode in ("curl", "bicep_curl") else "squat"
        payload = json.dumps({"mode": norm_mode, "ts": time.time()})
        tmp_path = "/dev/shm/exercise_mode.json.tmp"
        target_path = "/dev/shm/exercise_mode.json"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.rename(tmp_path, target_path)
        return Response(json.dumps({"ok": True, "mode": norm_mode}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/mvc_calibration', methods=['GET', 'POST'])
def api_mvc_calibration():
    """MVC 标定 (T3, plan §3.3):
    - GET: 读取当前 /dev/shm/emg_calibration.json, 返回状态
    - POST: 写入新标定（板未连时 peak_mvc 值可为 null, 仅记录 protocol + 动作 + 时间戳）
    契约参照 plan §2.1: {peak_mvc:{ch0,ch1}, protocol:'SENIAM-2000', exercise:'curl|squat', std_pct, ts}
    """
    target_path = "/dev/shm/emg_calibration.json"
    if request.method == 'GET':
        try:
            if os.path.exists(target_path):
                with open(target_path, "r", encoding="utf-8") as f:
                    return Response(f.read(), mimetype='application/json')
        except Exception:
            pass
        return Response(json.dumps({"calibrated": False}), mimetype='application/json')
    # POST
    try:
        data = request.get_json(force=True, silent=True) or {}
        exercise = data.get("exercise", "squat")
        if exercise not in ("squat", "curl", "bicep_curl"):
            return Response(json.dumps({"ok": False, "error": "invalid exercise"}),
                            mimetype='application/json', status=400)
        norm_ex = "curl" if exercise in ("curl", "bicep_curl") else "squat"
        peak_mvc = data.get("peak_mvc", {"ch0": None, "ch1": None})
        std_pct = data.get("std_pct", None)
        payload = {
            "calibrated": True,
            "protocol": "SENIAM-2000",
            "exercise": norm_ex,
            "peak_mvc": peak_mvc,
            "std_pct": std_pct,
            "ts": time.time(),
        }
        tmp_path = target_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.rename(tmp_path, target_path)
        return Response(json.dumps({"ok": True, **payload}, ensure_ascii=False),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/mvc_calibrate', methods=['POST'])
def api_mvc_calibrate():
    """V4.7 动态 MVC 校准触发端点（与 /api/mvc_calibration 区分：本接口真正触发硬件采集）。
    协议：前端 POST → 写 /dev/shm/mvc_calibrate.request → udp_emg_server 进入 3 秒峰值采集
          → udp_emg_server 写 /dev/shm/mvc_calibrate.result → 本端点轮询返回。
    返回 {ok, target, comp, duration_ms} 或 {ok:false, error:"timeout"}。
    """
    req_path = '/dev/shm/mvc_calibrate.request'
    res_path = '/dev/shm/mvc_calibrate.result'
    t0 = time.time()
    try:
        # 清理旧结果 + 提交请求
        try:
            os.remove(res_path)
        except OSError:
            pass
        with open(req_path, 'w') as _rf:
            _rf.write(str(t0))
        # 轮询 5 秒等 udp_emg_server 完成 3 秒采集 + 写盘
        for _ in range(25):
            time.sleep(0.2)
            if os.path.exists(res_path):
                with open(res_path, 'r') as _f:
                    payload = json.load(_f)
                return Response(json.dumps({
                    "ok": True,
                    "target": payload.get("target"),
                    "comp": payload.get("comp"),
                    "duration_ms": int((time.time() - t0) * 1000),
                }), mimetype='application/json')
        return Response(json.dumps({"ok": False, "error": "timeout"}),
                        mimetype='application/json', status=504)
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


# ========================================================================
# V7.16 测试采集 (FSM-对齐 rep) — 3 端点, 协议同 mvc_calibrate (shm trigger)
# ========================================================================
_TEST_CAPTURE_SESSION_SHM = '/dev/shm/test_capture.session'
_TEST_CAPTURE_STOP_SHM = '/dev/shm/test_capture.stop'
_TEST_CAPTURE_RESULT_SHM = '/dev/shm/test_capture.result'
_TEST_CAPTURE_ACK_SHM = '/dev/shm/test_capture.session.ack'
_TEST_CAPTURE_EXERCISES = ('squat', 'bicep_curl')
_TEST_CAPTURE_LABELS = ('standard', 'compensating', 'non_standard')

def _test_capture_root():
    # PROJECT_ROOT 定义在 L742, 本段代码位置更靠前, 所以懒加载
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'data', 'test_capture')


def _test_capture_safe_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


@app.route('/api/test_capture/start', methods=['POST'])
def api_test_capture_start():
    """
    V7.16 启动一次测试采集会话.
    入参: {"exercise":"squat"|"bicep_curl", "label":"standard"|"compensating"|"non_standard"}
    动作:
      1) 调 FitnessDB.start_session(exercise) 拿 session_id (失败则用时间戳兜底)
      2) mkdir -p data/test_capture/{exercise}/{label}/{YYYYMMDD_HHMMSS}_{sid}
      3) 写 /dev/shm/test_capture.session
      4) sleep 0.5s 检查 .session.ack, 未 ack 仅警告不阻断
    出参: {"ok", "session_id", "out_dir", "ack"}
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        exercise = str(data.get("exercise", "")).strip()
        label = str(data.get("label", "")).strip()
        if exercise not in _TEST_CAPTURE_EXERCISES:
            return Response(json.dumps({"ok": False, "error": "invalid exercise"}),
                            mimetype='application/json', status=400)
        if label not in _TEST_CAPTURE_LABELS:
            return Response(json.dumps({"ok": False, "error": "invalid label"}),
                            mimetype='application/json', status=400)

        # 已有 active session? 拒绝重复启动
        if os.path.exists(_TEST_CAPTURE_SESSION_SHM):
            return Response(json.dumps({
                "ok": False,
                "error": "session already active, stop it first"
            }), mimetype='application/json', status=409)

        # DB session_id (失败时用 int(time.time()) 兜底)
        sid = None
        db = _get_db()
        if db is not None:
            try:
                sid = db.start_session(exercise)
            except Exception as e:
                logging.warning("test_capture start_session 失败: %s", e)
        if not isinstance(sid, int):
            sid = int(time.time()) % 100000

        # 组装目录
        ts_str = time.strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(_test_capture_root(), exercise, label,
                               "{}_{}".format(ts_str, sid))
        # 冲突时追加 _b
        if os.path.exists(out_dir):
            out_dir = out_dir + "_b"
        os.makedirs(out_dir, exist_ok=True)

        # 清掉旧的 result / ack
        _test_capture_safe_remove(_TEST_CAPTURE_RESULT_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_ACK_SHM)

        # 写 session 信号
        payload = {
            "enabled": True,
            "session_id": sid,
            "exercise": exercise,
            "label": label,
            "out_dir": out_dir,
            "started_ts": time.time(),
        }
        tmp = _TEST_CAPTURE_SESSION_SHM + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.rename(tmp, _TEST_CAPTURE_SESSION_SHM)

        # 等 0.5s 看模拟器是否 ack (确认存活)
        ack = False
        for _ in range(5):
            time.sleep(0.1)
            if os.path.exists(_TEST_CAPTURE_ACK_SHM):
                ack = True
                break

        resp = {
            "ok": True,
            "session_id": sid,
            "exercise": exercise,
            "label": label,
            "out_dir": out_dir,
            "ack": ack,
        }
        if not ack:
            resp["warning"] = "simulator did not ack within 0.5s — ensure simulate_emg_from_{mia,bicep}.py is running"
        return Response(json.dumps(resp, ensure_ascii=False),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/test_capture/stop', methods=['POST'])
def api_test_capture_stop():
    """
    V7.16 停止采集并落盘.
    动作:
      1) 写 /dev/shm/test_capture.stop {"discard": false}
      2) 轮询 /dev/shm/test_capture.result (最多 5s, 0.2s 一次)
      3) 解析结果, 调 FitnessDB.end_session (best-effort)
      4) 清 session/stop/ack 信号
    出参: {"ok", "rep_count", "raw_rows", "duration_s", "out_dir"} 或 {"ok":false,"error":"timeout"}
    """
    try:
        # 无活动 session 时直接返回
        if not (os.path.exists(_TEST_CAPTURE_SESSION_SHM) or
                os.path.exists(_TEST_CAPTURE_RESULT_SHM)):
            return Response(json.dumps({"ok": False, "error": "no active capture session"}),
                            mimetype='application/json', status=409)

        # 写 stop 信号
        with open(_TEST_CAPTURE_STOP_SHM + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"discard": False}, f)
        os.rename(_TEST_CAPTURE_STOP_SHM + ".tmp", _TEST_CAPTURE_STOP_SHM)

        # 轮询 result (5s 上限)
        result = None
        for _ in range(25):
            time.sleep(0.2)
            if os.path.exists(_TEST_CAPTURE_RESULT_SHM):
                try:
                    with open(_TEST_CAPTURE_RESULT_SHM, "r", encoding="utf-8") as f:
                        result = json.load(f)
                    break
                except (IOError, ValueError):
                    continue
        if result is None:
            # 超时但仍清理信号
            _test_capture_safe_remove(_TEST_CAPTURE_STOP_SHM)
            return Response(json.dumps({"ok": False, "error": "timeout waiting for simulator flush"}),
                            mimetype='application/json', status=504)

        # best-effort DB end_session
        sid = result.get("session_id")
        if isinstance(sid, int):
            db = _get_db()
            if db is not None:
                try:
                    reps = int(result.get("rep_count", 0))
                    # 简化: good=rep_count, failed=0, peak_fatigue=0 (capture 模式不追踪)
                    db.end_session(sid, good=reps, failed=0, fatigue_peak=0.0)
                except Exception as e:
                    logging.warning("test_capture end_session 失败: %s", e)

        # 清理所有信号
        _test_capture_safe_remove(_TEST_CAPTURE_RESULT_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_STOP_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_ACK_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_SESSION_SHM)

        return Response(json.dumps(result, ensure_ascii=False),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/test_capture/clear', methods=['POST'])
def api_test_capture_clear():
    """
    V7.16 清空当前缓冲不落盘.
    动作: 写 /dev/shm/test_capture.stop {"discard": true}, 轮询 result.
    出参: {"ok", "discarded": true}
    """
    try:
        if not os.path.exists(_TEST_CAPTURE_SESSION_SHM):
            return Response(json.dumps({"ok": False, "error": "no active capture session"}),
                            mimetype='application/json', status=409)

        with open(_TEST_CAPTURE_STOP_SHM + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"discard": True}, f)
        os.rename(_TEST_CAPTURE_STOP_SHM + ".tmp", _TEST_CAPTURE_STOP_SHM)

        result = None
        for _ in range(15):
            time.sleep(0.2)
            if os.path.exists(_TEST_CAPTURE_RESULT_SHM):
                try:
                    with open(_TEST_CAPTURE_RESULT_SHM, "r", encoding="utf-8") as f:
                        result = json.load(f)
                    break
                except (IOError, ValueError):
                    continue

        # 清所有信号
        _test_capture_safe_remove(_TEST_CAPTURE_RESULT_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_STOP_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_ACK_SHM)
        _test_capture_safe_remove(_TEST_CAPTURE_SESSION_SHM)

        if result is None:
            return Response(json.dumps({"ok": True, "discarded": True,
                                        "note": "simulator did not respond; signals cleared"}),
                            mimetype='application/json')
        return Response(json.dumps(result, ensure_ascii=False),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/test_capture/status')
def api_test_capture_status():
    """V7.16 查询当前采集状态 (前端轮询用)."""
    active = os.path.exists(_TEST_CAPTURE_SESSION_SHM)
    ack = os.path.exists(_TEST_CAPTURE_ACK_SHM)
    payload = {"active": active, "ack": ack}
    if active:
        try:
            with open(_TEST_CAPTURE_SESSION_SHM, "r", encoding="utf-8") as f:
                sess = json.load(f)
            payload["session_id"] = sess.get("session_id")
            payload["exercise"] = sess.get("exercise")
            payload["label"] = sess.get("label")
            payload["started_ts"] = sess.get("started_ts")
            # FSM 实时 rep 计数
            try:
                with open("/dev/shm/fsm_state.json", "r", encoding="utf-8") as ff:
                    s = json.load(ff)
                payload["fsm_reps"] = int(s.get("good", 0)) + int(s.get("failed", 0)) + int(s.get("comp", 0))
            except (IOError, ValueError):
                payload["fsm_reps"] = -1
        except (IOError, ValueError):
            pass
    return Response(json.dumps(payload, ensure_ascii=False),
                    mimetype='application/json')


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
        # V4.8: drop signal so cloud_rtmpose_client drains its queue (prevents stuck request freeze)
        try:
            with open("/dev/shm/vision_reset.flag", "w") as _f:
                _f.write("1")
        except Exception:
            pass
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


# ===== Feishu Smart Push API (Nexus Enabled) =====

@app.route('/api/feishu/ping', methods=['GET'])
def feishu_ping():
    """V7.20 (2026-04-20): 纯飞书链路自检 —— 只测 token+msg, 不调 DeepSeek.
    返回 {ok, token_ms, send_ms, total_ms, error?} 方便快速定位"是否真的是飞书失败"。
    """
    import urllib.request, urllib.error, ssl
    api_cfg = _load_api_config()
    def _pick(*keys):
        for k in keys:
            v = api_cfg.get(k)
            if v: return v
        return ""
    fid = _pick("FEISHU_APP_ID", "feishu_app_id")
    fsec = _pick("FEISHU_APP_SECRET", "feishu_app_secret")
    fcid = _pick("FEISHU_CHAT_ID", "feishu_chat_id")
    if not (fid and fsec and fcid):
        return Response(json.dumps({"ok": False, "error": "缺少 FEISHU_APP_ID/SECRET/CHAT_ID"}),
                        mimetype='application/json', status=400)
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    t0 = time.time()
    try:
        tok_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": fid, "app_secret": fsec}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        tok_resp = json.loads(urllib.request.urlopen(tok_req, timeout=8, context=ctx).read())
        t1 = time.time()
        if tok_resp.get("code") != 0:
            return Response(json.dumps({"ok": False, "stage": "token", "token_ms": int((t1-t0)*1000),
                                        "error": tok_resp.get("msg", "unknown"), "code": tok_resp.get("code")}),
                            mimetype='application/json', status=502)
        tok = tok_resp.get("tenant_access_token", "")
        msg_req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            data=json.dumps({"receive_id": fcid, "msg_type": "text",
                             "content": json.dumps({"text": "🏓 IronBuddy feishu-ping " + time.strftime('%H:%M:%S')})}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok})
        msg_resp = json.loads(urllib.request.urlopen(msg_req, timeout=10, context=ctx).read())
        t2 = time.time()
        ok = (msg_resp.get("code") == 0)
        return Response(json.dumps({"ok": ok, "token_ms": int((t1-t0)*1000), "send_ms": int((t2-t1)*1000),
                                    "total_ms": int((t2-t0)*1000),
                                    "error": None if ok else msg_resp.get("msg", "unknown"),
                                    "code": msg_resp.get("code")}),
                        mimetype='application/json', status=200 if ok else 502)
    except urllib.error.HTTPError as e:
        return Response(json.dumps({"ok": False, "error": "HTTPError %d" % e.code, "detail": str(e)}),
                        mimetype='application/json', status=502)
    except urllib.error.URLError as e:
        return Response(json.dumps({"ok": False, "error": "URLError", "detail": str(e.reason)}),
                        mimetype='application/json', status=504)
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": "exception", "detail": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/feishu/push', methods=['POST'])
@app.route('/api/feishu/send_plan', methods=['POST'])
def feishu_smart_push():
    """Generate intelligent content via DeepSeek + SQLite Nexus and push to Feishu.
    V7.21: 互斥锁 + max_tokens + 降级 + 精准错误（防止"服务器忙碌"假警报）"""
    # V7.21: 并发互斥 —— 第二次点击立刻返回 429，不排队阻塞
    if not _FEISHU_PUSH_LOCK.acquire(blocking=False):
        _busy_for = int(time.time() - _FEISHU_PUSH_STARTED_AT[0])
        return Response(json.dumps({"ok": False, "error": "上一次推送仍在进行", "busy_for_s": _busy_for}),
                        mimetype='application/json', status=429)
    _FEISHU_PUSH_STARTED_AT[0] = time.time()
    try:
        return _feishu_smart_push_impl()
    finally:
        _FEISHU_PUSH_LOCK.release()


def _feishu_smart_push_impl():
    api_cfg = _load_api_config()
    # V4.8: case-insensitive config read (.api_config.json uses UPPERCASE,
    # but some legacy callers write lowercase). Always try both.
    def _pick(*keys):
        for k in keys:
            v = api_cfg.get(k)
            if v:
                return v
        return ""
    ds_key = _pick("DEEPSEEK_API_KEY", "deepseek_api_key")
    feishu_app_id = _pick("FEISHU_APP_ID", "feishu_app_id")
    feishu_app_secret = _pick("FEISHU_APP_SECRET", "feishu_app_secret")
    feishu_chat_id = _pick("FEISHU_CHAT_ID", "feishu_chat_id")
    feishu_webhook = _pick("FEISHU_WEBHOOK", "feishu_webhook")

    if not ds_key:
        return Response(json.dumps({"ok": False, "error": "DeepSeek API Key 未配置"}),
                        mimetype='application/json', status=400)
    if not feishu_app_id or not feishu_chat_id:
        if not feishu_webhook:
            return Response(json.dumps({"ok": False,
                "error": "飞书凭证未配置 (需要 APP_ID+CHAT_ID 或 WEBHOOK)"}),
                            mimetype='application/json', status=400)

    # Read current instantaneous training state
    fsm_data = {}
    try:
        if os.path.exists("/dev/shm/fsm_state.json"):
            with open("/dev/shm/fsm_state.json", "r") as f:
                fsm_data = json.load(f)
    except Exception:
        pass

    # Extract dynamic properties from request
    req_data = request.get_json(silent=True) or {}
    push_type = req_data.get("type", "plan")
    custom_prompt = req_data.get("prompt", "")

    # Engage Cognitive Nexus to build enriched prompts
    try:
        import sys
        _he_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hardware_engine')
        if _he_path not in sys.path:
            sys.path.append(_he_path)
        from cognitive.cognitive_nexus import CognitiveNexus
        nexus_proxy = CognitiveNexus()
        prompts = nexus_proxy.build_prompt_for_type(push_type, fsm_data, custom_prompt)
        sys_prompt = prompts["system"]
        user_prompt = prompts["user"]
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": "Cognitive Nexus 挂载失败: " + str(e)}),
                        mimetype='application/json', status=500)

    # Call DeepSeek with historical context
    import urllib.request
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def _call_deepseek(sys_p, user_p, timeout_s):
        _payload = json.dumps({
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": sys_p},
                {"role": "user", "content": user_p}
            ],
            "temperature": 0.6,
            "max_tokens": 400,   # V7.21: 硬限输出长度 —— 高峰期 DeepSeek 吐 2000+ token 能拖 25s+, 限 400 通常 5-10s 可返
        }).encode("utf-8")
        _req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=_payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + ds_key,
            },
        )
        _t0 = time.time()
        _resp = json.loads(urllib.request.urlopen(_req, timeout=timeout_s, context=ctx).read())
        _elapsed = time.time() - _t0
        return _resp["choices"][0]["message"]["content"], _elapsed

    # V7.21 (2026-04-21): DeepSeek 失败不再 return 500 —— 改为降级纯数据模板, 仍推飞书
    # 原链路: DeepSeek 挂 → 500 → voice_daemon 兜底"服务器忙碌" → 用户看不到任何飞书消息
    # 新链路: DeepSeek 挂 → degraded=True + 模板文本 → 飞书仍推送 → 用户至少收到基础战报
    bot_reply = ""
    degraded = False
    deepseek_elapsed = 0.0
    deepseek_err = None
    for _attempt in (1, 2):   # V7.21: 20s → 15s 两次, 总最坏 35s < voice_daemon 75s 超时
        _tmo = 20 if _attempt == 1 else 15
        try:
            bot_reply, deepseek_elapsed = _call_deepseek(sys_prompt, user_prompt, _tmo)
            logging.info("[feishu_push] DeepSeek 成功 尝试%d 耗时%.1fs 长度%d", _attempt, deepseek_elapsed, len(bot_reply))
            break
        except Exception as e:
            deepseek_err = str(e)[:120]
            logging.warning("[feishu_push] DeepSeek 失败 尝试%d: %s", _attempt, deepseek_err)

    if not bot_reply:
        # DeepSeek 连续两次失败 → 降级模板
        degraded = True
        _good = fsm_data.get("good", 0)
        _failed = fsm_data.get("failed", 0)
        _comp = fsm_data.get("comp", 0)
        _fatigue = fsm_data.get("fatigue", 0)
        _ex = "弯举" if fsm_data.get("exercise") == "bicep_curl" else "深蹲"
        bot_reply = (
            "⚠️ AI 点评暂不可用（%s）—— 以下为原始战报：\n\n"
            "**动作**：%s\n"
            "**标准**：%s 次\n"
            "**不标准**：%s 次\n"
            "**代偿**：%s 次\n"
            "**疲劳池**：%.0f / 1500\n\n"
            "_（DeepSeek 可能在限流或超时，飞书推送仍保障送达）_"
        ) % (deepseek_err or "unknown", _ex, _good, _failed, _comp, float(_fatigue or 0))

    # Push intelligent reply back to Feishu
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
        # V7.20: 飞书 token timeout 10→8s
        token_resp = json.loads(urllib.request.urlopen(token_req, timeout=8, context=ctx).read())
        access_token = token_resp.get("tenant_access_token", "")

        # Format Final Feishu Message
        import datetime
        type_banner = {
            "plan": "🏋️ IronBuddy 训练规划与处方",
            "summary": "🏆 IronBuddy 多日长效训练战报",
            "reminder": "🚨 IronBuddy 身体警钟与状态通报"
        }.get(push_type, "🤖 IronBuddy 助理播报")

        feishu_text = f"{type_banner}\n📅 {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n{bot_reply}"
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
            # V7.21: 回传 degraded + elapsed_s, 供 voice_daemon 和前端区分提示语
            logging.info("[feishu_push] ✅ 飞书送达 degraded=%s ds_elapsed=%.1fs", degraded, deepseek_elapsed)
            return Response(json.dumps({
                "ok": True,
                "type_triggered": push_type,
                "plan": bot_reply,
                "degraded": degraded,
                "elapsed_s": round(deepseek_elapsed, 2),
                "ds_error": deepseek_err if degraded else None,
            }), mimetype='application/json')
        else:
            return Response(json.dumps({"ok": False, "error": "飞书发送失败", "detail": str(msg_resp)}),
                            mimetype='application/json', status=502)
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": "飞书推送链路中断: " + str(e), "plan": bot_reply}),
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
BOARD_TARGET = "toybrick@10.18.76.224"
BOARD_KEY_PATH = os.path.expanduser("~/.ssh/id_rsa_toybrick")
CLOUD_SSH = "root@connect.westd.seetacloud.com"
CLOUD_PORT = 42924  # V4.5 2026-04-18 新实例端口
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
        "board_ip": "10.18.76.224",
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

    # V7.17: 全量启动时的初始化清理 —— 保证每次"一键启动"都是干净基线
    if target == "all":
        # 1) 推理模式强制回到 pure_vision (上次若切到 vision_sensor, 文件不擦会被继承)
        try:
            _ts = str(int(time.time()))
            _tmp_im = "/dev/shm/inference_mode.json.tmp"
            with open(_tmp_im, "w", encoding="utf-8") as _imf:
                _imf.write('{"mode":"pure_vision","ts":' + _ts + '}')
            os.rename(_tmp_im, "/dev/shm/inference_mode.json")
        except Exception:
            pass
        # 2) 清理语音守护的残留信号文件 —— chat_active / voice_interrupt / mute_signal
        #    若上次会话在长对话中被杀, chat_active 会残留导致新启动的唤醒词被吞
        for _stale in ("/dev/shm/chat_active",
                       "/dev/shm/voice_interrupt",
                       "/dev/shm/mute_signal.json"):
            try:
                if os.path.exists(_stale):
                    os.remove(_stale)
            except Exception:
                pass

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
        "board": {"online": False, "ip": "10.18.76.224"},
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


# Keys that get masked in GET responses (sensitive).
_API_CONFIG_SENSITIVE_KEYS = (
    'DEEPSEEK_API_KEY', 'deepseek_api_key', 'BAIDU_API_KEY', 'BAIDU_SECRET_KEY',
    'FEISHU_APP_SECRET', 'FEISHU_WEBHOOK', 'CLOUD_SSH_PASSWORD',
)

# Keys accepted from POST requests (SSH credentials intentionally excluded).
_API_CONFIG_WRITE_WHITELIST = (
    'deepseek_api_key', 'llm_backend',
    'BAIDU_APP_ID', 'BAIDU_API_KEY', 'BAIDU_SECRET_KEY',
    'FEISHU_APP_ID', 'FEISHU_APP_SECRET', 'FEISHU_CHAT_ID', 'FEISHU_WEBHOOK',
    'CLOUD_RTMPOSE_URL',
)


def _mask_secret(val):
    if not isinstance(val, str) or not val:
        return val
    if len(val) > 10:
        return val[:6] + '****' + val[-4:]
    return '****'


@app.route('/api/admin/api_config', methods=['GET', 'POST'])
def admin_api_config():
    if request.method == 'GET':
        cfg = _load_api_config()
        masked = {}
        for k, v in cfg.items():
            if k in _API_CONFIG_SENSITIVE_KEYS:
                masked[k] = _mask_secret(v)
            else:
                masked[k] = v
        return Response(json.dumps(masked), mimetype='application/json')
    else:
        data = request.get_json(silent=True) or {}
        cfg = _load_api_config()
        for key in _API_CONFIG_WRITE_WHITELIST:
            if key not in data:
                continue
            val = data[key]
            # Skip masked placeholders — caller didn't actually re-enter the secret.
            if isinstance(val, str) and '****' in val:
                continue
            cfg[key] = val
        _save_api_config(cfg)
        return Response(json.dumps({"ok": True}), mimetype='application/json')


@app.route('/api/admin/cloud_verify', methods=['GET'])
def admin_cloud_verify():
    """Probe CLOUD_RTMPOSE_URL's /health endpoint; report latency + status."""
    cfg = _load_api_config()
    url = cfg.get('CLOUD_RTMPOSE_URL', '')
    if not url:
        return Response(json.dumps({"ok": False, "error": "CLOUD_RTMPOSE_URL 未配置"}), mimetype='application/json')
    try:
        health_url = url.rsplit('/', 1)[0] + '/health'
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": "URL 解析失败: " + str(e)}), mimetype='application/json')
    t0 = time.time()
    try:
        resp = requests.get(health_url, timeout=3)
        t1 = time.time()
        status = None
        try:
            status = resp.json().get('status')
        except Exception:
            status = str(resp.status_code)
        return Response(json.dumps({
            "ok": True,
            "status": status,
            "latency_ms": int((t1 - t0) * 1000),
        }), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}), mimetype='application/json')


@app.route('/api/admin/reload_service', methods=['POST'])
def admin_reload_service():
    """V4.8: Restart a service so it picks up freshly-saved .api_config.json values.
    Accepts body {"service": "voice" | "tunnel"}. Safe on WSL dev host (no-op)."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        svc = data.get("service", "voice")
        root = "/home/toybrick/streamer_v3"
        if not os.path.isdir(root):
            return Response(json.dumps({"ok": False, "error": "not on board"}),
                            mimetype='application/json')
        import subprocess as _sp
        if svc == "voice":
            _sp.run(["pkill", "-f", "[v]oice_daemon"], timeout=5)
            time.sleep(1)
            # Relaunch via wrapper that re-reads .api_config.json
            _sp.Popen(
                ["setsid", "nohup", "bash", root + "/scripts/start_voice_with_env.sh"],
                stdout=open("/tmp/voice.log", "a"),
                stderr=_sp.STDOUT,
                stdin=_sp.DEVNULL,
                start_new_session=True,
            )
            return Response(json.dumps({"ok": True, "service": "voice",
                                        "msg": "voice_daemon 已重启，新凭证已加载"}),
                            mimetype='application/json')
        if svc == "tunnel":
            _sp.run(["pkill", "-f", "[s]sh.*-L.*6006:127.0.0.1:6006"], timeout=5)
            _sp.run(["pkill", "-f", "[c]loud_tunnel.py"], timeout=5)
            time.sleep(1)
            out = _sp.run(
                ["bash", root + "/scripts/cloud_tunnel.sh"],
                capture_output=True, timeout=20,
            )
            return Response(json.dumps({
                "ok": out.returncode == 0,
                "service": "tunnel",
                "msg": out.stdout.decode(errors='replace')[-200:]
                if out.stdout else out.stderr.decode(errors='replace')[-200:],
            }), mimetype='application/json')
        return Response(json.dumps({"ok": False, "error": "unknown service: " + svc}),
                        mimetype='application/json', status=400)
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


# ============================================================================
# 视觉特征探测 (Phase 0 · env-gated, 可一键回退)
# 启用：环境变量 IRONBUDDY_PROBE_ENABLED=1 启动 streamer 即激活 UI+接口
# 关闭：unset 环境变量重启 streamer，所有探测 UI 隐藏、接口返回 403
# 依赖：tools/vision_feature_probe_v2.py
# ============================================================================
_PROBE_ENABLED = os.environ.get('IRONBUDDY_PROBE_ENABLED', '0') == '1'
_PROBE_SCRIPT = os.path.join(PROJECT_ROOT, 'tools', 'vision_feature_probe_v2.py')
_PROBE_PID_FILE = '/dev/shm/probe_v2.pid'
_PROBE_FEATURES_JSONL = '/dev/shm/rep_features.jsonl'
_PROBE_LOG = '/tmp/probe_v2.log'


def _probe_is_running():
    """Check if probe_v2.py is alive by PID file + proc existence."""
    try:
        if not os.path.exists(_PROBE_PID_FILE):
            return False, None
        with open(_PROBE_PID_FILE, 'r') as f:
            pid = f.read().strip()
        if not pid or not pid.isdigit():
            return False, None
        if os.path.exists('/proc/{}'.format(pid)):
            return True, int(pid)
        # 陈旧 PID 文件，清掉
        try:
            os.remove(_PROBE_PID_FILE)
        except OSError:
            pass
        return False, None
    except Exception:
        return False, None


@app.route('/api/probe/enabled')
def probe_enabled():
    """UI 启动时查询, 只有启用时前端才会显示探测面板."""
    return Response(json.dumps({"enabled": _PROBE_ENABLED}), mimetype='application/json')


@app.route('/api/probe/start', methods=['POST'])
def probe_start():
    if not _PROBE_ENABLED:
        return Response(json.dumps({"ok": False, "error": "probe disabled"}),
                        mimetype='application/json', status=403)
    running, pid = _probe_is_running()
    if running:
        return Response(json.dumps({"ok": True, "status": "already running", "pid": pid}),
                        mimetype='application/json')
    if not os.path.exists(_PROBE_SCRIPT):
        return Response(json.dumps({"ok": False, "error": "script not found: " + _PROBE_SCRIPT}),
                        mimetype='application/json', status=500)
    try:
        import subprocess as _sp
        # nohup + setsid 保活，不占 streamer 子进程表
        _sp.Popen(
            ['nohup', 'python3', '-u', _PROBE_SCRIPT],
            stdout=open(_PROBE_LOG, 'a'),
            stderr=_sp.STDOUT,
            stdin=_sp.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.5)
        running, pid = _probe_is_running()
        return Response(json.dumps({"ok": bool(running), "pid": pid}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/probe/stop', methods=['POST'])
def probe_stop():
    if not _PROBE_ENABLED:
        return Response(json.dumps({"ok": False, "error": "probe disabled"}),
                        mimetype='application/json', status=403)
    _run_cmd("pkill -f 'vision_feature_probe_v2.py' 2>/dev/null", timeout=3)
    try:
        if os.path.exists(_PROBE_PID_FILE):
            os.remove(_PROBE_PID_FILE)
    except OSError:
        pass
    return Response(json.dumps({"ok": True}), mimetype='application/json')


@app.route('/api/probe/state')
def probe_state():
    """返回 {running, pid, current_label, features: [...]}"""
    if not _PROBE_ENABLED:
        return Response(json.dumps({"enabled": False, "running": False, "features": []}),
                        mimetype='application/json')
    running, pid = _probe_is_running()
    feats = []
    try:
        if os.path.exists(_PROBE_FEATURES_JSONL):
            with open(_PROBE_FEATURES_JSONL, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        feats.append(json.loads(line))
                    except ValueError:
                        continue
            feats = feats[-100:]  # 6 类录制需要更大窗口
    except (IOError, OSError):
        pass
    # 当前标签
    cur_label = "unlabeled"
    try:
        if os.path.exists('/dev/shm/probe_label.txt'):
            with open('/dev/shm/probe_label.txt', 'r') as f:
                cur_label = f.read().strip() or "unlabeled"
    except (IOError, OSError):
        pass
    return Response(json.dumps({
        "enabled": True,
        "running": bool(running),
        "pid": pid,
        "current_label": cur_label,
        "features": feats,
    }), mimetype='application/json')


_PROBE_ALLOWED_LABELS = {
    "squat_standard", "squat_compensating", "squat_non_standard",
    "curl_standard", "curl_compensating", "curl_non_standard",
    "unlabeled",
}


@app.route('/api/probe/set_label', methods=['POST'])
def probe_set_label():
    """UI 6 按钮调此接口切换当前标注. 写 /dev/shm/probe_label.txt (probe_v2 每 rep 结算时读)."""
    if not _PROBE_ENABLED:
        return Response(json.dumps({"ok": False, "error": "probe disabled"}),
                        mimetype='application/json', status=403)
    try:
        data = request.get_json(force=True, silent=True) or {}
        label = (data.get('label') or 'unlabeled').strip()
        if label not in _PROBE_ALLOWED_LABELS:
            return Response(json.dumps({"ok": False, "error": "invalid label: " + label}),
                            mimetype='application/json', status=400)
        tmp = '/dev/shm/probe_label.txt.tmp'
        with open(tmp, 'w') as f:
            f.write(label)
        os.rename(tmp, '/dev/shm/probe_label.txt')
        return Response(json.dumps({"ok": True, "label": label}),
                        mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/probe/clear', methods=['POST'])
def probe_clear():
    """清空 rep_features.jsonl, 重新开始采集."""
    if not _PROBE_ENABLED:
        return Response(json.dumps({"ok": False, "error": "probe disabled"}),
                        mimetype='application/json', status=403)
    try:
        if os.path.exists(_PROBE_FEATURES_JSONL):
            os.remove(_PROBE_FEATURES_JSONL)
        return Response(json.dumps({"ok": True}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/admin/fatigue_reset', methods=['POST'])
def admin_fatigue_reset():
    """V4.8: Drop signal file that FSM watches to zero out fatigue counter.
    Fires either from voice '清空疲劳' or auto-trigger when UI sees fatigue >= limit."""
    try:
        with open("/dev/shm/fatigue_reset.request.tmp", "w") as f:
            f.write(str(time.time()))
        os.rename("/dev/shm/fatigue_reset.request.tmp", "/dev/shm/fatigue_reset.request")
        return Response(json.dumps({"ok": True}), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/admin/voice_diag', methods=['GET'])
def admin_voice_diag():
    """V4.8: Diagnose why voice is silent. Returns full status tree:
      - baidu_configured: are all 3 BAIDU keys non-empty in .api_config.json?
      - voice_running: is voice_daemon process alive?
      - last_log_line: last error/info line from /tmp/voice.log
      - tts_volume: current vol level
      - alsa_mixer: current Playback Path value"""
    result = {}
    # 1. Check baidu keys
    try:
        cfg = _load_api_config()
        def _pick(*ks):
            for k in ks:
                v = cfg.get(k)
                if v:
                    return True
            return False
        result["baidu_configured"] = (
            _pick("BAIDU_APP_ID", "baidu_app_id") and
            _pick("BAIDU_API_KEY", "baidu_api_key") and
            _pick("BAIDU_SECRET_KEY", "baidu_secret_key")
        )
        result["baidu_app_id_head"] = (cfg.get("BAIDU_APP_ID") or cfg.get("baidu_app_id") or "")[:6]
    except Exception as e:
        result["baidu_configured"] = False
        result["baidu_err"] = str(e)

    # 2. Check voice_daemon process
    try:
        import subprocess as _sp
        out = _sp.run(["pgrep", "-f", "[v]oice_daemon.py"],
                      capture_output=True, timeout=3)
        result["voice_running"] = out.returncode == 0
        result["voice_pids"] = out.stdout.decode().strip().split()
    except Exception as e:
        result["voice_running"] = False
        result["voice_err"] = str(e)

    # 3. Last 5 log lines
    try:
        for log_path in ["/tmp/voice.log", "/tmp/voice_daemon.log"]:
            if os.path.exists(log_path):
                with open(log_path, "r") as f:
                    lines = f.readlines()[-5:]
                result["voice_log_tail"] = [l.rstrip() for l in lines]
                break
    except Exception:
        pass

    # 4. TTS volume
    try:
        if os.path.exists("/dev/shm/tts_volume.json"):
            with open("/dev/shm/tts_volume.json", "r") as f:
                result["tts_volume"] = json.load(f).get("vol", 7)
        else:
            result["tts_volume"] = 7
    except Exception:
        result["tts_volume"] = 7

    # 5. ALSA mixer state (Playback Path)
    try:
        import subprocess as _sp
        out = _sp.run(
            ["amixer", "-c", "0", "cget",
             "numid=1,iface=MIXER,name=Playback Path"],
            capture_output=True, timeout=3)
        result["alsa_playback_path"] = out.stdout.decode()[-120:] if out.stdout else ""
    except Exception:
        pass

    return Response(json.dumps(result, ensure_ascii=False), mimetype='application/json')


@app.route('/api/admin/voice_test', methods=['POST'])
def admin_voice_test():
    """V4.8: Trigger a test TTS playback. Writes to /dev/shm/chat_reply.txt
    so the existing speak() listener in voice_daemon picks it up.
    If Baidu not configured, falls back to `aplay` of a tone."""
    try:
        data = request.get_json(silent=True) or {}
        msg = data.get("msg", "你好，我是 IronBuddy 教练，语音系统工作正常")

        # Path A: if voice_daemon is running, poke its chat_reply.txt watcher
        # (it auto-speaks any new content in that file)
        with open("/dev/shm/chat_reply.txt.tmp", "w", encoding="utf-8") as f:
            f.write(msg)
        os.rename("/dev/shm/chat_reply.txt.tmp", "/dev/shm/chat_reply.txt")

        # Path B: ALSA sanity - play a 440Hz beep via speaker-test or sox if available
        # This verifies the hardware output path even without Baidu
        import subprocess as _sp
        beep_ok = False
        for cmd in [
            ["speaker-test", "-c", "2", "-t", "sine", "-f", "440", "-l", "1"],
            ["sh", "-c", "timeout 1 aplay /usr/share/sounds/alsa/Front_Center.wav"],
        ]:
            try:
                r = _sp.run(cmd, capture_output=True, timeout=4)
                if r.returncode == 0:
                    beep_ok = True
                    break
            except Exception:
                continue

        return Response(json.dumps({
            "ok": True,
            "message_queued": msg,
            "note": "若听见内容就是 TTS 通路 OK；若只听到静音请检查百度凭证+板端喇叭",
            "beep_fallback": beep_ok,
        }, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json', status=500)


@app.route('/api/fsm_state')
def api_fsm_state():
    """V4.8: Passthrough read of /dev/shm/fsm_state.json for UI polling.
    UI previously had no direct way to read angle/classification/emg."""
    try:
        path = "/dev/shm/fsm_state.json"
        if os.path.exists(path):
            with open(path, "r") as f:
                return Response(f.read(), mimetype='application/json')
    except Exception:
        pass
    return Response(json.dumps({"state": "IDLE", "good": 0, "failed": 0,
                                "angle": 0, "fatigue": 0, "exercise": "squat",
                                "classification": "standard"}),
                    mimetype='application/json')


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
        "board_ip": "10.18.76.224",
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


# ===== SQLite 历史数据 API (Sprint 5 新增, 懒加载 & 失败安全) =====
_db_singleton = [None]
def _get_db():
    if _db_singleton[0] is not None:
        return _db_singleton[0]
    try:
        from hardware_engine.persistence.db import FitnessDB
        _db = FitnessDB()
        _db.connect()
        _db_singleton[0] = _db
        return _db
    except Exception:
        return None

@app.route('/api/history/sessions')
def api_history_sessions():
    db = _get_db()
    data = db.get_recent_sessions(limit=20) if db is not None else []
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json')

@app.route('/api/history/today')
def api_history_today():
    db = _get_db()
    data = db.compute_daily_summary() if db is not None else {}
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json')

@app.route('/api/history/stats')
def api_history_stats():
    db = _get_db()
    data = db.get_range_stats(days=7) if db is not None else []
    return Response(json.dumps(data, ensure_ascii=False), mimetype='application/json')


# ===== 数据库可视化（Sprint 6, 一站式 DB Viewer）=====
_DB_VIEW_WHITELIST = {
    'training_sessions': {
        'order_by': 'started_at DESC',
        'exercise_col': 'exercise',
        'seed_col': 'is_demo_seed',
    },
    'rep_events': {
        'order_by': 'ts DESC',
        'exercise_col': 'exercise',
        'seed_col': 'is_demo_seed',
    },
    'voice_sessions': {
        'order_by': 'ts DESC',
        'exercise_col': None,
        'seed_col': 'is_demo_seed',
    },
    'preference_history': {
        'order_by': 'ts DESC',
        'exercise_col': None,
        'seed_col': 'is_demo_seed',
    },
    'system_prompt_versions': {
        'order_by': 'id DESC',
        'exercise_col': None,
        'seed_col': 'is_demo_seed',
    },
    'daily_summary': {
        'order_by': 'date DESC',
        'exercise_col': None,
        'seed_col': 'is_demo_seed',
    },
    'llm_log': {
        'order_by': 'ts DESC',
        'exercise_col': None,
        'seed_col': 'is_demo_seed',
    },
    'user_config': {
        'order_by': 'key ASC',
        'exercise_col': None,
        'seed_col': None,
    },
    'model_registry': {
        'order_by': 'exercise ASC, id ASC',
        'exercise_col': 'exercise',
        'seed_col': 'is_demo_seed',
    },
}


def _db_view_path():
    """Resolve local sqlite path for the DB viewer (dev machine)."""
    candidate = os.path.join(PROJECT_ROOT, 'data', 'ironbuddy.db')
    return candidate


@app.route('/database')
def database_page():
    """一站式数据库可视化页面。"""
    try:
        html_path = os.path.join(template_dir, 'database.html')
        with open(html_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        resp = Response(html_content, mimetype='text/html')
        resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        return resp
    except Exception as e:
        return '<h1>数据库页面加载失败</h1><p>%s</p>' % e, 500


@app.route('/api/db/tables')
def api_db_tables():
    """列出白名单表 + 每张表行数（区分种子/真实）。

    V5.1: 增加 exists 字段让前端能看出表不存在（之前只把 total 归 0 容易被
    误会成"表空"）。
    """
    import sqlite3 as _sq
    db_path = _db_view_path()
    db_exists = os.path.exists(db_path)
    db_size_kb = round(os.path.getsize(db_path) / 1024.0, 1) \
        if db_exists else 0
    result = []
    try:
        conn = _sq.connect(db_path)
        # 先扫一次 sqlite_master 拿到所有存在的表名
        existing = set(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        )
        for name, meta in _DB_VIEW_WHITELIST.items():
            exists = name in existing
            if not exists:
                result.append({
                    'name': name, 'total': 0, 'seed': 0, 'live': 0,
                    'exists': False,
                    'error': 'table not in schema (migration 未推送)',
                })
                continue
            try:
                total = conn.execute(
                    'SELECT COUNT(*) FROM ' + name
                ).fetchone()[0]
                seed = 0
                live = total
                if meta['seed_col']:
                    try:
                        seed = conn.execute(
                            'SELECT COUNT(*) FROM ' + name + ' WHERE ' +
                            meta['seed_col'] + '=1'
                        ).fetchone()[0]
                        live = total - seed
                    except Exception:
                        # seed_col 不存在（旧 schema 没这列），不算错
                        pass
                result.append({
                    'name': name,
                    'total': total,
                    'seed': seed,
                    'live': live,
                    'exists': True,
                })
            except Exception as e:
                result.append({
                    'name': name, 'total': 0, 'seed': 0, 'live': 0,
                    'exists': exists,
                    'error': str(e),
                })
        conn.close()
    except Exception as e:
        return Response(
            json.dumps({
                'error': str(e),
                'db_path': db_path,
                'db_exists': db_exists,
            }), status=500,
            mimetype='application/json'
        )
    return Response(
        json.dumps({
            'tables': result,
            'db_path': db_path,
            'db_exists': db_exists,
            'db_size_kb': db_size_kb,
        }, ensure_ascii=False),
        mimetype='application/json'
    )


@app.route('/api/db/diag')
def api_db_diag():
    """V5.1 · 深度诊断：DB 路径 / 大小 / WAL 状态 / schema 校验 / 迁移建议。

    前端 🩺 维护按钮点开后一次性展示所有关键信息，让用户自己就能看出是
    哪张表缺、哪个 migration 没跑。
    """
    import sqlite3 as _sq
    out = {
        'db_path': _db_view_path(),
        'db_exists': False,
        'db_size_kb': 0,
        'journal_mode': None,
        'sqlite_version': None,
        'tables_existing': [],
        'tables_missing': [],
        'rows': {},
        'cwd': os.getcwd(),
        'project_root': PROJECT_ROOT,
        'migrations_found': [],
        'recommendations': [],
    }
    try:
        if os.path.exists(out['db_path']):
            out['db_exists'] = True
            out['db_size_kb'] = round(
                os.path.getsize(out['db_path']) / 1024.0, 1
            )
            conn = _sq.connect(out['db_path'])
            out['journal_mode'] = conn.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0]
            out['sqlite_version'] = conn.execute(
                "SELECT sqlite_version()"
            ).fetchone()[0]
            existing = set(
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            )
            for name in _DB_VIEW_WHITELIST:
                if name in existing:
                    out['tables_existing'].append(name)
                    try:
                        out['rows'][name] = conn.execute(
                            'SELECT COUNT(*) FROM ' + name
                        ).fetchone()[0]
                    except Exception as e:
                        out['rows'][name] = 'ERR: ' + str(e)
                else:
                    out['tables_missing'].append(name)
            conn.close()
        else:
            out['recommendations'].append(
                '数据库文件不存在: ' + out['db_path'] +
                ' — 点击 "跑 Migration" 按钮，或检查 Flask CWD 是否正确'
            )
        # 扫本地可用的 migration 脚本
        scripts_dir = os.path.join(PROJECT_ROOT, 'scripts')
        if os.path.isdir(scripts_dir):
            for fn in sorted(os.listdir(scripts_dir)):
                if fn.startswith('migrate_') and fn.endswith('.sql'):
                    out['migrations_found'].append(fn)
        # 建议
        if out['tables_missing']:
            out['recommendations'].append(
                '缺失 %d 张表 — 点击 "🔧 跑 Migration" 按钮补齐 schema'
                % len(out['tables_missing'])
            )
        if out['db_exists']:
            empty = [t for t, c in out['rows'].items() if c == 0]
            if len(empty) >= 3:
                out['recommendations'].append(
                    '%d 张表空 — 点击 "📥 灌演示种子" 按钮生成 5 天演示数据'
                    % len(empty)
                )
    except Exception as e:
        out['error'] = str(e)
    return Response(
        json.dumps(out, ensure_ascii=False, indent=2, default=str),
        mimetype='application/json',
    )


# ---- V5.1 维护动作：白名单执行脚本（仅开发机/本机；板端按需打开）----
_MAINTENANCE_ACTIONS = {
    'seed_v50': {
        'label': '灌 V5.0 演示种子（5 天 × 成对 voice/llm）',
        'script': 'scripts/seed_v50_unified.py',
        'kind': 'python',
    },
    'cleanup': {
        'label': '清所有假数据（保真实 is_demo_seed=0 行）',
        'script': 'scripts/cleanup_fake_data.py',
        'kind': 'python',
    },
    'seed_models': {
        'label': '灌模型 + embeddings 种子',
        'script': 'scripts/seed_models_and_embeddings.py',
        'kind': 'python',
    },
    'migrate_core': {
        'label': '跑 core migration（voice_sessions 等 3 张新表）',
        'script': 'scripts/migrate_2026_04_20.sql',
        'kind': 'sql',
    },
    'migrate_models': {
        'label': '跑 model migration（model_registry + embeddings）',
        'script': 'scripts/migrate_2026_04_20_models.sql',
        'kind': 'sql',
    },
}


@app.route('/api/db/maintenance/<action>', methods=['POST'])
def api_db_maintenance(action):
    """V5.1 · 执行白名单维护脚本，返回 stdout/stderr 供前端展示。

    安全：
      - 动作必须在 _MAINTENANCE_ACTIONS 白名单里
      - 只执行项目内 scripts/ 目录下的文件
      - 60s 超时
      - 允许用 IRONBUDDY_DB_MAINT=0 环境变量关闭（板端默认可按需关）
    """
    if os.environ.get('IRONBUDDY_DB_MAINT', '1') == '0':
        return Response(
            json.dumps({
                'ok': False,
                'error': '维护 API 已被环境变量 IRONBUDDY_DB_MAINT=0 关闭',
            }, ensure_ascii=False),
            status=403, mimetype='application/json'
        )
    if action not in _MAINTENANCE_ACTIONS:
        return Response(
            json.dumps({
                'ok': False,
                'error': 'action not in whitelist',
                'available': list(_MAINTENANCE_ACTIONS.keys()),
            }, ensure_ascii=False),
            status=400, mimetype='application/json'
        )
    spec = _MAINTENANCE_ACTIONS[action]
    script_path = os.path.join(PROJECT_ROOT, spec['script'])
    if not os.path.isfile(script_path):
        return Response(
            json.dumps({
                'ok': False, 'error': 'script not found',
                'path': script_path,
            }, ensure_ascii=False),
            status=404, mimetype='application/json'
        )

    import subprocess
    t0 = time.time()
    try:
        if spec['kind'] == 'python':
            proc = subprocess.run(
                ['python3', script_path],
                cwd=PROJECT_ROOT,
                capture_output=True, timeout=60,
            )
            stdout = proc.stdout.decode('utf-8', errors='replace')
            stderr = proc.stderr.decode('utf-8', errors='replace')
            rc = proc.returncode
        elif spec['kind'] == 'sql':
            # 用 Python 的 sqlite3 模块直接跑 SQL 脚本（避免依赖系统 sqlite3 CLI）
            import sqlite3 as _sq
            with open(script_path, 'r', encoding='utf-8') as f:
                sql_text = f.read()
            # 备份一份
            db_path = _db_view_path()
            bak = db_path + '.bak_' + str(int(time.time()))
            if os.path.exists(db_path):
                import shutil
                shutil.copy2(db_path, bak)
            conn = _sq.connect(db_path)
            conn.executescript(sql_text)
            conn.close()
            stdout = '已执行 %s\n备份: %s' % (spec['script'], bak)
            stderr = ''
            rc = 0
        else:
            return Response(
                json.dumps({
                    'ok': False, 'error': 'unknown script kind',
                }),
                status=500, mimetype='application/json'
            )
    except subprocess.TimeoutExpired:
        return Response(
            json.dumps({
                'ok': False, 'error': 'timeout (60s)', 'action': action,
            }, ensure_ascii=False),
            status=504, mimetype='application/json'
        )
    except Exception as e:
        return Response(
            json.dumps({
                'ok': False, 'error': str(e), 'action': action,
            }, ensure_ascii=False),
            status=500, mimetype='application/json'
        )
    elapsed = round(time.time() - t0, 2)
    return Response(
        json.dumps({
            'ok': rc == 0, 'action': action, 'label': spec['label'],
            'returncode': rc, 'elapsed_s': elapsed,
            'stdout': stdout[-4000:],  # 截尾防过长
            'stderr': stderr[-2000:],
        }, ensure_ascii=False),
        mimetype='application/json'
    )


@app.route('/api/db/maintenance/list')
def api_db_maintenance_list():
    """V5.1 · 返回所有可用维护动作（给前端渲染按钮用）。"""
    avail = [
        {'key': k, 'label': v['label'], 'kind': v['kind'],
         'script': v['script']}
        for k, v in _MAINTENANCE_ACTIONS.items()
    ]
    return Response(
        json.dumps({
            'actions': avail,
            'enabled': os.environ.get('IRONBUDDY_DB_MAINT', '1') != '0',
        }, ensure_ascii=False),
        mimetype='application/json'
    )


@app.route('/api/db/query/<table>')
def api_db_query(table):
    """读白名单表，支持动作筛选 / 种子开关 / 行数限制。

    Query params:
      exercise: 过滤 exercise 列（如果该表有）
      seed: all(默认) / seed / live
      limit: 默认 200，上限 2000
    """
    import sqlite3 as _sq

    if table not in _DB_VIEW_WHITELIST:
        return Response(
            json.dumps({'error': 'table not in whitelist'}), status=400,
            mimetype='application/json'
        )
    meta = _DB_VIEW_WHITELIST[table]

    try:
        limit = int(request.args.get('limit', 200))
    except Exception:
        limit = 200
    limit = max(1, min(limit, 2000))

    exercise = request.args.get('exercise', '').strip()
    seed_mode = request.args.get('seed', 'all').strip().lower()

    where = []
    params = []
    if exercise and meta['exercise_col']:
        where.append(meta['exercise_col'] + '=?')
        params.append(exercise)
    if meta['seed_col']:
        if seed_mode == 'seed':
            where.append(meta['seed_col'] + '=1')
        elif seed_mode == 'live':
            where.append(meta['seed_col'] + '=0')

    sql = 'SELECT * FROM ' + table
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY ' + meta['order_by']
    sql += ' LIMIT ' + str(limit)

    try:
        conn = _sq.connect(_db_view_path())
        conn.row_factory = _sq.Row
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description]
        rows = [list(r) for r in cur.fetchall()]
        conn.close()
        return Response(
            json.dumps({
                'table': table, 'columns': cols, 'rows': rows,
                'count': len(rows), 'limit': limit,
                'filter': {'exercise': exercise, 'seed': seed_mode},
            }, ensure_ascii=False, default=str),
            mimetype='application/json'
        )
    except Exception as e:
        return Response(
            json.dumps({'error': str(e), 'sql': sql}), status=500,
            mimetype='application/json'
        )


@app.route('/api/db/update/<table>/<int:row_id>', methods=['POST'])
def api_db_update(table, row_id):
    """隐藏写接口：只允许 voice_sessions + 白名单字段。用于伪造演示对话。

    POST JSON: {"field": "transcript|response|summary|duration_s|trigger_src",
                "value": "..."}
    """
    if table != 'voice_sessions':
        return Response(
            json.dumps({'ok': False, 'error': 'table not editable'}),
            status=403, mimetype='application/json'
        )
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}
    field = body.get('field', '')
    value = body.get('value', '')
    if not field:
        return Response(
            json.dumps({'ok': False, 'error': 'field required'}),
            status=400, mimetype='application/json'
        )
    db = _get_db()
    if db is None:
        return Response(
            json.dumps({'ok': False, 'error': 'db unavailable'}),
            status=500, mimetype='application/json'
        )
    ok = db.update_voice_session_field(row_id, field, value)
    return Response(
        json.dumps({'ok': bool(ok), 'field': field, 'id': row_id},
                   ensure_ascii=False),
        status=(200 if ok else 400), mimetype='application/json'
    )


@app.route('/api/db/embeddings')
def api_db_embeddings():
    """(deprecated V5.0) 旧 PCA 散点接口，保留兼容；新前端已改用 feature_dist。
    """
    ex = request.args.get('exercise', '').strip() or None
    db = _get_db()
    points = db.get_feature_embeddings(ex) if db is not None else []
    return Response(
        json.dumps({'points': points, 'deprecated': True}, ensure_ascii=False),
        mimetype='application/json'
    )


# ============================================================
# V5.0 · 7D 维度对比：从真实 CSV 读 3 类样本，算直方图 + F 统计
# ============================================================

# 7 个维度定义（与 tools/dashboard.py 对齐）
_FEAT_DIMS = ["Ang_Vel", "Angle", "Ang_Accel", "Target_RMS", "Comp_RMS",
              "Symmetry_Score", "Phase_Progress"]
_FEAT_CN = {
    "Ang_Vel": "角速度", "Angle": "关节角度", "Ang_Accel": "角加速度",
    "Target_RMS": "目标肌 EMG", "Comp_RMS": "代偿肌 EMG",
    "Symmetry_Score": "对称性", "Phase_Progress": "动作阶段",
}

# label 映射：CSV 里是 golden/lazy/bad，前端想显示 standard/compensating/non_standard
_LABEL_MAP = {
    "golden": "standard",
    "lazy": "compensating",
    "bad": "non_standard",
}

# 每个 exercise × label 的 CSV 目录。优先真实采集，fallback 到 MIA/augmented。
_CSV_DIRS = {
    "bicep_curl": {
        "golden": [
            "data/bicep_curl/golden",
            "data/bicep_curl_augmented/golden",
        ],
        "lazy": [
            "data/bicep_curl/lazy",
            "data/bicep_curl_augmented/lazy",
        ],
        "bad": [
            "data/bicep_curl/bad",
            "data/bicep_curl_augmented/bad",
        ],
    },
    "squat": {
        "golden": ["data/mia/squat/golden"],
        "lazy":   ["data/mia/squat/lazy"],
        "bad":    ["data/mia/squat/bad"],
    },
}

# 每类最多合并多少个文件（控制内存与耗时）
_MAX_FILES_PER_LABEL = 6
# 每类最多采样多少行
_MAX_ROWS_PER_LABEL = 6000


def _load_feature_values(exercise, label, dim):
    """读取指定 exercise×label 的 CSV，抽取某维列，返回 float 列表。"""
    import csv as _csv
    dirs = _CSV_DIRS.get(exercise, {}).get(label, [])
    out = []
    files_used = 0
    for d in dirs:
        abs_dir = os.path.join(PROJECT_ROOT, d)
        if not os.path.isdir(abs_dir):
            continue
        for fn in sorted(os.listdir(abs_dir)):
            if not fn.endswith(".csv"):
                continue
            files_used += 1
            if files_used > _MAX_FILES_PER_LABEL:
                break
            fp = os.path.join(abs_dir, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    reader = _csv.DictReader(f)
                    for row in reader:
                        v = row.get(dim)
                        if v is None or v == "":
                            continue
                        try:
                            out.append(float(v))
                        except ValueError:
                            pass
                        if len(out) >= _MAX_ROWS_PER_LABEL:
                            return out
            except Exception:
                pass
        if files_used > _MAX_FILES_PER_LABEL:
            break
    return out


def _hist_bins(values, bin_count, vmin, vmax):
    """把 values 分到 bin_count 个 bin，返回每个 bin 的计数（归一化到比例）。"""
    if vmax <= vmin:
        return [0] * bin_count
    step = (vmax - vmin) / bin_count
    bins = [0] * bin_count
    for v in values:
        idx = int((v - vmin) / step)
        if idx < 0:
            idx = 0
        elif idx >= bin_count:
            idx = bin_count - 1
        bins[idx] += 1
    total = sum(bins) or 1
    return [round(b / total, 4) for b in bins]


def _f_statistic(groups):
    """单因素方差分析的 F 统计（越大越可分）。groups: list[list[float]]。"""
    groups = [g for g in groups if len(g) >= 2]
    if len(groups) < 2:
        return 0.0
    all_vals = [v for g in groups for v in g]
    grand_mean = sum(all_vals) / len(all_vals)
    # 类间 (between-group) 平方和
    ss_b = sum(len(g) * ((sum(g) / len(g) - grand_mean) ** 2) for g in groups)
    # 类内 (within-group) 平方和
    ss_w = 0.0
    for g in groups:
        m = sum(g) / len(g)
        ss_w += sum((v - m) ** 2 for v in g)
    df_b = len(groups) - 1
    df_w = len(all_vals) - len(groups)
    if df_w <= 0 or ss_w <= 0:
        return 0.0
    ms_b = ss_b / df_b
    ms_w = ss_w / df_w
    return ms_b / ms_w if ms_w > 0 else 0.0


@app.route('/api/db/feature_dist')
def api_db_feature_dist():
    """V5.0 · 7D 维度对比。
    Query:
      exercise: bicep_curl / squat （默认 bicep_curl）
      dim: 指定维度（若缺省，返回全部 7 个维度的 F 统计）
      bins: 直方图柱数（默认 24）
    Response:
      { dim, exercise, bins, range: [vmin, vmax],
        hist: { standard: [...], compensating: [...], non_standard: [...] },
        counts: { standard: N, compensating: N, non_standard: N },
        f_stat, all_dims_f: { dim: f_stat, ... } }
    """
    ex = request.args.get('exercise', 'bicep_curl').strip()
    dim = request.args.get('dim', '').strip()
    try:
        bins = int(request.args.get('bins', 24))
    except Exception:
        bins = 24
    bins = max(8, min(bins, 60))

    if ex not in _CSV_DIRS:
        return Response(
            json.dumps({'error': 'unknown exercise',
                        'available': list(_CSV_DIRS.keys())}),
            status=400, mimetype='application/json'
        )

    # 收集 3 类数据
    groups = {}  # label -> list[float]（指定 dim 列）
    all_dims_data = {}  # label -> { dim: list[float] }
    labels_raw = ("golden", "lazy", "bad")
    for lbl in labels_raw:
        if dim:
            groups[lbl] = _load_feature_values(ex, lbl, dim)
        else:
            # 全维度扫描（用于算 all_dims_f）
            all_dims_data[lbl] = {}
            for d in _FEAT_DIMS:
                all_dims_data[lbl][d] = _load_feature_values(ex, lbl, d)

    # 计算全部维度的 F 统计（给下方表格）
    all_dims_f = {}
    if dim:
        # 指定维度时只算这一个；其他维度要单独扫一遍（慢但数据小够快）
        for d in _FEAT_DIMS:
            if d == dim:
                gs = [groups[l] for l in labels_raw]
            else:
                gs = [_load_feature_values(ex, l, d) for l in labels_raw]
            all_dims_f[d] = round(_f_statistic(gs), 3)
        cur_dim = dim
    else:
        # 未指定 dim 时，挑 F 最大的那个做默认
        for d in _FEAT_DIMS:
            gs = [all_dims_data[l][d] for l in labels_raw]
            all_dims_f[d] = round(_f_statistic(gs), 3)
        cur_dim = max(all_dims_f.keys(), key=lambda k: all_dims_f[k])
        groups = {l: all_dims_data[l][cur_dim] for l in labels_raw}

    # 计算范围
    all_vals = [v for vs in groups.values() for v in vs]
    if not all_vals:
        return Response(
            json.dumps({
                'error': 'no data',
                'exercise': ex, 'dim': cur_dim,
                'hint': 'CSV 目录为空或列名不匹配',
            }, ensure_ascii=False),
            status=200, mimetype='application/json'
        )
    vmin = min(all_vals)
    vmax = max(all_vals)
    # 边缘留 5% 余量
    pad = (vmax - vmin) * 0.05
    vmin_p = vmin - pad
    vmax_p = vmax + pad

    hist = {}
    counts = {}
    for lbl_raw in labels_raw:
        lbl_new = _LABEL_MAP[lbl_raw]
        vs = groups.get(lbl_raw, [])
        hist[lbl_new] = _hist_bins(vs, bins, vmin_p, vmax_p)
        counts[lbl_new] = len(vs)

    # F 统计（仅 cur_dim）
    f_stat = all_dims_f.get(cur_dim, 0.0)
    # 雷达图数据：每类在每个维度上的均值（归一化到 [0,1]）
    radar = _compute_radar(ex, labels_raw)

    return Response(
        json.dumps({
            'exercise': ex,
            'dim': cur_dim,
            'dim_cn': _FEAT_CN.get(cur_dim, cur_dim),
            'bins': bins,
            'range': [round(vmin_p, 4), round(vmax_p, 4)],
            'hist': hist,
            'counts': counts,
            'f_stat': round(f_stat, 3),
            'all_dims_f': all_dims_f,
            'all_dims_cn': _FEAT_CN,
            'radar': radar,
        }, ensure_ascii=False),
        mimetype='application/json'
    )


def _compute_radar(exercise, labels_raw):
    """返回雷达图数据：每维度每类均值归一化到 [0,1]。
    归一化方式：把该维度下 3 类的全样本 min/max 映射到 0/1。
    """
    out = {"dims": _FEAT_DIMS, "dims_cn": [_FEAT_CN[d] for d in _FEAT_DIMS],
           "series": {}}
    dim_stats = {}  # dim -> (min, max, {lbl: mean})
    for d in _FEAT_DIMS:
        per_lbl = {}
        all_vals = []
        for lbl_raw in labels_raw:
            vs = _load_feature_values(exercise, lbl_raw, d)
            if vs:
                per_lbl[lbl_raw] = sum(vs) / len(vs)
                all_vals.extend(vs)
        if all_vals:
            dim_stats[d] = (min(all_vals), max(all_vals), per_lbl)
        else:
            dim_stats[d] = (0, 1, {})
    for lbl_raw in labels_raw:
        lbl_new = _LABEL_MAP[lbl_raw]
        series = []
        for d in _FEAT_DIMS:
            vmin, vmax, per_lbl = dim_stats[d]
            mean = per_lbl.get(lbl_raw)
            if mean is None or vmax <= vmin:
                series.append(0.5)
            else:
                series.append(round((mean - vmin) / (vmax - vmin), 4))
        out["series"][lbl_new] = series
    return out


@app.route('/api/db/exercises')
def api_db_exercises():
    """返回所有 distinct exercise 列表（用于前端 dropdown）。"""
    import sqlite3 as _sq
    names = set()
    try:
        conn = _sq.connect(_db_view_path())
        for t in ('training_sessions', 'rep_events'):
            try:
                for r in conn.execute(
                    'SELECT DISTINCT exercise FROM ' + t +
                    ' WHERE exercise IS NOT NULL'
                ):
                    if r[0]:
                        names.add(r[0])
            except Exception:
                pass
        conn.close()
    except Exception:
        pass
    return Response(
        json.dumps({'exercises': sorted(names)}, ensure_ascii=False),
        mimetype='application/json'
    )


if __name__ == '__main__':
    print("[*] IronBuddy v3.1 online -- 0.0.0.0:5000")
    print("[*] Admin panel: http://localhost:5000/admin")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
