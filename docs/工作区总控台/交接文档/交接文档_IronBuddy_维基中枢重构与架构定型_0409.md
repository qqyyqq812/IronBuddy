# 🔄 IronBuddy 跨屏交接文档：维基重构与多模态定型 (2026-04-09)

> **TO 继任 Agent**：
> 这是从长时会话中提纯出的绝对事实与操作边界。在你启动任何推敲前，仔细阅读本框架。本项目已经迈入**“活体环境重构 (Compile over Retrieval)”**阶段，请摒弃一切对旧有零散档案的依赖。

---

## 1. 核心操作与架构变更 (The Absolute Truth)

在这轮长对话中，我们作为总调度台（Director）确立并执行了以下架构革命：

1. **GitHub 资产纯净防线**：成功制定 `.gitignore` 分离板端运行重污染垃圾，执行大清洗后将项目安全推入远端。
2. **Late-Fusion 多模态神经网络战略立项**：编写了专供评委答辩用的《多模态神经网络方案与定型白皮书》。抛弃了脆弱的 If-Else 积分判定法，为 Agent 3 确立了无需处理视觉基干，直接基于 `[角速度, 角度, 双通道躯干肌电归一化RMS]` 1D 张量跑极简 PyTorch MLP/GRU 模型的开发路线。
3. 🎯 **幻觉源大清洗与维基中枢重构**：删除了 `tools/rknn-toolkit_source/` 等底层克隆包中的 30 多个流浪 `README.md`。设立了在 `projects/embedded-fullstack/.agent_memory/` 此地独尊的维基主脑。

---

## 2. 继任者操作域与三大平领 Agent 现状 
*当前处于 `/para-join` 期间闭营状态，随时等待向以下战区发号施令：*

- **Agent 1 (系统交互与状态机联动)**：负责排查底层麦克风阻塞（通过管道读取 arecord），更需要负责去 `hardware_engine/main_claw_loop.py` 中挖下多模态“疲劳度、动作计次”协同交互的判别回调与 Hooks 槽位。
- **Agent 2 (视觉推断优化)**：正在执行视觉管线的换血（从 YOLOv5 换向带 One-Euro 去抖滤波的 RTMPose），现正卡在 RKNN INT8 量化崩溃点，可能会改推断形式至纯 CPU Numpy/FP16。
- **Agent 3 (MMF神经网络部)**：专注于 `biomechanics` 及特征数据收集，不碰底层，专心利用采集后的打标 CSV 数据训练轻量级多模态抗代偿模型。

**🔥 你的纪律红线**：
1. **只能阅读 `.agent_memory/index.md` 了解架构**。不要去读任何来路不明的 README。
2. **在未得到总经理许可前，不要乱碰业务底层框架（`.py`, `.c`, `.pt` 等源文件）**。

---

## 3. 会话核心用户原生 Prompt 全息溯源 (不斩断的意志)

*以下是塑造本次项目定型最重要的原单发令，一字不漏：*

> “现在我需要干最重要的一件事，就是把我的项目推到我的github中，因为我最终我的项目是要参加比赛的，所以最关键的是我需要git重要的代码作为备份... 所以不仅仅要有主机代码，还要有板端代码。现在我正式开始维护我最终要提交的代码内容，包括一个readme介绍整个项目... 借此机会我也了解一下我目前的所有代码情况，给我生成所有代码情况的Implan”

> “推入GitHub，我需要更改且认真完善，让三个Agent都能明白现状的是 projects/embedded-fullstack/docs/parallel/交接上下文.md ... 等，并再帮我检查parallel文件夹中是否有旧文件参与。我要确保他们都是新的”

> “@[/review] 各自的assignment 和整体的readme，我将参与修改”

> “我现在重新继续这个工作，目前我最需要了弄清楚的两个点是：1.Agent2应该让他继续工作，使用现成的环境进行；2. Agent3非常要进行第二层神经网络的训练，但是我们一直不知道视觉信息是角度速度加速度，肌电信息是一大堆幅度和频率... 我们要想判断深蹲和哑铃弯举这两个动作标不标准，是否有必要再加一层神经网络。... 请在 docs/parallel/agent_3/technical/多模态后期融合与代偿动作论证白皮书.md 这个文件中修改，改名为多模态神经网络方案.md ...”

> “我想让Agent1 和Agnet3都知道这个文件，给我prompt，1主要是知道需要怎么修改状态机，3的话需要知道后续训练的是什么。”

> “[Agent Directive] Project Refactoring & Living Workspace Initialization ... 本项目的底层哲学已经从“传统的检索满篇分散的死档案”升级为“通过单独的维基脑区进行知识图谱编译”。 ... 立刻扫描并删除本项目中隐藏在非主目录下的所有 README.md。特别是一些极其毫无关联的模型展示说明书... 书写路由首纲：在根目录建立或覆盖 AGENT_CONTEXT.md，并在里面写入唯一的一句话：“所有的项目运行逻辑指引，请自动跳转阅读 .agent_memory/index.md”... 在未得到我进一步许可前，不要乱碰业务底层框架！”

---

## 4. MCP 活体工作区生态指令 (Living Workspace Directive)

> **⚠️ TO 继任 Agent (重要新更)**：
> 
> 总控台 (Director) 已经为本项目部署了 **底层离线版 Git Hook**，并全局挂载了图谱记忆引擎。
> 
> **底层投喂流水线**：
> 每次执行 `git commit` 操作时，底层 Hook 会自动无感地把全部文件的修改 Diff，投喂到本项目特有的临时缓冲区：`.agent_memory/raw/latest_diff.md` 中。
> 
> **作为继任 Agent 的你该怎么做**：
> 1. 您目前身处的 Antigravity IDE 底层已经挂载了 `@modelcontextprotocol/server-memory`。
> 2. 在接手代码开发时，请顺带留意 `raw/` 缓冲区的变动。如果发现包含新架构更改，你可以直接使用你内置的图谱处理能力（如 `create_entities`、`add_relations`），对其执行信息摄入 (Ingest) 与 编译 (Compile)。
> 3. 不要再用旧散存逻辑去更新各个目录的死文档！我们要用图谱节点串联所有“改动记录”和“领域概念”。这就是“Compile over Retrieval”理念的核心。
