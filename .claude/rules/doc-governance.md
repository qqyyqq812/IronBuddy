# 文档治理规则 (IronBuddy)

## 禁止生成的文档类型

严禁生成以下文档：

1. **AI 交接文档（三大禁止目录）**
   - 禁止：`docs/handoff/`, `docs/handover/`, `docs/工作区总控台/交接文档/`
   - 禁止：任何 `handoff_*.md`, `交接_*.md` 文件
   - 理由：项目结束后无价值，应删除

2. **多 Agent 协作日志**
   - 禁止：`docs/parallel/`（三个 Agent 的各自工作记录）
   - 禁止：`docs/review_implan/`（review 实现计划）
   - 理由：临时工作分工，不是永久技术文档

3. **规划与执行文档**
   - 禁止：`EXECUTION_PLAN_*.md`, `SPRINT_*.md`, `FINAL_*.md`
   - 禁止：`*状态汇总.md`, `*处理清单.md`
   - 理由：规划完成后过期

4. **临时演讲与调研**
   - 禁止：`docs/presentations/`（PPT 稿、演讲草稿）
   - 禁止：`docs/research/`（技术调研）
   - 理由：外宣临时资料，不是代码文档

5. **使用指南与运维**
   - 禁止：`可视化面板使用指南.md`, `测试计划_*.md`, `task.md`, `task_board.md`
   - 理由：项目后期不需要这些文档

---

## 永久文档标准

### 核心技术文档

| 文档 | 用途 | 更新频率 |
|-----|------|---------|
| `decisions.md` | 架构决策 + 踩坑 | 关键代码改动时 |
| `architecture.md` | 系统整体架构 | 偶尔 |
| `数据采集与训练指南.md` | GRU 训练流程 | 定期更新 |
| `sEMG泛化实现指南.md` | 肌电特征工程 | 偶尔 |
| `IronBuddy_Deployment_Guide.md` | 部署快速开始 | 重要保留 |
| `cc-updates.md` | AI 自动维护日志 | 自动 |

### 硬件参考

- `docs/hardware_ref/` 可保留（真实硬件参考文档），但应精简（删除驱动工具包等）

### 禁止的非文档

- 删除所有 `.pptx`, PowerPoint 源文件
- 删除 Windows 驱动工具包（FlashTool 等）

---

## 执行规则

1. **临时文档**：生成到 `/tmp/` 或 Colab session，不提交项目
2. **发现禁止文档**：
   - 检查是否有关键内容可提炼
   - 并入 `decisions.md` 后删除原文件
3. **月度检查**：`find docs -name "*.md" | wc -l` 应保持 < 10 个

---

## Board 环境约束（补充）

见 `.claude/rules/toybrick_board_rules.md`：
- Python 3.7 限制（无 `X | None`, 无 `pandas`）
- 进程管理红线（nohup, pgrep, kill 双重判定）
- 硬件调用约定（HDMI, 音频, NPU 置信度阈值）

---

## 参考

- 主要决策：`docs/technical/decisions.md`
- 架构说明：`docs/technical/architecture.md`
- 部署指南：`docs/technical/IronBuddy_Deployment_Guide.md`
