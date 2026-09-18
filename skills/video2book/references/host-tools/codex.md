# Codex 工具映射

**依据**：多智能体开关部分**已验证**（Codex 的 `~/.codex/config.toml` 配置项 `[features] multi_agent`）；
工具名部分为公开惯例，**以本平台实际工具列表为准**。

> 与其它平台一样：**当本表与你实际可用的工具列表冲突时，以你的工具列表为准**
> （"Trust your actual tool list over any table — including this one — when they disagree."）。

## ⚠️ 先开多智能体（本技能阶段一必需）

本技能阶段一靠**派发子智能体**（2 个专职转录子智能体按块听音 + 5~6 个写作子智能体按块成文，见 SKILL.md §1 红线 5）。
在 Codex 上需要显式开启多智能体特性，否则派发不可用。

在 `~/.codex/config.toml` 中加入：

```toml
[features]
multi_agent = true
```

要点：
- 可用的多智能体工具取决于你所用模型 preset 的版本（新版 preset 走 V2，旧版走 V1）；
  **以你实际拿到的工具列表为准**。
- 派生子智能体时给它干净上下文：用 `spawn_agent` 并设 `fork_turns: "none"`
  （默认 `"all"` 会把当前完整对话复制给子智能体，代价高）。
- 返修用 `followup_task` 续跑同一个实现者，而不是重新派一个。
- **不要**从任何技能文档里照抄模型名——先对照你当前的 spawn allowlist。
- 显式设置 `model` 与 `reasoning_effort`；只设 `model` 会让子智能体悄悄回落到该模型的默认档位。

## 工具名对照（以实际工具列表为准）

| 行动语义 | Codex 常见对应 |
| :--- | :--- |
| 读文件 | 文件查看工具（多模态读文件时可感知音频/图像内容） |
| 写盘 / 创建文件 | 文件写入工具 |
| 改文件 | 文件编辑 / patch 工具 |
| 执行 shell / CLI 命令 | shell 执行工具 |
| 检索文件内容 / 按名找文件 | 搜索工具 |
| 派发子智能体 | `spawn_agent`（开启 `multi_agent` 后）+ `wait_agent` 等待 |
| 等待 / 回收子智能体 | `wait_agent`（事件订阅式，不是轮询；`timeout_ms` 建议 300000–600000） |
| 原生听音 | MCP 工具 `read_audio` 取切片 → 用"读文件"能力聆听；无原生音频时用 `read_media` |

## 安装

作为插件安装（本仓库 `.codex-plugin/plugin.json` 已声明 `"skills": "./skills/"`），
或把 `skills/video2book/` 复制 / 软链到 `~/.codex/skills/video2book/`。
详见 `../install.md`。
