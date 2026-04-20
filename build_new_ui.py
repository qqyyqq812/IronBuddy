import os

def build_linear_ui():
    root = "/home/qq/projects/embedded-fullstack"
    old_html_path = os.path.join(root, "templates", "index.html")
    new_html_path = os.path.join(root, "templates", "index_linear.html")

    with open(old_html_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    parts = content.split("<script>", 1)
    if len(parts) < 2:
        print("未能在 index.html 中找到 <script> 分词")
        return

    js_logic = parts[1]

    # 构建 Linear 冷峻风格的 CSS 与 Dom
    html = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>IronBuddy • Control Center</title>
    <style>
        :root {
            --bg: #09090b;
            --surface: #18181b;
            --border: #27272a;
            --text: #fafafa;
            --text-dim: #a1a1aa;
            --accent: #22d3ee;
            --danger: #f87171;
            --success: #4ade80;
            --font-mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--bg);
            color: var(--text);
            font-family: var(--font-sans);
            height: 100vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            font-size: 14px;
        }
        /* Top Navigation */
        .navbar {
            height: 48px;
            border-bottom: 1px solid var(--border);
            display: flex;
            align-items: center;
            padding: 0 16px;
            justify-content: space-between;
        }
        .navbar-brand {
            font-weight: 600;
            font-size: 14px;
            letter-spacing: -0.02em;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .status-dot {
            width: 8px; height: 8px; border-radius: 50%;
        }
        .status-dot.online { background-color: var(--success); }
        .status-dot.offline { background-color: var(--danger); animation: blink 1s infinite alternate; }
        @keyframes blink { to { opacity: 0.3; } }
        
        .navbar-controls {
            display: flex;
            gap: 12px;
        }
        .btn {
            background: var(--surface);
            border: 1px solid var(--border);
            color: var(--text);
            padding: 4px 12px;
            border-radius: 6px;
            font-size: 12px;
            cursor: pointer;
            transition: border-color 0.15s;
        }
        .btn:hover { border-color: var(--accent); color: var(--accent); }
        
        /* Main Grid Layout */
        .workbench {
            flex: 1;
            display: flex;
            overflow: hidden;
        }
        .left-col {
            flex: 1;
            display: flex;
            flex-direction: column;
            border-right: 1px solid var(--border);
            padding: 16px;
        }
        .right-col {
            width: 320px;
            display: flex;
            flex-direction: column;
            background: var(--surface);
        }
        
        /* Video Player Frame */
        .video-board {
            flex: 1;
            border: 1px solid var(--border);
            border-radius: 8px;
            background: #000;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
        }
        .video-board img {
            width: 100%;
            height: 100%;
            object-fit: contain;
        }
        .video-overlay {
            position: absolute;
            top: 12px; left: 12px; right: 12px;
            display: flex; justify-content: space-between;
            pointer-events: none;
            font-family: var(--font-mono);
            font-size: 11px;
            text-shadow: 0 1px 2px rgba(0,0,0,0.8);
        }

        /* Metric Rows under Video */
        .metrics-bar {
            height: 80px;
            display: flex;
            gap: 12px;
            margin-top: 16px;
        }
        .metric-card {
            flex: 1;
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px;
            display: flex;
            flex-direction: column;
            justify-content: center;
        }
        .metric-card-title {
            font-size: 11px;
            text-transform: uppercase;
            color: var(--text-dim);
            margin-bottom: 4px;
            font-weight: 600;
        }
        .metric-card-val {
            font-family: var(--font-mono);
            font-size: 20px;
        }

        /* Right Panel Widgets */
        .widget-panel {
            flex: 1;
            border-bottom: 1px solid var(--border);
            padding: 16px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
        }
        .widget-header {
            font-weight: 600;
            font-size: 12px;
            color: var(--text-dim);
            margin-bottom: 12px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .data-row {
            display: flex;justify-content: space-between;
            margin-bottom: 8px;
            font-family: var(--font-mono);
            font-size: 13px;
        }
        .log-terminal {
            flex: 1;
            font-family: var(--font-mono);
            font-size: 11px;
            overflow-y: auto;
            color: var(--text-dim);
        }
        .log-terminal div { margin-bottom: 4px; border-left: 2px solid var(--border); padding-left: 6px; }

        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
    </style>
</head>
<body>
    <div class="navbar">
        <div class="navbar-brand">
            <div id="connDot" class="status-dot offline"></div>
            IronBuddy Nexus <span id="hudState" style="color:var(--text-dim);font-weight:400;margin-left:8px;font-size:11px;">Awaiting</span>
        </div>
        <div class="navbar-controls">
            <button class="btn" id="btnSumm" onclick="triggerDeepseek()">生成智能点评</button>
            <button class="btn" id="btnReset" onclick="resetSession()">硬件状态重置</button>
            <button class="btn" onclick="toggleMute()" id="muteBtn">静音模式</button>
        </div>
    </div>

    <div class="workbench">
        <div class="left-col">
            <div class="video-board">
                <div class="video-overlay">
                    <div style="color:var(--accent);">CAM0 / RT: <span id="hudFps">--</span></div>
                    <div><span id="statRate">--</span> ACC</div>
                </div>
                <img id="videoFeed" src="">
            </div>
            
            <div class="metrics-bar">
                <div class="metric-card">
                    <div class="metric-card-title" id="statGoodLabel">标准达标</div>
                    <div class="metric-card-val" id="statGood" style="color:var(--success);">0</div>
                </div>
                <div class="metric-card">
                    <div class="metric-card-title" id="statFailedLabel">违规动作</div>
                    <div class="metric-card-val" id="statFailed" style="color:var(--danger);">0</div>
                </div>
                <div class="metric-card">
                    <div class="metric-card-title">系统疲劳累加</div>
                    <div class="metric-card-val" id="statFatigue" style="color:var(--accent);">0</div>
                    <div style="height:2px; width:100%; background:var(--border); margin-top:6px;"><div id="fatigueBar" style="height: 100%; width: 0%; background: var(--accent);"></div></div>
                </div>
                <div class="metric-card">
                    <div class="metric-card-title" id="hudAngleLabel">动态关节角度</div>
                    <div class="metric-card-val" id="hudAngle">--</div>
                </div>
            </div>
        </div>

        <div class="right-col">
            <div class="widget-panel" style="flex:0 0 250px;">
                <div class="widget-header">DeepSeek 大脑解析流</div>
                <div class="log-terminal" id="dsHistoryPanel">
                    <div class="chat-placeholder">等待长效 AI 摘要...</div>
                </div>
            </div>
            <div class="widget-panel">
                <div class="widget-header">底层会话审计</div>
                <div class="log-terminal" id="chatArea">
                    <div class="chat-placeholder" id="chatPlaceholder">系统初始化完毕.</div>
                </div>
            </div>
        </div>
    </div>

    <!-- Hidden compatibility elements to prevent JS errors -->
    <div style="display:none;">
        <span id="connLabel">离线</span>
        <div id="exercisePills"></div>
        <div id="ampCircle"></div><span id="ampText"></span>
        <div id="nnCircle"></div><span id="nnText"></span><span id="nnLabel"></span>
        <span id="cfgFatigueLimit"></span>
        <div id="rigBody"></div><div id="rigThigh"></div><div id="rigCalf"></div><div id="rigUpperArm"></div><div id="rigForearm"></div><div id="rigGlow"></div>
        <span id="rigStatusText"></span>
        <span id="sbReps"></span><span id="sbRate"></span>
        <span id="infoBoardIp"></span><span id="infoBoardStatus"></span><span id="infoCsvCount"></span>
        <span id="infoCpuTemp"></span><span id="infoUptime"></span><span id="infoGpu"></span>
        <div id="logTerminal"></div><div id="dataTree"></div><div id="historyContainer"></div><div id="modelInfoContainer"></div>
        <canvas id="emgCanvas"></canvas><div id="emgNoSensor"></div><div id="emgLegend"></div>
        <select id="cfgExercise"><option value="bicep_curl">弯举</option></select><select id="cfgVisionMode"></select><select id="cfgInferenceMode"></select>
        <span id="alertToast"></span><span id="hdmiPlaceholder"></span><span id="tagHdmi"></span><span id="tagExercise"></span><span id="tagVision"></span><span id="romCenterAngle"></span>
    </div>

    <script>
    var isMuted = false;
    function toggleMute() {
        isMuted = !isMuted;
        fetch('/api/mute', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ muted: isMuted }) });
        var btn = document.getElementById('muteBtn');
        if (isMuted) { btn.innerHTML = '已静音'; btn.style.borderColor = 'var(--danger)'; btn.style.color = 'var(--danger)'; }
        else { btn.innerHTML = '静音模式'; btn.style.borderColor = ''; btn.style.color = ''; }
    }
    
    function escapeHtml(unsafe) {
        return (unsafe||'').replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    var STATE_MAP = {
        'NO_PERSON': { text: '无识别', color: 'var(--text-dim)' },
        'RESTING': { text: '休止位', color: 'var(--accent)' },
        'CURL_UP': { text: '发力收缩', color: 'var(--success)' },
        'CURL_DOWN': { text: '离心退让', color: 'var(--success)' },
        'SQUAT_DOWN': { text: '屈髋下蹲', color: 'var(--success)' },
        'SQUAT_UP': { text: '伸展起身', color: 'var(--success)' }
    };
    var connectionAlive = false;
    var lastChatReplyTs = 0, lastChatInputTs = 0, lastDraft = '', lastReply = '';
    var deepseekTimeoutId = null, logAutoScroll = true;

    // Helper functions needed by underlying logic
    function _setText(id, val) {
        var el = document.getElementById(id);
        if (el) el.textContent = val;
    }
    function appendChatBubble(role, text, cls) {
        var a = document.getElementById('chatArea'); if(!a)return;
        var p = document.getElementById('chatPlaceholder'); if(p)p.remove();
        var dv = document.createElement('div');
        dv.innerHTML = "<strong style='color:var(--accent);'>" + role + "</strong>: " + escapeHtml(text);
        a.appendChild(dv);
        a.scrollTop = a.scrollHeight;
    }
    function checkTimerAutoStart(){};
    function updateRig(){};
    function loadServices(){};
    function loadSystemInfo(){};
    function loadApiConfig(){};
    function switchVision(){};
    function switchInferenceMode(){};
"""

    html += js_logic
    with open(new_html_path, "w", encoding="utf-8") as f:
        f.write(html)
    print("New linear UI built.")

if __name__ == "__main__":
    build_linear_ui()
