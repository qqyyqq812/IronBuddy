# IronBuddy 重构 + 多模态打通设计稿

**日期**：2026-04-26
**状态**：设计阶段（待 writing-plans 转实施）
**作者**：CC + 用户协同（brainstorming skill 流程产出）
**目标 deadline**：2-3 周内拍摄完成比赛展示视频

---

## 1. 北极星与边界

**北极星**：拍摄成功 + 拿奖。不追求做实用产品。

**边界**：
- 代码最终要提交，所以不能太"剧本化"——重构后系统得能正常工作
- 前端已经精雕（39 个功能、36 个轮询循环、WebWorker 架构），**不重写**，仅删 4 个死函数
- FSM / EMG / Vision / GRU 内核不动，只修对不齐的 bug
- 凭证 / SQLite schema / 飞书集成 / database.html 全保留
- V4.2 双分支研究架构归 TODO，**不在本次范围**

---

## 2. 当前所有病灶（10 个具体 bug，含具体行号）

### 语音子系统（6 个）

| ID | 症状 | 位置 | 影响 |
|---|---|---|---|
| S1 | UI 对话框反复出现新气泡 | 前端 chat-poll 无 turn_id 概念 | 视觉混乱 |
| S2 | 没说"教练"也会触发录音 | voice_daemon.py 主循环无显式状态机 | Ghost activations |
| S3 | 疲劳值满自动播报后会开始录音 | 自动播报和用户对话共用录音通道 | 不该录的时候录 |
| S4 | MVC 测试期间几秒后又出现新对话框 | MVC 阻塞期间 main loop 仍跑 VAD | 同上 |
| S5 | 长对话 STT 文字偏差 | VAD_TIMEOUT=12s 导致长录音嘈杂 | 识别差 |
| S6 | 一句话被拉成多句"无限录入" | VAD silence_limit=1.2s + 12s hard cap | 系统看似停不下来 |

### 多模态推理（4 个，agent 深扫定位）

| ID | 症状 | 位置 | 影响 |
|---|---|---|---|
| M1 | Ang_Vel 列推理时未归一化 | [main_claw_loop.py:1062-1065](../../hardware_engine/main_claw_loop.py#L1062) 缺 col 0 归一化 | **GRU 第一个门饱和，所有推理输出都是垃圾** |
| M2 | V7.18 偷偷回滚 V7.15 的 FSM/GRU 解耦 | [main_claw_loop.py:384-392](../../hardware_engine/main_claw_loop.py#L384) + [L662-669](../../hardware_engine/main_claw_loop.py#L662) | 每个 rep 双计数 |
| M3 | Symmetry_Score 推理硬编码 1.0 | [main_claw_loop.py:1011](../../hardware_engine/main_claw_loop.py#L1011) | standard vs compensating 关键特征丢失 |
| M4 | deprecated `train_model.py` + `models/extreme_fusion_gru_*.pt` 未归档 | tools/, models/ | 调试时混淆，加载错权重风险 |

### 路由与意图（3 个，与语音 6 个有交集）

| ID | 症状 | 位置 | 影响 |
|---|---|---|---|
| R1 | "现在适合做深蹲吗"被误吃为命令 | `_is_command_intent` 把裸字"做"放进 `_EXPLICIT_CMD_MARKERS` | 剧本 T1 直接死 |
| R2 | DeepSeek system prompt 硬编码 biceps 偏好 + knee_caution | DB `system_prompt_versions` 旧 active 行 | 问深蹲被警告膝盖 |
| R3 | 4 处 DeepSeek 调用参数不一致，streamer 还少 `/v1` | voice_daemon / streamer / deepseek_direct / fsm | 一致性差 |

---

## 3. 三区分图

```
┌─────────────────────────────────────────────────────────────┐
│  绝对不动                                                    │
│  ─ .api_config.json + secrets fallback                      │
│  ─ SQLite 11 张表 schema + 233 rep + 12 LLM 历史            │
│  ─ templates/index.html 4025 行（39 个功能）                 │
│  ─ templates/database.html 1235 行（剧本 T7 直接拍）         │
│  ─ FSM main_claw_loop.py 主体（仅修 M1/M2/M3）               │
│  ─ GRU CompensationGRU 模型架构（不重训，仅微调）            │
│  ─ Vision pipeline + EMG pipeline                            │
│  ─ Feishu / RTMPose Cloud / HDMI                             │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  小修：~50 行级别                                             │
│  ─ M1 Ang_Vel 归一化（1 行）                                 │
│  ─ M2 FSM 解耦回退（4 段共 ~20 行）                          │
│  ─ M3 Symmetry 训练侧改 1.0 + 重训 5 分钟                   │
│  ─ R1 移出"做"关键词                                          │
│  ─ R2 INSERT 新 active prompt                                │
│  ─ 砍 4 个死路由 + 4 个前端死函数                            │
│  ─ M4 deprecated 文件归 .archive/                            │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  重构：~400 行级别                                            │
│  ─ voice/state.py 新建（VoiceStateMachine 3 状态）           │
│  ─ voice/recorder.py 抽 record_with_vad + arecord SIGSTOP    │
│  ─ voice/turn.py 新建（Turn dataclass + voice_turn.json）    │
│  ─ voice_daemon.py main() 从 464 行降到 ~80 行              │
│  ─ voice/router.py（DeepSeek tool calls + Tier A 兜底）      │
│  ─ cognitive/deepseek_client.py 统一 4 处调用                │
│  ─ shm 单写者协议（exercise_mode/fatigue_limit/inference_mode）│
│  ─ 自动疲劳触发链（FSM → auto_trigger.json → BUSY → TTS）    │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  必须新增                                                     │
│  ─ 8 条 DeepSeek tool definitions                           │
│  ─ Tier A INSTANT_FALLBACK 4 条 regex                       │
│  ─ subvideo prep scripts (reset_demo_state.sh)              │
│  ─ T1-T20 手测脚本 checklist                                │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. 四阶段实施方案

### 阶段 0：多模态打通（1.5-7 天，自适应）

**子阶段 0.1（4 小时）：修 4 个 bug**

```python
# main_claw_loop.py L1066 添加（M1）
window[:, 0] = np.clip(window[:, 0] / 30.0, -3.0, 3.0)

# main_claw_loop.py L384-392 + L662-669 恢复 V7.15 mode-gating（M2）
# 在 vision_sensor 模式下不直接 increment good/failed，由 GRU 路径决定
if self._inference_mode != "vision_sensor":
    if rep_quality_good:
        self._good_squats += 1
    else:
        self._failed_squats += 1

# main_claw_loop.py L1011 改用真 sym 或保持 1.0 + 训练侧也改 1.0（M3）
# 简化方案：训练 + 推理都用 1.0，弱化 sym 信号
# train_gru_three_class.py 的 _synthesize_compensating 删除 comp[:, 5] *= uniform 那行

# 把 deprecated 归档（M4）
mv tools/train_model.py .archive/deprecated_v3map/
mv models/extreme_fusion_gru_squat.pt .archive/deprecated_v3map/
mv models/extreme_fusion_gru_curl.pt .archive/deprecated_v3map/
```

**子阶段 0.2（4 小时）：用 simulator 验证三类**

```bash
# 起 FSM + EMG simulator (squat)
python3 hardware_engine/main_claw_loop.py &
python3 tools/simulate_emg_from_mia.py --label standard &
# 等用户做几个动作，看 UI statGood / statFailed / statComp 是否正确递增

# 重复 standard / compensating / non_standard 三遍
# 重复 squat 和 bicep_curl 各一次
# 总计 6 个组合，每个跑 30 秒

# 通过判定：
# - statGood 在 standard label 下递增
# - statComp 在 compensating label 下递增
# - statFailed 在 non_standard label 下递增
# - 计数器不双数
```

**子阶段 0.3（决策点）**：
- 三类都正确 → ✅ Phase 0 完成，simulator 是子视频拍摄方案
- 三类不能区分 → 进子阶段 0.4

**子阶段 0.4（仅在 0.3 失败时启动，5-7 天）**：

```
Day 1: 接 ESP32 + 跑 hardware_domain_calibrate.py 重生 α/β
Day 2: 用 simulate_mvc_burst.py + 真 ESP32 完成 MVC 校准
Day 3-4: 真录每类 ~10 段 compensating，加 augment_curl_data.py 增强
Day 5: 仅微调 GRU 最后两层（不全重训），保留 MIA 学到的特征
Day 6-7: 子视频拍摄演练
```

**Phase 0 工作量**：乐观 1.5 天，悲观 7 天。

### 阶段 1：语音状态机 + UI 协议 + VAD 边界（3 天）

**核心组件**（新增文件）：

```
hardware_engine/voice/
├── state.py          # VoiceState enum + VoiceStateMachine 类（~150 行）
├── recorder.py       # record_with_vad 抽出 + arecord 进程级 gate（~100 行）
├── turn.py           # Turn dataclass + voice_turn.json 写入（~50 行）
└── __init__.py
```

**3 状态机定义**：

```python
class VoiceState(Enum):
    LISTEN = "listen"      # 监听 wake word（默认，麦开）
    DIALOG = "dialog"      # wake 命中后录入和处理
    BUSY = "busy"          # 系统播报 / MVC / 自动触发（arecord SIGSTOP）

# 转移合法性：
LISTEN → DIALOG  (wake word 命中)
DIALOG → LISTEN  (TTS 播报完毕)
LISTEN → BUSY    (FSM 推 llm_reply / MVC / auto_trigger)
BUSY   → LISTEN  (播报或 MVC 完成)
```

**VAD 参数收紧**（修 S6）：
```python
SILENCE_LIMIT = 1.0       # 1.2s → 1.0s
VAD_TIMEOUT   = 6         # 12s → 6s 硬上限
ACTIVE_SPEECH_CAP = 5.0   # 新增：连续发声 5s 强制截断
```

**UI 对话气泡协议**（修 S1）：
- 每次 LISTEN → DIALOG 转移生成 `turn_id = uuid4().hex[:8]`
- 写入 `/dev/shm/voice_turn.json`：`{"turn_id": "...", "stage": "wake|user|reply|closed"}`
- 前端 chat-poll 改成相同 turn_id 复用气泡，不建新

**arecord 进程级 gate**（修 S2/S3/S4）：
```python
# BUSY 状态进入时
subprocess.run(["sudo", "kill", "-SIGSTOP", str(arecord_pid)])
# BUSY 状态退出时
subprocess.run(["sudo", "kill", "-SIGCONT", str(arecord_pid)])
```

**voice_daemon.py 改造**：从 2355 行降到 ~600 行（main 从 464 行降到 ~80 行）。

### 阶段 2：DeepSeek tool calls + 隐式 ack（3 天）

**所有命令统一处理**：
```python
def execute_tool(tool_name, args):
    speak(TOOL_ACK[tool_name].format(**args))   # 立即 ack 播报
    do_action(tool_name, args)                   # 立即执行
    # 完。无确认。无回退。
```

**8 条 tool definitions**：

| tool name | description | parameters |
|---|---|---|
| `switch_exercise` | 切换训练动作 | `action: enum[squat, curl]` |
| `switch_vision_mode` | 切视觉模式 | `mode: enum[pure_vision, vision_sensor]` |
| `switch_inference_backend` | 切推理后端 | `backend: enum[local_npu, cloud_gpu]` |
| `set_fatigue_limit` | 设疲劳上限 | `value: int [100, 5000]` |
| `start_mvc_calibrate` | 启动 MVC 校准 | (none) |
| `push_feishu_summary` | 推送飞书 | (none) |
| `shutdown` | 关闭系统 | (none) |
| `report_status` | 口头报告状态 | (none) |

**Tier A INSTANT_FALLBACK**（毫秒级，断网可用）：
```python
INSTANT_FALLBACK = {
    "静音": handle_mute,
    "闭嘴": handle_mute,
    "解除静音": handle_unmute,
    "可以说话": handle_unmute,
    "停": handle_stop,
    "取消": handle_cancel,
    "不对": handle_cancel,
}
```

**路由器**（替代 414 行 if 链 → 30 行）：
```python
def handle_user_text(text: str, ctx: dict) -> Action:
    # Tier A: 毫秒级 regex
    for keyword, fn in INSTANT_FALLBACK.items():
        if keyword in text:
            return fn()
    # Tier B: DeepSeek with tools
    resp = deepseek.chat_with_tools(
        system=NEUTRAL_PROMPT,
        user=text,
        tools=TOOLS,
        timeout=8.0,
    )
    if resp.tool_calls:
        for tc in resp.tool_calls:
            execute_tool(tc.name, tc.args)
        return Action.executed
    return Action.speak(resp.content)  # 纯闲聊
```

**新 system_prompt INSERT**（修 R2）：
```sql
INSERT INTO system_prompt_versions (ts, prompt_text, based_on_summary_ids, active, is_demo_seed)
VALUES (datetime('now'), '你是 IronBuddy 健身教练。
- 回答简短自然：3 句话以内，80 字以内，不用 markdown。
- 当前用户的训练实况会作为上下文给你（动作类型/达标数/违规数/疲劳值），可参考但不强求引用。
- 当用户问健身建议时，给专业、具体的建议，不预设用户偏好。
- 你不能执行系统命令；如果用户表达类似指令意图，回复"这条指令请直接对系统说，例如 切到深蹲"。',
NULL, 1, 0);
-- 旧 active 自动降级
```

**DeepSeek 客户端统一**（修 R3）：
```python
# hardware_engine/cognitive/deepseek_client.py（新建）
@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    timeout: float = 8.0

class DeepSeekClient:
    def chat(self, system, user, *, max_tokens=200, temperature=0.7) -> Optional[str]: ...
    def chat_with_tools(self, system, user, tools, *, timeout=8.0) -> ToolResponse: ...
    @classmethod
    def from_config(cls) -> "DeepSeekClient": ...

# 替换 4 处旧调用
```

**shm 双写 race 修法**：
- 引入 `intent_*.json` 文件，UI 写 intent，FSM 读 intent 后写状态
- voice 是系统级权威，可直接写状态文件
- 仅改 3 处 Flask 路由 + FSM 加 1 个 watcher

### 阶段 3：自动疲劳 + MVC + 拍摄准备（3 天）

**自动疲劳触发链**：
```
FSM 检测疲劳达上限
   ↓
写 /dev/shm/auto_trigger.json {"reason":"fatigue", "stats":{...}}
   ↓
voice 进 BUSY 状态 → arecord SIGSTOP
   ↓
DeepSeek 流式生成总结（TTS 边收边播）
   ↓
TTS 完毕 → arecord SIGCONT → 回 LISTEN
```

**不做 barge-in**（拍摄时用户配合等播完即可）。

**MVC 流程**（无 LISTEN_RESTRICTED 子状态，"开始"走 Tier A regex）：
```
切到弯举（tool_call switch_exercise(curl)）
   ↓ ack "好的，切换到弯举模式"
   ↓ 接播报 "准备好后请说开始 MVC 测试"
   ↓ 用户说 "开始"
Tier A 命中 → 倒数 3-2-1 → 录峰值 3.5s → 播报 "测试结束，训练正式开始"
```

**子视频拍摄检查脚本**：
```bash
# scripts/prepare_subvideo_squat.sh
# scripts/prepare_subvideo_curl.sh
# scripts/reset_demo_state.sh
# scripts/dryrun_main_video_checklist.md（手测，不是自动化）
```

---

## 5. 不做清单（明确边界）

- ❌ V4.2 双分支研究架构（dual_branch_fusion.py 留着但不上线）
- ❌ 三层确认（隐式/读回/显式）→ 全部隐式
- ❌ STT 置信度分级
- ❌ LISTEN_RESTRICTED MVC 子状态
- ❌ Barge-in（中断自动播报）
- ❌ 主视频 dry-run 全自动测试（改手测脚本）
- ❌ 重写前端 4025 行
- ❌ 拆 streamer_app.py 成 api/admin/pages package
- ❌ 健身专业 RAG 接入（TODO）
- ❌ 重训 V3 模型（先修 bug 看够不够）
- ❌ V7.18 双计数 → 复杂的"等 GRU"逻辑（直接恢复 V7.15 mode-gating 即可）

---

## 6. 测试清单（T1-T20）

### Phase 0 测试
```
T_M1. squat simulator --label standard → UI statGood++（不 statComp/statFailed）
T_M2. squat simulator --label compensating → UI statComp++（不 statGood/statFailed）
T_M3. squat simulator --label non_standard → UI statFailed++
T_M4-M6. 同上三类，但用 bicep_curl simulator
T_M7. 计数器单加（不双数）
```

### Phase 1 测试
```
T1. 喊"教练" → 说"现在适合做深蹲吗" → DeepSeek 回复 → 自动回 LISTEN
T2. IDLE 期间随便说话 30 秒 → 不应录入
T3. 喊"教练" + 长段 6+ 秒 → 6s 强制截断
T4. 连喊"教练 切深蹲" "教练 切弯举" "教练 切纯视觉" → UI 三对独立气泡
T5. FSM 触发自动播报 → 期间环境噪音不录
T6. "教练 开始 MVC" → MVC 期间环境噪音不录
T7. 唯一 turn_id 协议下相同 turn 不建新气泡
```

### Phase 2 测试
```
T8. "切到深蹲" / "切到弯举" / "切到纯视觉" → 三次切换不打架
T9. "现在适合做深蹲吗" → TIER B DeepSeek，不再"没听清"
T10. "我膝盖酸" → TIER B DeepSeek
T11. "推送多少组" → 应被 LLM 识别为 push_feishu，不是 report_status
T12. UI 触发 fatigue_limit + 50ms 后语音改 → 不互相覆盖
T13. 阶段 1 全部复测
```

### Phase 3 测试
```
T14. 自动播报期间环境噪音不应触发录音
T15. 自动播报期间喊"教练" → 等播完后才进 DIALOG（不打断）
T16. 弯举切换 → MVC 引导 → "开始" → 倒数 → 完成
T17. 弯举引导期说"我膝盖酸"等无关语 → DeepSeek 回（不影响 MVC 流程）
T18. "请关机" → 隐式 ack "再见，请明天准时来锻炼" → 立即关
T19. "切到深蹲" → 隐式 ack 不二次确认
T20. 子视频 1+2 拍摄环境一键 ready
```

---

## 7. 时间线

```
[阶段 0]    1.5-7 天    多模态 bug 修补 + simulator 验证
[阶段 1]    3 天        语音状态机 + UI 协议 + VAD 边界
[阶段 2]    3 天        DeepSeek tool calls + 隐式 ack
[阶段 3]    3 天        自动疲劳 + MVC + 拍摄准备

合计：10.5-16 天

并行机会：
   阶段 0 + 阶段 1 同时改不同文件（main_claw_loop vs voice_daemon）
   实际墙钟时间约 ~2 周

缓冲：
   2-3 周 deadline 内有 3-7 天 buffer 用于 bug fix + 拍摄演练
```

---

## 8. TODO 栏目（赛后处理）

### 8.1 健身专业资料库接入（用户老师建议）

调研结论（[JMIR Medical 2025](https://medinform.jmir.org/2025/1/e59309)、[Nature npj Digital Medicine 2025](https://www.nature.com/articles/s41746-025-01519-z)）：RAG 是运动建议领域的最佳路径。

**路径**：MCP server + 向量库（ChromaDB）+ DeepSeek tool registration

**工作量**：3-5 天
- 准备语料：CSCS / NSCA + 中文运动医学手册（100-300 万字）
- BGE-M3 embedding
- ChromaDB 向量库
- Python `mcp` server
- DeepSeek tools 加 `search_fitness_kb`

### 8.2 V4.2 双分支架构

完整 LOSO + masked AE pretraining 路径，270 reps/exercise 数据采集，[code 已写完](../../hardware_engine/cognitive/dual_branch_fusion.py)，未上线。可作为论文级研究方向继续。

### 8.3 真 ESP32 + 域映射 + 重训

如果 Phase 0.3 决策点显示 simulator 不够，已有 `hardware_domain_calibrate.py`、`augment_curl_data.py` 等工具就位，可启动真录路径。

### 8.4 Barge-in / 全双工

赛后改 SpeechManager 加 KWS 独立通道，让用户能中断系统播报。

### 8.5 streamer_app.py 拆 api/admin/pages package

仅当未来要加大量新功能时做，当前没必要。

---

## 9. 关键引用 / 调研源

**Agent 深扫报告**（本会话内 task `a33c8cae475fdcb16`）：
- 多模态 bug M1/M2/M3/M4 定位，含具体行号
- V3 / V4.2 架构对比

**Agent 报告**（task `accb46e5a07110925`）：
- 前端 39 个功能完整清单 + 36 个轮询循环
- database.html 9 张表 + 7D 雷达图

**Agent 报告**（task `ac822077b1ccd0a9f`）：
- voice_daemon.py 长对话 / DeepSeek prompt / 命令分支详细分析
- 5 个具体 bug

**学术 / 产业调研**：
- [DeepSeek Function Calling](https://api-docs.deepseek.com/guides/function_calling)
- [VUI Best Practices 2025 Lollypop Studio](https://lollypop.design/blog/2025/august/voice-user-interface-design-best-practices/)
- [Stanford HAI Voice Assistant Turn-Taking](https://hai.stanford.edu/news/it-my-turn-yet-teaching-voice-assistant-when-speak)
- [JMIR Medical 2025 LLM Exercise Recommendations](https://medinform.jmir.org/2025/1/e59309)
- [Nature npj Digital Medicine 2025 RAG Medical Fitness](https://www.nature.com/articles/s41746-025-01519-z)
- [Meta emg2pose NeurIPS 2024](https://ai.meta.com/blog/open-sourcing-surface-electromyography-datasets-neurips-2024/)
- [Springer EMG CNN 2025](https://link.springer.com/article/10.1007/s00521-025-11456-3)

**项目内权威文档**：
- [深蹲神经网络权威指南.md](../验收表/深蹲神经网络权威指南.md)
- [弯举神经网络权威指南.md](../验收表/弯举神经网络权威指南.md)
- [V3_7D_全链路地图.md](../验收表/V3_7D_全链路地图.md)
- [数据采集与训练指南.md](../technical/数据采集与训练指南.md)
- [presentation/ppt_outline_v2.md](../../presentation/ppt_outline_v2.md)

---

## 10. 设计签收

- [x] 用户确认三段拼接拍摄方案（主视频 + 子视频 1+2）
- [x] 用户确认 Route B 三阶段（精准修补 + 命令表化 + 自动触发）
- [x] 用户确认全部隐式确认（去掉三层确认/STT 置信度分级/LISTEN_RESTRICTED）
- [x] 用户确认 Phase 0 策略（先修 bug + simulator 验证优先）
- [ ] 进入 writing-plans skill 转交实施计划

下一步：调用 writing-plans skill，把本文档转成可执行的逐步实施计划（每个 phase 拆成 commit-level task）。
