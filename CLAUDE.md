# IronBuddy (Embedded-Fullstack)

**Project Core**: 边缘侧 AI 健身教练，基于 RK3399ProX 板端环境，集成 YOLOv5-Pose (RKNN)、Flask 一体化前端、Baide AipSpeech 语音流与 DeepSeek 直连推理。

## 导航索引 (Navigation)
- **技术手册与系统挂载**：详见 `docs/technical/architecture.md` (前身记忆核心)。
- **任务面板 (TODOs)**：详见 `docs/task_board.md`。
- **历史记录与拓扑图**：旧版实体关系与迭代方案归档至 `docs/review_implan/`。
- **硬件联调手册**：详尽部署文档见 `docs/hardware_ref/`。

## 代码原则与底层边界 (Boundaries)
- 本地项目私有法则位于 `.claude/rules/`，执行修改时遵循其中的板端排错铁律。
- 执行一切文件操作与开发任务时，严格通过 ECC 标准技能/验证工作流触发。
