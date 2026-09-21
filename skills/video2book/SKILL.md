---
name: video2book
description: 把 B 站、YouTube、抖音长视频/系列网课或本地音视频重构为精读教材长文、模块合辑全书与思维导图复习笔记的完整技能，自带多平台统一媒体内核、音频提取与块级转录（把连续几集拼成 40–60 分钟的块、按块一次转录后**按块成文**）、块级笔记归并、双通道听音（转录角色专用）与多阶段门禁工具链。当用户提出「把网课/视频做成教材」「整理成复习笔记或思维导图」「这门课帮我精读一遍」「B 站/油管/抖音这个合集重构成文档」，或给出本地课程目录要求系统化整理时，使用本技能。
license: MIT
metadata:
  author: LINJIANG12
  version: 3.0.0
  category: learning-and-education
  compatibility: Python 3.10+；系统 ffmpeg 在 PATH；宿主需具备 read_audio 或 read_media 听音通道之一。
---

# Video2Book: 视频网课重构教材与复习笔记 Skill

把 B 站、YouTube、抖音长视频/系列网课或本地音视频转换为结构化技术教材、模块合辑全书与思维导图复习笔记。
面向任意支持 Agent Skills 规范的宿主；平台差异（安装位置、工具名）见 `references/install.md` 与 `references/host-tools/`。

**只走一条主路径。** 下面第 2 节的命令序列就是完整流程；散落的可选分支与细则全部收在 `references/` 里（第 6 节索引），
需要时再读，**不要自行发明步骤、也不要跳步**。

## 1. 五条红线（不可逾越）

1. **黑盒调用**：不读、不改 `src/` 内部实现去找捷径；不写任何 `gen_*.py` 之类的离线造文脚本来伪造产物。
   所有任务经官方 CLI 与原生多模态工具链推进。
2. **逐字稿事实保真（Strict Transcript Grounding）**：块级逐字稿 `subtitles/BLKxx_*_逐字稿.md` 是模块长文的
   **唯一事实来源**；取音只发生在转录角色身上，写作角色只读逐字稿。正文必须保留讲师亲口讲的案例、例题与比喻，
   **严禁凭空脑补**。这一条已可校验：`check --stage1` 用技术实体覆盖率报警。
3. **拒绝脱缰黑话**：经典基础课（数据结构、操作系统、数据库……）不得套互联网大厂浮夸黑话，必须贴课程实际。
4. **提示词风格红线**：风格由用户确认，`pipeline` 必须带 `--article-type`。当前只提供 `learning`（学习，推荐）
   与 `legacy`（旧版）；`consulting`/`interview`/`review`/`livestream` 只登记、未提供提示词。
   未指定、拼写不中、命中未提供形态 → **以退出码 4 终止**，不猜、不降级、不硬套。
5. **阶段一派发纪律**：课程总时长 **≤ 60 分钟** 由主 Agent 串行亲做；**> 60 分钟必须派发**。两类角色分工：
   **转录角色**（专职，建议 2 个，只做音频转录、**不回传正文**、只回报一行）与 **写作角色**
   （一个块一篇模块长文，读该块逐字稿成文）。载荷中的 `dispatch_prompt` **必须原样透传**，严禁自编提示词。
   窗口兜底只对转录角色成立：实算音频 token（时长 × `BVB_AUDIO_TOKENS_PER_SEC`，默认 32）超过窗口 60% 时，
   按返回里的 `next_start_time` / `next_duration_minutes` 续读下一卷。

## 2. 唯一执行路径（SOP）

```text
【第 0 步：依赖与凭证】环境体检：python src/cli.py info
                      若走外部听音（read_media）：确认 omni-media/config.json 就绪（默认30m切片/5并发）
                      目标是抖音？→ 必须先向用户索取 Cookie（话术见 references/runtime.md）
                      B 站建议 login --sessdata；本地 / YouTube 跳过
【第 1 步：风格】向用户确认 --article-type（learning / legacy）
【第 2 步：准备】python src/cli.py pipeline "<链接或路径>" --all --article-type learning
                 → 收音频 → 装箱成块 → 自动去重 → 导出转录/长文任务书 → 自动回收 + 对账
【第 3 步：转录】转录角色取载荷：python scripts/queue_tracker.py --next-transcribe 2 --json
                 按块听音 → 写 subtitles/BLKxx_*_逐字稿.md → 回报一行（不回传正文）
【第 4 步：写作】主 Agent 取载荷：python scripts/queue_tracker.py --next-module 5 --json
                 一个块一个子智能体（并发 5~6），原样透传 dispatch_prompt
                 → 写 articles/模块XX_<块标题>_精读长文.md
【第 5 步：放行】python src/cli.py check --stage1 --strict      # 长文确实基于块逐字稿
                 通过后才允许进入阶段二
【第 6 步：聚合】① 规划笔记：python src/cli.py cluster-notes "<链接或路径>"（导出任务书）
                 ② 撰写笔记：python scripts/queue_tracker.py --next-note 5 --json（子智能体写笔记）
                 ③ 整编教材：python src/cli.py cluster-articles "<链接或路径>"（模块长文整编成册）
                 收尾自动执行 cleanup + sync
【第 7 步：体检】python src/cli.py check --deliver --strict     # 笔记成色 + 渲染合规
```

阶段二的两处语义规划（`note_plan.json` 归并笔记、`textbook_plan.json` 分册）由 Agent 依任务书写，
工具只校验与兜底、**永不回写**；缺规划不停机，按「一块一篇」/ 章节标记兜底继续。

> **块就是知识模块**：音频按 40–60 分钟装箱，块标题由块内分集名语义组合而来，写在块音频文件名里
> （想换标题：改 `audio/_blocks/block_titles.json` 后重跑 `merge-audio`，幂等改名）。
> 长文按块写、教材按块序整编、笔记按块归并——整条链路的下游都以块为粒度。

## 3. 命令入口（10 个子命令）

所有任务经标准入口调用（`python src/cli.py <子命令>` / `python scripts/run.py <子命令>` /
`pip install -e .` 后的 `video2book <子命令>`，三者等价）：

| 子命令 | 用途 |
| :--- | :--- |
| `pipeline` | **阶段一唯一入口**：`--dry-run` 只解析拓扑；`--audio-only` 只收音频并装箱；不加则跑完整链路 |
| `merge-audio` | 单独重跑装箱合并（幂等；改 `block_titles.json` 后重跑即按新标题改名） |
| `cluster-notes` | 块 → 笔记归并，导出笔记任务书（收尾自动 cleanup + sync） |
| `cluster-articles` | 按块序把模块长文整编成册（收尾自动 cleanup + sync） |
| `check` | 质量门禁：`--stage1` 依据级校验 / `--deliver` 交付前体检 / `--fix-numbering` 存量标题去号 |
| `cleanup` | 回收已完成任务书（每类留 1 份范本） |
| `sync` | 以磁盘产物回填 `manifest.json` |
| `info` | 环境与工具链就绪状态（含凭证来源、上次 412/熔断） |
| `login` / `logout` | 持久化或清除 B 站 SESSDATA / 抖音 Cookie |

完整开关、逐场景示例与退出码见 [`references/cli-cookbook.md`](references/cli-cookbook.md)。

## 4. 门禁 vs 纪律

| 项 | 性质 | 可校验 |
| :--- | :--- | :--- |
| 模块长文 ≥ 1000 字节、任务书/块清单/逐字稿齐备、`STAGE1_DONE=1` | 机器门禁 | ✅ |
| 长文风格命中已提供预设 | 机器门禁 | ✅ 未命中即以退出码 4 终止 |
| 长文基于本块逐字稿（技术实体覆盖率，启发式） | 机器门禁 | ✅ `check --stage1` |
| 笔记成色五类致命项、渲染致命项（告警块 / 围栏外裸字符画 / 围栏配对） | 机器门禁 | ✅ `check --deliver` |
| 围栏**语言标识**、标题手写序号 | 提示项 | 默认只统计；`--require-lang` / `--require-no-numbering` 才纳入门禁 |
| **谁写的**（主 Agent / 子智能体）、转录是否忠于原声、并发是否照建议 | 纪律条款 | ❌ 靠 `.dispatch_log.jsonl` 事后复盘 |

## 5. 环境与缺失处理

三层依赖：Python 3.10+、系统 `ffmpeg`、宿主听音通道之一（`read_audio` 或 `read_media`）。
一条命令自检环境：`python src/cli.py info`。
若使用外部模型代读（`read_media`），首次运行前必须在 `omni-media/config.json` 配好端点与密钥，默认采用 30 分钟切片与 5 并发。
**硬依赖缺失即停下并给出下一步命令，绝不静默跳过**；可降级项（`ffprobe` → `ffmpeg -i`、
在线解析 → 工作区离线基准）静默降级并打印说明。
完整的依赖清单、两条听音通道的调用与分页契约、六类缺失情形的处置见
[`references/runtime.md`](references/runtime.md)。

## 6. 交付物与细则索引

三类交付物（路径均相对产物根）：
`articles/模块XX_<块标题>_精读长文.md`（一块一篇，**严格保留**）、
`notes/笔记XX_*_笔记.md`（一篇可跨多块）、
`textbooks/模块<册号>_<册名>_精读全书.md`（册=书、章=块）。

其余细则按需查阅，不要凭记忆：

| 需要了解 | 读 |
| :--- | :--- |
| 这一层文件的分工与维护规矩（"哪件事该写进哪个文件"） | [`references/README.md`](references/README.md) |
| 阶段一/二逐步详解、派发与回报协议、验收与返修、断点续跑 | [`references/workflow.md`](references/workflow.md) |
| 三条运行依赖、两条听音通道、六类缺失处置、凭证获取与安全 | [`references/runtime.md`](references/runtime.md) |
| CLI 全表、参数、退出码 | [`references/cli-cookbook.md`](references/cli-cookbook.md) |
| 产物规范、长文类型矩阵、工作区目录树、排版与渲染兼容 | [`references/delivery_matrix.md`](references/delivery_matrix.md) |
| 安装位置与平台对照 | [`references/install.md`](references/install.md) |
| 非视频作品（抖音图文/图集）处置 | [`references/non-video-works.md`](references/non-video-works.md) |
| 宿主私有工具名映射 | [`references/host-tools/`](references/host-tools/) |

> **标题纪律**：长文 / 教材 / 笔记的标题**一律不写序号**（阅读器会自动编号，手写序号会叠成双号）；
> 存量产物用 `python src/cli.py check --fix-numbering` 就地清理（幂等，可先加 `--dry-run` 预演）。
> 本文件出现的 `> [!IMPORTANT]` 一类告警块只用于提示阅读者；**交付产物一律禁用该语法**。
