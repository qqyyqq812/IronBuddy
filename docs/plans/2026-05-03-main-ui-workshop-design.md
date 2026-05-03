# IronBuddy 主页面工作台化重构 — 设计稿

2026-05-03 由 Claude Code 起草，用户已通过 brainstorming 确认全部 7 项决策。

## 上下文与触发

2026-05-03 13:09 录制排练 12 步，7 通过 / 5 失败。其中**云端 GPU 切换页面卡死**、
**RAG/OpenCloud 展示"非常拉垮"**、**代码结构图只是 8 节点固定 div** 三项是用户
直接交给 Claude Code 处理的。叠加用户希望主页面成品化，把"调试" tab 重塑为
长期可复用的后台工作台，并用 Obsidian/Karpathy 风格的代码结构图展示。

整体决策来自 brainstorming：
1. 范围：新工作台一并上线，不做录制必须项 + 新功能两段拆分。
2. 代码可视化：[Understand-Anything](https://github.com/Lum1104/Understand-Anything)
   skill 生成数据 + 主网页用 [3d-force-graph](https://github.com/vasturiano/3d-force-graph)
   自渲染。
3. 调试 tab：iframe 嵌入 `http://127.0.0.1:8765/`，不再重写一份 thin client。
4. AI 实时调用 / 结果导向验收：不在代码中实现，是用户工作流，由 operator
   console 现有 events/upload 落盘能力承载。
5. 云端 GPU 失败回退：不做（用户明确承担失败时手动切回）。只修"成功路径"。
6. RAG / OpenCloud / 旧 code graph：从主页面 "数据" tab 全部拿掉，走后台。
7. UI 风格：Linear / Vercel 深色科技感（不动布局、不动交互）。

## 范围边界（必读）

### 做
- 修云端 GPU 热切换"成功路径"。
- 主页面 "数据" tab 清理：删 RAG showcase / OpenCloud records / 旧 code graph。
- 主页面 "调试" tab 重构：代码结构图 + 简易反馈区 + iframe 嵌入 8765 + 服务日志折叠。
- 代码结构图：Understand-Anything skill / 自写脚本生成 graph.json，
  `3d-force-graph` 前端渲染，git diff 着色，点击展开邻居节点。
- UI 全局质感升级：颜色 token、卡片阴影、按钮三层级、状态胶囊、tab 过渡。
- `tools/ironbuddy_operator_console.py` 主题对齐主网页深色科技感。

### 不做（拒绝项）
- 不 wire `hardware_engine/voice/router.py`。
- 不动 `voice_daemon.py` / `main_claw_loop.py` 的语音/FSM 行为。
- 不把 Sensor Lab 写进 `streamer_app.py`。
- 不测"请关机"。
- 不写 AI 实时调用面板。
- 不写"结果导向验收"专用 UI。
- 不做云端 GPU 失败自动回退。

## A. 修云端 GPU 热切换

### 当前 bug 路径
- [templates/index.html:2298](templates/index.html#L2298) `switchVision()` 写
  请求后 setTimeout 1500ms 即假成功。
- [streamer_app.py:988](streamer_app.py#L988) `/api/switch_vision` 写
  `vision_mode.json` + drop `vision_reset.flag` 直接 return ok，未关心
  `cloud_rtmpose_client.py` 是否真就绪。
- 设置页"云端连通测试"按钮 `cloudVerifyStatus` 当前 stub，调用零反馈。

### 修法
1. **板端 cloud_rtmpose_client 写状态**：在
   `hardware_engine/ai_sensory/cloud_rtmpose_client.py` 的连接握手关键节点写
   `/dev/shm/cloud_rtmpose_status.json`：
   ```json
   {"phase":"connecting|ready|failed","ts":<float>,"detail":"...","backend":"cloud|local"}
   ```
   写入 phase=`ready` 表示真已收到首帧 keypoint；phase=`failed` 表示连接超时
   或解析异常。
2. **后端新增 `/api/cloud_handshake_status`**：在 `streamer_app.py` 加只读
   端点读 `/dev/shm/cloud_rtmpose_status.json`，5s 内变化才算新结果。
3. **前端 `switchVision('cloud')`**：写完 `/api/switch_vision` 后开始轮询
   `cloud_handshake_status`，最多 6 秒。`phase=ready` 才 toast"已切换到云端"，
   超时或 failed toast"切换中（云端未响应）"，**不自动回退**。
4. **设置页"云端连通测试"**：直接 `fetch('/api/admin/cloud_verify')`（已存在
   端点），把延迟和 HTTP 返回贴回 `cloudVerifyStatus`。

## B. 主页面 tab 清理与重构

### "数据" tab：保留 → 简化
- 保留：`📊 一站式数据库` 链接（外跳 `/database`）、`📁 训练数据` 树。
- 删除：RAG showcase card、OpenCloud records card、旧 code graph card。
- 前端 `loadDemoShowcase()` / `loadCodeGraph()` 调用拿掉。后端
  `/api/demo/rag_status` / `/api/demo/opencloud_records` / `/api/demo/code_graph`
  **保留**（`/database` 页可能用，operator console 可能用）。

### "调试" tab：重构

```
┌─ 调试 tab ────────────────────────────┐
│ 代码结构图 (3d-force-graph)            │  约 360px 高
│   节点 · 边 · 搜索 · git diff 着色     │
├───────────────────────────────────────┤
│ 问题反馈区                             │  约 120px 高
│  截图粘贴 / 上传 + 备注 + 提交         │
│  → POST 到 8765 的 events.jsonl        │
├───────────────────────────────────────┤
│ iframe → 127.0.0.1:8765                │  约 720px 高
│   完整 scenario / upload / summary     │
├───────────────────────────────────────┤
│ 服务日志终端（可折叠，默认折叠）        │  按需展开
└───────────────────────────────────────┘
```

**问题反馈区行为**：截图粘贴用 `Ctrl+V` 或拖拽，备注写入文本框，点击"保存到当前
run"调用 `127.0.0.1:8765/api/note`（operator console 暴露的 helper），把
text + base64 image 写入对应 run 目录的 `events.jsonl` + `uploads/`。

## C. 代码结构图

### 数据生成
1. **首选**：装 [Understand-Anything](https://github.com/Lum1104/Understand-Anything)
   skill。如果发现该 skill 与 IronBuddy 数据格式不匹配或 Python 3.7 兼容性问题，
   退回到方案 2。
2. **退路**：自写 `tools/build_code_graph.py`：
   - 用 stdlib `ast` 扫 `hardware_engine/**/*.py`、`streamer_app.py`、
     `tools/ironbuddy_*.py`、`scripts/opencloud_reminder_daemon.py` 的 `import` 语句。
   - 用 `git log --since="30 days ago" --name-only --pretty=format:""` 拿改动
     文件清单。
   - 输出 `data/code_graph/graph.json`：
     ```json
     {
       "nodes": [
         {"id":"voice_daemon.py","label":"voice_daemon","kind":"voice","loc":1856,
          "git_age_days":2,"path":"hardware_engine/voice_daemon.py"},
         ...
       ],
       "edges": [
         {"source":"voice_daemon.py","target":"coach_knowledge.py","kind":"import"},
         ...
       ],
       "generated_at": "<iso8601>",
       "commit": "<short-sha>"
     }
     ```
   - CLI：`python3 tools/build_code_graph.py --refresh` 重建；不带参数仅打印路径
     是否存在。
3. 后端新增 `/api/code_graph`：读 `data/code_graph/graph.json`，原样返回
   JSON。如果文件不存在，返回 `{ok:false, message:"运行 tools/build_code_graph.py
   --refresh 生成"}`。

### 节点元数据
- `kind`：voice / fsm / api / vision / sensor / cognitive / frontend /
  debug / cloud / shared。
- `loc`：行数。
- `git_age_days`：最后一次提交距今天数；未提交改动 = -1（视觉用黄色）。
- `path`：相对项目根的路径。

### 前端渲染
- 库：`3d-force-graph` 通过 CDN 加载（`unpkg.com/3d-force-graph` + 它依赖的
  three.js）。如果板端无外网（CDN 不可达），改用本地 `static/vendor/3d-force-graph.min.js`。
- 默认 2D 模式（`ForceGraphVR/3D` 之外的 `ForceGraph` 函数），不开 3D，
  以避免低端设备 GPU 负担。**3D 模式可作为后续切换按钮**。
- 颜色规则（与 git_age_days 绑定）：
  - `-1`（未提交）→ `#facc15`（amber-400）
  - `0..7` 天 → `#fb923c`（orange-400）
  - `8..30` 天 → `#5ec8ff`（IronBuddy cyan）
  - `>30` 天或无数据 → `#4b5563`（gray-600）
- 节点大小 ∝ `sqrt(loc)`，clamp 到 4–14 px。
- 点击节点 → 弹右侧 drawer：路径、loc、最近 5 条 git log（含 sha、message、ts）、
  import 列表、被引用列表、点击邻居节点把它居中聚焦。
- 搜索框：模糊匹配 label / path，匹配命中 → 镜头 zoom 到 + 高亮 1-hop 邻居。
- 拖动 / 缩放 / hover 高亮邻居默认行为保留。

### 默认覆盖范围
**包含**：
- `hardware_engine/**/*.py`（voice、cognitive、sensor、ai_sensory、integrations）
- `streamer_app.py`、`templates/index.html`（标记为 frontend 单节点）
- `tools/ironbuddy_operator_console.py`、`tools/ironbuddy_sensor_lab.py`
- `scripts/opencloud_reminder_daemon.py`

**排除**：tests、`.archive`、`tools/rknn-toolkit_source`、所有 `.json` 数据。

## D. UI 全局质感升级（Linear 风）

### 颜色 token（替换 templates/index.html `:root`）
```css
--bg-primary:    #0a0a0c;
--bg-card:       #14141a;
--bg-hover:      #1c1c24;
--border-subtle: rgba(255,255,255,0.06);
--border-active: rgba(94,200,255,0.35);
--accent-cyan:   #5ec8ff;
--accent-green:  #4ade80;
--accent-red:    #ef4444;
--accent-orange: #fb923c;
--text-strong:   #fafafa;
--text-default:  #d4d4d8;
--text-muted:    #a1a1aa;
--text-dim:      #71717a;
font-feature-settings: "cv11", "ss01";
```

### 卡片
1px subtle border + `box-shadow: 0 0 0 1px rgba(94,200,255,0.04) inset, 0 1px
2px rgba(0,0,0,0.3);`，圆角 6px。

### 按钮三层级
- `.btn-primary`：实色填充（一键启动），活跃态发光 2px ring。
- `.btn-secondary`：subtle border + bg-card，hover 提亮。
- `.btn-ghost`：纯文字，hover 出 border（tab btn 用）。

### 状态胶囊
圆角 999px + 内层小圆点 + 微发光（在线/离线/未运行已有，加 `text-shadow:
0 0 6px currentColor` 微辉）。

### 过渡
tab 切换 opacity + translateY(4px → 0) 100ms ease-out。卡片 hover 100ms。

### 不改的
- 所有按钮 onclick handler。
- 所有 fetch 路径。
- 所有 setInterval 周期。
- 所有交互逻辑、布局 grid、组件位置。

## E. operator console 主题对齐

修改 [tools/ironbuddy_operator_console.py](tools/ironbuddy_operator_console.py)
内嵌 HTML 的 `<style>`：套用 § D 同款 css token，使主网页 iframe 加载时风格不打架。
**不改 scenario / upload / events / step result 行为**。

## F. 任务拆分 + 录制阻塞

| 阶段 | 任务 | 录制阻塞？ | 估时 |
|---|---|---|---|
| 1 | A 云端 GPU 热切换 | 是 | 1.5h |
| 2 | B 数据 tab 清理 + 调试 tab iframe 嵌入 | 是 | 1h |
| 3 | D UI 质感升级 | 是 | 2h |
| 4 | C 代码结构图（Understand-Anything 装 + 数据生成 + 渲染） | 否 | 3-4h |
| 5 | E operator console 主题对齐 | 否 | 0.5h |
| 6 | B 反馈区（截图/备注落 run） | 否 | 1h |

阶段 1-3 完成可立即录制。阶段 4-6 不阻塞录制，可在录制空档继续。

## G. 仍未验证的录制风险（Claude Code 不负责）

- 真实云端 GPU 长链路稳定性（Codex / 用户处理）。
- ASR "嗯" 后 1 秒"没听清"（语音线 Codex 处理）。
- ESP32 持续发包（Lane B 仍 fallback 模拟）。
- 角度 smooth 敏感度（Codex 处理中）。

## H. 部署与验证流程（Claude Code 必须遵循）

每个阶段交付前：
1. 本地 `python3 -m py_compile` 涉及 .py 文件。
2. `pytest` 涉及测试，确认绿。
3. CURRENT.md 顶部拿 `claude_code` 锁，写明 reason。
4. rsync 部署到板端 `/home/toybrick/streamer_v3/`，备份到 `.deploy_backups/claude_code_<ts>_<topic>/`。
5. 板端 `py_compile` 通过，仅重启受影响的 streamer 服务（不动 vision/voice/fsm/emg）。
6. 烟测涉及的 API 返回 200 + 字段正确。
7. 写回 CURRENT.md 部署摘要，释放锁。
8. 新建 operator run 复测（不复用旧 run）。

## 来源

- [Understand-Anything](https://github.com/Lum1104/Understand-Anything)
- [3d-force-graph](https://github.com/vasturiano/3d-force-graph)
- [Linear design references](https://linear.app)
