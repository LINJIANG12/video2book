---
name: video2book
description: 把 B 站、YouTube、抖音长视频/系列网课或本地音视频重构为精读教材长文、模块合辑全书与思维导图复习笔记的完整技能，使用本技能。v4 以根目录 block_plan.json 锁定逻辑块，字幕优先、缺字幕块按需物化音频，再统一写作、笔记与教材。
license: MIT
metadata:
  author: LINJIANG12
  version: 4.0.0
  category: learning-and-education
  compatibility: Python 3.10+；系统 ffmpeg 在 PATH；缺字幕块需要宿主听音通道之一（read_audio 或 read_media）。
---

# Video2Book：视频网课重构教材与复习笔记

把长视频、系列网课或本地课程转换为结构化技术教材、模块合辑全书与思维导图复习笔记。平台差异见 `references/host-tools/`。

**只走一条主路径。** 命令序列就是完整流程；细则按需读 `references/`，不要自行发明步骤或跳过阶段。

## 1. 五条红线

1. **黑盒调用**：所有任务经官方 CLI 与原生多模态工具链推进，不写离线造文脚本伪造产物。
2. **逐字稿事实保真**：`subtitles/BLKxx_*_逐字稿.md` 是模块长文的唯一事实来源。逐字稿可由 B 站中文字幕或听音转录产生；写作角色只读逐字稿，不重新听音。`check --stage1` 用双层实体覆盖率校验。
3. **拒绝脱缰黑话**：经典基础课必须贴课程实际，不套互联网大厂浮夸黑话。
4. **提示词风格红线**：`pipeline` 必须带 `--article-type`；当前提供 `learning`（推荐）与 `legacy`。未指定或命中未提供形态以退出码 4 终止。
5. **阶段一派发纪律**：课程总时长 ≤ 60 分钟由主 Agent 串行处理，超过 60 分钟必须派发。转录角色通过 `queue_tracker --next-transcribe` 按块消费，写作角色通过 `--next-module` 一块一篇；`dispatch_prompt` 必须原样透传，**不回传正文**，只回报一行。实算音频 token（时长 × `BVB_AUDIO_TOKENS_PER_SEC`，默认 32）超过窗口 60% 时按 `next_start_time` / `next_duration_minutes` 续读。

## 2. 唯一执行路径（SOP）

```text
【第 0 步：依赖与凭证】python src/cli.py info
                      B 站建议 login --sessdata；抖音先索取 Cookie
                      外部听音需先配置 omni-media/config.json
【第 1 步：风格】向用户确认 --article-type（learning / legacy）
【第 2 步：阶段一】python src/cli.py pipeline "<链接或路径>" --all --article-type learning
                 → 元数据 → parts.json → BlockPlan（block_plan.json）
                 → B 站字幕优先 → 只为缺字幕块物化音频
                 → 导出转录/模块长文任务书 → 自动回收 + 对账
【第 3 步：转录】python scripts/queue_tracker.py --next-transcribe --json
                 只处理缺字幕且物理音频已就绪的块
【第 4 步：写作】python scripts/queue_tracker.py --next-module 5 --json
                 一个块一个子智能体，原样透传 dispatch_prompt
【第 5 步：放行】python src/cli.py check --stage1 --strict
【第 6 步：聚合】python src/cli.py cluster-notes "<链接或路径>"
                 python scripts/queue_tracker.py --next-note 5 --json
                 python src/cli.py cluster-articles "<链接或路径>"
【第 7 步：体检】python src/cli.py check --deliver --strict
```

`--dry-run` 只解析拓扑；`--audio-only` 完成计划、字幕、缺块音频与转录任务书后返回，不派发模块长文任务书。`--force` 只重取音频、逐字稿与任务书，不改变已锁定 BlockPlan。

## 3. BlockPlan 契约

- 根目录 `block_plan.json` 是逻辑块唯一事实源，保存块号、标题、覆盖分集、`units`、`segments` 与时长；不保存音频路径。
- 块是知识模块：模块长文一块一篇，教材按块序整编，笔记按归并后的笔记分篇。
- 块时长目标由 `--block-minutes` / `BVB_AUDIO_BLOCK_MINUTES` 控制，默认 50 分钟，常规区间 40–60 分钟；超长集按 `Pxx上/Pxx下` 拆分。
- 连载课程只允许在旧分集前缀不变时追加新块；旧 parts 或 limits 改变会报错，不静默重排。
- 没有 `block_plan.json` 的旧工作区不迁移、不混写；请使用新的 `--task` 工作区名。
- 字幕完整块不下载音频；缺字幕块由 `AudioMaterializer` 按既有计划切分/拼接，绝不重新规划。

## 4. 命令入口

所有命令经 `python src/cli.py <子命令>`、`python scripts/run.py <子命令>` 或安装后的 `video2book <子命令>` 调用。

| 子命令 | 用途 |
| :--- | :--- |
| `pipeline` | 阶段一唯一入口：元数据、BlockPlan、字幕、按需音频与任务书 |
| `cluster-notes` | 块 → 笔记归并，导出笔记任务书 |
| `cluster-articles` | 按块序把模块长文整编成册 |
| `check` | `--stage1` 依据级校验 / `--deliver` 交付体检 / `--fix-numbering` 标题去号 |
| `cleanup` | 回收已完成任务书，每类保留 1 份范本 |
| `sync` | 以磁盘产物回填 `manifest.json` |
| `info` | 环境、工具链与凭证状态 |
| `login` / `logout` | 持久化或清除 SESSDATA / 抖音 Cookie |

完整开关、场景示例与退出码见 `references/cli-cookbook.md`。

## 5. 门禁与纪律

| 项 | 性质 | 可校验 |
| :--- | :--- | :--- |
| 模块长文 ≥ 1000 字节、任务书/计划/逐字稿齐备 | 机器门禁 | ✅ |
| 长文风格命中预设 | 机器门禁 | ✅ 未命中退出码 4 |
| 长文基于本块逐字稿（双层实体覆盖率） | 机器门禁 | ✅ `check --stage1` |
| 围栏语言标识与标题手写序号 | 提示项 | 默认只统计；`--require-lang` / `--require-no-numbering` 才纳入门禁 |
| 笔记成色五类致命项、渲染致命项 | 机器门禁 | ✅ `check --deliver` |
| 谁写的、转录是否忠于原声、并发是否照建议 | 纪律条款 | ❌ 只能看派发台账 |

## 6. 环境与缺失处理

运行依赖 Python 3.10+、系统 ffmpeg，以及缺字幕块实际需要时的 `read_audio` 或 `read_media`。B 站字幕完整时不调用听音；字幕缺失时没有听音通道就停下提示挂载，不绕过音频保真。依赖、凭证与分页契约见 `references/runtime.md`。

## 7. 交付物

- `articles/模块XX_<块标题>_精读长文.md`：一块一篇，严格保留。
- `notes/笔记XX_*_笔记.md`：按归并结果生成，可跨块。
- `textbooks/模块<册号>_<册名>_精读全书.md`：册=书、章=块。
- `parts.json`：分集拓扑缓存。
- `block_plan.json`：逻辑块计划（模块边界唯一事实源）。
- `subtitles/BLKxx_*_逐字稿.md`：块级逐字稿（写作唯一事实来源）。

细则索引：`workflow.md`（阶段流程）、`runtime.md`（依赖与听音）、`cli-cookbook.md`（命令）、`delivery_matrix.md`（产物规范）、`host-tools/`（宿主工具映射）。

> 标题纪律：长文、教材、笔记标题一律不写序号；用 `check --fix-numbering` 清理存量。交付物禁用 GitHub 告警块语法。
