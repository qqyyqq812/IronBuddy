# ECC Skills 使用纪律（IronBuddy 项目）

不在 CLAUDE.md 预先列举 skill 名字（每次会话头部白嫖 token 没收益）。按场景按需触发，CC 自动匹配 SKILL.md description。

## 场景 → Skill 速查

| 触发场景 | Skill |
|---|---|
| 写 LaTeX 报告 | `academic-writing-cs` |
| Python 重构 / 审查 | `python-expert-best-practices-code-review`, `python-patterns`, `python-testing-patterns` |
| 现状梳理 / 跨文件调用图 | `claude-mem:smart-explore`, `claude-mem:pathfinder` |
| 前端重写（Phase F）| `frontend-design`, `dashboard-builder` |
| PPT 编辑 | `pptx`, `frontend-slides` |
| 决策卡壳 | `brainstorming`, `problem-solving`, `council` |
| 长任务规划 | `writing-plans`, `executing-plans`, `blueprint` |
| 重构后验证 | `verification-loop` |
| 写 commit/PR/章节前 | `prompt-optimizer` |
| 安全审查 | `security-review`（Flask 接口、API key 处理时）|
| 写测试 | `tdd-workflow`, `python-testing` |
| 生成 CodeTour | `code-tour` |

## 不要这样做

- 不要在 CLAUDE.md 顶部列 skill 清单 —— CC 自己会读 SKILL.md description 匹配，重复列只浪费 context。
- 不要主动 `Skill skill=xxx` 调用，除非用户明确要求或场景高度匹配。
- 不要把 skill description 复述到 prompt 里 —— skill 加载时会自动注入。

## 何时打开 find-skills

忘了哪个 skill 干啥时，用 `find-skills` 让 CC 帮你找。
