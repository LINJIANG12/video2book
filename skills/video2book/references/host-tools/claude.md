# Claude Code 工具映射

**依据**：公开惯例。Claude Code 的工具集会随版本变化，**以本平台实际工具列表为准**。

> 当本表与你实际可用的工具列表冲突时，以你的工具列表为准。

| 行动语义 | Claude Code 常见对应 |
| :--- | :--- |
| 读文件 | `Read`（多模态可直接感知音频/图像内容） |
| 写盘 / 创建文件 | `Write` |
| 改文件 | `Edit` |
| 执行 shell / CLI 命令 | `Bash` |
| 检索文件内容 / 按名找文件 | `Grep`、`Glob` |
| 取网页 | `WebFetch` |
| 待办清单 | `TodoWrite` |
| 听音转录 | 通道与参数基线见 [`README.md`](README.md#两条通道是-mcp-工具名跨平台一致)（优先 `read_media` 直写；无 ext 时用 `read_audio` 取切片 → 用 `Read` 聆听） |


## 安装

把 `skills/video2book/` 复制或软链到 `~/.claude/skills/video2book`
（项目级则放 `<项目>/.claude/skills/video2book`）。
本仓库作为 Claude Code 插件安装时，根下的 `skills/` 会被**自动发现**（`.claude-plugin/plugin.json`
是纯元数据，无需声明 skills 路径）。详见 `../install.md`。
