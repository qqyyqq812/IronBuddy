# IronBuddy 主页面工作台化重构 — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把 IronBuddy 主网页从"工作台样"升级为成品感产品页，修云端 GPU 切换 bug，把 RAG/OpenCloud/旧 code graph 从主页拿掉，重塑"调试" tab 为 iframe 嵌入 + Obsidian 风代码结构图 + 反馈区。

**Architecture:** 设计稿 [`docs/plans/2026-05-03-main-ui-workshop-design.md`](docs/plans/2026-05-03-main-ui-workshop-design.md) § A-H。6 阶段拆分，前 3 阶段录制阻塞、后 3 阶段不阻塞。每阶段独立可 commit + 可回滚 + 必须经板端部署 + 烟测 + 新 operator run 复测。

**Tech Stack:** Python 3.7（板端约束）、Flask、原生 JS、3d-force-graph CDN、stdlib `ast`、`subprocess git`、Understand-Anything skill（数据生成可选退路）。

---

## 部署纪律（每阶段都要照做）

每阶段交付前的固定流程：

```bash
# 1. 本地静态校验
python3 -m py_compile <受影响.py 文件>
pytest <受影响测试> -q

# 2. 拿板端锁（编辑 docs/test_runs/ironbuddy_operator/CURRENT.md 顶部 lock block）
#    lock_owner: claude_code
#    lock_taken_at: 2026-05-03 HH:MM CST
#    lock_reason: 阶段 N <一句话>
#    next_release: 部署+烟测后立即释放

# 3. rsync 部署 + 备份
TS=$(date +%Y%m%d_%H%M%S)
ssh -i ~/.ssh/id_rsa_toybrick toybrick@10.244.190.224 \
  "mkdir -p /home/toybrick/streamer_v3/.deploy_backups/claude_code_${TS}_<topic>"
ssh -i ~/.ssh/id_rsa_toybrick toybrick@10.244.190.224 \
  "cd /home/toybrick/streamer_v3 && cp <files> .deploy_backups/claude_code_${TS}_<topic>/"
rsync -av <local-files> toybrick@10.244.190.224:/home/toybrick/streamer_v3/

# 4. 板端 py_compile + 仅重启受影响服务
ssh -i ~/.ssh/id_rsa_toybrick toybrick@10.244.190.224 \
  "cd /home/toybrick/streamer_v3 && python3 -m py_compile <files>"
# 重启 streamer 用 /api/admin/start 或 supervisor；不动 vision/voice/fsm/emg

# 5. 烟测 API 200 + 字段正确
curl --noproxy '*' -m 5 -sS http://10.244.190.224:5000/api/<endpoint>

# 6. 释放锁，写回 CURRENT.md 部署摘要

# 7. 新建 operator run
IRONBUDDY_BOARD_IP=10.244.190.224 python3 tools/ironbuddy_operator_console.py \
  --scenario <stage_name>_retest
```

---

## 阶段 1：云端 GPU 热切换修复（录制阻塞）

**Goal:** 切到云端时前端真等握手 ready 才显示成功；连通测试按钮可点出延迟。

**Files:**
- Modify: `hardware_engine/ai_sensory/cloud_rtmpose_client.py:475-700`（主循环关键节点写状态文件）
- Modify: `streamer_app.py`（新增 `/api/cloud_handshake_status`）
- Modify: `templates/index.html:2280-2310`（`switchVision` 真轮询）
- Modify: `templates/index.html:2667-2700`（`cloudVerifyTest` 真调）
- Test: `tests/test_cloud_handshake.py`（新增）

### Task 1.1：定义 cloud_rtmpose_status 协议 + 后端只读端点

**Step 1: 写失败测试**

`tests/test_cloud_handshake.py`：
```python
import json, os, tempfile
from unittest import mock

def test_handshake_status_returns_payload(monkeypatch, tmp_path):
    p = tmp_path / "cloud_rtmpose_status.json"
    p.write_text(json.dumps({"phase": "ready", "ts": 1.0, "detail": "ok", "backend": "cloud"}))
    monkeypatch.setenv("IRONBUDDY_CLOUD_STATUS_PATH", str(p))
    import importlib, streamer_app
    importlib.reload(streamer_app)
    client = streamer_app.app.test_client()
    r = client.get("/api/cloud_handshake_status")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["phase"] == "ready"
    assert data["backend"] == "cloud"

def test_handshake_status_missing_file_returns_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("IRONBUDDY_CLOUD_STATUS_PATH", str(tmp_path / "nope.json"))
    import importlib, streamer_app
    importlib.reload(streamer_app)
    client = streamer_app.app.test_client()
    r = client.get("/api/cloud_handshake_status")
    assert r.status_code == 200
    data = r.get_json()
    assert data["phase"] == "unknown"
```

**Step 2: 跑测试，确认 FAIL**

```bash
pytest tests/test_cloud_handshake.py -q
```
Expected: FAIL（端点未实现）

**Step 3: 加端点到 streamer_app.py**

在 `/api/vision_mode` 之后插入：
```python
@app.route('/api/cloud_handshake_status')
def cloud_handshake_status():
    """Cloud RTMPose 握手状态。phase: connecting|ready|failed|unknown."""
    path = os.environ.get("IRONBUDDY_CLOUD_STATUS_PATH",
                          "/dev/shm/cloud_rtmpose_status.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("phase", "unknown")
            data["ok"] = True
            return Response(json.dumps(data, ensure_ascii=False),
                            mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "phase": "unknown",
                                    "error": str(e)}),
                        mimetype='application/json')
    return Response(json.dumps({"ok": True, "phase": "unknown",
                                "detail": "no status file"}),
                    mimetype='application/json')
```

**Step 4: 跑测试，确认 PASS**

```bash
pytest tests/test_cloud_handshake.py -q
```
Expected: 2 passed

**Step 5: Commit**

```bash
git add tests/test_cloud_handshake.py streamer_app.py
git commit -m "feat(cloud): add /api/cloud_handshake_status reading shm status file"
```

### Task 1.2：cloud_rtmpose_client 写握手状态文件

**Step 1: 找到状态写入点**

读 `hardware_engine/ai_sensory/cloud_rtmpose_client.py` 的 `main()` 和 background
inference thread。识别这三个时刻：
- 切换到 cloud 后第一次发请求前 → `phase=connecting`
- 第一次成功收到合法 keypoint 响应后 → `phase=ready`
- 连续 3 次失败或超时 → `phase=failed`

**Step 2: 加 helper + 调用点**

在文件顶部 helper 区加：
```python
SHM_CLOUD_STATUS = "/dev/shm/cloud_rtmpose_status.json"

def _write_cloud_status(phase, detail="", backend="cloud"):
    try:
        payload = json.dumps({"phase": phase, "ts": time.time(),
                              "detail": detail, "backend": backend})
        tmp = SHM_CLOUD_STATUS + ".tmp"
        with open(tmp, "w") as f:
            f.write(payload)
        os.rename(tmp, SHM_CLOUD_STATUS)
    except Exception:
        pass
```

在三个时刻调用：
- vision_mode 切到 cloud 时：`_write_cloud_status("connecting", "switching to cloud")`
- 收到第一帧合法 keypoint 后：`_write_cloud_status("ready", "first frame ok")`（用 `_first_cloud_ok` 标志只写一次直到下次重置）
- 连续失败 3 次：`_write_cloud_status("failed", "3 consecutive errors")`
- 切回 local：`_write_cloud_status("ready", "local backend", backend="local")`

**Step 3: 本地静态校验**

```bash
python3 -m py_compile hardware_engine/ai_sensory/cloud_rtmpose_client.py
```
Expected: 无输出

**Step 4: Commit**

```bash
git add hardware_engine/ai_sensory/cloud_rtmpose_client.py
git commit -m "feat(cloud): write /dev/shm/cloud_rtmpose_status.json at handshake events"
```

### Task 1.3：前端 switchVision 真轮询

**Step 1: 替换 switchVision 实现**

`templates/index.html:2280-2310` 整段替换为：
```javascript
async function switchVision(mode) {
    visionMode = mode;
    var sbMode = document.getElementById('sbMode');
    if (sbMode) sbMode.textContent = mode === 'cloud' ? '云端' : '本地';
    var sel = document.getElementById('cfgVisionMode');
    if (sel) { sel.value = mode; sel.disabled = true; }
    var tag = document.getElementById('tagVision');
    if (tag) tag.textContent = mode === 'cloud' ? '云端' : '本地';
    showToast('视觉切换中', '正在切换到 ' + (mode === 'cloud' ? '云端' : '本地') + '...', false);
    try {
        await fetch('/api/switch_vision', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({mode: mode})
        });
    } catch(e) {}
    // 真轮询 cloud_handshake_status，最多 6 秒
    var startTs = Date.now();
    var poller = setInterval(async function() {
        try {
            var r = await fetch('/api/cloud_handshake_status', {cache:'no-store'});
            var d = await r.json();
            if (d.phase === 'ready' && d.backend === mode) {
                clearInterval(poller);
                showToast('视觉模式', '已切换到 ' + (mode === 'cloud' ? '云端' : '本地'), false);
                if (sel) sel.disabled = false;
                return;
            }
            if (d.phase === 'failed') {
                clearInterval(poller);
                showToast('切换失败', d.detail || '云端未响应', true);
                if (sel) sel.disabled = false;
                return;
            }
        } catch(e) {}
        if (Date.now() - startTs > 6000) {
            clearInterval(poller);
            showToast('切换中', '云端未在 6s 内响应（不会自动回退）', true);
            if (sel) sel.disabled = false;
        }
    }, 400);
}
```

**Step 2: 替换 cloudVerifyTest 实现**

定位 `templates/index.html:2667-2700` 区段（`cloudVerifyStatus` 旁边按钮处理函数），
替换 `cloudVerifyTest` 为：
```javascript
async function cloudVerifyTest() {
    var s = document.getElementById('cloudVerifyStatus');
    if (s) s.textContent = '探测中…';
    var t0 = Date.now();
    try {
        var r = await fetch('/api/admin/cloud_verify', {cache:'no-store'});
        var d = await r.json();
        var dt = Date.now() - t0;
        if (s) s.textContent = (d.ok ? '✓ 在线' : '✗ ' + (d.error || '失败')) +
                                ' · ' + dt + 'ms';
    } catch(e) {
        if (s) s.textContent = '✗ ' + e.message;
    }
}
```

**Step 3: Commit**

```bash
git add templates/index.html
git commit -m "feat(ui): switchVision polls cloud_handshake_status; cloudVerifyTest reports latency"
```

### Task 1.4：部署 + 烟测

部署清单：
- `hardware_engine/ai_sensory/cloud_rtmpose_client.py`
- `streamer_app.py`
- `templates/index.html`

**Step 1: 拿锁** — 改 `CURRENT.md` lock block

**Step 2: rsync + 备份 + 板端 py_compile**

**Step 3: 重启 vision + streamer**（云端切换需要重启 vision 才能生效）

```bash
ssh -i ~/.ssh/id_rsa_toybrick toybrick@10.244.190.224 \
  'cd /home/toybrick/streamer_v3 && curl -sS -X POST http://127.0.0.1:5000/api/admin/restart_service?name=vision'
```

**Step 4: 烟测**

```bash
# 默认 local 时 phase=ready, backend=local
curl --noproxy '*' -sS http://10.244.190.224:5000/api/cloud_handshake_status

# 切云端
curl --noproxy '*' -sS -X POST http://10.244.190.224:5000/api/switch_vision \
  -H 'Content-Type: application/json' -d '{"mode":"cloud"}'
sleep 4
curl --noproxy '*' -sS http://10.244.190.224:5000/api/cloud_handshake_status
# 期望 phase=ready, backend=cloud

# 切回 local
curl --noproxy '*' -sS -X POST http://10.244.190.224:5000/api/switch_vision \
  -H 'Content-Type: application/json' -d '{"mode":"local"}'
```

**Step 5: 释放锁** — 改 `CURRENT.md`

**Step 6: 现场新 operator run**

```bash
IRONBUDDY_BOARD_IP=10.244.190.224 python3 tools/ironbuddy_operator_console.py \
  --scenario cloud_switch_fix_retest
```
现场跑"云端/本地视觉热切换"步骤，标 PASS/FAIL + 备注。

**Definition of Done:**
- 测试 `pytest tests/test_cloud_handshake.py` 绿。
- 板端 `/api/cloud_handshake_status` 返回正确 phase。
- 现场 operator run 该步标 PASS。
- 切云端时主网页不卡；6s 超时后能继续操作。

---

## 阶段 2：主页面 tab 清理 + 调试 tab iframe 嵌入（录制阻塞）

**Goal:** "数据" tab 只剩 DB + 训练数据；"调试" tab 上方留代码图占位（阶段 4 填充），下方 iframe 嵌入 8765，最底部折叠服务日志终端。

**Files:**
- Modify: `templates/index.html:1895-1944`（tab-logs / tab-data 结构）
- Modify: `templates/index.html:2217-2230`（switchTab）
- Modify: `templates/index.html:3315-3375`（删 loadDemoShowcase / loadCodeGraph 调用，保留函数体注释或删除）
- Test: `tests/test_main_ui_tabs.py`（新增，静态检查 HTML 结构）

### Task 2.1：HTML 结构改造 + 测试

**Step 1: 写失败测试**

`tests/test_main_ui_tabs.py`：
```python
from pathlib import Path

INDEX = Path("templates/index.html").read_text(encoding="utf-8")

def test_data_tab_no_rag_or_opencloud():
    """数据 tab 不应再渲染 RAG/OpenCloud/旧 code graph 容器。"""
    assert 'id="demoShowcaseContainer"' not in INDEX
    assert 'id="codeGraphContainer"' not in INDEX

def test_switch_tab_no_demo_calls():
    assert 'loadDemoShowcase()' not in INDEX
    # loadCodeGraph 仍允许存在，但应当只在 logs tab 调用
    if 'loadCodeGraph()' in INDEX:
        assert "tabId === 'logs'" in INDEX

def test_logs_tab_has_iframe_and_graph_slots():
    assert 'id="codeGraphMount"' in INDEX
    assert 'id="operatorIframe"' in INDEX

def test_logs_tab_log_terminal_collapsible():
    assert 'id="logTerminalDetails"' in INDEX
```

**Step 2: 跑测试，确认 FAIL**

```bash
pytest tests/test_main_ui_tabs.py -q
```
Expected: 4 个 FAIL。

**Step 3: 改 templates/index.html**

3a. 删除"数据" tab 的 `demoShowcaseContainer` 和 `codeGraphContainer` div（行 1935 和 1940 附近）。

3b. 替换"调试" tab（`tab-panel id="tab-logs"`，行 1895 起）为：
```html
<div class="tab-panel" id="tab-logs">
    <div class="workbench-card" style="margin-bottom:10px;">
        <div class="workbench-head">
            <div class="workbench-title">&#129504; 代码结构图</div>
            <div class="workbench-meta" id="codeGraphMeta">点击节点展开邻居</div>
        </div>
        <div id="codeGraphMount" style="height:360px; position:relative; background:var(--bg-primary); border-radius:6px;">
            <div style="position:absolute; inset:0; display:flex; align-items:center; justify-content:center; color:var(--text-dim); font-size:0.8em;">
                运行 <code>python3 tools/build_code_graph.py --refresh</code> 后刷新
            </div>
        </div>
    </div>

    <div class="workbench-card" style="margin-bottom:10px;">
        <div class="workbench-head">
            <div class="workbench-title">&#128221; 现场反馈（写入当前 run）</div>
            <div class="workbench-meta" id="feedbackMeta">截图粘贴或拖拽，备注可选</div>
        </div>
        <textarea id="feedbackNote" placeholder="描述当前现象/期望行为/上下文..."
                  style="width:100%; min-height:60px; background:var(--bg-primary); color:var(--text-default); border:1px solid var(--border-subtle); border-radius:4px; padding:8px; font-size:0.8em; resize:vertical;"></textarea>
        <div id="feedbackImagePreview" style="margin-top:6px;"></div>
        <div style="display:flex; gap:6px; margin-top:6px;">
            <button class="term-btn" onclick="document.getElementById('feedbackFile').click()">选择截图</button>
            <input type="file" id="feedbackFile" accept="image/*" style="display:none;" onchange="onFeedbackFileChosen(event)">
            <button class="term-btn" onclick="submitFeedback()">保存到当前 run</button>
            <span id="feedbackStatus" style="font-size:0.72em; color:var(--text-dim); align-self:center;"></span>
        </div>
    </div>

    <div class="workbench-card" style="margin-bottom:10px;">
        <div class="workbench-head">
            <div class="workbench-title">&#128295; operator console</div>
            <a class="workbench-link" id="operatorConsoleLink" href="http://127.0.0.1:8765/" target="_blank">在新窗口打开</a>
        </div>
        <iframe id="operatorIframe" src="http://127.0.0.1:8765/"
                style="width:100%; height:720px; border:0; border-radius:6px; background:var(--bg-primary);"></iframe>
    </div>

    <details id="logTerminalDetails" style="margin-top:10px;">
        <summary style="cursor:pointer; font-size:0.78em; color:var(--text-muted); padding:6px 0;">服务日志终端（点击展开）</summary>
        <div class="terminal-toolbar">
            <span class="term-title">&#128187; 服务日志</span>
            <div style="display:flex; gap:4px;">
                <button class="term-btn" onclick="toggleLogScroll()">自动滚动</button>
                <button class="term-btn" onclick="clearLogs()">清空</button>
            </div>
        </div>
        <div class="terminal" id="logTerminal"></div>
    </details>
</div>
```

3c. 改 `switchTab` (line 2226 附近)：
```javascript
if (tabId === 'data') { loadTrainingData(); loadTrainingHistory(); loadModelInfo(); }
if (tabId === 'logs') { if (typeof loadCodeGraph === 'function') loadCodeGraph(); loadDebugWorkbench(); }
```

3d. **删除** `loadDemoShowcase` 整个函数定义（行 3315-3349）。`loadCodeGraph` 留待阶段 4 重写，**先把函数体替换为占位**：
```javascript
async function loadCodeGraph() {
    // 阶段 4 实现 3d-force-graph 渲染
    return;
}
```

3e. （反馈区相关函数 `onFeedbackFileChosen` / `submitFeedback` 阶段 6 实现，先加占位）：
```javascript
let _feedbackImageData = null;
function onFeedbackFileChosen(e) {
    var f = e.target.files && e.target.files[0]; if (!f) return;
    var reader = new FileReader();
    reader.onload = function(ev) {
        _feedbackImageData = ev.target.result;
        document.getElementById('feedbackImagePreview').innerHTML =
            '<img src="' + _feedbackImageData + '" style="max-width:200px; max-height:120px; border-radius:4px;">';
    };
    reader.readAsDataURL(f);
}
function submitFeedback() {
    var s = document.getElementById('feedbackStatus');
    s.textContent = '阶段 6 实现';  // placeholder
}
```

**Step 4: 跑测试，确认 PASS**

```bash
pytest tests/test_main_ui_tabs.py -q
```
Expected: 4 passed

**Step 5: Commit**

```bash
git add tests/test_main_ui_tabs.py templates/index.html
git commit -m "feat(ui): clean data tab, restructure logs tab to embed operator console + code graph + feedback area"
```

### Task 2.2：部署 + 烟测

**Step 1-3:** 拿锁 / rsync / py_compile（仅 `templates/index.html`，无需重启 streamer
即可生效，但保险起见 reload 一次）

**Step 4: 烟测**

```bash
# 主页面可达
curl --noproxy '*' -sS -o /dev/null -w "%{http_code}\n" \
  http://10.244.190.224:5000/

# 关键 fragment 不再出现
curl --noproxy '*' -sS http://10.244.190.224:5000/ | \
  grep -q 'demoShowcaseContainer' && echo 'STILL HAS RAG (BAD)' || echo 'data tab cleaned ok'
curl --noproxy '*' -sS http://10.244.190.224:5000/ | \
  grep -q 'operatorIframe' && echo 'iframe slot ok'
```

**Step 5:** 释放锁，新 operator run（scenario `tab_cleanup_retest`）。

**Definition of Done:**
- pytest 绿。
- 主页面打开 → 数据 tab 只剩 DB 链接 + 训练数据。
- 主页面打开 → 调试 tab 看到代码图占位 + 反馈区 + iframe（127.0.0.1:8765 加载）+ 折叠的日志终端。
- 切回桌面浏览器，iframe 内容能与原 8765 操作一致（按通过/失败/上传截图正常）。

---

## 阶段 3：UI 全局质感升级（录制阻塞）

**Goal:** 套用 Linear/Vercel 深色科技风：颜色 token 升级、卡片阴影、按钮三层级、状态胶囊、tab 过渡。**不动布局、不动交互、不动 fetch 路径**。

**Files:**
- Modify: `templates/index.html` `<style>` 区（行 ~30-1100）
- Test: `tests/test_main_ui_style_tokens.py`

### Task 3.1：颜色 token 升级

**Step 1: 写失败测试**

`tests/test_main_ui_style_tokens.py`：
```python
from pathlib import Path

INDEX = Path("templates/index.html").read_text(encoding="utf-8")

def test_color_tokens_present():
    assert "--bg-primary: #0a0a0c" in INDEX or "--bg-primary:#0a0a0c" in INDEX
    assert "--bg-card: #14141a" in INDEX or "--bg-card:#14141a" in INDEX
    assert "--border-subtle:" in INDEX
    assert "--accent-cyan:" in INDEX
    assert '"cv11"' in INDEX  # font-feature-settings

def test_button_layers_defined():
    assert ".btn-primary" in INDEX
    assert ".btn-secondary" in INDEX
    assert ".btn-ghost" in INDEX

def test_no_interaction_handler_changed():
    # 确保没动 onclick 数量量级（这是粗略保护）
    onclick_count = INDEX.count("onclick=")
    assert onclick_count >= 30  # 当前数量
```

**Step 2: 跑测试，确认 FAIL**

```bash
pytest tests/test_main_ui_style_tokens.py -q
```
Expected: 2 个 FAIL（btn-primary / 颜色 token 还没改）

**Step 3: 替换 css `:root`**

定位 `templates/index.html` 第一个 `:root {` 块，替换变量为：
```css
:root {
    --bg-primary:    #0a0a0c;
    --bg-card:       #14141a;
    --bg-hover:      #1c1c24;
    --bg-elevated:   #1a1a22;
    --border-subtle: rgba(255,255,255,0.06);
    --border-active: rgba(94,200,255,0.35);
    --accent-cyan:   #5ec8ff;
    --accent-green:  #4ade80;
    --accent-red:    #ef4444;
    --accent-orange: #fb923c;
    --accent-amber:  #facc15;
    --text-strong:   #fafafa;
    --text-default:  #d4d4d8;
    --text-muted:    #a1a1aa;
    --text-dim:      #71717a;
    /* legacy 别名保留兼容（不破坏其他规则）*/
    --text:          var(--text-default);
    --bg:            var(--bg-primary);
    --accent:        var(--accent-cyan);
}
body, html { font-feature-settings: "cv11", "ss01"; }
```

**Step 4: 加按钮三层级 css**

在 `:root` 之后加：
```css
.btn-primary {
    background: var(--accent-cyan);
    color: #062235;
    border: 0;
    padding: 8px 14px;
    border-radius: 6px;
    font-weight: 600;
    cursor: pointer;
    transition: box-shadow 100ms ease-out;
}
.btn-primary:hover { box-shadow: 0 0 0 2px rgba(94,200,255,0.35); }
.btn-primary:active { transform: translateY(1px); }

.btn-secondary {
    background: var(--bg-card);
    color: var(--text-default);
    border: 1px solid var(--border-subtle);
    padding: 8px 14px;
    border-radius: 6px;
    cursor: pointer;
    transition: background 100ms ease-out;
}
.btn-secondary:hover { background: var(--bg-hover); border-color: var(--border-active); }

.btn-ghost {
    background: transparent;
    color: var(--text-muted);
    border: 1px solid transparent;
    padding: 8px 14px;
    border-radius: 6px;
    cursor: pointer;
    transition: all 100ms ease-out;
}
.btn-ghost:hover { color: var(--text-default); border-color: var(--border-subtle); }

.tab-panel {
    transition: opacity 100ms ease-out, transform 100ms ease-out;
}
```

**Step 5: 卡片阴影统一**

定位 `.workbench-card` 规则（约行 1032），替换 box-shadow / border 部分为：
```css
.workbench-card {
    background: var(--bg-card);
    border: 1px solid var(--border-subtle);
    border-radius: 6px;
    padding: 12px;
    box-shadow: 0 0 0 1px rgba(94,200,255,0.04) inset, 0 1px 2px rgba(0,0,0,0.3);
}
```

**Step 6: 状态胶囊微辉**

找到 `.svc-mini-dot` / `.dot` 规则，加 `text-shadow: 0 0 6px currentColor;` 到 active/在线状态。

**Step 7: 跑测试，确认 PASS**

```bash
pytest tests/test_main_ui_style_tokens.py -q
```
Expected: 3 passed

**Step 8: Commit**

```bash
git add tests/test_main_ui_style_tokens.py templates/index.html
git commit -m "feat(ui): Linear-style dark theme tokens + button hierarchy + card shadow + status glow"
```

### Task 3.2：部署 + 烟测

**Step 1-3:** 拿锁 / rsync `templates/index.html` / 不需重启服务（静态文件）。

**Step 4: 现场目视检查**

打开 http://10.244.190.224:5000/，确认：
- 整体更深、对比更高
- 卡片有 subtle 边框 + 阴影
- 按钮三层级清晰
- tab 切换有微动画
- 服务状态灯有微发光

**Step 5:** 释放锁，新 operator run（scenario `ui_polish_retest`）。

**Definition of Done:**
- pytest 绿。
- 现场目视通过 + 操作不变。
- 录制可继续。

---

## 阶段 4：代码结构图（不阻塞录制）

**Goal:** 用 stdlib `ast` + git CLI 生成 `data/code_graph/graph.json`，前端 3d-force-graph 渲染并支持点击展开 + git 着色。

**Files:**
- Create: `tools/build_code_graph.py`
- Create: `data/code_graph/.gitkeep`
- Modify: `streamer_app.py`（新增 `/api/code_graph`）
- Modify: `templates/index.html`（loadCodeGraph 真实现 + 引入 3d-force-graph CDN）
- Test: `tests/test_build_code_graph.py`

### Task 4.1：build_code_graph.py 数据生成器

**Step 1: 写失败测试**

`tests/test_build_code_graph.py`：
```python
import json
from pathlib import Path
from tools.build_code_graph import build_graph, parse_imports

def test_parse_imports_simple(tmp_path):
    f = tmp_path / "demo.py"
    f.write_text("import os\nimport hardware_engine.voice_daemon\nfrom hardware_engine.cognitive import coach_knowledge\n")
    imports = parse_imports(f)
    assert "hardware_engine.voice_daemon" in imports
    assert "hardware_engine.cognitive.coach_knowledge" in imports

def test_build_graph_returns_nodes_and_edges():
    g = build_graph(repo_root=Path("."))
    assert "nodes" in g and "edges" in g
    ids = {n["id"] for n in g["nodes"]}
    assert "streamer_app.py" in ids
    assert "voice_daemon.py" in ids
    for n in g["nodes"]:
        assert "kind" in n and "loc" in n and "git_age_days" in n and "path" in n

def test_build_graph_excludes_tests_and_archive():
    g = build_graph(repo_root=Path("."))
    paths = [n["path"] for n in g["nodes"]]
    assert not any(p.startswith("tests/") for p in paths)
    assert not any(p.startswith(".archive/") for p in paths)
```

**Step 2: 跑测试，确认 FAIL**

```bash
pytest tests/test_build_code_graph.py -q
```
Expected: ImportError（tool 未存在）

**Step 3: 写 tools/build_code_graph.py**

完整实现：
```python
#!/usr/bin/env python3
"""Generate data/code_graph/graph.json for IronBuddy 3d-force-graph viewer.

Python 3.7 compatible. stdlib only.
"""
from __future__ import absolute_import, print_function

import argparse
import ast
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path


INCLUDE_DIRS = (
    "hardware_engine",
    "tools",
    "scripts",
)
INCLUDE_FILES = (
    "streamer_app.py",
)
FRONTEND_FILE = "templates/index.html"
EXCLUDE_PREFIXES = (
    "tests/",
    ".archive/",
    "tools/rknn-toolkit_source",
    "hardware_engine/__pycache__",
)


KIND_BY_PATH = (
    ("hardware_engine/voice", "voice"),
    ("hardware_engine/cognitive", "cognitive"),
    ("hardware_engine/sensor", "sensor"),
    ("hardware_engine/ai_sensory", "vision"),
    ("hardware_engine/integrations", "cloud"),
    ("hardware_engine/main_claw_loop.py", "fsm"),
    ("hardware_engine/voice_daemon.py", "voice"),
    ("streamer_app.py", "api"),
    ("templates/", "frontend"),
    ("tools/ironbuddy_operator_console.py", "debug"),
    ("tools/ironbuddy_sensor_lab.py", "debug"),
    ("tools/", "shared"),
    ("scripts/", "shared"),
)


def kind_for_path(rel_path):
    for prefix, kind in KIND_BY_PATH:
        if rel_path.startswith(prefix):
            return kind
    return "shared"


def is_excluded(rel_path):
    return any(rel_path.startswith(p) for p in EXCLUDE_PREFIXES)


def collect_files(repo_root):
    files = []
    for inc in INCLUDE_DIRS:
        base = repo_root / inc
        if not base.exists():
            continue
        for p in base.rglob("*.py"):
            rel = str(p.relative_to(repo_root)).replace(os.sep, "/")
            if is_excluded(rel):
                continue
            files.append((rel, p))
    for inc in INCLUDE_FILES:
        p = repo_root / inc
        if p.exists():
            files.append((inc, p))
    fp = repo_root / FRONTEND_FILE
    if fp.exists():
        files.append((FRONTEND_FILE, fp))
    return files


def parse_imports(file_path):
    """Return list of dotted module names imported. Ignores syntax errors."""
    if not str(file_path).endswith(".py"):
        return []
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    try:
        tree = ast.parse(text)
    except Exception:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                out.append((mod + "." + alias.name).strip("."))
    return [m for m in out if m]


def module_to_relpath(module_name, all_paths_set):
    """Map dotted module to one of our nodes if possible."""
    candidates = [
        module_name.replace(".", "/") + ".py",
        module_name.replace(".", "/") + "/__init__.py",
    ]
    for c in candidates:
        if c in all_paths_set:
            return c
    parts = module_name.split(".")
    while parts:
        c = "/".join(parts) + ".py"
        if c in all_paths_set:
            return c
        parts = parts[:-1]
    return None


def loc_count(file_path):
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def git_age_days(rel_path, repo_root):
    """Return -1 if uncommitted (dirty), else days since last commit."""
    try:
        dirty = subprocess.check_output(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--", rel_path],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8").strip()
        if dirty:
            return -1
        ts_str = subprocess.check_output(
            ["git", "-C", str(repo_root), "log", "-1", "--format=%ct", "--", rel_path],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8").strip()
        if not ts_str:
            return 999
        ts = int(ts_str)
        return int((time.time() - ts) / 86400)
    except Exception:
        return 999


def head_short_sha(repo_root):
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8").strip()
    except Exception:
        return ""


def build_graph(repo_root=None):
    repo_root = Path(repo_root or ".").resolve()
    files = collect_files(repo_root)
    paths_set = {rel for rel, _ in files}

    nodes = []
    edges = []
    label_to_id = {}
    for rel, p in files:
        node_id = os.path.basename(rel)
        # ensure unique id; if collision, use rel path
        if node_id in label_to_id:
            node_id = rel
        label_to_id[node_id] = rel
        nodes.append({
            "id": node_id,
            "label": os.path.splitext(node_id)[0],
            "kind": kind_for_path(rel),
            "loc": loc_count(p),
            "git_age_days": git_age_days(rel, repo_root),
            "path": rel,
        })

    rel_to_id = {rel: nid for nid, rel in label_to_id.items()}
    for nid, rel in label_to_id.items():
        if not rel.endswith(".py"):
            continue
        imports = parse_imports(repo_root / rel)
        seen = set()
        for mod in imports:
            target_rel = module_to_relpath(mod, paths_set)
            if not target_rel or target_rel == rel:
                continue
            target_id = rel_to_id.get(target_rel)
            if not target_id or (nid, target_id) in seen:
                continue
            seen.add((nid, target_id))
            edges.append({"source": nid, "target": target_id, "kind": "import"})

    return {
        "nodes": nodes,
        "edges": edges,
        "generated_at": datetime.datetime.now().isoformat(),
        "commit": head_short_sha(repo_root),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--out", default="data/code_graph/graph.json")
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args()

    out = Path(args.repo_root) / args.out
    if not args.refresh and out.exists():
        print("graph exists at", out, "(pass --refresh to rebuild)")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    g = build_graph(args.repo_root)
    out.write_text(json.dumps(g, ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", len(g["nodes"]), "nodes,", len(g["edges"]), "edges to", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Step 4: 跑测试，确认 PASS**

```bash
mkdir -p data/code_graph && touch data/code_graph/.gitkeep
pytest tests/test_build_code_graph.py -q
```
Expected: 3 passed

**Step 5: 生成首份 graph.json**

```bash
python3 tools/build_code_graph.py --refresh
ls -la data/code_graph/graph.json
python3 -c "import json; d=json.load(open('data/code_graph/graph.json')); print(len(d['nodes']),'nodes,',len(d['edges']),'edges')"
```
Expected: ~25-40 nodes，~50-150 edges

**Step 6: Commit**

```bash
git add tools/build_code_graph.py tests/test_build_code_graph.py data/code_graph/.gitkeep data/code_graph/graph.json
git commit -m "feat(viz): tools/build_code_graph.py generates ast+git graph.json (stdlib, py3.7)"
```

### Task 4.2：后端 /api/code_graph 端点

**Step 1: 写测试**

`tests/test_main_ui_tabs.py` 加：
```python
def test_api_code_graph_returns_data(tmp_path, monkeypatch):
    import importlib, streamer_app, json
    p = tmp_path / "graph.json"
    p.write_text(json.dumps({"nodes": [{"id":"a.py"}], "edges": []}))
    monkeypatch.setenv("IRONBUDDY_CODE_GRAPH_PATH", str(p))
    importlib.reload(streamer_app)
    client = streamer_app.app.test_client()
    r = client.get("/api/code_graph")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True
    assert d["nodes"][0]["id"] == "a.py"
```

**Step 2: 加端点 streamer_app.py**

```python
@app.route('/api/code_graph')
def api_code_graph():
    """Return data/code_graph/graph.json built by tools/build_code_graph.py."""
    path = os.environ.get("IRONBUDDY_CODE_GRAPH_PATH",
                          os.path.join(PROJECT_ROOT, "data", "code_graph", "graph.json"))
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["ok"] = True
            return Response(json.dumps(data, ensure_ascii=False),
                            mimetype='application/json')
        return Response(json.dumps({
            "ok": False,
            "message": "graph.json not found; run python3 tools/build_code_graph.py --refresh"
        }, ensure_ascii=False), mimetype='application/json')
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        mimetype='application/json')
```

**Step 3: pytest + commit**

```bash
pytest tests/test_main_ui_tabs.py::test_api_code_graph_returns_data -q
git add streamer_app.py tests/test_main_ui_tabs.py
git commit -m "feat(viz): /api/code_graph reads graph.json"
```

### Task 4.3：前端 3d-force-graph 渲染

**Step 1: 加 CDN 依赖（head 内）**

`templates/index.html` `<head>` 末尾加：
```html
<script src="https://unpkg.com/three@0.157.0/build/three.min.js"></script>
<script src="https://unpkg.com/3d-force-graph@1.73.4/dist/3d-force-graph.min.js"></script>
```

**降级**：板端无外网时把上述两个 .min.js 下载到 `static/vendor/` 改 src 为 `/static/vendor/...`（如必要）。

**Step 2: 替换 loadCodeGraph 实现**

```javascript
let _codeGraphInstance = null;

function _colorForGitAge(d) {
    if (d.git_age_days < 0) return '#facc15';
    if (d.git_age_days <= 7) return '#fb923c';
    if (d.git_age_days <= 30) return '#5ec8ff';
    return '#4b5563';
}

function _sizeForLoc(loc) {
    var s = Math.sqrt(loc || 0) * 0.5;
    if (s < 4) return 4;
    if (s > 14) return 14;
    return s;
}

async function loadCodeGraph() {
    var mount = document.getElementById('codeGraphMount');
    if (!mount) return;
    if (typeof ForceGraph3D !== 'function') {
        mount.innerHTML = '<div style="padding:14px; color:var(--text-dim); font-size:0.78em;">3d-force-graph CDN 未加载</div>';
        return;
    }
    try {
        var r = await fetch('/api/code_graph', {cache:'no-store'});
        var d = await r.json();
        if (!d.ok) {
            mount.innerHTML = '<div style="padding:14px; color:var(--text-dim); font-size:0.78em;">' + (d.message || '未生成') + '</div>';
            return;
        }
        // 转 3d-force-graph 数据格式
        var data = {
            nodes: d.nodes.map(function(n) {
                return Object.assign({}, n, {
                    val: _sizeForLoc(n.loc),
                    color: _colorForGitAge(n)
                });
            }),
            links: d.edges.map(function(e) { return {source: e.source, target: e.target}; })
        };
        mount.innerHTML = '';
        _codeGraphInstance = ForceGraph3D()(mount)
            .backgroundColor('#0a0a0c')
            .nodeLabel(function(n) { return n.path + ' · ' + n.loc + ' loc · ' + n.git_age_days + 'd'; })
            .linkColor(function() { return 'rgba(94,200,255,0.25)'; })
            .nodeAutoColorBy(null)
            .graphData(data)
            .onNodeClick(function(n) {
                var meta = document.getElementById('codeGraphMeta');
                if (meta) {
                    meta.textContent = n.path + ' · ' + n.kind + ' · ' + n.loc + ' loc · git ' +
                                        (n.git_age_days < 0 ? '未提交' : n.git_age_days + ' 天前');
                }
                // 居中聚焦
                var distance = 80;
                var distRatio = 1 + distance / Math.hypot(n.x, n.y, n.z);
                _codeGraphInstance.cameraPosition(
                    {x: n.x*distRatio, y: n.y*distRatio, z: n.z*distRatio},
                    n, 800
                );
            });
    } catch(e) {
        mount.innerHTML = '<div style="padding:14px; color:var(--accent-red); font-size:0.78em;">加载失败: ' + e.message + '</div>';
    }
}
```

**Step 3: 现场目视检查**

本地启动 streamer 或用板端，打开主网页 → 调试 tab → 看到 3D 力导向图。

**Step 4: Commit**

```bash
git add templates/index.html
git commit -m "feat(viz): 3d-force-graph rendering with git-age coloring + click-to-focus"
```

### Task 4.4：部署 + 烟测

部署清单：
- `tools/build_code_graph.py`
- `streamer_app.py`
- `templates/index.html`
- `data/code_graph/graph.json`

板端生成 graph.json：
```bash
ssh -i ~/.ssh/id_rsa_toybrick toybrick@10.244.190.224 \
  'cd /home/toybrick/streamer_v3 && python3 tools/build_code_graph.py --refresh'
```

烟测：
```bash
curl --noproxy '*' -sS http://10.244.190.224:5000/api/code_graph | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print('ok=',d['ok'],'nodes=',len(d.get('nodes',[])))"
```

新 operator run（scenario `code_graph_smoke`）现场打开调试 tab → 拖图 + 点节点 + 看着色。

**Definition of Done:**
- pytest 绿。
- `/api/code_graph` 返回 ok + 25+ 节点。
- 现场调试 tab 看到 3d-force-graph，节点可拖、可点、未提交节点黄色、近期改动节点橙色。

---

## 阶段 5：operator console 主题对齐（不阻塞）

**Goal:** 让 iframe 嵌入的 8765 视觉风格与主网页一致。

**Files:**
- Modify: `tools/ironbuddy_operator_console.py`（INDEX_HTML 内 `<style>` 区）
- Test: `tests/test_operator_console_scenarios.py`（已存在，确保不回归）

### Task 5.1：套同款 css token

**Step 1: 找到 INDEX_HTML `<style>`**

`tools/ironbuddy_operator_console.py:798` 起的 `INDEX_HTML` 字符串。

**Step 2: 替换 `:root`**

加同款颜色 token + 卡片阴影 + 按钮三层级（与阶段 3 一致）。**不动任何 onclick / fetch /
DOM id**。

**Step 3: 运行**

```bash
python3 -m py_compile tools/ironbuddy_operator_console.py
pytest tests/test_operator_console_scenarios.py -q
```
Expected: passed（行为不变）

**Step 4: 本机起一份目视对比**

```bash
IRONBUDDY_BOARD_IP=10.244.190.224 python3 tools/ironbuddy_operator_console.py \
  --scenario theme_align_check
```
打开 http://127.0.0.1:8765/ 看风格。

**Step 5: Commit**

```bash
git add tools/ironbuddy_operator_console.py
git commit -m "feat(operator): align operator console dark theme with main UI"
```

**Definition of Done:**
- pytest 绿（行为零回归）。
- 主网页调试 tab 内 iframe 与主网页深色风格一致，无明显反差。

---

## 阶段 6：调试 tab 反馈区落 run（不阻塞）

**Goal:** 阶段 2 占位的 `submitFeedback()` 真把备注 + 截图 写到当前 8765 run。

**Files:**
- Modify: `templates/index.html`（submitFeedback 函数）
- Test: 手动验证 + `tests/test_main_ui_tabs.py` 断言函数体非占位

### Task 6.1：实现 submitFeedback

**Step 1: 改 templates/index.html `submitFeedback`**

```javascript
async function submitFeedback() {
    var s = document.getElementById('feedbackStatus');
    var note = (document.getElementById('feedbackNote').value || '').trim();
    if (!note && !_feedbackImageData) {
        s.textContent = '请填写备注或选择截图';
        return;
    }
    s.textContent = '保存中…';
    try {
        // 1) 提交 action（备注 + 默认 monitor 类）
        if (note) {
            await fetch('http://127.0.0.1:8765/api/action', {
                method: 'POST',
                headers: {'Content-Type':'application/json'},
                body: JSON.stringify({action: 'monitor', note: '主网页反馈: ' + note})
            });
        }
        // 2) 提交截图
        if (_feedbackImageData) {
            var blob = await (await fetch(_feedbackImageData)).blob();
            var fd = new FormData();
            fd.append('file', blob, 'feedback_' + Date.now() + '.png');
            fd.append('note', note || '主网页粘贴截图');
            await fetch('http://127.0.0.1:8765/api/upload', {method:'POST', body: fd});
        }
        s.textContent = '已保存 ✓';
        document.getElementById('feedbackNote').value = '';
        document.getElementById('feedbackImagePreview').innerHTML = '';
        _feedbackImageData = null;
        setTimeout(function(){ s.textContent = ''; }, 4000);
    } catch(e) {
        s.textContent = '失败: ' + e.message;
    }
}
```

**Step 2: 加测试断言**

`tests/test_main_ui_tabs.py` 加：
```python
def test_submit_feedback_no_longer_placeholder():
    assert "阶段 6 实现" not in INDEX
    assert "127.0.0.1:8765/api/action" in INDEX
    assert "127.0.0.1:8765/api/upload" in INDEX
```

**Step 3: pytest + commit**

```bash
pytest tests/test_main_ui_tabs.py -q
git add templates/index.html tests/test_main_ui_tabs.py
git commit -m "feat(ui): debug tab feedback submits to operator console api/action + api/upload"
```

### Task 6.2：部署 + 烟测

部署 `templates/index.html`。

现场测试：
1. 浏览器开主网页 → 调试 tab。
2. 写一段备注 + 粘贴截图。
3. 点"保存到当前 run"。
4. iframe 内 8765 events 出现新条目 + uploads/ 出现新图。
5. 板端 `docs/test_runs/ironbuddy_operator/<latest>/events.jsonl` 有新行。

**Definition of Done:**
- pytest 绿。
- 现场反馈一次能写到 events.jsonl + uploads/。

---

## 全程总结（每阶段都要写到 CURRENT.md）

每阶段结束在 `CURRENT.md` 加一条形如：
```markdown
- 2026-05-03 HH:MM CST，Claude Code 已部署阶段 N <topic> 并释放锁：
  远端备份在 `.deploy_backups/claude_code_<TS>_<topic>/`；上传文件 ...；
  烟测通过 ...；新 operator run `<run_id>` scenario `<scenario>`。
```

## 紧急回滚

若任意阶段部署后烟测失败：
```bash
ssh -i ~/.ssh/id_rsa_toybrick toybrick@10.244.190.224 \
  "cp /home/toybrick/streamer_v3/.deploy_backups/claude_code_<TS>_<topic>/* \
      /home/toybrick/streamer_v3/"
# 重启相关服务
```
本地 git revert 对应 commit。
