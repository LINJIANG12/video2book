# Video2Book 只读审计报告

> 审计日期：2026-09-19  
> 审计范围：`D:\project\项目\笔记sikll\成品\skill`  
> 审计性质：只读审计；本报告只记录分析与建议，不代表已经修改技能实现。  
> 技能版本：`2.8.0`（`pyproject.toml`）  
> 基线结果：`cd skills/video2book && python scripts/selfcheck.py` 通过。  
> 当前未提交改动：`skills/video2book/SKILL.md`、`skills/video2book/scripts/queue_tracker.py`、`skills/video2book/scripts/selfcheck.py` 已有用户改动，本次报告未改动这些文件。

## 1. 执行摘要

Video2Book 是一个以 `skills/video2book/` 为安装单元的单 Skill 仓库，核心功能完整，自检基线通过；本轮未发现 P0 级功能损坏或必须立即删除的安全问题。项目当前的主要问题不是“不能用”，而是“维护成本偏高”：主技能说明过长、CLI 与交付格式文档多处重复、自检脚本体量过大、少量代码存在未使用导入/局部变量和重复请求逻辑。

建议采用分阶段收敛，而不是大重写：

1. 先保护现有基线，记录自检结果与未提交改动边界。
2. 优先合并文档重复内容，让 `SKILL.md` 回归入口和纪律约束，把细节交给 `references/`。
3. 再做低风险代码清理，删除已确认无引用的导入、局部变量和私有死代码。
4. 将 B 站请求头与重试/退避逻辑统一，但保持错误文案、退出行为和频控边界不变。
5. 最后评估 `topic_planner` 命名是否应统一为 `note_planner`，以及 `selfcheck.py` 是否需要拆分；这两项在公共契约确认前不直接动。

本轮没有发现可以安全删除的平台摄取子系统、双语 README 或现有 references 资源。所有“疑似未使用”但可能属于公共 API、测试契约或兼容入口的内容均标记为“待确认”。本报告最后按用户要求提出问题：**是否按路线图开始优化？从哪一批开始？**

## 2. 审计方法

- 读取仓库结构、`README.md`、`README.en.md`、`AGENTS.md`、`CLAUDE.md`、`skills/video2book/SKILL.md`、`pyproject.toml`、references、scripts、src、测试与 CI 配置。
- 建立 Skill 功能基线：意图、触发条件、输入输出、依赖资源、工作流、错误处理、安全边界和引用关系。
- 对文档、脚本、资源、命名、依赖和测试做交叉检查。
- 使用静态检查结果辅助识别未使用导入、未使用局部变量、无占位符 f-string 等问题。
- 运行只读基线验证：`cd skills/video2book && python scripts/selfcheck.py`。
- 对候选删除项要求同时满足“无引用证据”“非公共契约”“不影响运行路径”才归为安全删除；否则归为“待确认”。

说明：本报告中的行号来自本轮审计时的文件状态。由于当前工作区存在未提交改动，后续执行优化前应重新确认行号与上下文。

## 3. 项目概览与体量

| 项目 | 结论 |
|---|---|
| 仓库形态 | 单 Skill 仓库；安装单元是 `skills/video2book/` 整个目录 |
| 技能真源 | `skills/video2book/SKILL.md` |
| 版本 | `2.8.0`，见 `pyproject.toml:7` |
| Python 要求 | `>=3.10`，见 `pyproject.toml:10` |
| 声明依赖 | `yt-dlp>=2024.0.0`、`requests>=2.28.0`，见 `pyproject.toml:11-14` |
| 入口 | `video2book = "src.cli:main"`，见 `pyproject.toml:28` |
| 基线自检 | 通过 |
| tracked files | 84 |

主要大文件：

| 文件 | 体量 | 观察 |
|---|---:|---|
| `skills/video2book/SKILL.md` | 812 行 / 73,207 bytes | 承担入口、SOP、CLI 速查、交付格式和环境处理，职责偏多 |
| `skills/video2book/scripts/selfcheck.py` | 3,161 行 / 190,030 bytes | 64 项检查集中在单文件，维护成本高 |
| `skills/video2book/src/cli.py` | 1,264 行 / 65,271 bytes | CLI 命令面较大，但属于核心入口，暂不拆分 |
| `skills/video2book/src/core/pipeline.py` | 1,019 行 / 55,363 bytes | 核心流水线，属于不可轻动区域 |
| `skills/video2book/src/core/audio_merger.py` | 950 行 / 46,043 bytes | 核心音频处理，暂不拆分 |
| `skills/video2book/scripts/queue_tracker.py` | 749 行 / 35,135 bytes | 含已确认私有死代码候选 |

## 4. 当前工作区状态

审计时 `git status --short` 显示：

```text
 M skills/video2book/SKILL.md
 M skills/video2book/scripts/queue_tracker.py
 M skills/video2book/scripts/selfcheck.py
```

这些改动视为用户已有工作，不属于本次审计产生的修改。后续优化前应：

- 保留当前改动，不执行回退、重置或覆盖式重写。
- 建立临时备份或 Git 分支，不自动提交。
- 重新运行 `selfcheck.py`，确认清理前的真实基线。
- 若清理涉及 `selfcheck.py` 中死代码回归清单，应同步更新对应检查，保持门禁能力不降级。

## 5. Skill 功能基线矩阵

### 5.1 技能身份与触发

| 维度 | 基线 |
|---|---|
| Skill 名称 | Video2Book |
| 路径 | `skills/video2book/` |
| 核心意图 | 把 B 站、YouTube、抖音网课与本地音视频重构为模块精读长文、模块合辑教材和思维导图式复习笔记 |
| 主入口 | `skills/video2book/SKILL.md` |
| 触发概述 | 用户要求把视频网课、播放列表、课程目录或本地音视频转换为教材长文、模块全书、复习笔记或思维导图笔记 |
| 平台边界 | B 站、YouTube、抖音、本地媒体四类来源；平台摄取细节在 `src/fetchers/` 与 references 中 |
| 安装边界 | 复制 `skills/video2book/` 整个目录；根 README、LICENSE、平台声明不属于安装单元 |

### 5.2 核心输入、输出与中间产物

| 类型 | 内容 | 基线位置/说明 |
|---|---|---|
| 输入 | B 站 URL、YouTube URL、抖音 URL、本地音视频目录、分集区间、课程工作目录 | CLI 与 Skill SOP |
| 输入参数 | `--article-type`、平台 URL、`--base-dir`、SESSDATA、Cookie、分集选择、风格与质检开关 | `SKILL.md`、`references/cli-cookbook.md`、`src/cli.py` |
| 中间产物 | 下载音频、音频块、块逐字稿、任务书、`blocks.json`、`manifest.json`、`parts.json` | 工作区目录结构，`SKILL.md:669` 起 |
| 最终产物 A | `articles/` 模块精读长文 | 每块一篇，必须完整保留 |
| 最终产物 B | `notes/` 复习笔记 | 一篇笔记一个子智能体，按 Agent 归并产出 |
| 最终产物 C | `textbooks/` 模块合辑教材 | 按块序整编，册=书、章=块 |
| 交付门禁 | `cluster-notes`、`cluster-articles`、渲染/成色/依据级质检、`cleanup`、`reconcile` | 由主 Agent 手动执行，非流水线硬拦 |

### 5.3 依赖与外部契约

| 类别 | 依赖/契约 | 备注 |
|---|---|---|
| 运行时 | Python 3.10+ | `pyproject.toml:10` |
| 系统工具 | `ffmpeg`；`ffprobe` 可降级为 `ffmpeg -i` | `SKILL.md:761` 起 |
| Python 依赖 | `yt-dlp`、`requests` | `pyproject.toml:11-14` |
| 听音通道 | `omni-media:read_audio` 或 `read_media` 之一 | 两个通道各自独立成包，Skill 不直接 import |
| 平台凭证 | B 站 SESSDATA、抖音 Cookie | 本地与非 B 站任务可跳过 B 站凭证；凭证不得进入版本库 |
| 文件契约 | `blocks.json` 是模块边界与交付命名的唯一事实源；`parts.json` 是集号离线基准；`manifest.json` 由对账回填 | 属于不可随意改动的公共契约 |
| 任务书契约 | 任务书内含完整提示词与排版红线；子智能体直接透传 `dispatch_prompt` | 主 Agent 不代读、不代听、不自行拼接格式 |
| 退出行为 | `--article-type` 缺失时 exit 4；质检脚本默认提示级，`--strict` 才纳入门禁 | 属于错误处理和 CI/自动化边界 |

### 5.4 关键工作流

1. **环境与凭证检查**：确认 Python、ffmpeg、听音通道、SESSDATA/Cookie；缺硬依赖立即终止并给下一步。
2. **来源解析与规划**：解析 B 站/YouTube/抖音/本地来源，展开合集和分集，确定文章类型与长文风格。
3. **音频摄取与装箱**：下载/读取音频，去重，按预算装箱，生成 `blocks.json`。
4. **阶段一派发**：导出转录任务与模块长文任务书，子智能体按任务书产出逐字稿和 `articles/`。
5. **阶段一门禁**：检查块级转录与模块长文是否就绪，合格后放行阶段二。
6. **阶段二归并与整编**：生成复习笔记、模块合辑教材；缺归并允许继续，不停机。
7. **交付前质检**：跑笔记成色、长文依据、渲染兼容检查；必要时 `--strict`。
8. **收尾与对账**：`cleanup` 回收任务书，`reconcile` 以磁盘产物回填 manifest。

### 5.5 安全边界与错误处理

- 严禁编写离线批量造文脚本伪造产物；必须通过官方 CLI 与原生多模态工具链推进。
- 任务书已烘焙提示词与格式约束，主 Agent 不得自行扩展或重写规则。
- 凭证只通过参数或本地存档传入，命令参数优先；本地存档被 `.gitignore` 排除。
- 在线接口失败时，已有工作区可通过 `parts.json` / `manifest.json` 离线自愈；不得因解析失败破坏集号基准。
- 硬依赖缺失：立即终止并给出可照做的下一步。
- 可降级依赖缺失：静默或明确降级并打印说明。
- 质检默认提示级，只有 `--strict` 的致命项才返回非零退出码。

### 5.6 引用关系概览

| 被引用对象 | 主要引用方 | 结论 |
|---|---|---|
| `SKILL.md` | `AGENTS.md`、`CLAUDE.md`、README、平台安装说明 | 唯一真源，保留 |
| `references/cli-cookbook.md` | `SKILL.md`、README、安装文档 | CLI 场景手册，建议作为 CLI 细节主承载 |
| `references/delivery_matrix.md` | `SKILL.md`、README | 交付格式主承载，建议保留细节 |
| `references/install.md` | `AGENTS.md`、README | 安装对照，保留 |
| `scripts/selfcheck.py` | README、Agent 入口、CI/本地验证 | 主质量门禁，暂保留单文件 |
| `scripts/queue_tracker.py` | `SKILL.md`、CLI cookbook、README | 队列与阶段门禁，含私有死代码候选 |
| `src/` 工具链 | CLI、Skill SOP、任务书生成 | 安装必需，不可删 |
| `README.en.md` | 英文入口/翻译用户 | “待确认”是否仍为发布契约，不直接删除 |

## 6. 臃肿与混乱热力图

等级说明：P0=功能或安全立即受损；P1=高维护成本或高回归风险；P2=明确可收敛；P3=命名/整洁度问题。

| 区域 | 等级 | 证据 | 影响 |
|---|---:|---|---|
| `SKILL.md` 职责过载 | P1 | 812 行 / 73,207 bytes；CLI 速查、格式规范、环境处理并存 | 上下文占用高，修改入口时容易漏改引用 |
| CLI 文档三处重复 | P1 | `SKILL.md:570-653`、`references/cli-cookbook.md:15-121`、`README.md:350-400` | 同一命令和开关在三处维护，参数漂移风险高 |
| 交付格式重复 | P1 | `SKILL.md:656-758`、`references/delivery_matrix.md:11-120` | 格式规则双写，任务书、文档和门禁可能不一致 |
| 依赖描述冲突 | P1 | `README.md:271` 声明 `yt-dlp`/`requests`；`README.md:420` 写“只用标准库” | 安装预期错误，用户可能漏装依赖或误判运行时 |
| `selfcheck.py` 单文件过大 | P2 | 3,161 行 / 190,030 bytes，64 项检查 | 局部修改成本高，但它是现有主门禁，先保留 |
| 未使用导入/局部变量 | P2 | `pyflakes` 约 20 项；代表性位置见候选表 | 噪声增加，可能掩盖真实死代码 |
| 无占位符 f-string | P3 | `cli.py:641,678,704,714,752`；`block_synthesizer.py:374` | 非功能问题，易误判为格式化 bug |
| B 站请求头/重试重复 | P2 | 多 fetcher/core 路径各自维护请求头和退避 | 修改频控策略时容易只改一处 |
| 私有 helper 死代码 | P2 | `queue_tracker.py::_blocks_by_page` 无引用证据 | 可清理，但需同步死代码门禁清单 |
| 目录级临时物 | P3 | ignored `__pycache__` | 影响归档体积与发布整洁，不影响运行 |
| `topic_planner` 命名不一致 | P3 | 功能与 `note_planner` 概念接近 | 仅命名统一候选，公共入口确认前不动 |

## 7. 精简候选表

分类含义：安全删除=已确认无引用且非公共契约；合并=多处内容收敛到单一真源；内联=小逻辑就地实现；重命名/统一=命名一致化；简化=降低复杂度但不删能力；待确认=证据不足，先标记。

### 7.1 文档收敛候选

| ID | 等级 | 分类 | 位置 | 证据 | 影响 | 建议 | 验证方式 | 风险与副作用 |
|---|---:|---|---|---|---|---|---|---|
| D01 | P1 | 合并 | `SKILL.md:570-653`、`references/cli-cookbook.md:15-121`、`README.md:350-400` | 三个位置分别列出相同的 CLI 场景、参数和注意事项 | 参数漂移会导致 Agent 按旧说明执行 | 以 `cli-cookbook.md` 为 CLI 细节单一真源；`SKILL.md` 保留最小导航和硬性纪律；README 只保留快速入门示例 | 全文搜索命令名与开关，确认三处引用一致；跑 `selfcheck.py` | 若删除 README 示例过度，会影响新用户上手；需要保留快速上手路径 |
| D02 | P1 | 合并 | `SKILL.md:656-758`、`references/delivery_matrix.md:11-120` | 交付产物类型、格式与渲染兼容规则在两处重复 | 任务书生成与质检依据可能不一致 | 以 `delivery_matrix.md` 为格式细节真源；`SKILL.md` 只保留“必读、必须遵守、引用位置” | diff 两份文档的规则集合；跑渲染与成色质检脚本 | 格式规范是硬契约，合并时必须逐条核对，不能丢规则 |
| D03 | P1 | 统一 | `README.md:271`、`README.md:420`、`pyproject.toml:11-14` | 一处声明 `yt-dlp`/`requests`，另一处写“只用标准库” | 用户可能漏装运行时依赖，导致摄取失败 | 修正 README 技术栈描述，区分“Python 标准库能力”与“平台抓取依赖” | 搜索“标准库”“依赖”相关表述；检查 README 与 `pyproject.toml` 一致 | 纯文档修正，风险低；注意不要声称可移除 `yt-dlp`/`requests` |
| D04 | P2 | 简化 | `SKILL.md:764-812` | 环境要求、缺失处理、降级原则在主文件详述 | 主文件继续膨胀 | 保留硬依赖、终止条件和安全边界，详情可移至 references 的环境章节 | 搜索“ffprobe”“降级”“硬依赖”确认引用完整 | 环境处理属于错误处理契约，不能只留一句“见 references”而丢终止条件 |

### 7.2 代码清理候选

| ID | 等级 | 分类 | 位置 | 证据 | 影响 | 建议 | 验证方式 | 风险与副作用 |
|---|---:|---|---|---|---|---|---|---|
| C01 | P2 | 保留不动 | `scripts/selfcheck.py:1-3161` | 单文件 3,161 行、64 项检查，是当前主质量门禁 | 拆分可能破坏检查注册与退出码 | 本轮保留；后续单独评估按领域拆分 | 修改前后 `selfcheck.py` 全绿且检查数不降 | 拆分风险高，暂不列入前几批 |
| C02 | P2 | 安全删除 | `audio_chunker.py:11-12`、`local_media.py:14`、`base.py:9`、`douyin.py:61,74-79`、`youtube.py:7,28,72`、`fetcher.py:200`、`transcript_splitter.py:109,124`、`selfcheck.py:101` | `pyflakes` 报告未使用导入/局部变量 | 降低静态噪声，不改变运行路径 | 只删除确认未被侧效应依赖的导入和局部变量 | 每批跑 `pyflakes`；跑 `selfcheck.py`；对涉及模块跑最小 CLI 命令 | 需确认无 import 副作用；个别“未使用”可能用于兼容或注册 |
| C03 | P2 | 安全删除 | `queue_tracker.py::_blocks_by_page` | 私有 helper 无调用引用证据 | 去掉死代码，减少维护面 | 删除函数，并同步 `selfcheck.py:879-911` 死代码回归清单（如清单覆盖该项） | `rg "_blocks_by_page"` 无引用；跑 `selfcheck.py`、队列状态与派发载荷命令 | 若外部脚本私自 import 该私有函数会破坏兼容；私有前缀降低风险但需复核 |
| C04 | P2 | 合并 | 各 fetcher 的别名/入口定义 | 存在重复或未使用 fetcher 别名 | 降低平台入口混乱 | 保留实际命令路径使用的入口，清理重复别名 | CLI 全平台最小解析测试；`rg` 确认入口引用 | 别名可能作为公共调用名，需先确认 |
| C05 | P2 | 合并 | B 站请求头构造处 | 多处重复请求头与 UA/Referer 组装 | 修改平台策略时容易漏改 | 抽成单个 helper 或常量，保持字段完全一致 | 对比请求头集合；跑 B 站解析测试 | 请求头变化可能触发平台风控，必须逐字段一致 |
| C06 | P2 | 简化 | B 站重试/退避路径 | 重试次数、退避、412 状态记录分散 | 行为不一致会破坏频控处理 | 统一 retry/backoff helper，但保留错误输出文案与退出码 | 故障注入或 mock 测试；检查 412 状态输出 | 重试语义变化可能影响长时间任务，需保持次数和最终错误一致 |
| C07 | P3 | 简化 | `cli.py:641,678,704,714,752`、`block_synthesizer.py:374` | 无占位符 f-string | 仅静态检查噪声 | 去掉多余 `f` 前缀 | `pyflakes`；`selfcheck.py` | 无功能风险 |

### 7.3 命名、资源与仓库整洁候选

| ID | 等级 | 分类 | 位置 | 证据 | 影响 | 建议 | 验证方式 | 风险与副作用 |
|---|---:|---|---|---|---|---|---|---|
| N01 | P3 | 重命名/统一 | `topic_planner` 相关模块/命令 | 与 `note_planner` 概念接近 | 概念一致，降低认知负担 | 先确认是否公共 API；非公共则统一为 `note_planner` | `rg` 全部引用；CLI 回归 | 可能破坏用户脚本或任务书路径，必须最后做 |
| G01 | P3 | 简化 | ignored `__pycache__`、临时生成物 | 归档/发布时混入无关文件 | 增加体积，不影响运行 | 发布或打包前清理 ignored 缓存；必要时加归档排除规则 | `git status --ignored`；检查归档清单 | 不应把清理写成源码删除；避免误删用户本地产物 |
| K01 | P3 | 保留不动 | `README.en.md` | 双语入口 | 删除会损失英文用户入口 | 保留；如需更新译文，单独批次处理 | 链接检查 | 翻译同步是内容工作，不是死代码清理 |
| R01 | P3 | 待确认 | references 与 assets 中未被主流程直接引用的文件 | 部分资源只被间接引用或供宿主工具使用 | 误删会破坏宿主集成或安装说明 | 先做引用图，标记“待确认”，不直接删 | `rg` 文件名、README/SKILL 引用图、安装文档核对 | 资源可能由平台按约定路径发现，不一定有显式 import |

### 7.4 明确标记为“待确认”的项

| ID | 内容 | 为什么不能直接删 |
|---|---|---|
| T01 | 疑似公共 API 的 fetcher/planner 函数 | 可能被外部脚本、测试或文档示例调用；需先确认发布契约 |
| T02 | `selfcheck.py` 死代码门禁清单交互项 | 删除代码必须同步更新门禁，否则会出现“代码已删但检查仍要求存在”或反向漏检 |
| T03 | `topic_planner` 命名 | 可能已出现在任务书路径、CLI 帮助或用户脚本中，重命名属于破坏性变更 |
| T04 | `README.en.md` 与部分 references | 属于发布/分发契约，可能被平台安装说明或外部链接引用 |
| T05 | 平台摄取子系统的“低频”分支 | 低频不等于死代码；抖音、YouTube、本地媒体分支都要保持功能完整 |

## 8. 建议的目标结构

目标不是推倒重来，而是建立清晰分层：

```text
skills/video2book/
├── SKILL.md                  # 入口、纪律、关键契约、导航；控制在可快速阅读的长度
├── src/                      # 安装必需的工具链与核心实现
├── scripts/                  # 自检、队列、发布辅助脚本
├── references/
│   ├── install.md            # 安装与平台对照
│   ├── cli-cookbook.md       # CLI 与六个场景的单一真源
│   ├── delivery_matrix.md    # 交付格式与渲染规范的单一真源
│   └── host-tools/           # 宿主工具映射与外部集成说明
└── assets/                   # 确有引用、确有发布需要的资源
```

结构原则：

- `SKILL.md` 只回答“这是什么、什么时候触发、必须遵守什么、下一步看哪里”。
- CLI 细节集中到 `references/cli-cookbook.md`。
- 交付格式细节集中到 `references/delivery_matrix.md`。
- 环境依赖与降级规则的“硬边界”留在 `SKILL.md`，长解释放 references。
- 公共契约文件（`blocks.json`、`parts.json`、`manifest.json`、任务书、退出码）不因目录整洁而改名。
- 不在本阶段拆分 `src/core/`；它属于功能核心，先做文档和死代码清理。

## 9. 分批优化路线图

### 批次 0：保护基线

**目标**：在任何修改前锁定当前行为与用户未提交改动。

| 项目 | 内容 |
|---|---|
| 范围 | 只读与备份，不改实现 |
| 前置 | 保留现有 `SKILL.md`、`queue_tracker.py`、`selfcheck.py` 改动 |
| 操作 | 建立 Git 分支或临时备份；记录 `git status --short`；运行基线自检并保存输出 |
| 产出 | 基线自检日志、改动边界清单、目标文件哈希 |
| 中止条件 | 自检不绿或存在未识别的并发改动 |

建议命令：

```powershell
git status --short
cd skills/video2book
python scripts/selfcheck.py
```

### 批次 1：文档去重与一致性

**目标**：消除 CLI、交付格式和依赖描述的重复与冲突，不改变任何机器契约。

| 项目 | 内容 |
|---|---|
| 候选 | D01、D02、D03、D04 |
| 单一真源 | CLI→`references/cli-cookbook.md`；格式→`references/delivery_matrix.md`；安装→`references/install.md` |
| `SKILL.md` 保留 | 触发、SOP、硬性纪律、必须终止的错误边界、references 导航 |
| 不改 | 命令名、参数、退出码、任务书内容生成、门禁判定、目录契约 |
| 预期收益 | 主技能文件明显变短，参数与格式只有一处维护 |

### 批次 2：低风险代码清理

**目标**：删除已确认无引用且无副作用的静态噪声与私有死代码。

| 项目 | 内容 |
|---|---|
| 候选 | C02、C03、C07，以及 C04 中已确认非公共的部分 |
| 优先顺序 | 未使用导入/局部变量 → 无占位符 f-string → 私有 helper |
| 不改 | 公共函数签名、CLI 入口、异常类型、日志文案、退出码 |
| 门禁同步 | 若删除项出现在 `selfcheck.py:879-911` 的回归清单中，同步更新 |
| 预期收益 | 减少死代码和静态检查噪声，降低后续阅读成本 |

### 批次 3：B 站请求与重试收敛

**目标**：合并重复请求头与 retry/backoff 逻辑，保持平台行为完全兼容。

| 项目 | 内容 |
|---|---|
| 候选 | C05、C06 |
| 必须保持 | User-Agent、Referer、Cookie、SESSDATA 处理方式完全一致 |
| 必须保持 | 重试次数、退避曲线、412 频控/熔断状态记录、最终错误文案与退出码 |
| 验证重点 | mock 请求头对比、mock 412/超时/连接错误、离线自愈不回归 |
| 预期收益 | 平台策略只有一处实现，降低后续维护漏改概率 |

### 批次 4：命名与自检维护性评估

**目标**：处理需要确认公共契约的事项，避免把清理变成破坏性变更。

| 项目 | 内容 |
|---|---|
| 候选 | N01、C01、T01-T05 |
| `topic_planner` | 先做引用图；确认是否用户可见或已进入任务书/CLI 后再决定是否兼容别名 |
| `selfcheck.py` | 先评估能否按文档、CLI、管线、平台、发布分域；不承诺本批一定拆分 |
| `README.en.md` | 只做译文同步评估，不删除 |
| 预期收益 | 为后续长周期维护建立稳定命名与门禁结构 |

### 批次 5：发布整洁与归档检查

**目标**：清理缓存和生成物，确保交付包只包含安装必需内容。

| 项目 | 内容 |
|---|---|
| 候选 | G01、R01 |
| 检查 | `git status --ignored`、归档文件清单、Skill 目录自包含性 |
| 不做 | 删除无显式引用但可能由宿主约定的 resources |
| 预期收益 | 安装包更小，发布检查可重复 |

## 10. 不可动清单及原因

以下内容在本阶段不应删除或改名：

| 不可动项 | 原因 |
|---|---|
| `skills/video2book/SKILL.md` 的触发、SOP、硬性纪律和安全边界 | 技能唯一真源与 Agent 行为契约 |
| `skills/video2book/src/` 整个工具链 | 安装必需，直接支撑摄取、转录、任务书、整编与质检 |
| `references/install.md` | 平台安装对照，属于分发契约 |
| `references/cli-cookbook.md` | CLI 场景手册，建议成为 CLI 细节真源 |
| `references/delivery_matrix.md` | 交付格式和渲染规范真源，直接关联任务书与门禁 |
| `references/host-tools/` | 宿主工具映射，平台集成可能按约定路径发现 |
| `README.en.md` | 英文用户入口，除非维护者明确确认不再发布 |
| 三类最终产物与中间产物契约 | `articles/`、`notes/`、`textbooks/`、`blocks.json`、`parts.json`、`manifest.json` |
| 退出码、`--strict` 语义、`--article-type` 必填行为 | 自动化和用户已依赖的机器契约 |
| 凭证处理与 `.gitignore` 规则 | 安全边界，不得为了整洁弱化 |
| 任务书提示词与格式烘焙 | 防止主 Agent 自编格式，属于核心一致性机制 |
| 平台低频分支与降级路径 | 低频不等于死代码，删除会破坏功能完整性 |

## 11. 每批验证计划

### 通用验证

每个批次完成后至少执行：

```powershell
# 1. 工作区边界
git status --short

# 2. 技能自检（从技能目录执行）
cd skills/video2book
python scripts/selfcheck.py
```

同时检查：

- 修改文件是否与批次范围一致，无意外改动。
- 公共命令名、参数、退出码和产物路径是否保持。
- 文档引用目标是否存在。
- 备份或分支可用，且未自动提交。

### 文档批次验证

- 搜索 CLI 命令、参数和退出码，确认只在单一真源定义，其他位置引用它。
- 搜索“交付格式”“Typora”“Markmap”“XMind”等关键词，逐条核对规则未丢失。
- 对比 `README.md`、`SKILL.md`、`pyproject.toml` 的依赖描述。
- 检查所有 Markdown 相对链接。
- 运行 `selfcheck.py`。

### 代码清理批次验证

- 对删除的符号执行 `rg`，确认无调用。
- 运行 `pyflakes`，确认目标噪声消失且无新增问题。
- 对涉及的模块运行最小 CLI 冒烟：`info`、`parse` 的本地或 mock 场景、队列状态。
- 运行 `selfcheck.py`。
- 如果触碰死代码门禁清单，确认检查数量与语义不降级。

### 请求/重试批次验证

- 请求头逐字段对比，确认 UA、Referer、Cookie 等没有漂移。
- 对超时、连接失败、412、非 JSON 响应做 mock 或故障注入。
- 确认 412 状态可被 `info` 或状态输出观察。
- 确认最终错误提示、退出码和离线自愈路径不变。
- 运行 `selfcheck.py`。

### 命名与自检批次验证

- 建立旧名到新名的完整引用图。
- 若属于公共入口，保留兼容别名或迁移说明。
- 自检脚本拆分前后检查总数、失败语义、输出格式一致。
- 运行完整 `selfcheck.py`，并至少执行一次真实或 mock CLI 主流程。

### 归档批次验证

- `git status --ignored` 检查缓存与临时物。
- 解压或复制 `skills/video2book/` 到临时目录，确认自包含安装可运行。
- 确认没有把用户工作区产物或凭证纳入归档。

## 12. 结论

本项目当前功能基线完整，自检通过，未发现 P0 问题。最值得优先处理的是文档重复与依赖描述冲突，其次是已确认的低风险代码噪声；`selfcheck.py` 拆分、`topic_planner` 重命名和资源删除风险较高，应排在确认公共契约之后。

建议整体遵循“文档先收敛、代码后清理、行为契约最后碰”的顺序。任何批次都必须在独立备份或 Git 分支中进行，不自动提交；每批都要重新跑 `selfcheck.py` 并对照本报告的不可动清单。

---

**是否按路线图开始优化？从哪一批开始？**

