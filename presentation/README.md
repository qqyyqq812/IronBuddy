# 汇报演示规范

本目录用于管理项目的 Marp 格式演示文稿。

## 文件结构
- `slides.md` — 主演示文稿（Marp 格式）
- `assets/` — 演示用图片和媒体文件

## 渲染方式
npx @marp-team/marp-cli@latest slides.md --pdf

## 写作规范
1. **调用 Skills**：撰写前加载 `docs-writer` 技能。
2. **文风**：平实、直观、无废话。每页 slide 聚焦一个核心观点。
3. **配图**：所有图片存入 `assets/`，用相对路径引用。
