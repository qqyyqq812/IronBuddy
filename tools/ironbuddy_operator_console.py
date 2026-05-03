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
DEFAULT_BOARD_IP = os.environ.get("IRONBUDDY_BOARD_IP", "10.244.190.224")
LOG_PATTERN = (
    "ASR|识别|唤醒|教练|Chat_Watcher|LLM_Watcher|TTS|DeepSeek|chat_reply|"
    "chat_input|没听清|开始|切到|MVC|voice_turn|Turn|VAD校准|录音超时|"
    "squat|curl|弯举|深蹲|RAG|CoachKB|知识|功能|使用手册|Feishu|飞书|"
    "card_push|OpenCloud|opencloud|openclaw"
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
        "title": "RAG / 飞书 / OpenCloud 复测开始",
        "instruction": "刷新主网页和本调试台，确认这是新的 rag_feishu_cloud_retest run；后台预检已由 Codex 完成，你直接从下一步语音开始。",
        "expected": "调试台显示本步骤；右侧日志会捕捉 CoachKB、飞书和 OpenCloud 相关输出。",
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
        "expected": "接口不返回密钥值；能看到 presentation_name=OpenClaw 云端提醒、primary_runtime=opencloud、最近状态/日志/配置布尔值。",
    },
    {
        "id": "opencloud_offline_dry_run",
        "title": "云端提醒离线兜底 dry-run",
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
        "instruction": "汇总 RAG 功能、飞书卡片、OpenCloud 状态和回归项是否可以进入下一模块。",
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
        "id": "rag_opencloud_showcase",
        "title": "RAG / OpenCloud 展示",
        "instruction": "打开数据页的“知识库 / 云端记忆”，再用语音问“怎么使用你”和“膝盖不舒服怎么办”。",
        "expected": "页面展示真实 RAG 命中、来源和上下文摘要；OpenCloud 只展示真实状态和真实记录，没有历史就显示暂无真实记录。",
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
        "instruction": "按剧本顺序复盘本轮：开场、语音、动作切换、视觉切换、疲劳总结、RAG/OpenCloud、后台调试。",
        "expected": "每个失败点都有截图或备注；能判断是否进入正式录制或需要下一轮小修。",
    },
]


STEP_SETS = {
    "main": MAIN_STEPS,
    "bubble_retest": BUBBLE_RETEST_STEPS,
    "voice_emg_retest": VOICE_EMG_RETEST_STEPS,
    "rag_feishu_cloud_retest": RAG_FEISHU_CLOUD_RETEST_STEPS,
    "rag_voice_control_fix_retest": RAG_VOICE_CONTROL_FIX_RETEST_STEPS,
    "recording_rehearsal": RECORDING_REHEARSAL_STEPS,
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
      /* V7.36 Linear/Vercel-aligned dark theme (matches main UI bg-deep) */
      --bg: #0a0e17;
      --panel: rgba(20, 26, 44, 0.55);
      --panel-2: rgba(20, 26, 44, 0.85);
      --text: #e8ecf1;
      --muted: #8b949e;
      --line: rgba(255, 255, 255, 0.06);
      --green: #4ade80;
      --red: #ef4444;
      --blue: #5ec8ff;
      --yellow: #facc15;
      --purple: #a78bfa;
      --accent: #5ec8ff;
    }
    body, html { font-feature-settings: "cv11", "ss01"; }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
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
      background: #0b0f14;
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
      border-radius: 6px;
      padding: 5px 8px;
      color: var(--muted);
      background: var(--panel);
      font-size: 12px;
      white-space: nowrap;
    }
    main {
      display: grid;
      grid-template-columns: minmax(390px, 0.85fr) minmax(520px, 1.15fr);
      gap: 16px;
      padding: 16px;
      min-height: calc(100vh - 58px);
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      min-width: 0;
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
    .step-title { font-size: 29px; font-weight: 780; margin: 4px 0 10px; }
    .step-meta { color: var(--muted); font-size: 13px; }
    .instruction {
      margin: 14px 0;
      padding: 14px;
      border-left: 4px solid var(--blue);
      background: #111a27;
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
    }
    textarea {
      width: 100%;
      min-height: 72px;
      resize: vertical;
      border-radius: 6px;
      border: 1px solid var(--line);
      background: #0b0f14;
      color: var(--text);
      padding: 10px;
      font-size: 14px;
      margin-bottom: 10px;
    }
    .upload-box {
      border: 1px dashed #57606a;
      border-radius: 7px;
      padding: 12px;
      margin-bottom: 14px;
      background: #0b0f14;
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
      <div class="section-head"><h2>后台监控</h2><button class="secondary" onclick="openReport()">查看报告路径</button></div>
      <div class="body">
        <div class="grid">
          <div><h3>FSM / API</h3><pre id="fsm"></pre></div>
          <div><h3>Voice diag</h3><pre id="voice"></pre></div>
        </div>
        <h3>进程</h3><pre id="processes"></pre>
        <h3>/dev/shm</h3><pre id="shm"></pre>
        <h3>语音 / 主循环日志</h3><pre id="logs"></pre>
      </div>
    </section>
  </main>
  <script>
    let latest = null;
    function pretty(obj) { return JSON.stringify(obj || {}, null, 2); }
    function clsFor(action) {
      if (action === '通过') return 'ok';
      if (action === '失败') return 'bad';
      return 'warn';
    }
    async function loadStatus() {
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
      document.getElementById('timeline').innerHTML = (latest.results || []).slice().reverse().map(r => {
        return `<div class="result"><b>${r.time}</b> <span class="${clsFor(r.action)}">${r.action}</span> · ${r.step_title}<br>${r.note || ''}</div>`;
      }).join('') || '<div class="result">还没有步骤结果。</div>';
      document.getElementById('thumbs').innerHTML = (latest.uploads || []).slice(-8).reverse().map(u => {
        const img = u.url && /\.(png|jpe?g|gif|webp)$/i.test(u.url) ? `<img src="${u.url}" alt="upload preview">` : '';
        return `<a class="thumb" href="${u.url}" target="_blank">${img}<div>${u.step_title}<br>${u.path}</div></a>`;
      }).join('');
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
