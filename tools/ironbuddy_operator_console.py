#!/usr/bin/env python3
"""IronBuddy live-test operator console.

This tool is a local browser console for guided board-side testing. It keeps
the human operator in a Chinese button-driven workflow while continuously
capturing read-only evidence from the Toybrick board.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNS_DIR = ROOT / "docs" / "test_runs" / "ironbuddy_operator"
DEFAULT_BOARD_IP = os.environ.get("IRONBUDDY_BOARD_IP", "10.29.10.224")
LOG_PATTERN = (
    "ASR|识别|唤醒|教练|Chat_Watcher|LLM_Watcher|TTS|DeepSeek|chat_reply|"
    "chat_input|没听清|开始|切到|MVC|voice_turn|Turn|ManualVoice|"
    "manual_voice|voice_debug|SILENCE|VAD校准|录音超时|"
    "squat|curl|弯举|深蹲|RAG|CoachKB|知识|功能|使用手册|Feishu|飞书|"
    "card_push|OpenClaw|OpenCloud|opencloud|openclaw"
)


MAIN_STEPS = [
    {
        "id": "preflight",
        "title": "测前确认",
        "instruction": "确认当前只做拍摄优先实测：后台只读监控，不改代码，不 wire router，不测试“请关机”。",
        "expected": "你已准备好打开板端网页，并接受本控制台记录每一步结果。",
    },
    {
        "id": "webpage",
        "title": "网页打开",
        "instruction": "打开控制台顶部显示的板端网页地址，等待 IronBuddy 页面完整加载。",
        "expected": "页面完整加载，在线状态可见，不是浏览器错误页。",
    },
    {
        "id": "camera",
        "title": "摄像头画面",
        "instruction": "调整摄像头对准人或测试区域；让画面适合拍摄展示。",
        "expected": "视频区域能看到人或测试区域，不是天花板、黑屏或严重卡顿。",
    },
    {
        "id": "t1_voice",
        "title": "T1 语音入口",
        "instruction": "靠近板载麦克风 10-20cm，先说“教练”；听到“嗯”后说“现在适合做深蹲吗”。",
        "expected": "有 TTS 回应，UI 出现对话气泡，DeepSeek 回复，系统回到监听。",
    },
    {
        "id": "switch_squat",
        "title": "切深蹲",
        "instruction": "清楚说“教练，切到深蹲”。",
        "expected": "听到确认播报，UI/FSM 显示 squat 或深蹲。",
    },
    {
        "id": "squat_count",
        "title": "深蹲计数",
        "instruction": "在摄像头前做 2-3 个标准深蹲，动作慢一点，便于拍摄和识别。",
        "expected": "good counter 增加，不卡顿，不明显重复计数。",
    },
    {
        "id": "summary_mute",
        "title": "自动总结 / 播报禁录",
        "instruction": "如果触发了疲劳总结或 TTS 播报，在播报期间说几句环境话测试禁录。",
        "expected": "TTS 播报期间环境声音不进入新一轮 ASR；播报结束后恢复监听。",
    },
    {
        "id": "curl_mvc",
        "title": "切弯举 + MVC",
        "instruction": "说“教练，切到弯举”，再说“开始”或“开始 MVC”。",
        "expected": "弯举模式切换，MVC 引导、倒数、结束流程可见可听。",
    },
    {
        "id": "final",
        "title": "阶段总结",
        "instruction": "本轮已到总结阶段。等待汇总通过项、失败项、阻塞项和拍摄可用性。",
        "expected": "所有现场观察都已记录，下一步只做总结或修复计划。",
    },
]


BUBBLE_RETEST_STEPS = [
    {
        "id": "retest_start",
        "title": "复测段开始",
        "instruction": "刷新主网页和本调试台，确认这是新的气泡复测 run。后续每一句对话测完就先上传对应截图，再点通过/失败/重试。",
        "expected": "主网页是新部署后的页面；本调试台显示气泡复测步骤，不再停在旧 9/9 final。",
    },
    {
        "id": "qa_one_shot",
        "title": "问答气泡：一段式",
        "instruction": "对麦克风说：“教练，现在适合做深蹲吗”。说完后立刻观察主网页气泡。",
        "expected": "气泡顺序为“我：教练，现在适合做深蹲吗”→“教练：...”；不切换模式，不出现系统角色。",
    },
    {
        "id": "qa_two_step_wake",
        "title": "问答气泡：两段式",
        "instruction": "先说“教练”，听到回应后再说“现在适合做深蹲吗”。观察两段是否都进入气泡。",
        "expected": "气泡顺序为“我：教练”→“教练：嗯/我在”→“我：现在适合做深蹲吗”→“教练：...”。",
    },
    {
        "id": "switch_curl_bubbles",
        "title": "切弯举气泡",
        "instruction": "说：“教练，切换到哑铃弯举模式”。观察主网页气泡和模式状态。",
        "expected": "气泡顺序为“我：教练，切换到哑铃弯举模式”→“教练：已切换到弯举模式”→“教练：准备好后直接说：开始 MVC 测试”。",
    },
    {
        "id": "mvc_without_wake",
        "title": "MVC 免唤醒",
        "instruction": "在上一句弯举提示后的等待窗口内，直接说：“开始 MVC 测试”。",
        "expected": "无需再喊教练即可进入 MVC；气泡显示“我：开始 MVC 测试”，随后出现 MVC 引导和倒计时。",
    },
    {
        "id": "mvc_no_false_trigger",
        "title": "MVC 防误触发",
        "instruction": "不在 MVC 等待窗口时，说一句不带“教练”的普通话，例如“开始吧”或“我准备好了”。",
        "expected": "不应误进入 MVC；没有新的教练指令气泡。若现场不方便，可跳过并备注。",
    },
    {
        "id": "mute_unmute",
        "title": "静音 / 解除静音",
        "instruction": "先用页面或语音进入静音；静音后不喊教练，直接说：“解除静音”。",
        "expected": "能恢复对话；气泡显示“我：解除静音”与教练确认回复。",
    },
    {
        "id": "auto_summary",
        "title": "自动总结气泡",
        "instruction": "触发一次疲劳总结或手动总结；如果自然触发困难，可以备注跳过。",
        "expected": "先显示“正在生成本组总结”类气泡，返回后显示教练总结；重复触发显示本组已总结提示。",
    },
    {
        "id": "retest_final",
        "title": "气泡复测总结",
        "instruction": "汇总这轮复测：哪些句子顺序正确，哪些漏气泡/乱序/延迟明显。",
        "expected": "每个失败点都有对应步骤截图或备注，可以直接进入下一轮修复或进入下一个模块。",
    },
]


VOICE_EMG_RETEST_STEPS = [
    {
        "id": "voice_emg_start",
        "title": "语音 + EMG 复测开始",
        "instruction": "刷新主网页、本调试台和 Sensor Lab。确认这是新的 voice_emg_retest run，后续每一步测完再上传对应截图。",
        "expected": "调试台显示本步骤；主网页可用；Sensor Lab 如无硬件应显示 UDP 离线或无真实 EMG。",
    },
    {
        "id": "switch_curl_prompt",
        "title": "切弯举与 MVC 提示",
        "instruction": "说：“教练，切换到哑铃弯举模式”。观察气泡和播报。",
        "expected": "先显示你的完整原话，再显示“已切换到弯举模式”，然后提示“准备好后直接说：开始 MVC 测试”。",
    },
    {
        "id": "mvc_direct_start",
        "title": "MVC 直接开始",
        "instruction": "不要再喊教练，在上一句提示后的 60 秒内直接说：“开始 MVC 测试”。",
        "expected": "立即进入 MVC；气泡显示“我：开始 MVC 测试”，随后出现测量/倒计时/完成提示。",
    },
    {
        "id": "mvc_false_trigger_guard",
        "title": "MVC 防误触发",
        "instruction": "等 MVC 窗口结束后，说一句不带“教练”的普通话，例如“开始吧”或“我准备好了”。",
        "expected": "不进入 MVC，不出现新的教练指令气泡；后台日志应显示非唤醒语句忽略。",
    },
    {
        "id": "unclear_returns_sleep",
        "title": "没听清后归位",
        "instruction": "喊“教练”后故意说一句很短或含糊的话，让系统没听清。",
        "expected": "只播一次“没听清”，随后回到等待“教练”；不会继续录下一段，也不会把教练自己的播报录成你的话。",
    },
    {
        "id": "tts_no_self_capture",
        "title": "TTS 禁止自录",
        "instruction": "触发一次固定回复或问答回复，播报期间不要说话，观察气泡和后台日志。",
        "expected": "播报内容不会再次作为 ASR 文本进入气泡；播完后需要重新喊“教练”才响应。",
    },
    {
        "id": "mute_unmute_still_ok",
        "title": "静音例外仍可用",
        "instruction": "进入静音后，不喊教练直接说：“解除静音”。",
        "expected": "直接解除静音，气泡显示你的原话和教练确认回复。",
    },
    {
        "id": "emg_no_sensor_display",
        "title": "EMG 无硬件展示",
        "instruction": "打开 Sensor Lab，观察实时波形与状态栏；如 ESP32/EMG 未工作，不需要切模式。",
        "expected": "明确显示 UDP 离线/无真实 EMG/视觉模拟数据，不把模拟波形当真实目标/代偿通道。",
    },
    {
        "id": "voice_emg_final",
        "title": "本轮复测总结",
        "instruction": "汇总 MVC 免唤醒、没听清归位、TTS 禁录和 EMG 展示是否可进入下一模块。",
        "expected": "每个失败点都有截图或备注；能判断是否继续下一模块或继续修本模块。",
    },
]


RAG_FEISHU_CLOUD_RETEST_STEPS = [
    {
        "id": "rag_module_start",
        "title": "RAG / 飞书 / OpenClaw 复测开始",
        "instruction": "刷新主网页和本调试台，确认这是新的 rag_feishu_cloud_retest run；后台预检已由 Codex 完成，你直接从下一步语音开始。",
        "expected": "调试台显示本步骤；右侧日志会捕捉 CoachKB、飞书和 OpenClaw 相关输出。",
    },
    {
        "id": "offline_api_smoke",
        "title": "后台接口预验收",
        "instruction": "这一步由 Codex 后台完成并记录；你不用运行命令，直接点通过进入现场语音。",
        "expected": "后台已验证教练能力、知识库、飞书卡片 dry-run 和 OpenClaw 云端提醒状态接口。",
    },
    {
        "id": "capability_intro",
        "title": "教练功能介绍",
        "instruction": "说：“教练，请简要介绍一下你的功能”。观察主网页气泡和播报。",
        "expected": "气泡显示你的完整原话；回复简短介绍视觉+传感纠偏、长期记忆、飞书和陪伴能力；回复内容不直接念出唤醒词，也不展开 MVC 细节。",
    },
    {
        "id": "barge_in_interrupt",
        "title": "播报打断",
        "instruction": "先触发一段较长回复；播报中直接说一次唤醒词。观察是否立即停播并进入下一轮监听。",
        "expected": "当前 TTS 立刻停止；页面按顺序出现你的打断输入；随后可以继续说下一条命令或问题。",
    },
    {
        "id": "ui_mute_volume",
        "title": "UI 静音 / 音量",
        "instruction": "播报较长回复时点击主网页右上角静音；再用音量滑条调到 5/11 两档，最后解除静音。",
        "expected": "静音一键立即止播且后台不继续偷偷播放；解除后可继续播报；音量滑条变化能反映到 TTS 音量。",
    },
    {
        "id": "manual_command_help",
        "title": "使用手册问答",
        "instruction": "说：“教练，怎么切换到弯举模式”。",
        "expected": "这句话走固定使用说明，不应直接切模式；教练说明应先喊教练再说切换命令，并提示弯举后的 MVC 入口。",
    },
    {
        "id": "manual_feishu_help",
        "title": "飞书使用说明",
        "instruction": "说：“教练，怎么推送训练总结到飞书”。",
        "expected": "教练说明推送口令和卡片内容，不应立即真发飞书；气泡顺序仍是我→教练。",
    },
    {
        "id": "rag_knee_advice",
        "title": "健身知识问答",
        "instruction": "说：“教练，膝盖不舒服怎么办”。",
        "expected": "教练回答应包含停止硬练、降低幅度或暂停观察等安全建议；气泡顺序仍是我→教练。",
    },
    {
        "id": "rag_fatigue_advice",
        "title": "疲劳知识问答",
        "instruction": "说：“教练，我现在很累还要继续吗”。",
        "expected": "教练回答应建议降低强度、保证动作质量或休息；不触发模式切换。",
    },
    {
        "id": "feishu_card_push",
        "title": "飞书卡片推送",
        "instruction": "说：“教练，推送训练总结到飞书”。观察主网页气泡、播报，以及飞书里是否收到卡片。",
        "expected": "飞书链路使用 interactive card；DeepSeek 失败时仍生成降级卡片，不再是割裂纯文本模板。",
    },
    {
        "id": "opencloud_status",
        "title": "OpenClaw 云端提醒状态",
        "instruction": "这一项由 Codex 后台只读查看；你只需要确认调试台备注里是否写了 OpenClaw 云端提醒状态。",
        "expected": "接口不返回密钥值；能看到 presentation_name=OpenClaw 后台提醒、板端运行状态、最近记录和配置布尔值。",
    },
    {
        "id": "opencloud_offline_dry_run",
        "title": "OpenClaw 提醒离线兜底 dry-run",
        "instruction": "这一项由 Codex 后台执行 dry-run；你不用操作。现场只记录它是否能生成离线提醒卡片。",
        "expected": "命令返回 ok=true、board_online=false、snapshot_source 为 default 或 cached，并生成 interactive card dry-run。",
    },
    {
        "id": "mvc_regression",
        "title": "回归：MVC 免唤醒",
        "instruction": "说“教练，切换到哑铃弯举模式”，提示后不喊教练，直接说“开始 MVC 测试”。",
        "expected": "仍能进入 MVC；RAG/飞书修改没有破坏上一轮通过项。",
    },
    {
        "id": "voice_regression",
        "title": "回归：没听清 / 禁录 / 解除静音",
        "instruction": "抽测上一轮关键项：没听清后归位、TTS 禁录、静音后直接说“解除静音”。",
        "expected": "三项仍可用；若现场时间不够，可逐项跳过并备注风险。",
    },
    {
        "id": "rag_feishu_cloud_final",
        "title": "本模块复测总结",
        "instruction": "汇总 RAG 功能、飞书卡片、OpenClaw 状态和回归项是否可以进入下一模块。",
        "expected": "每个失败点都有截图或备注；能判断是否需要继续修本模块或上板后复测。",
    },
]

RAG_VOICE_CONTROL_FIX_RETEST_STEPS = [
    {
        "id": "fix_retest_start",
        "title": "RAG/语音控制修复复测开始",
        "instruction": "刷新主网页和本调试台，确认这是新的 rag_voice_control_fix_retest run；不要复用 20260502-225052 的截图和备注。",
        "expected": "调试台显示新 run；右侧能看到 voice、/dev/shm 和主循环日志。",
    },
    {
        "id": "wake_full_window",
        "title": "唤醒后完整收音窗口",
        "instruction": "说：“教练，请简要介绍一下你的功能”。观察是否只听到“教练”就立刻没听清。",
        "expected": "气泡显示完整原话；不会只显示唤醒词；不会立刻播没听清。",
    },
    {
        "id": "wake_only_no_unclear",
        "title": "只喊唤醒词后安静回待机",
        "instruction": "只说一次“教练”，然后保持安静 8 到 12 秒。",
        "expected": "系统播“嗯”后进入监听窗口；没有后续语音时安静回待机，不立刻播“没听清”。",
    },
    {
        "id": "fixed_intro_variants",
        "title": "固定介绍同义入口",
        "instruction": "分别说：“介绍一下自己”“你能做什么”“怎么用”“介绍你的功能”“你有什么功能”。每句测完再进入下一句。",
        "expected": "三句都走同一类固定自然回复；不出现“拍摄”“演示”；不生硬连续堆“我可以”。",
    },
    {
        "id": "barge_in_immediate",
        "title": "播报中唤醒打断",
        "instruction": "触发一段较长回复，播报中直接说一次“教练”，然后继续说下一条短问题。",
        "expected": "当前 TTS 立刻停止；页面按顺序出现打断输入；随后进入下一轮监听并能接下一句。",
    },
    {
        "id": "ui_mute_unmute_next_voice",
        "title": "UI 静音 / 解除后恢复播报",
        "instruction": "播报较长回复时点主网页静音；确认停播后解除静音，再触发一条测试播报。",
        "expected": "静音一键止播；解除后恢复或补播刚才那段，若没有可补播内容，则下一条播报正常有声；后台不残留 muted=true。",
    },
    {
        "id": "tts_volume_next_clip",
        "title": "音量下一条 TTS 生效",
        "instruction": "把音量调到 5，触发测试播报；再调到 11，触发下一条测试播报。",
        "expected": "至少下一条 TTS 的响度有变化；若实时播放中变化不明显，备注 mixer 诊断结果。",
    },
    {
        "id": "core_regression",
        "title": "关键回归",
        "instruction": "抽测使用手册问答、健身知识问答、飞书卡片、OpenClaw 状态和 MVC 免唤醒。",
        "expected": "RAG、飞书、OpenClaw 和 MVC 入口仍可用；不测试“请关机”。",
    },
    {
        "id": "fix_retest_final",
        "title": "本轮修复复测总结",
        "instruction": "汇总失败点是否已经满足拍摄需要；失败项必须上传截图或备注现象。",
        "expected": "能判断语音展示闭环是否可以进入拍摄主线。",
    },
]


RECORDING_REHEARSAL_STEPS = [
    {
        "id": "startup_prompt",
        "title": "上线提示音",
        "instruction": "重启必要服务后等待主网页在线，确认只播一次“IronBuddy 已上线，随时准备指导”。同时观察 /api/admin/voice_diag 的 voice_boot_status。",
        "expected": "上线提示只出现一次；voice_boot_status 显示 queued/done 类状态；不会反复播或沉默无日志。",
    },
    {
        "id": "two_step_wake",
        "title": "两种唤醒方式",
        "instruction": "先测“教练，现在适合做深蹲吗”；再测只说“教练”，听到“嗯”后说“介绍一下功能”。",
        "expected": "同句唤醒和两段式唤醒都能完整进入气泡；只喊唤醒词后有合理收音窗口，不立刻没听清。",
    },
    {
        "id": "combo_voice_command",
        "title": "组合语音命令",
        "instruction": "说：“教练，调整疲劳度上限到1300，并进行下一组训练”。",
        "expected": "疲劳上限变成 1300，同时发起下一组训练请求；只播一次合并确认。",
    },
    {
        "id": "exercise_mode_roundtrip",
        "title": "深蹲/弯举来回切换",
        "instruction": "用主网页设置和语音分别切换深蹲、哑铃弯举，再切回深蹲；观察是否被旧 Sensor Lab 状态拉回。",
        "expected": "顶部模式条、计数标签、FSM exercise 一致；旧 user_profile/sensor_lab 残留不会持续覆盖主训练动作。",
    },
    {
        "id": "fusion_mode_toggle",
        "title": "纯视觉 / 视觉+传感",
        "instruction": "在设置页点击“纯视觉”和“视觉+传感”，再用语音各切一次。",
        "expected": "顶部模式条和 /api/inference_mode 一致；不会出现横跳；传感未接入时也明确显示状态。",
    },
    {
        "id": "vision_backend_switch",
        "title": "云端/本地视觉热切换",
        "instruction": "在设置页从本地 NPU 切到云端 RTMPose，再切回本地；同时观察 GPU 状态和视频是否恢复。",
        "expected": "切换过程中页面可见状态反馈；失败时有明确提示，不让画面永久卡住。",
    },
    {
        "id": "angle_diag",
        "title": "角度诊断",
        "instruction": "做一个标准深蹲和一个不标准深蹲，观察主画面下方 raw/smooth/decision/KPT/FPS/backend 诊断。",
        "expected": "诊断字段随动作更新，可解释角度判定；如果角度明显不合理，截图记录 raw 与 smooth 差异。",
    },
    {
        "id": "fixed_fatigue_summary",
        "title": "固定疲劳总结",
        "instruction": "把疲劳上限调低触发一次总结，分别在纯视觉和视觉+传感模式抽测。",
        "expected": "总结模板固定，动作名正确；纯视觉包含标准/不标准数量和不标准程度；视觉+传感额外包含代偿次数。",
    },
    {
        "id": "rag_openclaw_showcase",
        "title": "RAG / OpenClaw 展示",
        "instruction": "打开主网页“数据”页查看知识库状态，再打开“设置”页的 OpenClaw 后台推送卡片；随后用语音问“怎么使用你”和“膝盖不舒服怎么办”。",
        "expected": "知识库只展示真实 RAG 命中、来源和上下文摘要；OpenClaw 只展示后台提醒真实状态和真实记录，没有历史就显示暂无真实记录。",
    },
    {
        "id": "debug_workbench_code_graph",
        "title": "后台调试与代码结构图",
        "instruction": "点击主网页“调试”页打开后台调试台；上传一张截图或写备注；再打开数据页代码结构图。",
        "expected": "operator console 能保存步骤、备注、截图到 run 目录；代码结构图只读展示 UI/API/voice/FSM/RAG/DB/cloud 关系。",
    },
    {
        "id": "recording_final",
        "title": "录制可用性结论",
        "instruction": "按剧本顺序复盘本轮：开场、语音、动作切换、视觉切换、疲劳总结、RAG/OpenClaw、后台调试。",
        "expected": "每个失败点都有截图或备注；能判断是否进入正式录制或需要下一轮小修。",
    },
]


ONE_SHOT_ACCEPTANCE_STEPS = [
    {
        "id": "acceptance_start",
        "title": "一次性验收开始",
        "instruction": "刷新主网页、本调试台和飞书；确认板端地址是当前验收板，后续每项只记录通过/失败/跳过和必要截图。",
        "expected": "调试台是新的 one_shot_acceptance run；主网页可打开；飞书接收端已准备查看本次推送。",
    },
    {
        "id": "one_click_start_smoothness",
        "title": "一键启动流畅度",
        "instruction": "从主网页执行一次一键启动；观察启动提示、服务状态、视频恢复和语音上线是否连贯。",
        "expected": "一键启动不需要多次点击；页面状态能连续更新；视频和语音在合理时间内恢复，失败时有明确提示。",
    },
    {
        "id": "curl_three_sets",
        "title": "弯举 3 组验收",
        "instruction": "切到哑铃弯举，按拍摄节奏完成 3 组；每组结束后记录 good/failed/comp 和是否有卡顿或错判。",
        "expected": "3 组都能完成记录；计数随动作增长；组间切换不把模式拉回深蹲，不丢失本轮统计。",
    },
    {
        "id": "plan_progress",
        "title": "计划进度",
        "instruction": "查看主网页计划/进度区域；完成一组后确认进度、组数和本次训练状态是否同步刷新。",
        "expected": "计划进度能体现当前弯举 3 组流程；不会显示旧 run 或静态占位数据。",
    },
    {
        "id": "rag_auto_feishu",
        "title": "RAG 自动飞书",
        "instruction": "用语音问一次训练相关问题，再触发或等待自动飞书逻辑；观察气泡、RAG 命中、飞书卡片是否串成同一轮。",
        "expected": "问答来自真实 RAG/固定手册链路；自动飞书使用 interactive card；失败时有降级说明而不是静默无响应。",
    },
    {
        "id": "openclaw_runtime",
        "title": "OpenClaw 后台提醒",
        "instruction": "后台只读检查 OpenClaw 状态；现场确认调试台备注写入 runtime、board_online、最近推送或 dry-run 结果。",
        "expected": "状态能体现 local/offboard runtime；不返回密钥值；可看到当前板端 URL、最近记录和缓存来源。",
    },
    {
        "id": "feishu_session_report",
        "title": "本次飞书报告",
        "instruction": "触发本次训练报告推送到飞书；核对标题、动作、3 组数据、RAG/建议和 OpenClaw 状态是否属于本轮。",
        "expected": "飞书报告是本次验收 run 的数据；不是旧历史卡片；内容包含弯举 3 组、计划进度和教练建议。",
    },
    {
        "id": "acceptance_final",
        "title": "一次性验收结论",
        "instruction": "汇总一键启动、弯举 3 组、计划进度、RAG 自动飞书、OpenClaw 和本次飞书报告是否可进入展示。",
        "expected": "每个失败点有截图或备注；能直接判断是否进入正式录制或只剩小修。",
    },
]


VIDEO_ACCEPTANCE_REHEARSAL_STEPS = [
    {
        "id": "environment_ready",
        "title": "环境恢复",
        "instruction": "确认主网页 http://10.29.10.224:5000/ 可打开；如打不开，在 WSL 执行 IRONBUDDY_BOARD_IP=10.29.10.224 bash scripts/recover_streamer.sh。",
        "expected": "主网页在线；本页右侧 FSM 有数据；语音诊断显示单个 voice 进程。",
    },
    {
        "id": "ui_baseline",
        "title": "主界面基线",
        "instruction": "打开数据页，确认只看到今日方案、本次训练、训练趋势、知识库与报告四块；后台数据库按钮可打开。",
        "expected": "界面简洁；没有大段说明、英文实现词或拍摄 session 文案；后台数据库默认只看真实数据。",
    },
    {
        "id": "generate_daily_plan",
        "title": "生成今日方案",
        "instruction": "点击“生成今日方案”，等待四段状态走到方案；读出计划摘要和组数目标。",
        "expected": "阶段显示为历史、思考、方案、采纳；方案来自真实历史数据；没有跨行或杂乱说明。",
    },
    {
        "id": "accept_training_plan",
        "title": "采纳并开始",
        "instruction": "点击“采纳并开始”，确认训练进入第 1 组，左侧计数和本次训练卡片从新基线开始。",
        "expected": "训练状态清零；动作模式为深蹲；本次训练卡显示第 1 组和目标次数。",
    },
    {
        "id": "squat_set_one",
        "title": "深蹲第 1 组",
        "instruction": "做 2 到 3 个标准深蹲，再做 1 个明显不标准深蹲；全身入镜，动作慢一点。",
        "expected": "计数增长；标准/不标准能区分；本次训练卡同步更新。",
    },
    {
        "id": "next_set_sync",
        "title": "切到第 2 组",
        "instruction": "点击“下一组”，确认第 1 组保留记录，第 2 组开始，左侧本组计数从 0 重新计。",
        "expected": "本次累计不丢；当前组切换清楚；飞书报告预览仍使用本轮训练数据。",
    },
    {
        "id": "voice_intro_mute",
        "title": "语音展示",
        "instruction": "说“教练，介绍一下你自己”；播报中尝试静音按钮或“教练，静音”。失败就截图记录，不反复折腾。",
        "expected": "播报自然；静音能止播；voice 仍保持单进程。",
    },
    {
        "id": "rag_feishu",
        "title": "知识库与飞书",
        "instruction": "问“膝盖酸痛怎么办”，观察知识库命中和飞书状态；训练报告仍可单独用“发送本次报告”。",
        "expected": "专业问题触发知识库后自动推送飞书详报；非专业问题不误触发。",
    },
    {
        "id": "database_evidence",
        "title": "数据库证据",
        "instruction": "打开后台数据库，查看训练、对话、系统页的最后写入时间；确认默认筛选是真实数据。",
        "expected": "训练记录是 5 月真数据；伪造/种子记录不在默认视图；新问答能形成最新记录。",
    },
    {
        "id": "debug_console",
        "title": "调试台状态",
        "instruction": "回到主网页调试页，点击“打开调试台”，确认可以跳到 http://127.0.0.1:8765/；记录路径可在弹窗里查看。",
        "expected": "主网页显示 8765 在线状态；按钮跳转可用；本页继续保存步骤和截图。",
    },
    {
        "id": "shooting_final",
        "title": "拍摄结论",
        "instruction": "标记每一步通过/失败；失败项上传截图或写备注；最后判断是否进入正式录制。",
        "expected": "得到一份可复盘 run 记录；范围只覆盖主视频，不扩到 B 线路。",
    },
]


LANE_C_RECORDING_REHEARSAL_STEPS = [
    {
        "id": "lane_c_environment_ready",
        "title": "环境恢复",
        "instruction": "确认主网页 http://10.29.10.224:5000/ 可打开；如果打不开，只记录现象，不临时改后台。",
        "expected": "主网页可访问；本调试台是 lane_c_recording_rehearsal run；右上状态条能显示板端和语音状态。",
    },
    {
        "id": "lane_c_main_ui_baseline",
        "title": "主网页基线",
        "instruction": "打开主网页，检查顶部状态、视频区、动作计数、疲劳条、控制台/数据/调试/设置四个 tab。",
        "expected": "入口清楚；按钮文字不溢出；tab 顺序是控制台、数据、调试、设置；不出现旧拍摄 session 文案。",
    },
    {
        "id": "lane_c_vision_frame",
        "title": "视觉画面",
        "instruction": "观察主网页视频画面；必要时分别打开 /video_feed 和 8080/stream；让人入镜 5 到 10 秒。",
        "expected": "画面能持续刷新；人入镜后不黑屏不卡死；只记录流畅度、清晰度和明显延迟，不判断 EMG 或 GRU。",
    },
    {
        "id": "lane_c_wake_listen",
        "title": "语音唤醒",
        "instruction": "先只说“教练”并保持安静，再说“教练，介绍一下你自己”。",
        "expected": "只喊唤醒词后不立刻反复“没听清”；完整问题能进入气泡或形成可理解回复。",
    },
    {
        "id": "lane_c_voice_playback",
        "title": "语音播报 / 静音 / 打断",
        "instruction": "触发一段介绍播报；播报中点主网页静音，再解除静音；播报中再说一次“教练”测试打断。",
        "expected": "播报可听懂；静音能止播；解除后下一条播报正常；打断不造成长时间卡死。本轮不改 voice_daemon。",
    },
    {
        "id": "lane_c_short_training_flow",
        "title": "短训练流程",
        "instruction": "在主网页启动深蹲 3 组或采纳今日方案；做 1 到 2 个标准动作，再点下一组。",
        "expected": "当前组、累计次数、训练卡片和计数区同步；失败只记录前台现象，不扩展到训练计划算法。",
    },
    {
        "id": "lane_c_recording_evidence",
        "title": "8765 记录",
        "instruction": "为本轮至少一个通过项和一个问题项写备注；如有截图，粘贴或上传到当前步骤。",
        "expected": "通过/失败/重试/跳过、备注、上传证据、run 目录、summary 都可用；工程诊断默认折叠但可以展开。",
    },
    {
        "id": "lane_c_final_call",
        "title": "现场结论",
        "instruction": "汇总主网页、视觉、语音、短训练和 8765 记录是否可以进入正式录制；把 RAG/飞书转 Lane A，把 EMG/GRU 转 Lane B。",
        "expected": "得到明确结论：可以录制、需要 Lane C 小修、或需转交 Lane A/B；不把后台专业回答或传感问题算作 Lane C 失败。",
    },
]


VECTOR_RAG_FATIGUE_ACCEPTANCE_STEPS = [
    {
        "id": "vector_acceptance_start",
        "title": "本轮验收开始",
        "instruction": "确认主网页、本调试台和板端都能打开；本轮只验收向量 RAG、在线证据、训练计划 fatigue target 和 fatigue model 状态。",
        "expected": "调试台是新的 vector_rag_fatigue_acceptance run；主网页可访问；本轮不改语音、OpenClaw、Sensor Lab 或 Route B。",
    },
    {
        "id": "cloud_rag_health",
        "title": "云端 RAG 健康检查",
        "instruction": "打开或后台检查 /api/demo/rag_status，观察 vector_status 和 embedding_status。",
        "expected": "能看到 Qdrant/vector store 与 embedding 服务状态；配置只显示状态，不显示密码、token 或 API key。",
    },
    {
        "id": "professional_rag_query",
        "title": "专业问题检索",
        "instruction": "在页面或接口问：“膝盖酸痛怎么办”或“肌电疲劳怎么判断”。",
        "expected": "命中来自向量 RAG 或外部学术来源的 evidence；专业问答命中后自动推送飞书详报；如果云端不可用，要显示 vector_unavailable。",
    },
    {
        "id": "daily_plan_evidence",
        "title": "今日方案证据",
        "instruction": "点击“生成今日方案”，等待方案生成；观察 evidence_ids、source_mode 和方案阶段。",
        "expected": "DeepSeek 方案只能引用真实返回的 evidence_ids；失败时回到 rule_fallback，并写清原因。",
    },
    {
        "id": "accept_target_fatigue",
        "title": "采纳疲劳目标",
        "instruction": "点击“采纳并开始”，观察第 1 组目标疲劳值和 /api/training_plan 状态。",
        "expected": "第 1 组写入 target_fatigue；/dev/shm/fatigue_limit.json 与 UI 镜像使用同一个目标值。",
    },
    {
        "id": "next_set_fatigue",
        "title": "下一组目标切换",
        "instruction": "点击“下一组”，确认当前组变化和目标疲劳值切换。",
        "expected": "目标从第 1 组切到第 2 组；训练合同仍是 target_fatigue，不回退成只按次数推进。",
    },
    {
        "id": "fatigue_model_status",
        "title": "疲劳模型状态",
        "instruction": "做 1 到 2 个动作，看主网页疲劳条下面的“疲劳模型”一句话说明。",
        "expected": "页面直接写清是“可解释公式”还是“旧固定积分”，并用人话说明本次增加多少、EMG 是否参与；不用看后台 JSON。",
    },
    {
        "id": "readable_doc_check",
        "title": "说明文件核对",
        "instruction": "打开 docs/test_runs/ironbuddy_operator/USER_README_20260511_VECTOR_RAG_FATIGUE_ACCEPTANCE.md，确认它能解释本轮改了什么、怎么验收。",
        "expected": "文件短、清楚、可读；不包含真实密码、token、API key 或云端凭据值。",
    },
    {
        "id": "vector_acceptance_final",
        "title": "本轮验收结论",
        "instruction": "标记每一步通过/失败/跳过；失败项上传截图或写备注，最后判断是否需要继续修 RAG、训练计划或 fatigue model。",
        "expected": "得到一份可复盘 run 记录；结论只覆盖本轮向量 RAG 和疲劳模型，不扩展到无关模块。",
    },
]


LANE_D_VOICE_TUNING_STEPS = [
    {
        "id": "lane_d_start",
        "title": "Lane D 基线确认",
        "instruction": "打开主网页 http://10.29.10.224:5000/ 和本调试台，确认这是 lane_d_voice_tuning run；右侧语音状态显示 running。",
        "expected": "板端可访问；voice_daemon 在线；本轮只测语音，不改 RAG、飞书、训练计划、EMG、GRU 或视觉逻辑。",
    },
    {
        "id": "lane_d_main_controls",
        "title": "主网页语音入口",
        "instruction": "在主网页设置或调试区域找到“手动语音录入”和“语音实时录入”；如果页面没显示，刷新一次再记录。",
        "expected": "页面包含开始录入、停止录入、默认折叠的实时识别面板，并能访问 /api/voice_manual/start、/api/voice_manual/stop、/api/voice_debug。",
    },
    {
        "id": "lane_d_wake_empty",
        "title": "只喊唤醒词",
        "instruction": "只说“教练”，然后保持安静；不要接第二句话。观察是否安静回待机。",
        "expected": "不会立刻反复播“没听清”；如果有一次没听清，也应很快回到可再次唤醒状态。",
    },
    {
        "id": "lane_d_next_turn_gap",
        "title": "连续两轮指令",
        "instruction": "说“教练，介绍一下你自己”；播报结束后马上再喊“教练”。用备注记录大约等待几秒、是否有正在播报或后台日志。",
        "expected": "如果等待发生在 TTS/LLM 工作中，记录为正常互斥；如果没有播报、没有请求、没有录音却不能唤醒，标记失败并截图/备注。",
    },
    {
        "id": "lane_d_mvc_voice_removed",
        "title": "MVC 语音触发移除",
        "instruction": "说“教练，切换到哑铃弯举模式”；随后不喊教练，直接说“开始 MVC 测试”。",
        "expected": "切换弯举后不再提示语音 MVC；直接说“开始 MVC 测试”不应触发 MVC。UI 手动 MVC 入口仍可保留。",
    },
    {
        "id": "lane_d_manual_fallback",
        "title": "手动语音录入",
        "instruction": "在右侧 Lane D 语音测试面板点“开始手动录入”，靠近板端麦克风说一句短指令或问题，再点“停止并识别”。观察状态是否经过 recording、stop_requested、recognizing。",
        "expected": "手动录入经过 /api/voice_manual/start 和 /api/voice_manual/stop；录的是板端麦克风，不是电脑麦克风；有效语音应进入现有 ASR/路由，听不清时应是 asr_empty，不应再因为 VAD 没触发直接 SILENCE。",
    },
    {
        "id": "lane_d_realtime_debug",
        "title": "语音实时录入",
        "instruction": "展开右侧 Lane D 面板或主网页的语音实时录入面板，观察最近识别文本、草稿、turn 阶段、能量和阈值。",
        "expected": "能看到 /api/voice_debug、/api/chat_draft、/api/voice_turn 和手动录入状态；面板默认可折叠，展开后用于现场定位。",
    },
    {
        "id": "lane_d_feedback",
        "title": "现场反馈结论",
        "instruction": "为本轮至少记录一个通过项和一个问题项；如果语音完全正常，也写明连续唤醒、手动录入、debug 面板、MVC 移除都通过。",
        "expected": "得到一份可复盘 run 记录；失败项能定位到唤醒空窗、手动录入、debug 面板或 MVC 回归中的哪一类。",
    },
]


STEP_SETS = {
    "main": MAIN_STEPS,
    "bubble_retest": BUBBLE_RETEST_STEPS,
    "voice_emg_retest": VOICE_EMG_RETEST_STEPS,
    "rag_feishu_cloud_retest": RAG_FEISHU_CLOUD_RETEST_STEPS,
    "rag_voice_control_fix_retest": RAG_VOICE_CONTROL_FIX_RETEST_STEPS,
    "recording_rehearsal": RECORDING_REHEARSAL_STEPS,
    "one_shot_acceptance": ONE_SHOT_ACCEPTANCE_STEPS,
    "video_acceptance_rehearsal": VIDEO_ACCEPTANCE_REHEARSAL_STEPS,
    "lane_c_recording_rehearsal": LANE_C_RECORDING_REHEARSAL_STEPS,
    "vector_rag_fatigue_acceptance": VECTOR_RAG_FATIGUE_ACCEPTANCE_STEPS,
    "lane_d_voice_tuning": LANE_D_VOICE_TUNING_STEPS,
}


def iso_now():
    return datetime.now().isoformat(timespec="seconds")


def clock_now():
    return datetime.now().strftime("%H:%M:%S")


def slugify(value):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return value.strip("-") or "file"


def safe_read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


class OperatorSession:
    def __init__(self, args):
        self.args = args
        self.board_url = "http://%s:5000" % args.board_ip
        self.ssh_target = "%s@%s" % (args.ssh_user, args.board_ip)
        self.no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.lock = threading.Lock()
        self.logs = deque(maxlen=320)
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_dir = Path(args.runs_dir).resolve() / self.run_id
        self.upload_dir = self.run_dir / "uploads"
        self.snapshot_dir = self.run_dir / "snapshots"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.state_path = self.run_dir / "state.json"
        self.summary_path = self.run_dir / "summary.md"
        self.state = {
            "run_id": self.run_id,
            "scenario": args.scenario,
            "run_dir": str(self.run_dir),
            "step_index": 0,
            "results": [],
            "uploads": [],
            "started_at": iso_now(),
            "board": {},
            "voice_diag": {},
            "shm": "",
            "processes": "",
            "poll_error": None,
            "last_poll": None,
            "log_status": "starting",
            "monitor_paused": False,
            "operator_note": "",
        }
        self.steps = STEP_SETS[args.scenario]
        self._record_event("session_start", {"args": vars(args), "steps": self.steps})

    def ssh_args(self, remote_cmd):
        return [
            "ssh",
            "-i",
            os.path.expanduser(self.args.ssh_key),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=6",
            self.ssh_target,
            remote_cmd,
        ]

    def run_cmd(self, args, timeout=5):
        try:
            out = subprocess.check_output(args, stderr=subprocess.STDOUT, timeout=timeout)
            return out.decode("utf-8", "replace")
        except subprocess.CalledProcessError as exc:
            return exc.output.decode("utf-8", "replace")
        except Exception as exc:
            return "%s: %s" % (type(exc).__name__, exc)

    def fetch_json(self, path, timeout=4):
        with self.no_proxy.open(self.board_url + path, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def call_board_json(self, path, method="GET", payload=None, timeout=4):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif method.upper() != "GET":
            data = b""
        req = urllib.request.Request(
            self.board_url + path,
            data=data,
            headers=headers,
            method=method.upper(),
        )
        with self.no_proxy.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        return json.loads(body) if body.strip() else {}

    def _record_event(self, kind, payload):
        event = {"time": iso_now(), "kind": kind, "payload": payload}
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._write_state()
        self._write_summary()
        return event

    def _write_state(self):
        with self.lock:
            data = dict(self.state)
            data["logs"] = list(self.logs)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def _write_summary(self):
        with self.lock:
            data = dict(self.state)
        lines = [
            "# IronBuddy operator run %s" % self.run_id,
            "",
            "- Started: `%s`" % data.get("started_at"),
            "- Scenario: `%s`" % data.get("scenario"),
            "- Board: `%s`" % self.args.board_ip,
            "- Run dir: `%s`" % self.run_dir,
            "",
            "## Step results",
            "",
        ]
        if data.get("results"):
            for item in data["results"]:
                lines.append(
                    "- `%s` **%s**: %s. %s"
                    % (
                        item["time"],
                        item["step_title"],
                        item["action"],
                        item.get("note") or "",
                    )
                )
        else:
            lines.append("- No step result has been recorded yet.")
        lines.extend(["", "## Uploads", ""])
        if data.get("uploads"):
            for item in data["uploads"]:
                lines.append(
                    "- `%s` %s: `%s`. %s"
                    % (
                        item["time"],
                        item.get("step_title", "unknown"),
                        item["path"],
                        item.get("note") or "",
                    )
                )
        else:
            lines.append("- No image or file upload has been recorded yet.")
        lines.extend(["", "## Latest board snapshot", "", "```json"])
        lines.append(json.dumps(data.get("board") or {}, ensure_ascii=False, indent=2))
        lines.extend(["```", "", "## Latest voice diagnostic", "", "```json"])
        lines.append(json.dumps(data.get("voice_diag") or {}, ensure_ascii=False, indent=2))
        lines.extend(["```", ""])
        self.summary_path.write_text("\n".join(lines), encoding="utf-8")

    def current_step(self):
        idx = min(self.state["step_index"], len(self.steps) - 1)
        return self.steps[idx]

    def snapshot(self):
        with self.lock:
            data = dict(self.state)
            data["logs"] = list(self.logs)
        data["steps"] = self.steps
        data["current_step"] = self.steps[min(data["step_index"], len(self.steps) - 1)]
        return data

    def poll_once(self):
        poll = {"poll_error": None}
        try:
            poll["board"] = self.fetch_json("/api/fsm_state")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            poll["poll_error"] = "fsm_state: %s: %s" % (type(exc).__name__, exc)
        try:
            poll["voice_diag"] = self.fetch_json("/api/admin/voice_diag")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            prev = poll.get("poll_error")
            poll["poll_error"] = ((prev + " | ") if prev else "") + "voice_diag: %s: %s" % (
                type(exc).__name__,
                exc,
            )
        remote = (
            "pgrep -af '[s]treamer_app|[m]ain_claw_loop|[v]oice_daemon|"
            "[u]dp_emg_server|[c]loud_rtmpose_client'; "
            "echo ---shm---; "
            "for p in /dev/shm/fsm_state.json /dev/shm/voice_turn.json "
            "/dev/shm/chat_input.txt /dev/shm/chat_reply.txt /dev/shm/inference_mode.json "
            "/dev/shm/user_profile.json; do echo \"--- $p\"; cat \"$p\" 2>/dev/null || true; done"
        )
        remote_out = self.run_cmd(self.ssh_args(remote), timeout=8)
        parts = remote_out.split("---shm---", 1)
        poll["processes"] = parts[0].strip()
        poll["shm"] = parts[1].strip() if len(parts) == 2 else remote_out.strip()
        poll["last_poll"] = iso_now()
        with self.lock:
            self.state.update(poll)
        self._write_state()

    def poll_loop(self):
        while True:
            with self.lock:
                paused = self.state.get("monitor_paused")
            if not paused:
                self.poll_once()
            time.sleep(self.args.poll_interval)

    def log_tail_loop(self):
        cmd = (
            "cd %s && tail -n 0 -F /tmp/voice.log /tmp/mainloop.log 2>/dev/null | "
            "grep --line-buffered -E '%s'"
        ) % (self.args.remote_dir, LOG_PATTERN)
        while True:
            with self.lock:
                paused = self.state.get("monitor_paused")
                self.state["log_status"] = "paused" if paused else "connecting"
            if paused:
                time.sleep(1)
                continue
            proc = subprocess.Popen(
                self.ssh_args(cmd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            with self.lock:
                self.state["log_status"] = "running"
            try:
                for line in proc.stdout:
                    with self.lock:
                        paused = self.state.get("monitor_paused")
                    if paused:
                        break
                    line = line.rstrip("\n")
                    if line:
                        self.logs.append("%s  %s" % (clock_now(), line))
                        self._write_state()
            finally:
                with self.lock:
                    self.state["log_status"] = "restarting"
                try:
                    proc.kill()
                except Exception:
                    pass
                time.sleep(1)

    def record_action(self, action, note):
        if action not in ("通过", "失败", "重试", "跳过"):
            raise ValueError("bad action")
        with self.lock:
            idx = self.state["step_index"]
            step = self.steps[min(idx, len(self.steps) - 1)]
            item = {
                "time": clock_now(),
                "iso_time": iso_now(),
                "step_id": step["id"],
                "step_title": step["title"],
                "action": action,
                "note": note,
                "board": self.state.get("board"),
                "voice_diag": self.state.get("voice_diag"),
                "shm": self.state.get("shm"),
                "recent_logs": list(self.logs)[-40:],
            }
            self.state["results"].append(item)
            if action in ("通过", "失败", "跳过") and idx < len(self.steps) - 1:
                self.state["step_index"] = idx + 1
        self._record_event("step_action", item)
        return item

    def record_upload(self, file_storage, note):
        step = self.current_step()
        original = slugify(file_storage.filename or "upload")
        ext = Path(original).suffix.lower()[:10]
        if not ext:
            ext = ".bin"
        name = "%s-%s-%s%s" % (
            datetime.now().strftime("%H%M%S"),
            step["id"],
            len(self.state.get("uploads", [])) + 1,
            ext,
        )
        path = self.upload_dir / name
        file_storage.save(path)
        item = {
            "time": clock_now(),
            "iso_time": iso_now(),
            "step_id": step["id"],
            "step_title": step["title"],
            "filename": original,
            "path": str(path.relative_to(self.run_dir)),
            "size": path.stat().st_size,
            "note": note,
            "url": "/uploads/%s" % name,
        }
        with self.lock:
            self.state["uploads"].append(item)
        self._record_event("upload", item)
        return item

    def toggle_monitor(self, paused):
        with self.lock:
            self.state["monitor_paused"] = bool(paused)
        self._record_event("monitor_toggle", {"paused": bool(paused)})


def build_app(session):
    app = Flask(__name__)

    def board_proxy(path, method="GET", payload=None):
        try:
            data = session.call_board_json(path, method=method, payload=payload)
            return jsonify({"ok": True, "board": data})
        except Exception as exc:
            return jsonify({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}), 502

    @app.route("/")
    def index():
        return INDEX_HTML

    @app.route("/api/status")
    def api_status():
        return jsonify(session.snapshot())

    @app.route("/api/action", methods=["POST"])
    def api_action():
        payload = request.get_json(force=True, silent=True) or {}
        try:
            item = session.record_action(payload.get("action", ""), payload.get("note", ""))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "item": item, "state": session.snapshot()})

    @app.route("/api/upload", methods=["POST"])
    def api_upload():
        if "file" not in request.files:
            return jsonify({"ok": False, "error": "missing file"}), 400
        item = session.record_upload(request.files["file"], request.form.get("note", ""))
        return jsonify({"ok": True, "item": item, "state": session.snapshot()})

    @app.route("/api/monitor", methods=["POST"])
    def api_monitor():
        payload = request.get_json(force=True, silent=True) or {}
        session.toggle_monitor(bool(payload.get("paused")))
        return jsonify({"ok": True, "state": session.snapshot()})

    @app.route("/api/lane_d/voice_manual/start", methods=["POST"])
    def api_lane_d_voice_manual_start():
        return board_proxy("/api/voice_manual/start", method="POST")

    @app.route("/api/lane_d/voice_manual/stop", methods=["POST"])
    def api_lane_d_voice_manual_stop():
        return board_proxy("/api/voice_manual/stop", method="POST")

    @app.route("/api/lane_d/voice_manual/status")
    def api_lane_d_voice_manual_status():
        return board_proxy("/api/voice_manual/status")

    @app.route("/api/lane_d/voice_debug")
    def api_lane_d_voice_debug():
        data = {}
        errors = {}
        for key, path in (
            ("voice_debug", "/api/voice_debug"),
            ("chat_draft", "/api/chat_draft"),
            ("voice_turn", "/api/voice_turn"),
            ("manual_status", "/api/voice_manual/status"),
        ):
            try:
                data[key] = session.fetch_json(path, timeout=3)
            except Exception as exc:
                errors[key] = "%s: %s" % (type(exc).__name__, exc)
        return jsonify({"ok": not errors, "data": data, "errors": errors})

    @app.route("/api/report")
    def api_report():
        return jsonify(
            {
                "run_id": session.run_id,
                "run_dir": str(session.run_dir),
                "summary_path": str(session.summary_path),
                "events_path": str(session.events_path),
                "summary": safe_read_text(session.summary_path),
            }
        )

    @app.route("/uploads/<path:name>")
    def uploads(name):
        return send_from_directory(session.upload_dir, name)

    return app


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>IronBuddy 调试台</title>
  <style>
    :root {
      --bg: #0a0e17;
      --panel: rgba(12, 18, 31, 0.82);
      --panel-2: rgba(17, 24, 39, 0.96);
      --panel-soft: rgba(94, 200, 255, 0.07);
      --text: #e8ecf1;
      --muted: #8b949e;
      --line: rgba(255, 255, 255, 0.06);
      --line-strong: rgba(148, 163, 184, 0.2);
      --green: #4ade80;
      --red: #ef4444;
      --blue: #5ec8ff;
      --yellow: #facc15;
      --purple: #a78bfa;
      --accent: #5ec8ff;
      --radius: 8px;
      --shadow: 0 18px 48px rgba(0, 0, 0, 0.32);
    }
    body, html { font-feature-settings: "cv11", "ss01"; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      background-image:
        radial-gradient(ellipse at 18% 10%, rgba(94, 200, 255, 0.09), transparent 42%),
        radial-gradient(ellipse at 80% 0%, rgba(167, 139, 250, 0.08), transparent 38%);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
      letter-spacing: 0;
    }
    header {
      min-height: 58px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(7, 11, 16, 0.88);
      backdrop-filter: blur(14px);
      position: sticky;
      top: 0;
      z-index: 5;
    }
    .brand { display: flex; gap: 12px; align-items: baseline; min-width: 250px; }
    .brand h1 { margin: 0; font-size: 19px; font-weight: 760; }
    .brand span { color: var(--muted); font-size: 13px; }
    .statusbar { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    .pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 6px 10px;
      color: #cbd5e1;
      background: var(--panel);
      font-size: 12px;
      white-space: nowrap;
    }
    main {
      display: grid;
      grid-template-columns: minmax(420px, 0.92fr) minmax(500px, 1.08fr);
      gap: 16px;
      padding: 16px;
      min-height: calc(100vh - 58px);
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      overflow: hidden;
      min-width: 0;
      box-shadow: var(--shadow);
    }
    .section-head {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      background: var(--panel-2);
    }
    .section-head h2 { margin: 0; font-size: 15px; }
    .body { padding: 14px; }
    .step-title { font-size: 29px; font-weight: 780; margin: 4px 0 10px; letter-spacing: 0; }
    .step-meta { color: var(--muted); font-size: 13px; }
    .instruction {
      margin: 14px 0;
      padding: 14px;
      border: 1px solid rgba(94, 200, 255, 0.18);
      border-left: 4px solid var(--blue);
      border-radius: 8px;
      background: var(--panel-soft);
      font-size: 18px;
      line-height: 1.65;
    }
    .expected { margin: 12px 0 18px; color: #c9d1d9; line-height: 1.55; }
    .buttons { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
    button {
      min-height: 50px;
      border-radius: 7px;
      border: 1px solid transparent;
      color: white;
      font-size: 17px;
      font-weight: 720;
      cursor: pointer;
      box-shadow: 0 8px 22px rgba(0, 0, 0, 0.18);
    }
    button:active { transform: translateY(1px); }
    .pass { background: var(--green); }
    .fail { background: var(--red); }
    .retry { background: var(--yellow); color: #111; }
    .skip { background: #57606a; }
    .secondary {
      background: transparent;
      border-color: var(--line);
      color: var(--text);
      min-height: 36px;
      font-size: 13px;
      font-weight: 650;
      padding: 0 10px;
      box-shadow: none;
    }
    .secondary:hover { border-color: var(--line-strong); background: rgba(255, 255, 255, 0.04); }
    textarea {
      width: 100%;
      min-height: 72px;
      resize: vertical;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: rgba(7, 11, 16, 0.78);
      color: var(--text);
      padding: 10px;
      font-size: 14px;
      margin-bottom: 10px;
    }
    .upload-box {
      border: 1px dashed #57606a;
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 14px;
      background: rgba(7, 11, 16, 0.7);
      color: #c9d1d9;
      min-height: 96px;
    }
    .upload-box.drag { border-color: var(--blue); background: #101d2f; }
    .upload-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    input[type=file] { color: var(--muted); max-width: 100%; }
    .thumbs { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 8px; margin-top: 10px; }
    .thumb { border: 1px solid var(--line); border-radius: 6px; overflow: hidden; background: #070b10; }
    .thumb img { display: block; width: 100%; height: 82px; object-fit: cover; }
    .thumb div { padding: 6px; font-size: 11px; color: var(--muted); word-break: break-all; }
    .status-strip {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }
    .status-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(7, 11, 16, 0.58);
      padding: 10px;
      min-width: 0;
    }
    .status-card .k { color: var(--muted); font-size: 11px; margin-bottom: 4px; }
    .status-card .v { color: var(--text); font-size: 14px; font-weight: 700; overflow-wrap: anywhere; }
    .lane-d-console {
      border: 1px solid rgba(94, 200, 255, 0.26);
      border-radius: 8px;
      background: rgba(7, 11, 16, 0.7);
      padding: 14px;
      margin-bottom: 14px;
    }
    .lane-d-console h3 { margin: 0 0 4px; font-size: 18px; }
    .lane-d-console .hint { color: #c9d1d9; font-size: 13px; margin-bottom: 12px; line-height: 1.5; }
    .voice-actions { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin: 0 0 12px; }
    .voice-actions button {
      min-height: 62px;
      font-size: 16px;
      white-space: normal;
    }
    .voice-start { background: #0ea5e9; }
    .voice-stop { background: #ef4444; }
    .voice-refresh { background: #334155; }
    .voice-strip { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .diagnostics {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: rgba(7, 11, 16, 0.48);
      overflow: hidden;
    }
    .diagnostics summary {
      cursor: pointer;
      padding: 12px 14px;
      color: var(--text);
      background: rgba(255, 255, 255, 0.03);
      font-weight: 720;
    }
    .diagnostics-body { padding: 14px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 12px;
      line-height: 1.45;
      color: #c9d1d9;
      max-height: 250px;
      overflow: auto;
      background: #0b0f14;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
    }
    .timeline { display: flex; flex-direction: column; gap: 8px; }
    .result { border: 1px solid var(--line); border-radius: 6px; padding: 8px; background: #0b0f14; font-size: 13px; }
    .result b { color: var(--text); }
    .ok { color: #7ee787; }
    .bad { color: #ff7b72; }
    .warn { color: #f2cc60; }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      .buttons { grid-template-columns: 1fr 1fr; }
      .status-strip { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand"><h1>IronBuddy 调试台</h1><span id="runId">--</span></div>
    <div class="statusbar">
      <span class="pill" id="boardPill">板端: --</span>
      <span class="pill" id="voicePill">语音: --</span>
      <span class="pill" id="logPill">日志: --</span>
      <span class="pill" id="pollPill">刷新: --</span>
    </div>
  </header>
  <main>
    <section>
      <div class="section-head">
        <h2>现场步骤</h2>
        <div>
          <button class="secondary" onclick="toggleMonitor()">暂停/继续监控</button>
          <button class="secondary" onclick="manualRefresh()">立即刷新</button>
        </div>
      </div>
      <div class="body">
        <div class="step-meta" id="stepMeta"></div>
        <div class="step-title" id="stepTitle"></div>
        <div class="instruction" id="instruction"></div>
        <div class="expected" id="expected"></div>
        <textarea id="note" placeholder="记录现场现象：例如画面卡顿、ASR 空文本、TTS 有回应但没气泡。"></textarea>
        <div class="upload-box" id="dropzone">
          <div class="upload-row">
            <input type="file" id="file" accept="image/*,.txt,.json,.log" />
            <button class="secondary" onclick="uploadSelected()">上传证据</button>
          </div>
          <div style="margin-top:8px;font-size:13px;">支持截图文件、拖拽图片、直接 Ctrl+V 粘贴截图。上传会绑定当前步骤并写入 session 记录。</div>
          <div class="thumbs" id="thumbs"></div>
        </div>
        <div class="buttons">
          <button class="pass" onclick="sendAction('通过')">通过</button>
          <button class="fail" onclick="sendAction('失败')">失败</button>
          <button class="retry" onclick="sendAction('重试')">重试</button>
          <button class="skip" onclick="sendAction('跳过')">跳过</button>
        </div>
        <div class="timeline" id="timeline"></div>
      </div>
    </section>
    <section>
      <div class="section-head"><h2>现场状态</h2><button class="secondary" onclick="openReport()">查看报告路径</button></div>
      <div class="body">
        <div class="status-strip">
          <div class="status-card"><div class="k">训练状态</div><div class="v" id="liveBoardSummary">--</div></div>
          <div class="status-card"><div class="k">语音状态</div><div class="v" id="liveVoiceSummary">--</div></div>
          <div class="status-card"><div class="k">记录状态</div><div class="v" id="liveRunSummary">--</div></div>
        </div>
        <div class="lane-d-console" id="laneDVoicePanel">
          <h3>Lane D 语音控制台</h3>
          <div class="hint">这块固定显示，不需要展开。它远程控制板端麦克风录音，不录电脑麦克风；点击开始后请靠近板子说话，再点停止并识别。正常状态应经过 recording / stop_requested / recognizing。</div>
          <div class="voice-actions">
            <button class="voice-start" onclick="laneDManualStart()">开始手动录入</button>
            <button class="voice-stop" onclick="laneDManualStop()">停止并识别</button>
            <button class="voice-refresh" onclick="loadLaneDVoiceDebug()">刷新语音状态</button>
          </div>
          <div class="status-strip voice-strip">
            <div class="status-card"><div class="k">手动录入</div><div class="v" id="laneDManualState">--</div></div>
            <div class="status-card"><div class="k">最近识别</div><div class="v" id="laneDLastText">--</div></div>
            <div class="status-card"><div class="k">Turn 阶段</div><div class="v" id="laneDTurnStage">--</div></div>
          </div>
          <pre id="laneDVoiceDebugRaw">正在读取 /api/voice_debug、/api/chat_draft、/api/voice_turn 和 /api/voice_manual/status。</pre>
        </div>
        <details class="diagnostics">
          <summary>工程诊断模式：FSM / Voice / 进程 / 日志</summary>
          <div class="diagnostics-body">
            <div class="grid">
              <div><h3>FSM / API</h3><pre id="fsm"></pre></div>
              <div><h3>Voice diag</h3><pre id="voice"></pre></div>
            </div>
            <h3>进程</h3><pre id="processes"></pre>
            <h3>/dev/shm</h3><pre id="shm"></pre>
            <h3>语音 / 主循环日志</h3><pre id="logs"></pre>
          </div>
        </details>
      </div>
    </section>
  </main>
  <script>
    let latest = null;
    let statusBusy = false;
    function pretty(obj) { return JSON.stringify(obj || {}, null, 2); }
    function clsFor(action) {
      if (action === '通过') return 'ok';
      if (action === '失败') return 'bad';
      return 'warn';
    }
    async function loadStatus() {
      if (statusBusy || document.visibilityState === 'hidden') return;
      statusBusy = true;
      try {
        const res = await fetch('/api/status');
        latest = await res.json();
        const step = latest.current_step;
        document.getElementById('runId').textContent = `${latest.run_id} · ${latest.scenario || 'main'}`;
        document.getElementById('stepMeta').textContent = `步骤 ${latest.step_index + 1} / ${latest.steps.length} · ${step.id}`;
        document.getElementById('stepTitle').textContent = step.title;
        document.getElementById('instruction').textContent = step.instruction;
        document.getElementById('expected').textContent = '预期：' + step.expected;
        document.getElementById('fsm').textContent = pretty(latest.board);
        document.getElementById('voice').textContent = pretty(latest.voice_diag);
        document.getElementById('processes').textContent = latest.processes || '--';
        document.getElementById('shm').textContent = latest.shm || '--';
        document.getElementById('logs').textContent = (latest.logs || []).slice(-100).join('\n') || '--';
        document.getElementById('boardPill').textContent = latest.board && latest.board.state ? `板端: ${latest.board.state}` : '板端: --';
        document.getElementById('voicePill').textContent = latest.voice_diag && latest.voice_diag.voice_running ? '语音: running' : '语音: --';
        document.getElementById('logPill').textContent = `日志: ${latest.log_status}${latest.monitor_paused ? ' / paused' : ''}`;
        document.getElementById('pollPill').textContent = latest.poll_error ? '刷新: 异常' : `刷新: ${latest.last_poll || '--'}`;
        document.getElementById('liveBoardSummary').textContent = latest.board && latest.board.state
          ? `${latest.board.exercise || '--'} / ${latest.board.inference_mode || '--'} / ${latest.board.state}`
          : '未连接';
        document.getElementById('liveVoiceSummary').textContent = latest.voice_diag && latest.voice_diag.voice_running
          ? `running · 音量 ${latest.voice_diag.tts_volume || '--'}`
          : '未确认';
        document.getElementById('liveRunSummary').textContent = `${latest.scenario || 'main'} · 步骤 ${latest.step_index + 1}/${latest.steps.length}`;
        await loadLaneDVoiceDebug();
        document.getElementById('timeline').innerHTML = (latest.results || []).slice().reverse().map(r => {
          return `<div class="result"><b>${r.time}</b> <span class="${clsFor(r.action)}">${r.action}</span> · ${r.step_title}<br>${r.note || ''}</div>`;
        }).join('') || '<div class="result">还没有步骤结果。</div>';
        document.getElementById('thumbs').innerHTML = (latest.uploads || []).slice(-8).reverse().map(u => {
          const img = u.url && /\.(png|jpe?g|gif|webp)$/i.test(u.url) ? `<img src="${u.url}" alt="upload preview">` : '';
          return `<a class="thumb" href="${u.url}" target="_blank">${img}<div>${u.step_title}<br>${u.path}</div></a>`;
        }).join('');
      } finally {
        statusBusy = false;
      }
    }
    async function sendAction(action) {
      const note = document.getElementById('note').value.trim();
      await fetch('/api/action', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({action, note})});
      document.getElementById('note').value = '';
      await loadStatus();
    }
    async function uploadFile(file) {
      if (!file) return;
      const data = new FormData();
      data.append('file', file, file.name || 'clipboard.png');
      data.append('note', document.getElementById('note').value.trim());
      await fetch('/api/upload', {method:'POST', body:data});
      await loadStatus();
    }
    function uploadSelected() { uploadFile(document.getElementById('file').files[0]); }
    function renderLaneDVoiceDebug(payload) {
      const data = (payload && payload.data) || {};
      const manualResp = data.manual_status || {};
      const manual = manualResp.status || manualResp;
      const voice = data.voice_debug || {};
      const draft = data.chat_draft || {};
      const turn = data.voice_turn || {};
      const text = manual.text || voice.text || draft.text || '--';
      const energy = voice.energy !== undefined ? ` · E ${voice.energy}` : '';
      const threshold = voice.threshold !== undefined ? ` / T ${voice.threshold}` : '';
      document.getElementById('laneDManualState').textContent = manual.state
        ? `${manual.state}${manual.error ? ' · ' + manual.error : ''}`
        : '--';
      document.getElementById('laneDLastText').textContent = text || '--';
      document.getElementById('laneDTurnStage').textContent = `${turn.stage || '--'}${energy}${threshold}`;
      document.getElementById('laneDVoiceDebugRaw').textContent = JSON.stringify(payload || {}, null, 2);
    }
    async function loadLaneDVoiceDebug() {
      const res = await fetch('/api/lane_d/voice_debug', {cache:'no-store'});
      renderLaneDVoiceDebug(await res.json());
    }
    async function laneDManualStart() {
      const res = await fetch('/api/lane_d/voice_manual/start', {method:'POST'});
      document.getElementById('laneDVoiceDebugRaw').textContent = JSON.stringify(await res.json(), null, 2);
      await loadLaneDVoiceDebug();
    }
    async function laneDManualStop() {
      const res = await fetch('/api/lane_d/voice_manual/stop', {method:'POST'});
      document.getElementById('laneDVoiceDebugRaw').textContent = JSON.stringify(await res.json(), null, 2);
      await loadLaneDVoiceDebug();
    }
    async function toggleMonitor() {
      await fetch('/api/monitor', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({paused: !latest.monitor_paused})});
      await loadStatus();
    }
    async function openReport() {
      const res = await fetch('/api/report');
      const report = await res.json();
      alert(`记录目录:\n${report.run_dir}\n\n摘要:\n${report.summary_path}\n事件:\n${report.events_path}`);
    }
    function manualRefresh() { loadStatus(); }
    const dz = document.getElementById('dropzone');
    dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag'); });
    dz.addEventListener('dragleave', () => dz.classList.remove('drag'));
    dz.addEventListener('drop', e => {
      e.preventDefault(); dz.classList.remove('drag');
      if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
    });
    document.addEventListener('paste', e => {
      const files = Array.from(e.clipboardData.files || []);
      if (files.length) uploadFile(files[0]);
    });
    loadStatus();
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') loadStatus();
    });
    setInterval(loadStatus, 2200);
  </script>
</body>
</html>"""


def parse_args():
    parser = argparse.ArgumentParser(description="IronBuddy guided live-test operator console")
    parser.add_argument("--board-ip", default=DEFAULT_BOARD_IP)
    parser.add_argument("--ssh-user", default="toybrick")
    parser.add_argument("--ssh-key", default="~/.ssh/id_rsa_toybrick")
    parser.add_argument("--remote-dir", default="/home/toybrick/streamer_v3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR))
    parser.add_argument("--poll-interval", type=float, default=2.5)
    parser.add_argument(
        "--scenario",
        choices=sorted(STEP_SETS.keys()),
        default=os.environ.get("IRONBUDDY_OPERATOR_SCENARIO", "main"),
        help="guided step set to show in the local operator console",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    session = OperatorSession(args)
    threading.Thread(target=session.poll_loop, daemon=True).start()
    threading.Thread(target=session.log_tail_loop, daemon=True).start()
    app = build_app(session)
    print("IronBuddy operator console")
    print("URL: http://%s:%s/" % (args.host, args.port))
    print("Run dir: %s" % session.run_dir)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
