"""Static checks for the main UI training-center restructure.

Verifies:
- 数据 tab 是用户训练中心，不暴露 raw 训练数据树/拍摄 session
- switchTab 不再调用 loadDemoShowcase
- 调试 tab 有 codeGraphMount + 内置记录 + 折叠日志
- loadCodeGraph 在 logs tab 触发
"""
import os


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_HTML = os.path.join(PROJECT_ROOT, "templates", "index.html")


def _read():
    with open(INDEX_HTML, "r", encoding="utf-8") as f:
        return f.read()


def test_data_tab_no_rag_or_opencloud():
    src = _read()
    assert 'id="demoShowcaseContainer"' not in src
    assert 'id="codeGraphContainer"' not in src


def test_tab_order_is_console_data_debug_settings():
    src = _read()
    start = src.find('<div class="tab-bar">')
    assert start != -1
    chunk = src[start:src.find('</div>', start) + 6]
    order = [
        chunk.find('data-tab="console"'),
        chunk.find('data-tab="data"'),
        chunk.find('data-tab="logs"'),
        chunk.find('data-tab="settings"'),
    ]
    assert all(i >= 0 for i in order)
    assert order == sorted(order)


def test_data_tab_is_training_center_not_raw_shooting_ui():
    src = _read()
    assert "今日方案" in src
    assert "本次训练" in src
    assert "训练趋势" in src
    assert "知识库与报告" in src
    assert "后台数据库" in src
    assert "当前拍摄 session" not in src
    assert 'id="dataTree"' not in src
    assert "打开一站式数据库可视化" not in src
    assert "拍摄清场" not in src
    assert "让摄像头拍摄" not in src


def test_data_tab_avoids_internal_product_words():
    src = _read()
    start = src.find('<div class="tab-panel" id="tab-data">')
    end = src.find('<!-- TAB 3:', start)
    assert start != -1 and end != -1
    data_tab = src[start:end]
    for phrase in (
        "rule_fallback",
        "dry-run",
        "daemon:",
        "dependency graph",
        "当前拍摄 session",
    ):
        assert phrase not in data_tab
    assert 'id="dailyPlanStages"' not in data_tab
    assert 'id="dailyPlanOrb"' in data_tab
    assert 'id="dailyPlanThinking"' in data_tab
    assert "rejectDailyPlan()" in data_tab
    assert "plan-orb ' + (state || 'idle')" in src


def test_switch_tab_does_not_call_demo_showcase():
    src = _read()
    assert "loadDemoShowcase()" not in src


def test_logs_tab_has_debug_console_and_graph_slots():
    src = _read()
    assert 'id="codeGraphMount"' in src
    assert 'id="operatorConsoleLink"' in src
    assert 'id="customActionBuilder"' in src


def test_logs_tab_log_terminal_collapsible():
    src = _read()
    assert 'id="logTerminalDetails"' in src


def test_logs_tab_is_not_a_feedback_capture_surface():
    src = _read()
    start = src.find('<div class="tab-panel" id="tab-logs">')
    end = src.find('<!-- TAB 4:', start)
    assert start != -1 and end != -1
    logs_tab = src[start:end]
    assert 'id="feedbackNote"' not in logs_tab
    assert 'id="feedbackFile"' not in logs_tab
    assert 'id="feedbackDropzone"' not in logs_tab
    assert "上传截图" not in logs_tab
    assert "截图与验收记录" not in logs_tab


def test_load_code_graph_called_in_logs_tab():
    src = _read()
    # Inside switchTab, when tabId === 'logs', loadCodeGraph should be called
    idx = src.find("function switchTab(")
    assert idx != -1
    # Look at next ~600 chars
    body = src[idx:idx + 1200]
    assert "loadCodeGraph" in body
    assert "'logs'" in body


def test_debug_workbench_points_to_lane_c_operator_runbook():
    src = _read()
    assert "打开调试台" in src
    assert "代码仓库导航" in src
    assert "新动作采集模式" in src


def test_streamer_has_code_graph_endpoint():
    """Stage 4.2: /api/code_graph endpoint reads graph.json."""
    streamer_path = os.path.join(PROJECT_ROOT, "streamer_app.py")
    with open(streamer_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "@app.route('/api/code_graph')" in src
    assert "data/code_graph/graph.json" in src
    assert "IRONBUDDY_CODE_GRAPH_PATH" in src


def test_main_ui_uses_rep_event_for_completed_curl_classification():
    src = _read()
    assert "d.last_rep_event.final_result || d.last_rep_event.visual_result" in src
    assert "_lastRepEventIndex = d.last_rep_event.rep_index" in src
    assert "triggerFlash(flash, _lastClassification, flashDelta)" in src


def test_rag_delivery_ui_shows_send_state_and_unique_manual_turns():
    src = _read()
    assert 'id="ragSendState"' in src
    assert 'id="feishuPolicyMeta"' in src
    assert 'id="openclawMeta"' in src
    assert "function formatRagSendState" in src
    assert "飞书投递" in src
    assert "OpenClaw 周报推送" in src
    assert "测试命中预览" not in src
    assert "发送命中详报" not in src
    assert "仅生成草稿 · 未发送" not in src
    assert "testRagDelivery" not in src
    assert "知识问答：等待专业问题" in src
    assert "知识问答：已推送飞书" in src


def test_settings_ui_can_connect_cloned_cloud_gpu():
    src = _read()
    assert 'class="settings-shell"' in src
    assert 'class="settings-section"' in src
    assert 'id="cfgCloudSshCommand"' in src
    assert 'id="cfgCloudSshPassword"' in src
    assert 'input-mono" id="cfgCloudSshCommand"' in src
    assert "保存并联通 GPU" in src
    assert "function describeCloudGpuStatus" in src
    assert "async function connectCloudGpu" in src
    assert "/api/admin/cloud_gpu/connect" in src
    assert "password_configured" in src


def test_training_plan_ui_covers_squat_plan_and_current_evidence():
    src = _read()
    assert "启动深蹲 3 组" not in src
    assert "function startDefaultSquatPlan" in src
    assert "reset_session:true" in src
    assert "resetRecordingFrontendState(label + ' 3 组计划已启动，等待入镜')" in src
    assert "setTimeout(loadTrainingPlan, 600)" in src
    assert "setTimeout(loadTrainingEvidence, 600)" in src
    assert "/api/training_session/evidence" in src
    assert 'id="trainingEvidenceMeta"' in src
    assert 'id="trainingEvidenceBody"' in src
    assert "本次累计" in src


def test_daily_plan_ui_and_api_contract_present():
    src = _read()
    assert 'id="dailyPlanCard"' in src
    assert 'class="workbench-card daily-ai-card"' in src
    assert 'id="dailyPlanPrompt"' in src
    assert "generateDailyPlan()" in src
    assert "acceptDailyPlan()" in src
    assert "function _startPlanThinking" in src
    assert "function _renderThinking" in src
    assert "function rejectDailyPlan" in src
    assert "正在生成今日方案" in src
    assert "await _sleep(1800 - elapsed)" in src
    assert "智能方案" in src
    assert "基础方案" in src
    assert "/api/training_plan/daily" in src
    assert "/api/training_plan/daily/accept" in src

    streamer_path = os.path.join(PROJECT_ROOT, "streamer_app.py")
    with open(streamer_path, "r", encoding="utf-8") as f:
        streamer_src = f.read()
    assert "@app.route('/api/training_plan/daily'" in streamer_src
    assert "@app.route('/api/training_plan/daily/accept'" in streamer_src
    assert "def _call_deepseek_daily_plan" in streamer_src
    assert "def _normalize_deepseek_daily_plan" in streamer_src
    assert '"source": "deepseek"' in streamer_src
    assert "rule_fallback" in streamer_src


def test_console_shows_accepted_daily_plan_and_auto_advances_sets():
    src = _read()
    assert 'id="consoleDailyPlanCard"' in src
    assert 'id="consolePlanSummary"' in src
    assert 'id="consolePlanProgress"' in src
    assert "function renderConsolePlanCard" in src
    assert "function maybeAutoAdvanceTrainingSet" in src
    assert "advanceTrainingPlanSet(true)" in src
    assert "renderConsolePlanCard(_latestRuntimePlan, _latestRuntimeReport, d)" in src
    assert "function _currentSetLiveTotal" in src
    assert "function _currentSetLiveFatigue" in src
    assert 'id="fatigueModelSummary"' in src
    assert "function formatFatigueModelSummary" in src
    assert "可解释公式" in src
    assert "旧固定积分" in src
    assert "waitingForReset:true, pendingSet:current + 1" in src
    assert "当前第 ' + current + ' 组，目标疲劳 ' + target + '。" in src


def test_training_plan_reset_clears_visible_counters_and_rig_state():
    src = _read()
    idx = src.find("function resetRecordingFrontendState")
    assert idx != -1
    body = src[idx:idx + 2400]
    assert "_repEventQueue.length = 0" in body
    assert "_rigLastGood = 0" in body
    assert "_rigLastFailed = 0" in body
    assert "_rigLastComp = 0" in body
    assert "'sbTotalGood','sbTotalFailed','sbTotalComp'" in body
    assert "fatigueBar" in body
    assert "rigStatusText" in body


def test_header_audio_controls_are_not_duplicate_speaker_icons():
    src = _read()
    assert 'class="volume-label">VOL</span>' in src
    assert 'class="icon-btn mute-toggle" id="muteBtn"' in src
    assert '>有声</button>' in src
    assert "btn.textContent = '静音'" in src
    assert "btn.textContent = '有声'" in src


def test_main_ui_has_separate_raw_emg_panel_and_endpoint():
    src = _read()
    assert 'id="emgCanvas"' in src
    assert 'id="emgRawCanvas"' in src
    assert 'id="emgRawStatus"' in src
    assert "EMG_FILTERED_REFRESH_MS = 500" in src
    assert "EMG_RAW_REFRESH_MS = 1000" in src
    assert "function pollEmgOnce(force)" in src
    assert "function pollEmgRawOnce(force)" in src
    assert "if (!force && !isTabActive('console')) return;" in src
    assert "fetch('/api/emg_fast'" in src
    assert "function drawEmgRawWaveform" in src
    assert "signal_mode" in src
    assert "valid_for_gru" in src

    streamer_path = os.path.join(PROJECT_ROOT, "streamer_app.py")
    with open(streamer_path, "r", encoding="utf-8") as f:
        streamer_src = f.read()
    assert "@app.route('/api/emg_fast')" in streamer_src
    assert "@app.route('/api/emg_stream')" in streamer_src
    assert "/dev/shm/emg_stream_buffer.json" in streamer_src
    assert "text/event-stream" in streamer_src
    assert "/dev/shm/emg_raw_waveform.json" in streamer_src
    assert "railish_ratio" in streamer_src
    assert "floating_no_contact" in streamer_src


def test_lane_c_polish_preserves_vanilla_stack_and_contracts():
    src = _read()
    assert "Lane C recording polish" in src
    assert "shadcn-inspired, no React/Tailwind dependency" in src
    assert "--lane-c-panel" in src
    assert "rgba(94,200,255,0.13)" in src
    assert "data-tab=\"console\"" in src
    assert "data-tab=\"data\"" in src
    assert "data-tab=\"logs\"" in src
    assert "data-tab=\"settings\"" in src
    assert 'id="muteBtn"' in src
    assert 'id="videoFeed"' in src
    assert 'id="emgCanvas"' in src
    assert 'id="emgRawCanvas"' in src
    assert 'id="operatorConsoleLink"' in src
    assert 'id="codeGraphMount"' in src
    assert 'id="cfgCloudSshCommand"' in src
    assert 'id="cfgCloudSshPassword"' in src
    for forbidden in (
        "react.development.js",
        "ReactDOM",
        "tailwind.config",
        "components.json",
        "@radix-ui",
    ):
        assert forbidden not in src


def test_main_ui_realtime_polling_is_bounded_for_board_load():
    src = _read()
    assert "timer = setInterval(function() { poll(false); }, 200)" in src
    assert "else if (e.data === 'STOP')" in src
    assert "syncWorker.postMessage('STOP')" in src
    assert "function isPageVisible()" in src
    assert "let busy = false" in src
    assert "now - lastFetch < 180" in src
    assert "function _syncRepCounters" in src
    assert "if (_chatEventsBusy) return;" in src
    assert "if (_legacyChatBusy) return;" in src
    assert "if (_llmReplyBusy) return;" in src
    assert "if (_emgFilteredBusy) return;" in src
    assert "if (_emgRawBusy) return;" in src
    assert "if (_servicesPollBusy) return;" in src
    assert "if (_systemInfoPollBusy) return;" in src
    assert "if (_logsPollBusy) return;" in src
    assert "if (_headerTagsBusy) return;" in src
    assert "if (_inferenceModePollBusy) return;" in src
    assert "_enqueueRepEvents" not in src
    assert "syncWorker.postMessage('FORCE'); } catch(e) {}\n}, 300)" not in src
    assert "setInterval(updateHeaderTags, 5000)" in src

    streamer_path = os.path.join(PROJECT_ROOT, "streamer_app.py")
    with open(streamer_path, "r", encoding="utf-8") as f:
        streamer_src = f.read()
    assert "state_feed._cache" in streamer_src
    assert "now - cache.get(\"ts\", 0.0) < 0.12" in streamer_src
    assert "muscle_activation._cache" in streamer_src
    assert "now - cache.get(\"ts\", 0.0) < 0.35" in streamer_src
    assert "emg_fast._cache" in streamer_src
    assert "sample_limit = int(request.args.get(\"limit\", 420 if full else 160))" in streamer_src
    assert "samples_all[-sample_limit:]" in streamer_src


def test_debug_tab_is_simple_console_link_and_custom_action_builder():
    """Debug tab is a compact display surface, not the full 8765 workflow."""
    src = _read()
    assert "阶段 6 实现" not in src
    assert "http://127.0.0.1:8765/" in src
    assert "打开调试台" in src
    assert "新动作采集模式" in src
    assert 'id="customActionName"' in src
    assert "function createCustomAction" in src
    assert "function renderCustomActionBuilder" in src
    assert "SENSOR_LAB_BASE_URL" in src
    assert "/api/recording/start" in src
    assert "/api/recording/stop" in src
    assert "data/custom_actions/" in src
    assert "http://127.0.0.1:8766/" in src
    assert "CH0 主发力 / CH1 代偿发力" in src
    assert "飞鸟" not in src
    assert "打开 8765" not in src
    assert "operatorIframe" not in src


def test_custom_action_followup_doc_exists_and_marks_backend_future_work():
    doc_path = os.path.join(
        PROJECT_ROOT,
        "docs",
        "给用户看的交付",
        "LaneC_视觉语音UI验收",
        "10_新动作采集模式后续扩展.md",
    )
    with open(doc_path, "r", encoding="utf-8") as f:
        doc = f.read()
    assert "保存口径对齐" in doc
    assert "Sensor Lab 结束记录后，会保存一份镜像 JSON" in doc
    assert "不会自动训练新的 GRU 权重" in doc
    assert "data/custom_actions/<action_slug>/<label>/" in doc


def test_manual_voice_fallback_controls_exist():
    src = _read()
    assert "手动语音录入" in src
    assert 'id="manualVoiceStartBtn"' in src
    assert 'id="manualVoiceStopBtn"' in src
    assert 'id="manualVoiceStatus"' in src
    assert "function manualVoiceStart()" in src
    assert "function manualVoiceStop()" in src
    assert "/api/voice_manual/start" in src
    assert "/api/voice_manual/stop" in src
    assert "/api/voice_manual/status" in src


def test_voice_realtime_debug_panel_is_collapsed_and_uses_existing_apis():
    src = _read()
    assert 'id="voiceRealtimeDebugPanel"' in src
    panel_idx = src.find('id="voiceRealtimeDebugPanel"')
    panel_tag = src[max(0, panel_idx - 80):panel_idx + 120]
    assert "<details" in panel_tag
    assert " open" not in panel_tag
    assert "语音实时录入" in src
    assert "function toggleVoiceRealtimeDebug" in src
    assert "function pollVoiceRealtimeDebug" in src
    assert "/api/voice_debug" in src
    assert "/api/chat_draft" in src
    assert "/api/voice_turn" in src
    assert "setInterval(pollVoiceRealtimeDebug, 800)" in src
