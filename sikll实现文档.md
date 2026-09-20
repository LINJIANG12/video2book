# Video2Book 实现层技术文档

> **文档定位**：面向**开发与维护者**，讲清楚"代码怎么组织、任务怎么落地、改一处要动哪些地方"。
> 使用者上手看 `README.md`；Agent 执行契约看 `skills/video2book/SKILL.md`；上轮审计与精简结论看 `AUDIT_REPORT.md`。
>
> **校订基线**：提交 `fd72412`；**v2.9.0「入口收敛重构」** 后的入口集合、脚本清单与本文章节已同步。
> 改动代码后请同步更新受影响的小节。
>
> **命名说明**：文件名沿用本项目既有的 `sikll` 写法（与容器目录 `笔记sikll` 一致）。

---

## 1. 架构总览

### 1.1 三域分离（最底层的结构约束）

```text
<容器根>/                         ← 域② 容器域（可选，用 .bvb-home 或 $BVB_HOME 标记）
├── skill/                        ← 域① 代码域（本仓库，git 仓库在这里）
│   └── skills/video2book/        ←   安装单元：SKILL.md + references/ + src/ + scripts/
├── omni-media/                   ←   可选：两个听音 MCP 服务的仓库（与技能互不 import）
└── output/                       ← 域③ 产物域：<task>/ 工作区 + 凭证 + WBI 密钥 + 412 状态
```

三条硬约束（`src/core/paths.py` 是唯一实现处，`selfcheck` 的「三域分离契约」把它固化成断言）：

1. **产物永不落进代码域**：产物根 = 容器内工作时 `<容器根>/output`，否则 `<工作目录>/output`，可用 `--base-dir` / `BVB_OUTPUT_DIR` 覆盖。
2. **容器根 `.git` 必须保持为空标记**（零提交、零跟踪文件）。它是工作区边界标记，不是版本库——往里提交会让自检第 3 项直接失败。
3. **代码域内每个仓库自包含**：安装 = 复制 `skills/video2book/` 一个目录，不带仓库根的 README 与平台声明。

### 1.2 分层视图

| 层 | 位置 | 职责 | 明确不做 |
|---|---|---|---|
| 契约层 | `SKILL.md` | Agent 行为契约：红线、SOP、阶段门禁、交付纪律 | 不写实现细节（细节在 `references/`） |
| 入口层 | `src/cli.py`、`scripts/*.py` | 10 个子命令 + 3 个独立脚本；参数解析、退出码、用户可见输出 | 不承载业务逻辑（转发给 core/generator） |
| 领域服务层 | `src/core/pipeline.py` | 流水线编排：来源解析 → 取音 → 装箱 → 任务书导出 → 412 状态记录 | 不写长文/笔记内容 |
| 基础能力层 | `src/core/*.py` | 路径、工作区、凭证、抓取、签名、音频、质检内核、对账、回收 | 不关心 CLI 参数形状 |
| 摄取层 | `src/core/ingestion/**` | 多平台统一媒体内核：4 个 Provider + 协调器 + 抖音/YouTube 自有引擎 | 不产出任务书 |
| 生成层 | `src/generator/**` | 提示词模板、任务书渲染、笔记归并、教材整编 | 不发起网络与子进程 |
| 门禁层 | `scripts/selfcheck.py` | 49 项可复算断言，把文档与机器契约一起钉住 | 不修改任何文件（只读校验） |

**关键取向：工具层只产出「任务书 + 派发载荷 + 门禁」，内容由宿主 Agent 写。** 这是本项目与常规「脚本生成文档」最大的不同——实现里到处可见「任务书烘焙提示词、禁止主 Agent 自编格式」的约束。

### 1.3 端到端数据流

```text
URL / 本地目录
   │  ① 来源解析（BilibiliParser / Provider.match+probe）
   ▼
parts.json ── 分集拓扑（集号基准，离线自愈依赖它）
   │  ② 取音（AudioFetcher / Provider.fetch_audio）16kHz 单声道
   ▼
audio/P01…PN.m4a
   │  ③ 装箱成块（AudioMerger，40–60 分钟/块，超长集劈上下）
   ▼
audio/_blocks/BLK01_P01-P07_<标题>.m4a + blocks.json（块=知识模块，模块边界唯一来源）
   │  ④ 导出块级转录任务书（pipeline.export_block_transcribe_task）
   ▼
subtitles/BLK01_P01-P07_转录任务书.md
   │  ⑤ 专职转录角色按块取音（read_audio 原生听 / read_media 外部模型代读）
   ▼
subtitles/BLK01_P01-P07_逐字稿.md ── 写作的唯一事实来源
   │  ⑥ 导出模块长文任务书（pipeline.export_block_article_task）→ 写作角色一块一篇
   ▼
articles/模块XX_<块标题>_精读长文.md
   │  ⑦ 阶段门禁（queue_tracker --summary 的 STAGE1_DONE + cli.py check --stage1）
   ▼
   ├─ ⑧ 块 → 笔记归并（Agent 写 note_plan.json）→ 笔记任务书 → 笔记子智能体
   │     ▼ notes/笔记XX_*.md
   └─ ⑨ 教材分册（Agent 写 textbook_plan.json）→ 按块序整编
         ▼ textbooks/模块<册号>_<册名>_精读全书.md
   │  ⑩ 收尾（已自动）：pipeline 与 cluster-* 结束时回收任务书 / 对账回填 manifest.json
   ▼
交付前质检：cli.py check --deliver（笔记成色 + 渲染合规）+ check --stage1（依据级；默认提示级，--strict 才拦）
```

### 1.4 参与角色与工具层边界

| 角色 | 由谁承担 | 读什么 | 写什么 | 工具层能否校验 |
|---|---|---|---|---|
| 主 Agent | 宿主模型本体 | `SKILL.md`、任务书、派发载荷 | 编排命令、派发、验收 | `.dispatch_log.jsonl` 可观察派发节奏 |
| 转录角色 | 子智能体（建议 2 个并行） | 块级转录任务书 | `subtitles/BLKxx_*_逐字稿.md` | 只回报一行，正文不回传 |
| 写作角色 | 子智能体（一块一个） | 块级逐字稿 + 模块长文任务书 | `articles/模块XX_*_精读长文.md` | `article_grounding_check` 启发式校验 |
| 笔记写作 | 子智能体（一篇一个） | 该篇涵盖各块的模块长文 | `notes/笔记XX_*_笔记.md` | `note_quality_check` 五类致命项 |
| 工具层 | CLI / scripts | 磁盘产物 | 任务书、载荷、门禁结论 | —— |

「谁写的」「是否真听了音频」属**纪律条款**，工具层无法校验；能校验的部分（文件齐备、字节数下限、依据覆盖率、版式）都做成了脚本门禁。

---

## 2. 文件层级与逐文件说明

### 2.1 仓库根（分发单元，15 个文件）

| 文件 | 行数 | 内容 | 关联 | 作用 |
|---|---:|---|---|---|
| `README.md` / `README.en.md` | 427 / 426 | 使用侧门面：快速开始、命令示例、依赖、配置、FAQ、安全 | 指向 `references/cli-cookbook.md`（CLI 单一真源） | 双语入口；两版**标题结构必须保持一一对应**（各 36 个标题） |
| `AGENTS.md` / `CLAUDE.md` | 16 / 48 | 各平台自动加载的仓库级入口摘要 | 均以 `SKILL.md` 为准 | 让不同宿主自动发现本技能 |
| `.claude-plugin/plugin.json` | 21 | Claude Code 插件清单（不含路径字段，靠插件根 `skills/` 自动发现） | 版本号三处一致（自检断言） | 平台声明 |
| `.codex-plugin/plugin.json` | 43 | Codex 插件清单（显式 `"skills": "./skills/"`） | 同上 | 平台声明 |
| `.agents/plugins/marketplace.json` | 20 | 通用 agents 侧 marketplace，插件源指向本仓库自身 | 同上 | 平台声明 |
| `agents/openai.yaml` | 16 | 通用 agents 侧元数据 + `allow_implicit_invocation` + 系统依赖声明 | 同上 | 平台声明 |
| `.opencode/INSTALL.md` | 90 | OpenCode 安装说明（无打包插件，按目录安装） | 引用 `references/install.md` | 平台安装路径 |
| `pyproject.toml` | 35 | 包元数据、依赖、`video2book = src.cli:main`、包发现指向 `skills/video2book` | 版本号与依赖被 README、自检引用 | 可 `pip install -e .` |
| `.github/workflows/windows.yml` | 36 | Windows + Python 3.10/3.12 矩阵跑 `scripts/selfcheck.py` | 与本地同一道门禁 | CI |
| `.gitattributes` | 1 | `* text=auto eol=lf` | 所有文本文件 | **行尾契约**：新增文件必须 LF |
| `.gitignore` | 64 | 缓存、产物、凭证、`.archive/`、`.bvb-home` 等 | `paths.py`、凭证存储 | 防误提交 |
| `LICENSE` | 21 | MIT | —— | —— |
| `AUDIT_REPORT.md` | 405 | 上轮只读审计报告（含待确认项与路线图） | 与本文件互补 | 历史决策依据 |

### 2.2 技能安装单元（`skills/video2book/`）

`SKILL.md` 与 `references/` 是**契约与文档**，`src/` 与 `scripts/` 是**工具链**；四者必须一起复制，少一个都跑不起来（自检「技能自包含布局」有权重断言）。

| 文件 | 行数 | 内容 / 关键符号 | 关联 | 作用 |
|---|---:|---|---|---|
| `SKILL.md` | 762 | §1 红线、§2 凭证、§3 SOP、§4 阶段一规范、§5 阶段二、§6 命令导航、§7 交付标准、§8 环境与缺失处理 | 引用全部 references；被所有入口文件引用 | 唯一真源 |
| `references/cli-cookbook.md` | 185 | 六个场景 + 子命令全表 + 完整参数表 + 其余脚本 + 退出码 | 被 `SKILL.md §6`、README 指向 | **CLI 细节单一真源** |
| `references/delivery_matrix.md` | 122 | 产物体系总览、三类产物规范、长文类型矩阵、排版与渲染兼容 | 被 `SKILL.md §7` 指向 | **交付格式单一真源**；自检断言其含 `learning`/`legacy`/`Typora` 与字符画围栏要求 |
| `references/install.md` | 78 | 各平台安装位置与挂载方式对照 | 被 AGENTS / CLAUDE / README / INSTALL 引用 | 分发契约 |
| `references/non-video-works.md` | 116 | 抖音图文/图集（无口播）的识别口径与处置 | `pipeline.py` 亦引用 | 非视频作品规则 |
| `references/host-tools/{README,claude,codex,opencode}.md` | 56/24/47/24 | 各宿主「行动语义 → 私有工具名」映射 | **唯一允许写宿主私有工具名的位置** | 平台适配层 |
| `scripts/queue_tracker.py` | 749 | 队列、阶段门禁、派发载荷、台账 | `budget.py`、`workspace.py`、`transcript_splitter.py` | 派发中枢 |
| `scripts/selfcheck.py` | ~3180 | 49 项检查（见 §3.12） | 读全部文档与代码 | 主质量门禁 |
| `scripts/run.py` | 22 | 免安装入口：注入仓库根后调 `src.cli:main` | 与 `python src/cli.py` 行为等价 | 任意工作目录调用 |

> **v2.9.0 入口收敛**：原独立脚本已合并——三个质检脚本（`article_grounding_check` / `note_quality_check` /
> `render_compat_check`）与 `strip_heading_numbers` 的规则全部落进 `src/core/quality_gate.py` 与
> `src/core/heading_cleanup.py`，统一由 `cli.py check`（`--stage1` / `--deliver` / `--fix-numbering`）承载；
> 与 `cleanup` 子命令重复的 `cleanup_tasks.py` 已删除。

### 2.3 `src/` 入口与领域层

| 文件 | 行数 | 内容 / 关键符号 | 关联 | 作用 |
|---|---:|---|---|---|
| `src/cli.py` | ~840 | `main()`、10 个 `cmd_*` 处理函数、`_resolve_base_dir`、`_confirm_article_prompt_style`、`_autoclose_workspace` | 调 pipeline / core / generator / scripts 能力 | **唯一命令入口**；退出码在这里产生 |
| `src/core/pipeline.py` | 775 | `PipelineCoordinator`、`resolve_target_info`、`resolve_scope_parts`、`is_412`/`record_412_status`/`format_412`、`classify_audio_error` | 依赖 parser、fetcher、audio_merger、workspace、budget、taskbook | 领域调度：把「来源 + 参数」变成「工作区 + 音频 + 任务书派发」 |
| `src/core/taskbook.py` | 185 | `export_block_transcribe_task`、`export_block_article_task`、`TRANSCRIBE_INSTRUCTION` | 被 pipeline、selfcheck 引用 | **任务书与提示词导出解耦**（直写落盘指引 + 单任务直达规范） |
| `src/core/paths.py` | 340 | `code_root`、`home_root`、`is_container_layout`、`resolve_base_dir`、`mcp_candidate_bases` | 被几乎全部模块引用 | **三域路径唯一真相** |
| `src/core/workspace.py` | 594 | `TaskWorkspace`（root/audio/notes/articles/subtitles + parts_cache/manifest 路径）、`find_module_article`、`sanitize_filename` | 被 pipeline、队列、全部生成器引用 | 工作区与命名契约 |
| `src/core/parser.py` | 554 | `BilibiliParser`：`extract_bvid`、`extract_season_ref`、`_fetch_public_json`、`resolve_season_seed_bvid`、`fetch_video_view` | `wbi.py`、`bili_web.py` | B 站拓扑解析（含独立 BV 合集归一） |
| `src/core/fetcher.py` | 435 | `AudioFetcher`、`_请求元数据`（限速 + 重试 + 熔断）、`_atomic_replace`、`_解析等待秒数` | `wbi.py`、`bili_web.py`、`proc.py` | 播放地址解析与音频落盘 |
| `src/core/wbi.py` | 281 | `WbiSigner`：动态密钥获取、加签、内存 + 文件两级缓存、集中限速基准 | `paths.py`、`bili_web.py` | B 站接口签名 |
| `src/core/bili_web.py` | 45 | UA、`BROWSER_HEADERS`(9 字段)、`NAV_HEADERS`(6 字段)、`is_retryable_status` | 被 fetcher / parser / wbi 引用 | **B 站请求头单一真源** |
| `src/core/audio_merger.py` | 950 | `AudioMerger`：`pack`、`plan_units`、`build_block`、`load_block_titles`、块清单落盘 | `audio_chunker.py`、`workspace.py`、`proc.py` | 装箱成块（块级链路第一步） |
| `src/core/audio_chunker.py` | 59 | `AudioChunker.get_audio_duration` / `format_seconds` | 被 merger 与 splitter 复用 | 时长探测与时间格式化纯工具 |
| `src/core/transcript_splitter.py` | 398 | `TranscriptSplitter`：`split`（块稿 → 分集稿）、`episode_path`、`block_path` | 被 `split-transcript`、grounding_check、队列引用 | **可选**环节：按集查阅用 |
| `src/core/budget.py` | 125 | `est_audio_tokens`、`serial_ok`、`dispatch_required`、`suggest_workers`、`describe` | `queue_tracker.py`、自检 | 阶段一预算与派发阈值（全部可用环境变量覆盖） |
| `src/core/credentials.py` | 163 | `SessdataStore`、`DouyinCookieStore`、脱敏指纹 | `cli.py`、`ingestion/bilibili.py` | 凭证本地持久化（落在产物根，永不入库） |
| `src/core/state_sync.py` | 168 | `reconcile_workspace_manifest`、`reconcile_all` | CLI `sync` | 以磁盘为真相回填 manifest |
| `src/core/task_cleanup.py` | 306 | `_iter_tasks`、`_product_ready`、回收主流程 | CLI `cleanup`、`cleanup_tasks.py`、多个质检脚本 | 任务书回收（成品齐备才回收） |
| `src/core/deliverable_lint.py` | 317 | `lint_note`、`lint_render`、`FATAL_NOTE_KEYS`（5 类）、`summarize_*` | 被三个质检脚本与自检引用 | 质检规则内核 |
| `src/core/heading_numbers.py` | 146 | `strip_heading_number`、`is_numbered_heading`、`lint_heading_numbers` | strip 脚本、integrator、deliverable_lint | 标题序号纪律（一处规则） |
| `src/core/contract.py` | 57 | `parse_status`、`check_contract`、`parse_compatible_status` | 被自检「OMNI_STATUS 契约版本兼容」引用 | 与两个听音 MCP 的兼容契约解析 |
| `src/core/fsutil.py` | 149 | `iter_files`、`iter_child_dirs`、`is_reparse_point` | 被 workspace、质检引用 | 坏链接只跳过不崩 |
| `src/core/console.py` | 48 | `enable_utf8_console` | CLI 与 `run.py` | 管道下编码硬化 |
| `src/core/proc.py` | 47 | `run_quiet`、`quiet_kwargs` | fetcher、merger、ingestion | 静默子进程（统一超时口径） |

### 2.4 `src/core/ingestion/` 多平台媒体内核

| 文件 | 行数 | 内容 | 作用 |
|---|---:|---|---|
| `base.py` | 84 | `BaseMediaProvider`（`match`/`probe`/`fetch_audio`/`check_readiness` 四个抽象方法）、`IngestionError`、`UnsupportedTargetError` | **Provider 协议**；`probe` 返回体字段带注释，注释即契约 |
| `coordinator.py` | 201 | `IngestionCoordinator`、`get_coordinator` | 按 `match()` 选 Provider；工作区名与缓存 parts 回填 |
| `bilibili.py` | 87 | `BilibiliProvider` | 薄适配层：转发 `parser.py` + `fetcher.py`（**不是第二套实现**） |
| `local.py` | 49 | `LocalMediaProvider` | 本地目录 / 单文件 |
| `youtube.py` | 240 | `YouTubeProvider`（单视频与频道两分支，单文件收敛，直接基于 yt-dlp + ffmpeg） | YouTube 自有引擎（按需导入 `yt_dlp`） |
| `douyin.py` | 285 | `DouyinProvider`、`expand_share_link` | 转发 `dyaudio/*`；图文与图集走 `media_kind` |
| `dyaudio/*.py`（9 个） | 81–411 | `DouyinClient`（签名 / 重试 / 限速）、`abogus`、`sm3`、`share_parser`、`user_crawler`、`mix_crawler`、`downloader`、`config`、`utils` | 抖音自有引擎（`requests`） |

### 2.5 `src/generator/` 生成层

| 文件 | 行数 | 内容 / 关键符号 | 作用 |
|---|---:|---|---|
| `prompt_templates.py` | 394 | `RENDER_COMPAT_RULES`、`MODULE_NOTE_PROMPT`、`ARTICLE_LEARNING_PROMPT`、`ARTICLE_LEGACY_PROMPT`、`ARTICLE_PROMPT_TYPES`、`IMPLEMENTED_ARTICLE_TYPES`、`resolve_article_prompt`、`render_article_prompt_menu` | **提示词单一真源**；任务书从这里取文本 |
| `topic_planner.py` | 549 | `SemanticTopicPlanner`：归并任务书渲染、规划校验、缓存复用 | 笔记归并（**只做第二趟**：块即模块） |
| `block_synthesizer.py` | 381 | `BlockSynthesizer`：逐篇导出笔记任务书、`build_synthesis_prompt` | 笔记任务书生成；缺归并按「一块一篇」兜底且**不回写规划文件** |
| `integrator.py` | 647 | `ArticleIntegrator`：读 `textbook_plan.json`、章节编排、承前启后、标题去号降级、册体量切分 | 教材整编（册 = 书、章 = 块） |

### 2.6 模块可达性（已实测结论）

模块总数随本轮新增的两个内核（`quality_gate.py` / `heading_cleanup.py`）而变化；从 4 个入口
（`src/cli.py` + `scripts/{queue_tracker,run,selfcheck}.py`）可达的模块集没有死代码——未命中的只有
`src.generator`、`ingestion.dyaudio` 等包的 `__init__.py`，它们被
`from <pkg>.<mod> import …` 的形式实际加载，**不是死代码**。结论：`src/` 下当前没有可安全删除的模块。

---

## 3. 核心任务实现说明

每个任务按「参与文件 / 前置条件 / 底层逻辑 / 受影响配置 / 产物 / 验证方式」六项说明。

### 3.1 来源解析与工作区建立（`pipeline --dry-run`）

- **参与文件**：`cli.py:cmd_pipeline`（`mode="dry-run"`）→ `pipeline.PipelineCoordinator.run` → `ingestion/coordinator.py` → 4 个 Provider（`bilibili.py`/`local.py`/`youtube.py`/`douyin.py`）→ `parser.py`（B 站）、`local_media.py`（本地）、`youtube.py`（YouTube）、`dyaudio/share_parser.py`+`user_crawler.py`（抖音）；落盘走 `workspace.py` 的 `save_parts`。
- **前置条件**：B 站建议 `SESSDATA`（高并发稳定性）；抖音需要 Cookie（否则只抓到约 20 条，**不终止**）；YouTube 需要 `yt-dlp`；抖音需要 `requests`。
- **底层逻辑**：`coordinator` 遍历 Provider 调 `match()` 选路 → `probe()` 返回统一结构的字典（`bvid`/`title`/`parts[]`/`video_type` 等）→ 工作区名 = 清洗后的课程标题 + `_<bvid>`（`workspace.py`）→ `parts.json` 落盘（**集号基准**）。
- **受影响配置**：`BVB_OUTPUT_DIR`、`BVB_HOME`、`--base-dir`、`--task`、`--limit`。
- **产物**：`<产物根>/<task>/parts.json`、`manifest.json`。
- **验证**：`cli.py pipeline "<链接>" --dry-run`；离线场景靠 `parts.json` 自愈（`workspace.offline_candidate_dirs`）。

### 3.2 音频摄取与装箱成块（`pipeline` / `pipeline --audio-only` / `merge-audio`）

- **参与文件**：`cli.py:cmd_pipeline`（含 `mode="audio-only"`）/`cmd_merge_audio` → `pipeline.PipelineCoordinator` → `fetcher.AudioFetcher`（B 站）或 `Provider.fetch_audio`（其它平台）→ `audio_merger.AudioMerger` → `audio_chunker.AudioChunker`（时长）。
- **前置条件**：系统 `ffmpeg` 在 `PATH`（硬前置）；`ffprobe` 可选（缺失降级为 `ffmpeg -i`）。
- **底层逻辑**：① 按集下载/抽取 16kHz 单声道音频 → ② `AudioMerger.plan_units` 把集规划成「装箱单元」（整集一个单元；超长集按上限劈上/下两条腿） → ③ `pack` 把单元装进 `[min, max]` 分钟的连续块（默认目标 50、区间 40–60） → ④ 块标题由块内分集名语义组合，写进块音频文件名 → ⑤ 落 `blocks.json` + `block_titles.json`。箱子是**知识模块边界**，此后不再有独立的模块规划。
- **受影响配置**：`--block-minutes`、`BVB_AUDIO_BLOCK_MINUTES`、`BVB_AUDIO_BLOCK_MIN_MINUTES`、`BVB_AUDIO_BLOCK_MAX_MINUTES`、`BVB_AUDIO_ONESHOT_LIMIT_MINUTES`（单块硬上限，默认 75）、`--quality`、`--skip-failed`、`--force`。
- **产物**：`audio/P*.m4a`、`audio/_blocks/*.m4a`、`audio/_blocks/blocks.json`、`audio/_blocks/block_titles.json`。
- **验证**：自检「块级转录契约（装箱不劈集 / 块时长可配 / 切分幂等）」；`merge-audio` 幂等重跑。

### 3.3 块级转录任务书与听音通道（阶段一 A）

- **参与文件**：`pipeline.export_block_transcribe_task`(237) + `TRANSCRIBE_INSTRUCTION`(225) → 产物 `subtitles/BLKxx_Paa-Pbb_转录任务书.md`；`SKILL.md §4.2` 定义两条通道的选择规则。
- **前置条件**：宿主必须挂载 `read_audio` 或 `read_media` 之一（**硬依赖**，缺失即停下提示）；通道挂着但上游不可达时同样停下（`SKILL.md §8.2 ⑥`）。
- **底层逻辑**：任务书里烘焙了「纯文本忠实转录」要求 + 块音频绝对路径 + 块内每集起止时间表；转录角色**原样透传**任务书 2.1 节到 `instruction`，不得自行改写。块级逐字稿是模块长文的唯一事实来源。
- **受影响配置**：无环境变量；受 `--block-minutes` 影响的块边界间接决定任务书数量。
- **产物**：`subtitles/BLKxx_*_转录任务书.md`（临时派发物，成品后被 `cleanup` 回收）、`subtitles/BLKxx_*_逐字稿.md`。
- **验证**：`queue_tracker.py --next-transcribe N --json` 取载荷；`--summary` 看 `BLOCKS_TRANSCRIBED`/`TRANSCRIPT_READY`。

### 3.4 模块长文任务书与成文（阶段一 B）

- **参与文件**：`pipeline.export_block_article_task`(138) + `resolve_article_type`(131) + `prompt_templates.resolve_article_prompt` → `articles/模块XX_*_TASK.md`；写作角色产出 `articles/模块XX_*_精读长文.md`。
- **前置条件**：本块逐字稿就绪；`--article-type` 必填（缺失或被判为「提示词未提供」→ 打印菜单并 `exit 4`，不落盘任务书）。
- **底层逻辑**：按所选风格注入提示词（`learning` 保住讲师口吻 / `legacy` 客观学术）+ 渲染兼容硬规则（`RENDER_COMPAT_RULES`：字符画必须进围栏、围栏成对、禁用 GitHub 告警块）+ 语料清单与目标路径；长文**一块一篇**，不逐集分小节。
- **受影响配置**：`--article-type`、`ARTICLE_PROMPT_TYPES`（支持的风格键）。
- **产物**：`articles/模块XX_<块标题>_精读长文.md`（**严格保留，不予删除**）。
- **验证**：`queue_tracker.py --summary` 的 `STAGE1_DONE`；`article_grounding_check.py --strict` 一块一验覆盖率。

### 3.5 笔记归并（阶段二①）

- **参与文件**：`cli.py:cmd_cluster_notes`(625) → `topic_planner.SemanticTopicPlanner`（渲染 `note_plan_TASK.md`、校验规划、缓存复用）→ Agent 产出 `note_plan.json` → `block_synthesizer.BlockSynthesizer`（逐篇导出 `notes/笔记XX_*_TASK.md`）。
- **前置条件**：块与块长文齐备。归并**不是硬前置**：缺 `note_plan.json` 时按「一块一篇」兜底继续，命令始终正常退出。
- **底层逻辑**：归并粒度由知识体系决定（一篇笔记可跨多个块）；工具层只做「渲染提示词 / 校验规划 / 复用缓存」，真正的归并由 Agent 写。兜底结果只在内存里用，**永不回写** `note_plan.json`。损坏的规划会被抢救（丢弃不存在的块、先到先得处理重复认领、无人认领的块各补一篇）。
- **受影响配置**：无；`--force` 强制重导任务书，`--block-id/--start-block/--end-block` 按笔记序号局部派发。
- **产物**：`note_plan.json`（Agent 写）、`note_plan_TASK.md`（课程级唯一，**永不回收**）、`notes/笔记XX_*_TASK.md`、`notes/笔记XX_*_笔记.md`。
- **验证**：自检「笔记归并契约」「块级归并契约」「模块笔记契约」三组断言。

### 3.6 教材整编（阶段二②）

- **参与文件**：`cli.py:cmd_cluster_articles`(688) → `integrator.ArticleIntegrator`；分册依据 `textbook_plan.json`（Agent 写）或章节标记兜底。
- **前置条件**：块长文齐备（缺长文的块 gate 跳过，不落占位册）。
- **底层逻辑**：按块序把块长文整编成册（册 = 书、章 = 块）；章标题取该块**长文 H1**（取不到才退回块标题，并在章下「对应块」备注保留分集名供溯源）；标题**先幂等去号再整体降级**（`## 2.1 …` → `### 某主题`）；一册超 300KB 按块边界续切；本轮不再产出的旧册自动清理。
- **受影响配置**：`--force`（**旧册是缓存产物**：不加 `--force` 会命中「教材已存在」直接跳过）。
- **产物**：`textbook_plan.json`、`textbook_plan_TASK.md`、`textbooks/模块<册号>_<册名>_精读全书.md`。
- **验证**：自检「ArticleIntegrator 无硬编码课程数据」「标题序号纪律」。

### 3.7 交付前质检（`check` 统一门禁 + `--strict` 语义）

- **参与文件**：`cli.py:cmd_check` → `core/quality_gate.py`（`run_stage1` / `run_deliver`）与 `core/heading_cleanup.py`（`run_fix_numbering`），共用 `core/deliverable_lint.py`、`core/heading_numbers.py`、`core/workspace.py`。
- **前置条件**：产物已落盘；`check` **不在流水线上拦人**（交付前由主 Agent 手动跑）。
- **底层逻辑**：默认**提示级**，只有 `--strict` 才把致命项变成非零退出码。`--stage1` 做依据级校验（块级逐字稿技术实体覆盖率，默认 `--min-freq 2`、`--min-coverage 0.5`）；`--deliver`（默认）做笔记成色 + 渲染合规——笔记五类致命项（套话填充 / 空壳标题 / 分集平铺标题 / 行内残缺引用 / 分集口吻）定义在 `deliverable_lint.FATAL_NOTE_KEYS`，渲染致命项为告警块 / 围栏外裸字符画 / 围栏配对，缺围栏语言标识默认只统计（`--require-lang` 才拦）；`--fix-numbering` 就地清理存量标题手写序号。
- **受影响配置**：`--stage1`/`--deliver`/`--fix-numbering`、`--strict`、`--require-structure`、`--max-truncated`、`--require-lang`、`--require-no-numbering`、`--min-freq`、`--min-coverage`、`--only`、`--dry-run`、`--dir`/`--task`/`--base-dir`/`--json`。
- **验证**：自检「质检文档口径与门禁一致」（断言致命项恰为 5 类，且 README 中英都写了对应标签）+「标题序号纪律」（`heading_cleanup.clean_text` 真跑、幂等）。

### 3.8 收尾与对账（`cleanup` / `sync`）

- **参与文件**：`cli.py:cmd_cleanup` → `core/task_cleanup.py`；`cli.py:cmd_sync` → `core/state_sync.py`；两处均已被 `cli.py:_autoclose_workspace` 在 `pipeline` / `cluster-*` 收尾时自动调用。
- **底层逻辑**：任务书（`*_TASK.md`、`*_转录任务书.md`）是**临时派发物**，只有成品齐备才回收，每类保留编号最小的 N 份作为提示词范本；`note_plan_TASK.md` 属课程级规划，永不回收。`sync` 以磁盘为唯一真相回填 `manifest.json`（含按块对账）。两个子命令仍保留，供单独复算。
- **受影响配置**：`--keep N`、`--dry-run`、`--task`、`--all`。
- **验证**：自检「对账按块跑通（sync 的静默失败防线）」；`--dry-run` 预演。

### 3.9 去重（`pipeline` 自动执行）

- **参与文件**：`pipeline.PipelineCoordinator.run`（音频收齐后自动调用）；`workspace.py` 的音频指纹与产物复用判定。
- **底层逻辑**：对音频算 SHA-256 指纹，相同分集的语料与长文直接复用（0 Token）；原 `dedup` 子命令已删除，由 `pipeline` 在音频收口后**自动执行**。
- **受影响配置**：无（需要手动重算时重跑 `pipeline` 即可）。
- **验证**：自检「重复分集免字幕复用」。

### 3.10 凭证管理（`login` / `logout` / `info`）

- **参与文件**：`cli.py:cmd_login`(852)/`cmd_logout`(884)/`cmd_info`(897) → `core/credentials.py`；B 站签名与请求头在 `wbi.py`、`bili_web.py`。
- **底层逻辑**：凭证只通过命令行参数或本地存档传入，**参数优先**；存档落在产物根（`.sessdata.json` / `.douyin_cookie.json`），被 `.gitignore` 排除；`info` 只显示脱敏指纹与来源，从不回显明文。
- **受影响配置**：`--sessdata`、`--douyin-cookie`。
- **验证**：自检「SESSDATA 存档安全（脱敏 / 不入库）」「缓存与凭证路径锚定产物根」。

### 3.11 阶段门禁与派发队列（`queue_tracker.py`）

- **参与文件**：`scripts/queue_tracker.py` → `core/workspace.py`、`core/budget.py`、`core/transcript_splitter.py`（分集映射）。
- **底层逻辑**：以磁盘产物为唯一进度依据，产出三种派发载荷：`--next-transcribe N`（转录侧：待转录的块）、`--next-module N`（写作侧：块逐字稿就绪且模块长文缺失的块）、`--next-note N`（笔记侧）。载荷内自带预制 `dispatch_prompt`，**主 Agent 必须原样透传**。`--summary` 给出单行状态（`STAGE1_DONE`、`BLOCKS/BLOCKS_TRANSCRIBED/TRANSCRIPT_READY`），`--log-dispatch` 追加 `.dispatch_log.jsonl` 台账（默认关闭）。派发阈值来自 `budget.py`：课程总时长 ≤ 60 分钟可由主 Agent 串行，超过必须派发。
- **受影响配置**：`--dir`、`--pattern`、`--base-dir`、`--json`；阈值系数可用 `BVB_AUDIO_TOKENS_PER_SEC`、`BVB_CONTEXT_WINDOW_TOKENS` 覆盖。
- **验证**：自检「派发载荷与台账契约」「阶段一派发纪律已写入文档」。

### 3.12 技能自检（`scripts/selfcheck.py`，49 项）

- **职责**：把「代码、文档、机器契约」三者的一致性变成可复算断言。覆盖七类：
  1. **结构与边界**：技能自包含、三域分离、跨仓不互引、容器根不得是版本库；
  2. **契约**：OMNI_STATUS 兼容、笔记归并、块级转录、统一 Provider 返回体、退出码与 `--strict` 语义；
  3. **端到端**：任务书导出门禁、阶段一门禁不误认任务书、对账按块跑通、去重复用；
  4. **安全**：凭证脱敏、不入库、路径锚定产物根；
  5. **文档一致性**：文档无悬空小节引用、无已删除风格残留、交付矩阵类型表齐备、版本号三处一致、质检口径与致命项一致（含 README 中英标签）；
  6. **可移植性**：无硬编码本机路径、Python 3.10 语法兼容、清单路径可移植；
  7. **回归**：历史修复项与死代码清单。
- **运行**：`cd skills/video2book && python scripts/selfcheck.py`（CI 同一道门禁，见 `.github/workflows/windows.yml`）。
- **注意**：容器专属断言仅在容器布局（存在 `.bvb-home` 或 `BVB_HOME`）下生效——**CI 里这些断言会被跳过**，所以本地红/绿与 CI 不总等价。

---

## 4. 数据契约与调用关系

### 4.1 产物目录结构（域③，路径均相对产物根）

```text
<产物根>/
├── .sessdata.json          # B 站凭证（脱敏存储；.gitignore 排除）
├── .douyin_cookie.json     # 抖音 Cookie
├── .wbi_keys.json          # WBI 签名密钥缓存（内存 + 文件两级）
├── .cli_status.json        # 上次 412 / 熔断状态（info 可查）
└── <task>/                 # 一门课一个工作区（目录名 = 清洗后标题 + _<bvid>）
    ├── audio/              # 逐集音频
    │   └── _blocks/        #   块音频 + blocks.json + block_titles.json
    ├── parts.json          # 分集拓扑缓存（集号基准，离线自愈依赖它）
    ├── manifest.json       # 任务清单与断点续跑状态（sync 回填）
    ├── note_plan.json      # 笔记归并（Agent 产出）
    ├── note_plan_TASK.md   # 归并任务书（课程级唯一，永不回收）
    ├── textbook_plan.json  # 教材分册（Agent 产出）
    ├── textbook_plan_TASK.md
    ├── .dispatch_log.jsonl # 派发台账（--log-dispatch 时追加）
    ├── articles/           # 模块长文（最终产物，严格保留）+ 长文任务书
    ├── subtitles/          # 块级转录任务书 + 块级逐字稿（+ 可选分集逐字稿）
    ├── notes/              # 笔记成品 + 笔记任务书
    └── textbooks/          # 教材成品
```

### 4.2 关键文件契约（改动前务必确认消费方）

| 文件 | 生产者 | 消费者 | 关键字段 / 不变式 |
|---|---|---|---|
| `parts.json` | `workspace.save_parts`（各 Provider 的 `probe` 结果） | pipeline、队列、对账、离线自愈 | `[{page, title, duration, cid, url, filepath, media_kind?}]`；**集号基准**，禁止用子集覆盖导致截断 |
| `blocks.json` | `audio_merger` | pipeline 任务书导出、队列、生成器、质检 | `block_id`、`title`、`span`、`episodes[]`、`units[{page,label,split}]`、`audio`（相对路径）、`duration_sec`、`duration_min`、`segments`、`single_episode`、`episode_split`、`reencoded`、`oversized`、`undersized`；**模块边界唯一来源** |
| `block_titles.json` | Agent 手改 | `audio_merger.load_block_titles` | 块标题覆盖；**标题进指纹**（改了必须重跑 `merge-audio` 才会改名） |
| `manifest.json` | CLI / pipeline / sync | 断点续跑、`info`、质检 | 含 `article_type`、块与产物清单、`details[]`（每块状态）；路径一律**相对化**（`workspace._relativize`，`PATH_LIST_KEYS = {textbooks, notes_files, kernels}`） |
| `note_plan.json` | Agent（工具只校验与兜底） | `block_synthesizer` | 把块归并成若干篇笔记；**工具永不回写**；损坏时当场抢救 |
| `textbook_plan.json` | Agent（任务书由 `integrator` 导出） | `integrator` | 分册（册名取自内容）与块认领；缺失或损坏走章节标记兜底 |
| `*.m4a` / 逐字稿 / 长文 / 笔记 / 教材 | 工具链 / Agent | 下游各环节 | 命名契约见 §6 |

### 4.3 模块依赖关系（要点）

- **自底向上**：`paths` → `wsutil/console/proc` → 平台引擎（`wbi`/`bili_web`/`parser`/`fetcher`/`dyaudio`/`youtube`）→ `ingestion` → `workspace`/`audio_merger`/`transcript_splitter` → `pipeline` → `cli`；`generator` 依赖 `workspace` 与 `prompt_templates`，不被 `core` 反向依赖。
- **高被依赖（改一处影响面大）**：`workspace.py`（14 个模块引用）、`task_cleanup.py`（10）、`paths.py`、`prompt_templates.py`、`budget.py`。
- **无环**：`ingestion` 不 import `pipeline`；`generator` 不 import `core.pipeline`（避免入口反向依赖）。
- **单一真源一览**：路径 → `paths.py`；工作区与命名 → `workspace.py`；提示词 → `prompt_templates.py`；B 站请求头 → `bili_web.py`；预算阈值 → `budget.py`；CLI 细节 → `references/cli-cookbook.md`；交付格式 → `references/delivery_matrix.md`；质检规则 → `deliverable_lint.py`。

---

## 5. 依赖与前置条件

| 类别 | 依赖 | 缺失时 |
|---|---|---|
| 运行时 | Python 3.10+（`pyproject.toml` 声明；自检断言全仓语法可在 3.10 解析） | 全部命令无法启动（无其它语言实现） |
| 系统工具 | `ffmpeg` 在 `PATH` | 取音阶段失败，任务书不落盘（**硬前置**） |
| 系统工具 | `ffprobe` | 自动降级为 `ffmpeg -i`（精度略低、速度略慢） |
| Python 依赖 | `yt-dlp`（YouTube 链路）、`requests`（抖音链路） | 对应平台链路不可用；**B 站与本地链路只用标准库 `urllib`** |
| Python 依赖 | Python 3.12+（可选） | 3.12 以下链接去重改用文件属性位识别重解析点，junction 与符号链接同样跳过 |
| 宿主能力 | `read_audio`（原生音频模态）或 `read_media`（外部模型代读）之一 | 阶段一取不到音频事实，**必须停下提示挂载** |
| 凭证 | B 站 `SESSDATA`（建议）、抖音 Cookie（抖音目标不可跳过询问） | B 站匿名可跑但易触发 412；抖音匿名只能抓到约 20 条且**照常继续** |
| 可选 | `git`（仅自检用） | 自检里两条「产物/凭证未入库」的校验降级为提示 |

**惰性导入分布**（影响你怎么测试）：`yt_dlp` 在 `youtube.py` 的函数体内导入，`requests` 在 `dyaudio/*` 与 `douyin.py` 的函数体内导入；B 站与本地的 HTTP 走标准库 `urllib`。因此"能 import 模块"不等于"这条链路能跑"。

---

## 6. 对外契约（改动即破坏性变更）

| 契约 | 内容 | 钉住它的地方 |
|---|---|---|
| 退出码 | `0` 正常；`1` 通用错误/工作区缺失/参数非法；`2` 阶段一准备错误；`3` 装箱切分异常或任务书导出失败；`4` `--article-type` 缺失或非法 | `cli.py`、`references/cli-cookbook.md`、README |
| `--strict` 语义 | 质检脚本默认提示级，只有 `--strict` 的致命项返回非零 | 三个质检脚本 + `deliverable_lint` |
| 任务书格式 | `*_TASK.md` 内含完整提示词与排版红线；子智能体**原样透传** `dispatch_prompt` | `pipeline.export_*`、`generator/*` |
| 产物命名 | `articles/模块XX_<块标题>_精读长文.md`、`notes/笔记XX_*_笔记.md`、`textbooks/模块<册号>_<册名>_精读全书.md`、`subtitles/BLKxx_Paa-Pbb_逐字稿.md` | `workspace.py`（宽容定位 `find_module_article`：按前缀匹配、排除任务书、要求 ≥ 1000 字节） |
| 标识符口径 | `bvid`：有原生 id 用 id，否则用清洗后的名字（`yt_<slug>`、`dy_u_<safe_author>`…） | `ingestion/base.py` 注释 + 各 Provider |
| 听音分页契约 | 两个 MCP 同构：`OMNI_STATUS` 注释、`contract_version: 1`、续读用 `next_start_time` / `next_duration_minutes` | `core/contract.py` + 自检 |
| 容器根 `.git` | 必须是空标记（零提交、零跟踪文件） | 自检「三域分离契约」 |
| 行尾 | 全仓 LF（`.gitattributes: * text=auto eol=lf`） | git + 自检的文本比对 |

---

## 7. 扩展点

每个扩展点都标注「必须同步什么」——不同步的话，自检或文档一致性会当场转红。

### 7.1 新增一个媒体来源（平台）

1. `src/core/ingestion/<platform>.py`：继承 `BaseMediaProvider`，实现四个方法——`match(target)`（选路）、`probe(target, **kwargs)`（返回统一结构，字段见 `base.py:34` 注释）、`fetch_audio(...)`、`check_readiness()`。
2. `src/core/ingestion/__init__.py`：导入并加进 `__all__`；`coordinator.py` 的 Provider 列表加入新类。
3. **必须同步**：`README.md`/`README.en.md` 的摄取说明、`SKILL.md` 的平台列表、`references/install.md` 的平台对照；若涉及新的宿主工具名，写进 `references/host-tools/<host>.md`（**唯一允许写私有工具名的地方**）。
4. 若该链路需要新的第三方包：**先确认是否能用标准库**（B 站与本地就是 `urllib`），再加进 `pyproject.toml` 并按 §5 的口径写进 README 三处依赖说明。

### 7.2 新增一种长文类型（`--article-type`）

1. `src/generator/prompt_templates.py`：在 `ARTICLE_PROMPT_TYPES` 加键与判定信号；把提示词加进 `IMPLEMENTED_ARTICLE_TYPES`（当前只有 `learning`/`legacy`）。
2. **必须同步**：`references/delivery_matrix.md` 的「长文类型判定与提示词类型矩阵」表——自检 `check_delivery_matrix_article_types` 会遍历所有类型键断言表里都有，并断言"推荐"字样存在。
3. 若只是"登记但暂不提供提示词"，命中时必须走 `exit 4` 分支（`SKILL.md §4` 有纪律条文）。

### 7.3 新增一条质检规则

1. `src/core/deliverable_lint.py`：规则实现 + 归类（致命项进 `FATAL_NOTE_KEYS`，提示项另计）。
2. **必须同步**：`FATAL_NOTE_KEYS` 数量被自检断言为 **5**；README 中英各有 5 个标签被断言（中文：套话填充 / 空壳标题 / 分集平铺标题 / 行内残缺引用 / 分集口吻；英文：boilerplate / hollow / per-episode headings / inline quote / episode voice）。加第 6 类要同时改这三处（`deliverable_lint.py`、两个 README、必要时 `SKILL.md`），否则门禁红。
3. 相应的规则要在 `cli.py check` 上暴露 `--strict` 语义与 `--json`。

### 7.4 新增一项自检

在 `scripts/selfcheck.py` 写 `def check_xxx()`，然后在 `main()` 里 `check("人话标题", check_xxx)` 注册——**两个动作缺一不可**（只写函数不注册等于没写）。若要防止某个已删功能回流，参考 `check_dead_modules_removed()` 的清单式写法：它断言"某些文件/目录**不得存在**"，是防止回退最便宜的手段。

### 7.5 调整预算与派发阈值

`src/core/budget.py` 是唯一真相源（`TASK_BOOK_TOKENS` 等常量 + `_env_float` 读取环境变量），运行时可覆盖：`BVB_AUDIO_TOKENS_PER_SEC`、`BVB_CONTEXT_WINDOW_TOKENS`。**不要**把阈值散写进 `queue_tracker.py`——据 `dispatch_required()` 的返回值决定是否派发，是自检与文档都在引用的口径（`SKILL.md` 断言含"60 分钟"关键词）。

### 7.6 调整装箱策略

只动 `src/core/audio_merger.py` 的 `plan_units`/`pack`（区间常量与 `SPLIT_MIN_TOLERANCE` 在文件头）。**必须同步**：`SKILL.md §4.2`、`references/delivery_matrix.md` 的块定义、`BVB_AUDIO_BLOCK_*` 环境变量说明——自检「块级转录契约」会验证"装箱不劈集 / 块时长可配 / 切分幂等"。

---

## 8. 注意事项（含已踩过的坑）

### 8.1 门禁即文档契约：改文档前先查断言

`selfcheck.py` 对文档有**硬字符串断言**，改文档前请先搜一遍：

| 被断言的文档 | 断言内容 |
|---|---|
| `SKILL.md` | 含"语言标识"；派发纪律 6 个关键词（"60 分钟""转录角色""写作角色""不回传正文""`BVB_AUDIO_TOKENS_PER_SEC`""`next_start_time`"）；小节引用 `§ x` 必须在本文件内存在 |
| `references/delivery_matrix.md` | 含 `Typora`、字符画围栏要求、全部长文类型键与"推荐" |
| `README.md` | 5 个中文致命项标签、"语言标识"、"60 分钟"且含"派发" |
| `README.en.md` | 5 个英文标签、"language tag"、"60 minutes" |
| 上述四者 | **不得出现 `minimal` / `detailed`**（已删除的旧笔记风格字样，大小写不敏感，子串匹配） |
| `SKILL.md` + `references/*.md` + 各平台声明文件 | 不得出现未证实平台名（`_UNVERIFIED_TRACE_NAMES` 名单） |
| `SKILL.md` + `references/cli-cookbook.md` 等 | 不得写宿主私有工具名（`_PRIVATE_TOOL_NAMES` 名单） |

> **本文档（`sikll实现文档.md`）不在上述扫描面内**——扫描面只有 `SKILL.md`、`references/**`、两个 README、`AGENTS.md`/`CLAUDE.md`、`.opencode/INSTALL.md`、`pyproject.toml` 与三个平台声明文件。所以本文可以点名上面那两个被禁的旧风格词；若日后把本文也纳入扫描面，需要先把那两处改写掉。

### 8.2 函数体内的惰性导入不受门禁覆盖（真实事故）

`check_imports()` 只 import 模块，`check_cli_help()` 只跑 `--help`——两者都不会执行函数体内的 `from X import Y`。**本轮就踩到过**：`youtube.py` 从 `ytaudio.channel` 导入实际定义在 `ytaudio.utils` 的 `normalize_channel_base`，导致 YouTube 摄取整体抛 `ImportError`，而 48 项门禁全绿。

自查方法（建议写进日常流程）：用 AST 遍历所有 `ImportFrom`，对包内目标 `importlib.import_module` 后逐个 `hasattr` 校验名字存在。本仓库实测 350 个包内导入名中只此 1 处破损，修复后为 0。

### 8.3 容器根 `.git` 必须是空标记

往里提交（哪怕只跟踪一个 `.gitignore`）会让自检失败，报「容器根 .git 已包含提交」。备份请用 `成品/.archive/`（已被忽略）或代码域仓库的分支；日志里记录过一次真实事故：容器根多出一个提交 `fcaf2a8`，自检从 48/48 变 47/48。

### 8.4 行尾与写文件的坑

`.gitattributes` 要求全仓 LF。**用 Python 的 `Path.write_text()` 在 Windows 写文件会把 `\n` 翻成 `\r\n`**（文本模式默认换行翻译），改完立刻违反行尾契约。批量改文件时请用二进制模式（`read_bytes`/`write_bytes`）或显式 `newline=""`；提交前用 `git ls-files --eol` 或按字节统计 `CRLF` 数量核对。

### 8.5 有意保留的不一致，别去"统一"

| 位置 | 差异 | 为什么不统一 |
|---|---|---|
| `bili_web.NAV_HEADERS` vs `BROWSER_HEADERS` | 导航接口只发 6 个字段（无 `Sec-Fetch-*`） | 改变真实请求字节可能与平台风控行为不符；`bili_web.py` docstring 已写明 |
| `fetcher` vs `parser` 的退避曲线 | 前者纯指数（2/4/8s、容忍任意 `Retry-After`），后者 `1.5·2ⁿ+抖动`（只认整数） | 两者都是既有行为，统一会改变真实等待时间（实测等待序列不同） |
| `fetcher` 有熔断与失败计数，`parser` 没有 | —— | 详情接口是低频单次调用，熔断语义只对高频元数据接口成立 |
| `scripts/run.py` 与 `python src/cli.py` | 行为等价 | `run.py` 是文档承诺的免安装入口（3 处文档引用），属公共入口 |
| `fetcher.BROWSER_HEADERS` | 与 `DEFAULT_HEADERS` 同值的别名，全仓零引用 | 类级公共名字，可能是外部脚本调用名——**待确认**，未删 |

### 8.6 不要用删除产物文件的方式"重派"

任务书会被自动回收，因此**删任务书重派是错的做法**。正确做法见 `SKILL.md §7.2`：笔记与归并用 `cluster-notes --force`，教材用 `cluster-articles --force`（旧册是缓存产物，不加 `--force` 会直接跳过），长文删 `articles/模块XX_*_精读长文.md` 或 `pipeline --force`，块装箱用 `merge-audio`。

### 8.7 `articles/` 严格保留

教材整编**只向 `textbooks/` 写新文件**，`articles/` 必须 100% 保留（供定向精读与按模块溯源）。这是纪律条款，也在 `integrator` 的安全性设计里。

---

## 9. 维护建议

### 9.1 改动流程（本轮验证有效的做法）

1. **先建立基线**：`cd skills/video2book && python scripts/selfcheck.py`，并 `git status --short` 记录改动边界。
2. **备份用分支**：在**代码域**仓库建分支（`成品/skill/.git`），绝不在容器根 `.git` 提交。
3. **改前先定位消费方**：`rg` 名字，确认是私有实现还是公共契约（本项目的类属性如 `DEFAULT_HEADERS`、模块级常量如 `BROWSER_HEADERS` 都可能是外部调用名）。
4. **机械改动先断言再改**：批量删导入/变量时，脚本里对每处 `assert 原文命中次数 == 1`，避免误删仍在用的名字（`douyin.py` 的多名导入串里就有"一半在用一半没用"的情况）。
5. **行为改动做差分测试**：把改前实现从 `git show HEAD:<file>` 还原到临时副本，与工作区版本跑同一批探针，输出逐字节比对。注意消除非确定性（冻结 `time.monotonic`、固定 `random` 种子），并**检查探针产出的是真实数据**（本项目遇到过两次"两边都报错所以看起来一致"的假通过）。
6. **同步文档与门禁**：改文档先查 §8.1 的断言表；改契约先查 §6。
7. **收尾**：重跑 `selfcheck.py` + `pyflakes` + 一次真实命令冒烟（`cli.py info`、`queue_tracker.py --summary`）。

### 9.2 变更影响速查表

| 你要改的东西 | 必须同步的位置 | 触发哪条自检 |
|---|---|---|
| 任何 `src/` 公共函数签名 | 调用方 + `references/cli-cookbook.md`（若暴露为参数） | `check_cli_help`、各契约断言 |
| B 站请求头 / UA | `src/core/bili_web.py`（**唯一改这里**） | —— |
| 提示词文本 | `prompt_templates.py`；若改排版红线还要看 `delivery_matrix.md` | `check_render_compat_rules`、`check_heading_number_discipline` |
| 五类笔记致命项 | `deliverable_lint.FATAL_NOTE_KEYS` + README 中英标签 | `check_quality_gate_copy` |
| 长文类型 | `prompt_templates.ARTICLE_PROMPT_TYPES` + `delivery_matrix.md` 类型表 | `check_delivery_matrix_article_types` |
| 版本号 | `SKILL.md` frontmatter + `pyproject.toml` + 两个 `plugin.json` | `check_version_consistency` |
| 目录结构 / 安装方式 | `SKILL.md §3` + `references/install.md` + 各平台声明 | `check_skill_root_layout`、`check_host_declarations` |
| 退出码 | `cli.py` + `cli-cookbook.md` 退出码段 + README | —— |
| 装箱区间常量 | `audio_merger.py` + `SKILL.md §4.2` + `delivery_matrix.md` | `check_audio_block_contract` |
| 新增/删除模块 | 消费方 + `src/core/__init__.py`（若属公共 API） | `check_imports`、`check_dead_modules_removed` |
| 阈值/系数 | `budget.py`（唯一真相）+ 文档中的默认值说明 | `check_dispatch_discipline_documented` |

### 9.3 当前已知的待处理项

| 项 | 位置 | 状态 |
|---|---|---|
| `fetcher.BROWSER_HEADERS` 零引用别名 | `src/core/fetcher.py` | 待确认：是保留（可能被外部脚本引用）还是删除 |
| `scripts/selfcheck.py` 单文件 3100+ 行 | —— | 未拆分：拆分风险高（检查注册与退出码集中），建议按「结构 / 契约 / 端到端 / 安全 / 文档 / 可移植性 / 回归」七类评估后再动 |
| `topic_planner` 命名 | `src/generator/topic_planner.py` | 概念上等同 `note_planner`，但类名 `SemanticTopicPlanner` 已被 `block_synthesizer` 与自检引用，重命名属破坏性变更 |
| `src/core/contract.py` 消费者 | 仅 `selfcheck.py` | 生产目录里的门禁专用件；如需更清晰的归属可评估移入 `scripts/` 侧 |
| `requests` / `yt_dlp` 的惰性导入无门禁覆盖 | 各 Provider | 建议把 §8.2 的 AST 校验固化成一项自检 |
