"""Source checks for operator-console scenarios used in recording rehearsals."""

import os


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSOLE = os.path.join(ROOT, "tools", "ironbuddy_operator_console.py")


def test_operator_console_has_key_retest_scenarios():
    with open(CONSOLE, "r", encoding="utf-8") as f:
        src = f.read()
    for name in (
        "rag_feishu_cloud_retest",
        "voice_emg_retest",
        "recording_rehearsal",
        "rag_voice_control_fix_retest",
        "one_shot_acceptance",
        "video_acceptance_rehearsal",
        "lane_c_recording_rehearsal",
        "vector_rag_fatigue_acceptance",
        "lane_d_voice_tuning",
    ):
        assert name in src


def test_operator_console_defaults_to_current_board_ip_shape():
    with open(CONSOLE, "r", encoding="utf-8") as f:
        src = f.read()
    assert "IRONBUDDY_BOARD_IP" in src
    assert "DEFAULT_BOARD_IP" in src
    assert "10.29.10.224" in src


def test_one_shot_acceptance_covers_final_demo_surfaces():
    with open(CONSOLE, "r", encoding="utf-8") as f:
        src = f.read()
    idx = src.find("ONE_SHOT_ACCEPTANCE_STEPS")
    assert idx != -1
    body = src[idx:src.find("STEP_SETS", idx)]
    for phrase in (
        "一键启动流畅度",
        "弯举 3 组验收",
        "计划进度",
        "RAG 自动飞书",
        "OpenClaw 后台提醒",
        "本次飞书报告",
    ):
        assert phrase in body


def test_video_acceptance_rehearsal_covers_clean_start_and_main_shoot():
    with open(CONSOLE, "r", encoding="utf-8") as f:
        src = f.read()
    idx = src.find("VIDEO_ACCEPTANCE_REHEARSAL_STEPS")
    assert idx != -1
    end = src.find("LANE_C_RECORDING_REHEARSAL_STEPS", idx)
    if end == -1:
        end = src.find("VECTOR_RAG_FATIGUE_ACCEPTANCE_STEPS", idx)
    if end == -1:
        end = src.find("STEP_SETS", idx)
    body = src[idx:end]
    for phrase in (
        "环境恢复",
        "主界面基线",
        "生成今日方案",
        "采纳并开始",
        "深蹲第 1 组",
        "切到第 2 组",
        "语音展示",
        "知识库与飞书",
        "数据库证据",
        "调试台状态",
        "拍摄结论",
    ):
        assert phrase in body
    assert "膝盖酸痛怎么办" in body
    assert "默认筛选是真实数据" in body
    assert "http://127.0.0.1:8765/" in body
    assert "视觉+传感" not in body
    assert "EMG" not in body
    assert "真实关机" not in body


def test_lane_c_recording_rehearsal_is_frontstage_only():
    with open(CONSOLE, "r", encoding="utf-8") as f:
        src = f.read()
    idx = src.find("LANE_C_RECORDING_REHEARSAL_STEPS")
    assert idx != -1
    body = src[idx:src.find("VECTOR_RAG_FATIGUE_ACCEPTANCE_STEPS", idx)]
    for phrase in (
        "环境恢复",
        "主网页基线",
        "视觉画面",
        "语音唤醒",
        "语音播报 / 静音 / 打断",
        "短训练流程",
        "8765 记录",
        "现场结论",
    ):
        assert phrase in body
    assert "lane_c_recording_rehearsal" in src
    assert "通过/失败/重试/跳过" in body
    assert "本轮不改 voice_daemon" in body
    assert "RAG/飞书转 Lane A" in body
    assert "EMG/GRU 转 Lane B" in body
    assert "专业问答命中后自动推送飞书详报" not in body
    assert "MVC" not in body
    assert "MIA" not in body


def test_operator_console_status_refresh_is_bounded_for_slow_pages():
    with open(CONSOLE, "r", encoding="utf-8") as f:
        src = f.read()
    assert "let statusBusy = false;" in src
    assert "if (statusBusy || document.visibilityState === 'hidden') return;" in src
    assert "statusBusy = true;" in src
    assert "statusBusy = false;" in src
    assert "document.addEventListener('visibilitychange'" in src
    assert "setInterval(loadStatus, 2200)" in src


def test_lane_d_voice_tuning_covers_manual_voice_debug_and_mvc_removal():
    with open(CONSOLE, "r", encoding="utf-8") as f:
        src = f.read()
    idx = src.find("LANE_D_VOICE_TUNING_STEPS")
    assert idx != -1
    body = src[idx:src.find("STEP_SETS", idx)]
    for phrase in (
        "Lane D 基线确认",
        "主网页语音入口",
        "只喊唤醒词",
        "连续两轮指令",
        "MVC 语音触发移除",
        "手动语音录入",
        "语音实时录入",
        "现场反馈结论",
        "/api/voice_manual/start",
        "/api/voice_manual/stop",
        "/api/voice_debug",
        "lane_d_voice_tuning",
    ):
        assert phrase in body or phrase in src
    assert "/api/lane_d/voice_manual/start" in src
    assert "/api/lane_d/voice_manual/stop" in src
    assert "/api/lane_d/voice_debug" in src
    assert "开始手动录入" in src
    assert "停止并识别" in src
    assert '<div class="lane-d-console" id="laneDVoicePanel">' in src
    assert '<details class="diagnostics" id="laneDVoicePanel">' not in src
    assert "它远程控制板端麦克风录音，不录电脑麦克风" in src
    assert "靠近板端麦克风" in body
    assert "recording、stop_requested、recognizing" in body
    assert "asr_empty" in body
    assert "直接 SILENCE" in body
    assert "不改 RAG、飞书、训练计划、EMG、GRU 或视觉逻辑" in body


def test_vector_rag_fatigue_acceptance_covers_new_acceptance_surface():
    with open(CONSOLE, "r", encoding="utf-8") as f:
        src = f.read()
    idx = src.find("VECTOR_RAG_FATIGUE_ACCEPTANCE_STEPS")
    assert idx != -1
    body = src[idx:src.find("STEP_SETS", idx)]
    for phrase in (
        "云端 RAG 健康检查",
        "/api/demo/rag_status",
        "vector_unavailable",
        "今日方案证据",
        "采纳疲劳目标",
        "下一组目标切换",
        "旧固定积分",
        "自动推送飞书详报",
        "USER_README_20260511_VECTOR_RAG_FATIGUE_ACCEPTANCE.md",
    ):
        assert phrase in body
    assert "密码" in body
    assert "token" in body
