# V7.37 后台真实化重构 — 设计稿

2026-05-03 由 Claude Code 起草，brainstorming skill 引导，用户已确认全部 7 项决策 + §E P0 已通过（飞书 webhook 真发成功 `msg_id=om_x100b504488a104acb395b144eb6423e`）。

## 上下文与触发

20260503-160609 run（scenario `v736_workbench_voice_retest`）反馈：
- ✅ V7.36 工作台入口、调试 tab、代码图、动作控件、上线提示、唤醒、固定介绍、静音音量 全部通过
- ❌ **代码图**：用户说"前端显示效果不好，应该改成 GitHub 链接"
- ❌ **云端切换**：实际 V7.36 代码工作正常，"失败"是配置问题（CLOUD_RTMPOSE_URL 还是 stub `127.0.0.1:6006`）
- ❌ **RAG / OpenCloud / 数据库**：用户说"还是展示端的内容"+"RAG 在哪里看"+"OpenClaw 不是 OpenCloud" + 期望"周日 17:00 周报、每天 9:00 提醒、详细清晰"
- ❌ **疲劳总结角度**：lane_a 在处理（FSM pending rep 修复，16:48 持锁），不在本设计范围

V7.37 范围聚焦"后台真实化" — 让数据库、推送、RAG 都展示**真实在工作**的证据。

## 范围边界

### 做（7 项 + 1 个 P0 已闭环）
0. **§E P0** ✅ 已完成：飞书 webhook 真发卡片 (msg_id 已确认)
1. OpenClaw 卡片在设置页（运行状态 + 上次/下次推送 + 最近 5 条历史 + 验证按钮）
2. OpenClaw 推送内容升级为 4 区块（统计量 + LLM 热话题 + RAG 该补 + footer）
3. systemd unit 部署 OpenClaw daemon（板端，环境变量 `WEEKLY_HOUR=17 / WEEKLY_DOW=6 / MORNING_HOUR=9`）
4. /database 默认 `?seed=all`，顶部 chip live/seed/all，底部加每表行数 + 最后写入时间
5. RAG 可视化：聊天回复后跟"参考：xxx"小胶囊，点击展开 popup
6. 调试 tab 代码图 → 4 个 GitHub 外链卡（仓库 / 当前分支 / 本轮 sha / 依赖图），删 force-graph CDN 引用
7. GitHub repo `qqyyqq812/IronBuddy` 全面 push：secret 扫描 + .gitignore + push main + README/CONTRIBUTING 更新

### 不做
- 云端 GPU 切换"成功路径"（用户需要在云端 GPU 上跑 RTMPose 服务 + 配真实 URL，不是代码改动）
- 不动语音/FSM 行为（lane_a 16:48 在持锁，本次窗口内不抢锁）
- 不 wire `voice/router.py`
- 不测"请关机"
- 不强 push history / 不重写 git 历史（如果发现已 commit 的 secret，rotate 而不是 filter-repo）

## A. OpenClaw 卡片 + 后端

### 后端新增（streamer_app.py）
```python
@app.route('/api/openclaw/status')          # 已有，扩字段
@app.route('/api/openclaw/once', methods=['POST'])  # 触发 once
@app.route('/api/openclaw/history')         # 最近 5 条 status
```

`/api/openclaw/status` 现有返回扩展为：
```json
{
  "ok": true,
  "presentation_name": "OpenClaw 后台提醒",
  "daemon_running": true,
  "daemon_pid": 12345,
  "last_push_ts": 1777798247,
  "last_push_mode": "weekly",
  "last_push_ok": true,
  "next_push_ts": 1777801800,
  "next_push_mode": "evening",
  "weekly_hour": 17,
  "morning_hour": 9
}
```

### 前端（templates/index.html 设置页底部）
新增一个 `.workbench-card`：
- 顶部：标题 "🦞 OpenClaw 后台推送" + 子标题"通过数据库归总自动反哺 RAG"
- 状态行：daemon 运行中 / PID / 上次推送时间 / 下次预计时间
- 历史区：最近 5 条 `mode + ts + ok` 表格
- 按钮：`验证推送（dry-run）` / `立即推送（weekly --send）`

## B. OpenClaw 推送内容升级

`scripts/opencloud_reminder_daemon.py` 的 `build_reminder_text` 替换为 4 区块构造器：

```python
def build_reminder_card(mode, snapshot, db_path, board_online):
    """Returns interactive card with 4 blocks."""
    # Block 1: 训练统计（从 fsm_state snapshot 拿 good/comp/failed）
    # Block 2: LLM 热话题 Top 3（SELECT user_text FROM llm_log GROUP BY user_text ORDER BY count(*) DESC LIMIT 3）
    # Block 3: RAG 应补的知识点（SELECT user_text FROM llm_log WHERE rag_hits=0 ORDER BY ts DESC LIMIT 3）
    # Block 4: footer 推送时间 + 下次预计 + version
```

Feishu 卡片 schema 改用 `template_blocks: [hr, markdown, hr, markdown, hr, action]` 形式。

数据来源：
- 训练统计：`/api/fsm_state` 实时（已经在用）
- LLM 热话题：`SELECT user_text, COUNT(*) c FROM llm_log WHERE ts >= datetime('now', '-7 day') GROUP BY user_text ORDER BY c DESC LIMIT 3`
- RAG 应补：`SELECT user_text FROM llm_log WHERE COALESCE(rag_hits,0)=0 AND ts >= datetime('now', '-7 day') ORDER BY ts DESC LIMIT 3`

## C. systemd unit

`systemd/ironbuddy-openclaw.service`（仓库内新文件）：
```ini
[Unit]
Description=IronBuddy OpenClaw reminder daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/toybrick/streamer_v3
Environment=IRONBUDDY_BOARD_URL=http://127.0.0.1:5000
Environment=IRONBUDDY_WEEKLY_HOUR=17
Environment=IRONBUDDY_WEEKLY_DOW=6
Environment=IRONBUDDY_MORNING_HOUR=9
Environment=IRONBUDDY_EVENING_HOUR=21
ExecStart=/usr/bin/python3 -u scripts/opencloud_reminder_daemon.py --loop --send --interval 60
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

板端部署：
```bash
sudo cp systemd/ironbuddy-openclaw.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ironbuddy-openclaw
sudo systemctl start ironbuddy-openclaw
```

如果 toybrick 没有 sudo 权限，**回退方案**：tmux 起 daemon `tmux new-session -d -s openclaw 'cd /home/toybrick/streamer_v3 && python3 scripts/opencloud_reminder_daemon.py --loop --send'`。

## D. /database 真实化

### 默认 view 改 `?seed=all`
- `templates/database.html:349` `getSeed()` 默认从 `URL_PARAMS.get('seed') || 'all'`
- 顶部 chip：`[live] [seed] [all]` 三态切换，URL 同步

### 表底加摘要行
每张表底部加一行：
```
共 9849 行 · 最后写入 2026-05-03 08:29:50
```
后端 `/api/db/tables` 加 `last_ts` 字段（按各表自动选 `created_at|ts|updated_at|timestamp`）。

## E. RAG 可视化（聊天气泡）

### 后端
chat_reply 已有 `manual_reply / hits` 字段（`/api/coach/rag_query` 已支持），现在让 `voice_daemon` 在生成回复时也把命中卡片附在 `voice_turn.json.rag_hits`：
```json
{"turn_id":"abc","reply":"...","rag_hits":[
  {"id":"wake_rule","title":"唤醒规则","source":"manual"}
]}
```

### 前端
`templates/index.html` 聊天气泡渲染逻辑加：if `rag_hits` 非空 → 在气泡下面 inline 一行 `参考：唤醒规则 · 模式切换`，每个为可点击 chip，点击调 `/api/coach/rag_query?id=wake_rule` 弹出 popup（不刷页面）。

## F. 代码图改 GitHub 外链

`templates/index.html` 调试 tab：
- 删除 `<script src="https://unpkg.com/force-graph@1.43.5/dist/force-graph.min.js"></script>`
- 删除 `loadCodeGraph` 函数体（保留空函数防 onclick 报错）
- `#codeGraphMount` 替换为 4 卡片网格：
  ```
  ┌──────────────┬──────────────┐
  │ GitHub 仓库  │ 当前分支     │
  │ qqyyqq812/...│ main         │
  └──────────────┴──────────────┘
  ┌──────────────┬──────────────┐
  │ 本轮版本 sha │ 依赖图       │
  │ 6febca2      │ deps view    │
  └──────────────┴──────────────┘
  ```
  每卡片是 `<a target="_blank">` 直接打开对应 GitHub URL：
  - 仓库：`https://github.com/qqyyqq812/IronBuddy`
  - 分支：`https://github.com/qqyyqq812/IronBuddy/tree/main`
  - sha：`https://github.com/qqyyqq812/IronBuddy/commit/<HEAD>`（前端用 `/api/code_graph` 返回的 commit 字段）
  - 依赖图：`https://github.com/qqyyqq812/IronBuddy/network/dependencies`

`tools/build_code_graph.py` 保留（CLI 工具仍能跑），`/api/code_graph` 保留（数据可能给 GitHub Action 用）。

## G. GitHub push 安全协议

### 前置 secret 扫描
```bash
# 1. 列追踪文件中含敏感关键字的
git ls-files | xargs grep -lE 'API_KEY|APP_SECRET|PASSWORD|WEBHOOK|seetacloud\.com|OmloGl2JXBK0' 2>/dev/null

# 2. 历史 commit 中含敏感 string 的
git log --all -p | grep -E 'OmloGl2JXBK0|seetacloud\.com.*password|FEISHU_APP_SECRET="|BAIDU_SECRET' | head -20

# 3. .gitignore 检查
grep -E '\.api_config\.json|data/runtime|data/ironbuddy\.db|\.deploy_backups|\.archive' .gitignore
```

### 处理矩阵
| 发现 | 处理 |
|---|---|
| 当前 working tree 有 secret 文件 | 加 .gitignore，不 commit |
| 已 commit 但未 push | git rm --cached + .gitignore + amend |
| 已 push 到远端 | **rotate secret**，**不强 push history**（用户原话"内容应该很久没更新了"，可能远端没敏感数据） |
| 上次会话 SSH 密码贴在聊天 | 已提示用户 rotate，不处理 |

### push 流程
1. `git status` 列工作区
2. 三批 commit：
   - C1: 文档（design + plan + handoff 索引）
   - C2: V7.37 后端代码（streamer_app.py + scripts/opencloud_reminder_daemon.py + systemd/）
   - C3: V7.37 前端代码（templates/*.html）
3. `git push origin main` （首次 push 用 -u）
4. 写 README 顶部"安装 / 启动 / 拍摄演示"三步

## H. 任务拆分

| 阶段 | 任务 | 估时 | 部署 |
|---|---|---|---|
| §E P0 | 飞书 webhook 验证 ✅ | -- | 已完成 |
| 1 | OpenClaw 后端 status/once/history + 卡片前端 | 1h | 部署 |
| 2 | OpenClaw 4 区块卡片内容 | 1h | 部署 |
| 3 | systemd unit + 板端 enable + 立即起 loop | 30min | 部署（如无 sudo 用 tmux 兜底）|
| 4 | /database 默认 view + chips + 行数 | 30min | 部署 |
| 5 | RAG 胶囊（chat_reply 渲染 + popup）| 1h | 部署 |
| 6 | 调试 tab GitHub 外链卡 + 删 force-graph | 30min | 部署 |
| 7 | GitHub repo push（secret 扫描 + push）| 1h | 仅本地 + GitHub |

每阶段完成后：
1. 本地 `pytest` 绿
2. CURRENT.md 拿 `claude_code` 锁（如 lane_a 已释放）
3. rsync + 备份 + 烟测
4. 释放锁
5. 写部署摘要

## I. 拒绝项 + 风险

- **不动 lane_a 在持的锁**（FSM pending rep 修复，16:48 起）。如果 lane_a 还在持锁，§1-7 部署等它释放。
- **不强 push GitHub history**（如果发现旧 commit 已含 secret，rotate 而不是 filter-repo）
- **systemd 部署若失败**回退到 tmux 启动（这是 Toybrick 板端兼容性约束）

## 参考

- `scripts/opencloud_reminder_daemon.py` 已存在，本次升级 build_reminder_card
- `hardware_engine/integrations/feishu_client.py` 已支持 app_id+secret+chat_id 模式
- `templates/database.html:349` `getSeed()` 默认逻辑改一行
- 16:50 飞书发送成功证据：`msg_id=om_x100b504488a104acb395b144eb6423e`
