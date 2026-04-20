# IronBuddy UI 推流模块完整调试记录（V7.3 → V7.16 黄金版）

> **本文件为 UI / 推流网页模块的唯一权威源**。之后任何 UI / 骨架渲染 / 推流顺滑度 / 设置页 / 顶部状态条 / 底部总计 / 诊断测试区 的修改，**必须追加到这里**。换会话 / 换窗口做 UI debug，把这一个文件塞进上下文就够。
>
> **验收状态：持续迭代中**。V7.6 为最新一次落地（2026-04-20 凌晨）。
> 改动物理位置：**单文件** `templates/index.html`（~3400 行），其他模块零改动。
> 部署路径：`rsync` 到板端 `toybrick@10.18.76.224:/home/toybrick/streamer_v3/templates/index.html`，Flask 自动重载 template，浏览器 Ctrl+Shift+R 硬刷即可。

---

## 一、最终 UI 架构（V7.6 当前生效）

```
┌───────────────────────────────────────────────────────────────┐
│ HEADER（52px 固定）                                            │
│   ┌─ logo      ── [mode-segbar 居中三段] ──  🔊  ● 在线 ──┐  │
│   │  IronBuddy     深蹲│本地│纯视觉                      │  │
│   │                （纵轴与底部总计对齐）                  │  │
│   └──────────────────────────────────────────────────────┘  │
├───────────────────────────────────────────────────────────────┤
│ MAIN LAYOUT (flex row)                                        │
│                                                                │
│ ┌─ LEFT PANEL (flex:1) ─┬─ CENTER KIN-COL (260px) ─┬─ SIDEBAR ─┐│
│ │ • 视频容器            │ • KINEMATICS 骨架       │ 控制台/日志││
│ │   HUD: 状态/角度/fps  │   - rigGlow             │ 数据/设置 ││
│ │ • 2× gauge (ROM/AI)   │   - rigFlash 浮卡       │          ││
│ │ • 5× stat-card        │   - kin-scaler          │          ││
│ │   标准/违规/代偿/     │       └─ kin-rig-holder │          ││
│ │   时长/疲劳积分        │             └─ rigBody  │          ││
│ │ • 疲劳进度条           │   - kin-hud-bar         │          ││
│ │ • 3× action-row        │     Action/Angle/Status │          ││
│ └───────────────────────┴─────────────────────────┴──────────┘│
├───────────────────────────────────────────────────────────────┤
│ STATUS BAR（34px 固定）                                        │
│         总标准 · 总违规 · 总代偿（居中三格，与顶部对齐）      │
└───────────────────────────────────────────────────────────────┘

        数据通路：
        templates/index.html
            │
            ├─ Web Worker（150ms）──→ GET /state_feed
            │                          └─ 读 /dev/shm/fsm_state.json
            │
            ├─ setInterval 3s  ──→ GET /api/muscle_activation
            ├─ setInterval 2s  ──→ GET /api/chat_reply / chat_input
            ├─ setInterval 1s  ──→ GET /api/fsm_state (头部标签)
            ├─ setInterval 5s  ──→ 控制台/日志/overview
            └─ <img src> ──────→ MJPEG :8080（视觉进程直推）
```

---

## 二、模块职责一览

| 模块 | 元素 ID / 类名 | 数据源 | 刷新节奏 |
|---|---|---|---|
| 顶部模式条 | `.mode-segbar > .seg#tagExercise/#tagVision/#tagInferenceMode` | `/api/fsm_state` + `/api/vision_mode` | 1s |
| 视频流 | `#videoFeed` | `http://<board>:8080/mjpeg` → fallback `/video_feed` | 流式 |
| 视频 HUD | `#hudState / #hudAngle / #hudFps` | `/state_feed` | 150ms |
| ROM / AI gauge | `#ampCircle / #nnCircle + #ampText / #nnText` | `/state_feed` | 150ms |
| 5× 统计卡 | `#statGood / #statFailed / #statComp / #timerValue / #statFatigue` | `/state_feed` + 前端计时 | 150ms |
| 疲劳条 | `#fatigueBar` | `/state_feed`.fatigue / fatigue_limit | 150ms |
| **中柱骨架展示舱** | `#kinematicsRigStage` | `/state_feed`.angle（经 `filterAngleForRig` 平滑） | 150ms |
| ↳ 浮卡 | `#rigFlash`（`.rig-flash-anim` 950ms 动画） | state 从非 BOTTOM/TOP 进入 BOTTOM/TOP，或 counter 递增 | 事件驱动 |
| ↳ 肌肉底色 | `#rigThigh/#rigCalf`（深蹲）/ `#rigUpperArm/#rigForearm`（弯举） | `(fatigue / limit) + emg × 0.25` → `_rigLerpColor(灰→红)` | 150ms |
| ↳ 底部 HUD | `#kinHudAction / #kinHudAngle / #rigStatusText` | 镜像既有 DOM（250ms 轮询） | 250ms |
| 底部总计 | `#sbTotalGood / #sbTotalFailed / #sbTotalComp` | `/state_feed`.total_good/_failed/_comp | 150ms |
| 设置页保存按钮 | `.save-btn` | POST `/api/admin/api_config` | 按钮触发 |
| 诊断组 1（云端 RTMPose） | `.diag-group` + `.diag-btn` ×2 | `/api/admin/cloud_verify`、`/api/admin/reload_service` | 按钮触发 |
| 诊断组 2（百度语音） | `.diag-group` + `.diag-btn` ×3 | `/api/admin/reload_service`、`voice_test`、`voice_diag` | 按钮触发 |

---

## 三、版本演进史（UI 层根因突破表）

| 版本 | 问题 | 根因 | 修复 |
|-----|-----|-----|-----|
| **V7.3** | 顶部 3 状态标签散落在 logo 右侧，与底部 3 总计不对齐；整体太"AI 风"（发光胶囊、彩色大按钮） | status-tag × 3 独立胶囊 + 散乱彩色 big-btn | 新增 `.mode-segbar` 居中分段条（1 个 pill 容器串三段，细分隔线 + accent 下划线激活态）；header 加 `position:relative` 让 absolute 居中生效 |
| V7.3 | 中柱"骨架渲染" 200px 宽、320px 高，下方大量留白，颜色蓝紫荧光像 demo | `.center-panel{flex:0 0 200px}` + rig 固定 320px + 鲜色 gradient | `.kin-col{flex:0 0 260px}`；`.kin-stage{flex:1; min-height:360px}` 占满整列；rig 内部全改钛金属灰 `#94a3b8→#475569`；shadow 换 `inset 1px 高光 + 投影` 去荧光；底部新增 `.kin-hud-bar` 3 格 |
| V7.3 | 设置页底部 5 个大色块按钮（蓝紫绿琥珀混乱） | `big-btn` + inline `background:var(--accent-*)` 乱配 | `.diag-group` 两张卡（云端 RTMPose / 百度语音）；outline 风格 `.diag-btn`（透明底 + 边框，hover 才亮 accent） |
projects/embedded-fullstack/docs/验收表/弯举神经网络权威指南.md| **V7.4** | 中柱骨架小人偏上不居中，头部被裁 | `.kin-scaler{transform:scale(1.55); transform-origin: center 52%}` —— 原点 52% 导致缩放时 head(-30 偏移)被压出顶部 | 改 flex 居中 + 固定尺寸 holder：`.kin-scaler{display:flex; align-items:center; justify-content:center}` + 新 `.kin-rig-holder{width:60px; height:260px; transform:scale(1.3)}` |
| V7.4 | 保存按钮文字消失（只剩 💾） | `.big-btn.start { color:var(--accent-green) }` + inline `background:var(--accent-green)` = 绿字绿底 | 新 `.save-btn`：accent 底 + 深绿 `#062a23` 文字，足够对比度 |
| V7.4 | 到位无闪卡反馈、肌肉底色不会随疲劳变化 | `updateRig()` 只管角度 + glow，没有浮卡 DOM，肌肉 gradient 是 inline 静态 | 新 `<div id="rigFlash">` + CSS 950ms `@keyframes rigFlashPop` 缩放淡入淡出；`_rigLerpColor` 灰↔红线性插值，每帧喂 `thigh/calf/upperArm/forearm.style.background` |
| **V7.5** | 骨架 / HUD 肉眼可感"动一下停一下"卡顿；快速连做多个 rep 会"突然一次跳 3 个" | Web Worker poll = **500ms**（2Hz），CSS transition = 150ms → 动 150ms 停 350ms；额外串联了 `/api/voice_debug` fetch 但主线程从未使用（dead fetch） | Worker poll `500 → 150ms`，删除 voice_debug 整段；showRigFlash 接 `delta` 支持 `+N` 显示 |
| **V7.6** | 按 V7.5 推完后**闪卡完全不显示** | `_rigLastGood/_rigLastFailed` 卡在上个 session 的高水位（fatigue 达标自动清零后板端 good=0 但前端变量=N，`good > _rigLast` 永假） | 加 reset 守卫：`if (good < _rigLastGood) _rigLastGood = good`，同理 failed |
| V7.6 | 闪卡时机在起身那一刻（counter ++），比用户预期的"蹲到底瞬间"晚半个 rep | counter 递增绑定 `ASCENDING → STAND`，不是 `BOTTOM` | 在 `syncWorker.onmessage` 加 state 转移检测：`(cur==BOTTOM\|TOP) && (last!=BOTTOM&&last!=TOP)` → `triggerFlash`；counter 递增仍保留作兜底，两路共用 300ms 去重窗口 |
| V7.6 | NPU 4fps + 关键点噪声 → 骨架抽搐横跳 | 本地 NPU 推理稀疏 + `MIN_KPT_CONF=0.08` 低置信度关键点误差 ±5-10° | 客户端加 `filterAngleForRig` = **median(3) + EMA(α=0.5)**；仅喂给 rig 可视化，HUD 大数字 / FSM 判定 / classification 完全走原值，数据完整性零损失；视觉代价 ~150-200ms 滞后 |
| **V7.7** | 顶部第三段 `tagInferenceMode` "纯视觉 ↔ 视觉+传感" 每秒横跳 | **两个轮询同时写同一个元素**：3s 的 `/api/inference_mode`（读 `/dev/shm/inference_mode.json` = 用户意图真源）+ 1s 的 `updateHeaderTags` 里 `/api/fsm_state`.inference_mode（FSM 观察态，读意图文件后才更新，有 lag）。两文件同步期间写不同值 → 每秒来回刷 | **单一权威源化**：删除 `updateHeaderTags` 里 tagInf 分支，inference_mode.json 成为 chip 唯一写入源；同时把 `/api/inference_mode` poll 由 3s 提速到 1s，语音切换后 UI 反应与其他标签同频 |
| **V7.16** | 骨架抖动 + "不标准"浮卡**持续误触发**（用户站立不动也弹红色浮卡） | **三重**：① 两个 FSM 缺 rep 级消抖 —— 单帧 NPU 噪声让 smoothed mean 掉破 140° 再回 150° 即可伪造完整 rep，bottom 被锁在噪声角度值触发 `failed++` + 写 `violation_alert.txt`；② 客户端 `d.state === 'BOTTOM' \|\| 'TOP'` 分支为 V7.6 留下的死码（FSM 从未发射 BOTTOM/TOP 状态）；③ `filterAngleForRig` 平滑强度不够（NPU 4fps + `MIN_KPT_CONF=0.05` 噪声残留大） | **服务端四级防抖**：`_get_trend()` 门控（入场要 2 连续 falling，离场要 2 连续 rising）+ 最小 rep 时长 0.5s + rep 冷却 0.8s + 安全边距 angle>150°（由 >145°）；**同步改造 DumbbellCurlFSM**；**客户端死码清理** + `filterAngleForRig` 升级 median(3→5) + EMA α(0.5→0.3) + **速度钳位 45°**（单步 >45° 认为关键点误检，丢弃保持上一值）|

---

## 四、关键文件锚点

### 板端 `/home/toybrick/streamer_v3/templates/index.html`

| 区段 | 行号（V7.6） | 内容 |
|---|---|---|
| CSS 设计 token | L23-81 | 颜色 / 间距 / 字号 / 玻璃模糊 / 语义色中间层 |
| CSS 响应式 | L1210-1228 | `@media (max-width:900px)` 移动端 |
| **CSS V7.3 增量**（UI 重做的核心） | L1232-1410 | `.mode-segbar` / `.kin-col` / `.kin-stage` / `.kin-scaler` / `.kin-rig-holder` / `.kin-hud-bar` / `.kin-hud-cell` / `.diag-group` / `.diag-btn` / `.diag-status` / `.save-btn` / `.rig-flash` / `@keyframes rigFlashPop` |
| HEADER HTML | L1416-1435 | mode-segbar 居中 |
| LEFT 视频+gauge+stats+action | L1447-1572 | 无大改，仅 stat-card / btn-action 样式受 V7.3 影响 |
| **CENTER 骨架展示舱** | L1575-1612 | `.kin-col` + `.kin-stage` + `rigFlash` + `kin-scaler` + `kin-rig-holder` + `kin-hud-bar` |
| SIDEBAR（控制台/日志/数据/设置） | L1617-1770 | 设置 Tab 底部改为 `.diag-group` ×2；保存按钮用 `.save-btn` |
| STATUS BAR | L1774-1788 | 3 总计，居中 |
| **Web Worker 代码** | L2722-2760 | **150ms** 轮询 `/state_feed`；已剥离 voice_debug |
| `syncWorker.onmessage` | L2763-2910 | 主线程接数据；V7.6 在 L2870-2876 加 state-based flash 触发 |
| `filterAngleForRig` + `triggerFlash` + `showRigFlash` | L2928-2990 | V7.6 / V7.5 / V7.4 平滑+调度+浮卡 |
| `updateRig()` | L2995-3100 | 核心动画；V7.6 reset 守卫 + triggerFlash 路由；V7.4 muscleGrad 背景喂入 |
| 头部标签轮询 | L3440-3460 | `updateHeaderTags` 每 1s 拉 `/api/fsm_state` + `/api/vision_mode`；V7.3 给镜像注入 `kinHudAction/kinHudAngle` 250ms |

### 前端核心全局状态量（V7.6 完整列表）

```js
// Rig 动画状态（~L2917-2945）
let _rigLastGood = 0, _rigLastFailed = 0;      // counter 基线（V7.6 加 reset 守卫）
let _lastClassification = null;                 // FSM 最新分类
let _muscleFlashUntil = 0;                      // 肌肉 flash 窗口结束时戳
let _muscleFlashType = null;                    // 'good' | 'bad'
let _lastFlashTime = 0;                         // V7.6: 300ms 去重锁
let _lastFsmState = null;                       // V7.6: state-based 触发比较基
let _rigAngleBuf = [];                          // V7.6: median(3) 滑窗
let _rigEmaAngle = null;                        // V7.6: EMA 状态
let _compCount = 0, _lastRepTotal = 0;          // 代偿累计（前端兜底，FSM 后续补 d.total_comp 已覆盖）
var _latestActs = {quadriceps:0, glutes:0, calves:0, biceps:0}; // EMG 缓存（驱动骨架阴影强度）
```

---

## 五、当前生效核心代码片段（V7.6 源码快照）

### 5.1 Flash 调度器（去重 + 统一入口）

```js
function triggerFlash(flash, classification, delta) {
    var now = Date.now();
    if (now - _lastFlashTime < 300) return;   // 300ms 去重
    _lastFlashTime = now;
    _muscleFlashUntil = now + 450;
    _muscleFlashType = flash;
    showRigFlash(flash, classification, delta);
}
```

### 5.2 State-based 触发（onmessage 内）

```js
if (d.classification) _lastClassification = d.classification;

// V7.6: BOTTOM/TOP 入场瞬间 —— 真正的到底
var _curSt = d.state;
if ((_curSt === 'BOTTOM' || _curSt === 'TOP') &&
    _lastFsmState !== 'BOTTOM' && _lastFsmState !== 'TOP') {
    triggerFlash('good', d.classification || _lastClassification, 1);
}
_lastFsmState = _curSt;
```

### 5.3 角度平滑（median + EMA）

```js
function filterAngleForRig(raw) {
    if (typeof raw !== 'number' || raw <= 0) return raw;
    _rigAngleBuf.push(raw);
    if (_rigAngleBuf.length > 3) _rigAngleBuf.shift();
    var mid = raw;
    if (_rigAngleBuf.length === 3) {
        var s = _rigAngleBuf.slice().sort(function(a,b){ return a-b; });
        mid = s[1];
    }
    if (_rigEmaAngle == null) _rigEmaAngle = mid;
    _rigEmaAngle = 0.5 * mid + 0.5 * _rigEmaAngle;
    return _rigEmaAngle;
}

// 在 onmessage 里
updateRig(filterAngleForRig(d.angle), d.exercise, currentFatigue, d.good || 0, d.failed || 0, fatigueLimit);
```

### 5.4 Counter reset 守卫 + 触发（updateRig 内）

```js
// V7.6: reset 守卫
if (good < _rigLastGood) _rigLastGood = good;
if (failed < _rigLastFailed) _rigLastFailed = failed;

let flash = null;
let flashDelta = 0;
if (good > _rigLastGood)   { flash = 'good'; flashDelta = good - _rigLastGood; _rigLastGood = good; }
if (failed > _rigLastFailed){ flash = 'bad';  flashDelta = failed - _rigLastFailed; _rigLastFailed = failed; }

if (flash) triggerFlash(flash, _lastClassification, flashDelta);
```

### 5.5 疲劳渐变背景（V7.4）

```js
var _RIG_C_GRAY_TOP = [148,163,184], _RIG_C_GRAY_BOT = [71,85,105];
var _RIG_C_RED_TOP  = [239,68,68],   _RIG_C_RED_BOT  = [127,29,29];

var effT = Math.min(1, fp + (emgSel / 100) * 0.25);
var mTop = _rigLerpColor(_RIG_C_GRAY_TOP, _RIG_C_RED_TOP, effT);
var mBot = _rigLerpColor(_RIG_C_GRAY_BOT, _RIG_C_RED_BOT, effT);
var muscleGrad = 'linear-gradient(180deg, ' + mTop + ', ' + mBot + ')';

// Flash 窗口内强制覆盖为三色（绿/琥珀/红）
if (Date.now() < _muscleFlashUntil) {
    if (_muscleFlashType === 'good') {
        if (_lastClassification === 'compensating')       muscleGrad = 'linear-gradient(180deg, #fbbf24, #b45309)';
        else if (_lastClassification === 'non_standard') muscleGrad = 'linear-gradient(180deg, #fb7185, #be123c)';
        else                                              muscleGrad = 'linear-gradient(180deg, #34d399, #059669)';
    } else {
        muscleGrad = 'linear-gradient(180deg, #fb7185, #be123c)';
    }
}

// 应用到对应肌群（深蹲: thigh+calf；弯举: upperArm+forearm）
```

### 5.6 Worker 150ms 轮询（V7.5）

```js
const workerCode = `
    let timer = null;
    self.onmessage = function(e) {
        if (e.data === 'START') {
            if (timer) clearInterval(timer);
            timer = setInterval(async () => {
                try {
                    const res = await fetch(location.origin + '/state_feed', { cache: 'no-store' });
                    if (!res.ok) throw new Error();
                    const d = await res.json();
                    self.postMessage({ type: 'DATA', payload: { d } });
                } catch(err) {
                    self.postMessage({ type: 'ERROR' });
                }
            }, 150);
        } else if (e.data === 'FORCE') {
            (async () => {
                try {
                    const res = await fetch(location.origin + '/state_feed', { cache: 'no-store' });
                    if (res.ok) {
                        const d = await res.json();
                        self.postMessage({ type: 'DATA', payload: { d } });
                    }
                } catch(e) {}
            })();
        }
    };
`;
```

---

## 六、卡顿调优决策树（下次遇到抽搐 / 延迟时）

```
抽搐/卡顿现象
├─ 整站全部卡（含 chat / settings）
│    → 抓 F12 Network，查并发数。8+ 条 setInterval 挤占 6 条 TCP 时，升级 D 方案：分档调度器
│
├─ 只骨架卡，视频也卡，但其他 OK
│    → 几乎必是 NPU 帧率过低。先确认 HUD 右上 fps
│       - <10fps  → 云端模式更优，演示切云
│       - <4fps   → 本地 NPU 异常，查 `/dev/shm/fsm_state.json` mtime 新鲜度
│    → 已有 median+EMA 兜底，再卡就降 CSS transition 150→80ms 加剧响应感（代价：锯齿）
│
├─ 骨架跟手但闪卡不显示
│    → 先看 console 有无 updateRig 报错
│    → 检查 good/failed 数值是否被 reset 为 0 而 _rigLastGood 仍在高位（看 `_rigLastGood` window 变量）
│    → 确认 `/state_feed` 里 d.state 有没有出现 BOTTOM/TOP
│
├─ 闪卡叠图（一次 rep 跳 2-3 个浮卡）
│    → triggerFlash 的 300ms 去重被绕过。查是否多处直接调用 showRigFlash
│    → state 触发 + counter 触发时差 > 300ms 也会双弹，那就加宽到 500ms
│
└─ 视频流卡但 MJPEG 本身板端测试 OK
     → 前端并发太多。确认 worker poll 仍是 150ms 没被误改到更短
```

---

## 七、下次 UI Debug 的 SOP

### 7.1 环境准备
```bash
# WSL 侧，确保能 ssh 板子
ssh -o ConnectTimeout=5 toybrick@10.18.76.224 "echo ok"

# 本地打开项目
cd /home/qq/projects/embedded-fullstack
```

### 7.2 只推前端（**不重启任何服务**）
```bash
rsync -avz -e "ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=no" \
    templates/index.html \
    toybrick@10.18.76.224:/home/toybrick/streamer_v3/templates/index.html
```
Flask 自动重载模板，浏览器 Ctrl+Shift+R 即可。

### 7.3 完整推（含 hardware_engine / 配置）
```bash
bash scripts/一键启动全部终端.sh
```
⚠️ 会重启所有服务，用于板端重启后第一次推。日常 UI 迭代**不要**跑这个。

### 7.4 本机预览（不推板）
```bash
python3 streamer_app.py   # 但需要 /dev/shm 假数据，日常不推荐
```
更实际的方案：直接在浏览器 DevTools 里编辑 CSS/JS 热调样式。

### 7.5 常用排错片段

```bash
# 板端 FSM 当前全量状态（rig 的真实输入）
ssh toybrick "cat /dev/shm/fsm_state.json | python3 -m json.tool"

# EMG 肌肉激活（骨架阴影强度数据源）
ssh toybrick "cat /dev/shm/muscle_activation.json"

# 查看视觉模式
ssh toybrick "cat /dev/shm/vision_mode.json"

# 看视频流 FPS（浏览器 HUD 有）
curl -sI http://10.18.76.224:8080/mjpeg
```

### 7.6 下次换会话开工提示词模板

> 请先读 `/home/qq/projects/embedded-fullstack/docs/验收表/UI推流模块完整调试记录.md`，所有 UI / 骨架 / 推流 / 卡顿 / 设置页相关的上下文都在这里。目前 V7.6 最新。然后我要做……

---

## 八、严格约束（沿用项目级规则）

1. **只动 `templates/index.html`** —— UI 迭代不应牵扯 `streamer_app.py` / `hardware_engine/*` / FSM。任何需要改后端的 UI 需求，**先报警再动**。
2. **不碰既有 ID / 事件绑定** —— updateRig / FSM 回调 / /api/* 路由已稳定。新功能通过新 ID + 新函数挂接，不覆盖原 handler。
3. **函数名 / classList / 元素 ID 受保护**：
   - `rigBody / rigThigh / rigCalf / rigUpperArm / rigForearm / rigGlow / rigStatusText / rigFlash`
   - `tagExercise / tagVision / tagInferenceMode / tagHdmi`
   - `sbTotalGood / sbTotalFailed / sbTotalComp`
   - `statGood / statFailed / statComp / timerValue / statFatigue`
   - `cloudVerifyStatus / voiceDiagStatus / apiConfigStatus`
4. **不新增后端 API** —— 所有前端想要的数据，必须从现有 `/state_feed / /api/fsm_state / /api/muscle_activation / /api/hdmi_status` 里解析或镜像。
5. **保留所有功能** —— 美化 = 换样式，不等于删功能。删按钮前先到 `grep -n "onclick=\|addEventListener\|getElementById" templates/index.html` 核对是否还有依赖。

---

## 九、未竟事宜（下一轮可选优化）

| 优先级 | 方案 | 说明 | 代价 |
|---|---|---|---|
| 🟡 中 | **C. rAF 插值动画** | 用 `requestAnimationFrame` 在两帧 NPU 数据间平滑插补 rig 姿态，替代 CSS transition | 改 updateRig 时序，需回归测试深蹲/弯举 |
| 🟢 低 | **D. 分档调度器** | 把 8+ 条 setInterval 合并为 tier0(150ms)/tier1(2s)/tier2(5s) 三档统一 tick | 影响面大，只在并发明显挤占 MJPEG 时才做 |
| 🟢 低 | 音量滑块 | 设置页补一个音量 slider，读写 `amixer SPK Playback Volume`；需后端开新 endpoint | 明确违反约束 4，需用户明确授权 |
| 🔵 观察 | 浮卡坐标微调 | `.rig-flash { top: 20% }` 可能与缩放后的 rig 头部重叠；若视觉冲突再调到 `10%` 或锚到 `bottom: 90px` |  |
| 🔵 观察 | flash 叠加 EMG 同步特效 | 当 emgSel > 80 且触发 flash 时，肌群发出更强 pulse 辉光；目前 muscleShadow 已部分实现 | 锦上添花，非必要 |

---

## 十、版本修订日志（追加式，新版本放最上）

### V7.16（2026-04-20 下午）—— 状态机 rep 消抖 + 骨架抖动根治
- **`main_claw_loop.py` SquatStateMachine**：新增 3 字段（`_descending_start_ts` / `_falling_frames` / `_rising_frames`）；入场门改为「`angle<140` AND `falling_frames>=2` AND 距上次结账 `>=0.8s`」；结账门改为「`angle>150` AND `rising_frames>=2` AND DESCENDING 时长 `>=0.5s`」；结算后统一清零 debounce 计数并激活冷却锁
- **DumbbellCurlFSM** 对称改造：补齐 `_get_trend()` 方法（原本只有 Squat 有）+ 3 个镜像字段（`_curling_start_ts` / `_closing_frames` / `_opening_frames`）+ 同门控逻辑
- **`templates/index.html`**：删除 V7.6 的 BOTTOM/TOP state 触发块（FSM 从未发射此状态，死码）；删除全局 `_lastFsmState`；升级 `filterAngleForRig` —— 窗口 3→5、EMA α 0.5→0.3、新增**速度钳位 45°**（单步角度变化超过即判为 NPU 关键点误检，保持上一值）
- 视频推流：分析确认已无进一步优化空间（V7.5 已把并发 poll 压到极限），本轮不动
- **零回归**：sync_to_frontend 输出 schema 不变、trigger_buzzer_alert 时机不变（只是不会再因噪声误触发）、GRU 触发链路 / 疲劳积分 / vision_sensor 模式分支全部保留

### V7.7（2026-04-20 上午）—— 顶部模式条横跳修复
- 移除 `updateHeaderTags` 内 tagInferenceMode 写入分支（消除与 /api/inference_mode poll 的竞争写）
- /api/inference_mode 轮询 3s → **1s**（与其他标签同频，语音切换响应更快）
- **单一权威源**：chip 文字仅由 `/dev/shm/inference_mode.json` 经 /api/inference_mode 驱动
- 默认启动显示"纯视觉"（文件不存在时 /api/inference_mode 返回 `pure_vision`，HTML 默认 textContent 也是"纯视觉"）

### V7.6（2026-04-20 凌晨）—— flash 修复 + NPU 抖动平滑
- 修 counter 卡在高水位导致 flash 永不触发
- 新增 BOTTOM/TOP state 触发（真·到底瞬间）
- 新增 `triggerFlash` 300ms 去重
- 新增 `filterAngleForRig`（median3 + EMA α=0.5）给 rig 可视化

### V7.5（2026-04-20 凌晨）—— 顺滑度大幅提升
- Worker poll 500ms → **150ms**
- 剥离 `/api/voice_debug` dead fetch
- flash 浮卡支持 `+N` 显示

### V7.4（2026-04-20 凌晨）—— 骨架功能性增强
- 骨架居中（flex + kin-rig-holder 固定尺寸 + scale(1.3)）
- 保存按钮绿字绿底 bug 修复（新 `.save-btn`）
- 新增 `rigFlash` 浮卡 + `@keyframes rigFlashPop` 动画
- 新增疲劳渐变底色（`_rigLerpColor` 钛灰↔血红）
- 应用到深蹲 thigh+calf、弯举 upperArm+forearm

### V7.3（2026-04-20 凌晨）—— UI 重做第一刀
- 顶部 mode-segbar 居中三段（与底部对齐）
- 中柱 `.kin-col` 加宽 200→260px，骨架展示舱去荧光金属化
- 底部新增 `.kin-hud-bar`（Action/Angle/Status）
- 设置页诊断区从 5 彩色大按钮改为 2 组 outline `.diag-btn`
- 全局 accent 统一为 teal，减装饰 emoji

---

**本文档到此为止**。下一次修改时，把新版本节追加到【九】与【十】下，再在正文对应位置更新代码段与锚点行号。无论什么时候开新会话做 UI debug，只要把这份丢进上下文，上下文自包含。
