# 工具链权衡记录（IronBuddy 项目）

记录已评估过的工具的 tradeoff，让未来会话不重新踩点。

## rtk-ai/rtk（token 节流代理）— 暂不全局开

**它做什么**：bash hook 拦截 `git status` / `cat` / `ls` / `cargo test` 等命令，过滤+分组+去重压缩输出 60-90%（118k → 24k tokens / 30 分钟会话），<10ms 开销。

**为什么不全局开**：
1. **板端调试丢关键行**：toybrick 的 `pgrep -f "[c]loud_rtm"` / `ss -tlnp | grep pid=` / `ps aux` 输出每行都关键（V7.34 handoff 踩过 SSH `pkill -f` 误杀自己的坑），rtk 的去重/截断可能盖掉真问题。
2. 与现有 hook 系统执行顺序未知。
3. 只影响 Bash 工具，不影响 Read / WebFetch。

**何时考虑装**：项目重构稳定 + LaTeX 报告骨架完成后，开新会话**手动**调用 `rtk git status` 单条命令试 3 天。**永远不要在板端调试期间开启**。

**装法（备查，不要执行）**：
```bash
curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh
rtk gain    # 看节省统计
# 不要 rtk init -g（全局开）
```

## 绘图路线（已锁定）

| 类型 | 工具 | 理由 |
|---|---|---|
| 硬件原理图 / PCB | `\includegraphics` 直接嵌 `total/队友交付/原理图和PCB/*.pdf` | 已有现成 PDF，不重画 |
| 架构图 / 状态机 / 数据流 | `.drawio` XML（让 CC 输出 mxGraph 格式）| 文本可生成 + https://app.diagrams.net 双向编辑 |
| GRU 训练曲线 | `tools/dashboard.py` Streamlit 截图 | 已有数据源 |
| 技术动画（可选）| `manim-video` skill | 数学/系统流程动画 |
| 可编辑 PPT | `pptx` skill（pptxgenjs 路线）| 比 markdown→pptx 更可编辑 |

## ECC skills 使用纪律

见 `.claude/rules/skills-discipline.md`。

## Karpathy 编码规范

见 `.claude/rules/karpathy-guidelines.md`。
