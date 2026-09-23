# OpenCode 工具映射

**依据**：已核实——按 Agent Skills 规范整理，工具名与行动语义一一对应。

> 与其它平台一样：**当本表与你实际可用的工具列表冲突时，以你的工具列表为准。**

| 行动语义 | OpenCode 对应工具 |
| :--- | :--- |
| 读文件 | `read` |
| 写盘 / 创建文件 / 改文件 / 删文件 | `apply_patch` |
| 执行 shell / CLI 命令 | `bash` |
| 检索文件内容 / 按名找文件 | `grep`、`glob` |
| 取网页 | `webfetch` |
| 待办清单 | `todowrite` |
| 派发子智能体 | `task` 工具，`subagent_type: "general"`（代码库探索可用 `"explore"`） |
| 调用技能 | OpenCode 原生 `skill` 工具 |
| 听音转录 | 优先用 MCP 工具 `read_media`（外部代读，支持 `output_file` 直写落盘）；<br>无 ext 时用 `read_audio` 取切片，再用上表的 `read` 打开切片路径聆听 |

## 安装

见**插件根**（仓库根，不是技能目录）下的 `.opencode/INSTALL.md`。

技能被单独安装时该文件不存在，此时按 [`../install.md`](../install.md) 的「手动安装三法」，
把本技能目录放进 OpenCode 的技能目录即可（工具链随目录一起走）。
